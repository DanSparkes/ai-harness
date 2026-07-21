import argparse
import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from core.claim_verifier import annotate_review, extract_claims, verify_claims
from core.git_provider import GitDiffProvider
from core.judge import AutomatedEvaluator
from core.mcp_orchestrator import init_orchestrator
from core.parser import DjangoTopographer, minify_markdown
from core.runner import StatefulHarnessRunner
from core.warehouse import HarnessWarehouse

# ==============================================================================
# MODEL & API CONFIGURATION
# ==============================================================================
# Default is local Ollama. Set USE_GEMINI=true to use Gemini cloud API.
USE_GEMINI = os.getenv("USE_GEMINI", "").lower() in ("1", "true", "yes")

CLOUD_MODEL = "gemini-2.5-flash"
LOCAL_MODEL = "ornith:35b"

REASONING_ARCHITECT = CLOUD_MODEL if USE_GEMINI else LOCAL_MODEL
ARCHITECT_API_BASE = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
    if USE_GEMINI
    else "http://localhost:11434"
)
ARCHITECT_API_KEY = os.getenv("GEMINI_API_KEY") if USE_GEMINI else None

# Local Fallback: Ollama-served model used when cloud API is unavailable.
# MUST be a model name that Ollama has pulled locally. Using a cloud model
# name here would cause the local fallback path to always fail with 404.
FALLBACK_REVIEWER = LOCAL_MODEL
# Local Judge: Scores the review against a rubric
LOCAL_JUDGE = "qwen3-coder:32k"
# ==============================================================================

TARGET_REPO = os.environ.get("TARGET_REPO")
MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.python.json")

# Total character budget for the prompt context sent to the model. With
# num_ctx=65536 tokens, ~100K chars is a safe upper bound that leaves room
# for the model's own multi-pass output. Override via env var if needed.
TOTAL_CONTEXT_BUDGET = int(os.environ.get("CODE_REVIEW_BUDGET_CHARS", "100000"))

_mcp_orch = None
_mcp_lock = threading.Lock()


def init_mcp(repo_path: str | None = None, config_path: str | None = None):
    """Initialize the global MCP orchestrator. Thread-safe singleton."""
    global _mcp_orch
    with _mcp_lock:
        if _mcp_orch is not None:
            return _mcp_orch
        cfg_path = config_path or MCP_CONFIG_PATH
        path = repo_path or TARGET_REPO or os.getcwd()
        orch = init_orchestrator(cfg_path, path)
        if orch:
            _mcp_orch = orch
        return orch


# ── Context budgeting helpers ────────────────────────────────────────────────


def _clip(text: str, limit: int, label: str = "context") -> str:
    """Truncate text to limit chars with a marker if it exceeds."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{label} truncated at {limit} chars]"


def _read_capped(path: Path, cap: int = 8000, label: str = "file") -> str:
    """Read up to `cap` bytes from `path` without loading the whole file.

    Prevents OOM on pathological inputs (e.g. multi-MB generated models.py).
    """
    try:
        with path.open("rb") as f:
            raw = f.read(cap + 1)
    except OSError:
        return ""
    text = raw[:cap].decode("utf-8", errors="replace")
    if len(raw) > cap:
        text += f"\n# ... [{label} truncated at {cap} chars]"
    return text


def _allocate_context_budget(
    persona_chars: int, diff_chars: int, *, total_chars: int = TOTAL_CONTEXT_BUDGET
) -> dict[str, int]:
    """Partition the model's character budget across context blocks.

    The diff is always kept in full (clipping it would skip real changes).
    Everything else is allocated from the remaining budget.
    """
    # Reserve room for: system persona, the full diff, and a generous
    # allowance for the model's pass-1 output (which feeds into pass 2 once
    # the multi-pass chain is active).
    reserved = persona_chars + diff_chars + 20_000
    remaining = max(5_000, total_chars - reserved)
    return {
        "map": int(remaining * 0.50),
        "key_files": int(remaining * 0.30),
        "mcp_project": int(remaining * 0.20),
    }


def build_mcp_context() -> str:
    orch = _mcp_orch
    if not orch:
        return ""
    return orch.build_mcp_context_block(tags=["code_review", "architectural_rule"])


def collect_key_file_context(
    target_repo: str, changed_files: list[str], max_chars: int = 15000
) -> str:
    """Read key source files referenced by changed files to give the reviewer ground truth.

    Collects:
    - Model definition files (models.py) for any changed model-related file
    - settings.py for any changed settings-related file
    - Migration files referenced by the diff

    Returns a markdown block with file contents, capped at max_chars.
    """
    repo_path = Path(target_repo).resolve()
    sections: list[str] = []
    total_chars = 0

    # Per-file cap; smaller of (2000, max_chars // 4) so we never blow the
    # whole budget on a single file.
    per_file_cap = min(8000, max(2000, max_chars // 4))

    # Determine which app directories are affected
    affected_app_dirs: set[Path] = set()
    for fpath in changed_files:
        full = repo_path / fpath
        if not full.exists():
            continue
        # Walk up to find the app directory (contains models.py)
        for parent in full.parents:
            if (parent / "models.py").exists():
                affected_app_dirs.add(parent)
                break

    # Collect models.py for affected apps
    for app_dir in sorted(affected_app_dirs):
        models_file = app_dir / "models.py"
        if not models_file.exists():
            continue
        content = _read_capped(models_file, cap=per_file_cap, label="models.py")
        if not content:
            continue
        rel = models_file.relative_to(repo_path)
        sections.append(f"### {rel}\n```python\n{content}\n```")
        total_chars += len(content)
        if total_chars >= max_chars:
            break

    # Collect settings.py if any settings-related file was changed
    settings_changed = any(
        "settings" in f.lower() or "config" in f.lower() for f in changed_files
    )
    if settings_changed and total_chars < max_chars:
        for settings_path in repo_path.rglob("settings.py"):
            if "migrations" in str(settings_path) or "fixtures" in str(settings_path):
                continue
            try:
                content = _read_capped(
                    settings_path, cap=per_file_cap * 2, label="settings.py"
                )
                if not content:
                    continue
                # Only include translation-related settings sections
                lines = content.split("\n")
                relevant_lines: list[str] = []
                in_relevant_section = False
                for line in lines:
                    if (
                        "TRANSLATION" in line.upper()
                        or "MODELTRANSLATION" in line.upper()
                    ):
                        in_relevant_section = True
                    elif (
                        line.strip()
                        and not line.startswith(" ")
                        and not line.startswith("#")
                    ):
                        in_relevant_section = False
                    if in_relevant_section:
                        relevant_lines.append(line)
                if relevant_lines:
                    snippet = "\n".join(relevant_lines[:100])
                    rel = settings_path.relative_to(repo_path)
                    sections.append(
                        f"### {rel} (translation-related settings)\n```python\n{snippet}\n```"
                    )
                    total_chars += len(snippet)
                break
            except Exception:
                continue

    if not sections:
        return ""

    header = "## Key Source File Contents (for fact-checking)\n"
    block = header + "\n\n".join(sections)
    if total_chars >= max_chars:
        block += f"\n\n... [context capped at ~{max_chars} chars]"
    return block


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Local-Cloud Hybrid Code Review Engine"
    )
    parser.add_argument(
        "--target",
        "-t",
        default="main",
        help="Target branch to merge into (default: main)",
    )
    parser.add_argument(
        "--source",
        "-s",
        default="develop",
        help="Source branch containing new changes (default: develop)",
    )
    parser.add_argument(
        "--project-context",
        "-c",
        default=None,
        help="Path to a project-specific context file (markdown) with domain knowledge to inject into the review",
    )
    parser.add_argument(
        "--repo",
        "-r",
        default=None,
        help="Path to the target repository (overrides TARGET_REPO env var and defaults)",
    )
    parser.add_argument(
        "--mcp-config",
        "-m",
        default=None,
        help="Path to MCP server config file (overrides MCP_CONFIG env var and default)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Per-request timeout in seconds for LLM calls. Defaults to 600s "
            "for local models, 120s for cloud."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    target_branch = args.target
    source_branch = args.source

    target_repo = args.repo or os.environ.get("TARGET_REPO", TARGET_REPO)
    mcp_config_path = args.mcp_config or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)

    if not target_repo:
        print("Error: No target repository specified.")
        print("Set TARGET_REPO env var or pass --repo /path/to/project")
        return 1

    print(f"{'=' * 60}")
    print("Launching Local Code Review Engine (Hybrid Mode)")
    print(f"Target Project   : {target_repo}")
    print(f"Cloud Architect  : {REASONING_ARCHITECT}")
    print(f"Local JSON Judge : {LOCAL_JUDGE}")
    print(f"Review Delta     : {target_branch} <--- {source_branch}")
    print("Review Lens      : Ponytail concision + reuse constraints active")
    print(f"{'=' * 60}\n")

    start_time = time.time()

    # 1. Gather Global Structural Picture
    print("📦 Step 1a: Parsing global project topography...")
    topographer = DjangoTopographer(target_repo)
    project_map = topographer.scan_project()

    # 2. Gather Local Line Changes Picture
    print("🔍 Step 1b: Extracting Git modifications...")
    git_layer = GitDiffProvider(target_repo)
    raw_diff = git_layer.get_diff(target_branch, source_branch)
    changed_files = git_layer.get_changed_files(target_branch, source_branch)

    if not raw_diff or not raw_diff.strip():
        print("❌ Error: No Git differences found between these branches. Exiting.")
        return 1

    print(f"   [Git] Detected changes across {len(changed_files)} files.")
    if len(raw_diff) > 50_000:
        print(
            f"   ⚠️  Diff is large ({len(raw_diff):,} chars). Context budget "
            "for map/key files will be reduced; review may be incomplete."
        )

    # 4. Load Reviewer Persona
    persona_path = "agents/code_reviewer.md"
    if not os.path.exists(persona_path):
        print(f"❌ Error: System prompt missing at {persona_path}")
        return 1

    with open(persona_path, encoding="utf-8") as f:
        system_agent_prompt = f.read()

    # 4b. Initialize MCP workbench for richer context
    print("   Initializing MCP workbench...")
    orch = init_mcp(repo_path=target_repo, config_path=mcp_config_path)
    if orch:
        print("   [Done] MCP workbench active (tools + git + memory)\n")
    else:
        print("   [Skipped] No MCP config found\n")

    try:
        return _run_review(
            target_repo=target_repo,
            target_branch=target_branch,
            source_branch=source_branch,
            raw_diff=raw_diff,
            changed_files=changed_files,
            project_map=project_map,
            system_agent_prompt=system_agent_prompt,
            project_context_path=args.project_context,
            request_timeout=args.timeout,
            start_time=start_time,
        )
    except KeyboardInterrupt:
        print("\n⛔ Review interrupted by user.")
        return 130
    except Exception as e:
        print(f"\n❌ Review failed: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
        return 2
    finally:
        # Always release MCP subprocesses / HTTP connections, even on failure.
        # Without this, an LLM timeout or judge parse error would orphan them.
        if _mcp_orch:
            with contextlib.suppress(Exception):
                _mcp_orch.stop()


def _run_review(
    *,
    target_repo: str,
    target_branch: str,
    source_branch: str,
    raw_diff: str,
    changed_files: list[str],
    project_map: dict,
    system_agent_prompt: str,
    project_context_path: str | None,
    request_timeout: float | None,
    start_time: float,
) -> int:
    """Execute the review pipeline. Returns process exit code."""

    # 4c. Load project-specific context file if provided
    project_context_block = ""
    project_context_basename = ""
    if project_context_path:
        if os.path.exists(project_context_path):
            if os.path.isdir(project_context_path):
                print(
                    f"   [Warning] Project context path is a directory, "
                    f"skipping: {project_context_path}\n"
                )
            else:
                with open(project_context_path, encoding="utf-8") as f:
                    project_context_block = minify_markdown(f.read())
                project_context_basename = os.path.basename(project_context_path)
                print(f"   [Loaded] Project context from {project_context_path}\n")
        else:
            print(
                f"   [Warning] Project context file not found: {project_context_path}\n"
            )
    else:
        print("   [Skipped] No project context file specified (-c to add)\n")

    # 5. Compute a single token-budget allocation shared by all prompts.
    # This replaces the previous ad-hoc [:8000] clips that were applied
    # inconsistently between the fallback and two-pass prompts.
    budget = _allocate_context_budget(
        persona_chars=len(system_agent_prompt), diff_chars=len(raw_diff)
    )

    project_map_json = json.dumps(project_map, default=str, separators=(",", ":"))
    changed_files_json = json.dumps(changed_files, separators=(",", ":"))

    # 5b. Collect key file contents for fact-checking (model definitions, settings)
    print("📚 Step 1c: Collecting key source files for fact-checking...")
    key_file_context = collect_key_file_context(
        target_repo, changed_files, max_chars=budget["key_files"]
    )
    if key_file_context:
        print(f"   [Done] Collected key file context ({len(key_file_context)} chars)\n")
    else:
        print("   [Skipped] No key files matched changed files\n")

    # 6. Build context sections. Each section is clipped to its allocated
    # share so the combined prompt stays within the model's context window.
    mcp_block = build_mcp_context() if _mcp_orch else ""
    mcp_budget = budget["mcp_project"] // 2
    project_budget = budget["mcp_project"] - mcp_budget

    mcp_block = _clip(mcp_block, mcp_budget, "MCP context")
    project_context_block = _clip(
        project_context_block, project_budget, "project context"
    )
    clipped_map = _clip(project_map_json, budget["map"], "project map")

    mcp_prompt_section = (
        f"\n\n### MCP-Augmented Context (Live Project State)\n{mcp_block}"
        if mcp_block
        else ""
    )
    project_context_section = (
        f"\n\n### Project-Specific Context ({project_context_basename})\n{project_context_block}"
        if project_context_block
        else ""
    )
    key_file_section = f"\n\n{key_file_context}" if key_file_context else ""

    # Single-pass fallback: used for local-only mode and cloud API failures.
    # Must be self-contained (no chained history), so include everything.
    fallback_prompt = f"""Below is the project model map (field names for fact-checking) and the git diff.{mcp_prompt_section}{project_context_section}{key_file_section}

## Project Model Map
```json
{clipped_map}
```

## Git Diff
```diff
{raw_diff}
```

## Review Instructions
Review the diff above. For each changed file, evaluate whether the changes are correct.

### Mandatory Rules (violations will be flagged):
1. **CITE DIFF LINES** — For every issue, quote the actual `+` or `-` lines from the diff. Example: `in profiles/models.py line 42: +   is_active = BooleanField(default=True)`
2. **NO FABRICATED EXAMPLES** — Never invent function names, permission codenames, method signatures, or field names. If you cannot see them in the diff, the project map, or the key source file contents, do not mention them.
3. **UNCERTAIN MEANS UNCERTAIN** — If you aren't sure whether a change is correct, say `UNCERTAIN: [what you're unsure about]`. Do not hedge with vague wording.
4. **CORRECT IS A FINDING** — If a change looks correct, say "Looks correct" explicitly. A review that finds no issues is valid.
5. **NO GENERIC ADVICE** — Do not give generic Django/python architecture lectures. Only comment on what the diff actually changes.
6. **FILE-BY-FILE** — Cover each changed file in order. For each one: (a) what changed, (b) is it correct, (c) any issues found.
7. **TEST COVERAGE & VALIDITY** — For every changed source file, check that corresponding test files exist and adequately cover the new/modified logic. Flag tests that use vague or tautological assertions (e.g., `assert response.status_code == 200` without checking response body, or `assert True`). Tests must assert meaningful outcomes — request that tests validate actual state changes, error messages, or data transformations.
8. **CONCISION & REUSE (Ponytail lens)** — Flag over-engineering in the diff:
   - Could existing codebase utilities or helpers have been reused instead of writing new code?
   - Could stdlib or an already-installed package have handled this without a new dependency?
   - Does the change add speculative abstractions or future-proofing not required by the feature?
   - Is the diff unnecessarily large for what it accomplishes?
   - Flag with "OVER-ENGINEERED" where applicable, noting what could be simplified.
9. **VERIFY CLAIMS** — When stating field attributes (max_length, null, choices), cross-reference the project topography map or key source file contents. Do not assume defaults without evidence.

Format as markdown with file paths as headings."""

    # Two-pass reasoning. The runner now chains assistant turns, so Pass 2
    # can see Pass 1's analysis. We therefore put ALL shared context (map,
    # key files, MCP, project rules) into Pass 1 only and leave Pass 2 to
    # focus on the diff plus a reference to Pass 1's context. This roughly
    # halves the token cost of the multi-pass flow.
    pass1 = f"""[Pass 1: Context Alignment & Blast Radius Mapping]
Here is the global system layout of the app (models with their fields, serializers, views):
{clipped_map}

Here are the files changed in this branch:
{changed_files_json}{mcp_prompt_section}{project_context_section}{key_file_section}

Analyze the structural intersection. Which upstream modules, views, or serializers could break or be impacted by changes to these specific files?
Identify potential vulnerabilities or scaling defects introduced by the patch.

Keep this analysis focused — Pass 2 will use it as grounding for the line-by-line review."""

    pass2 = f"""[Pass 2: Detailed Code Review]
Using the structural analysis from Pass 1 above (project map, key source files, MCP context, project rules), review the raw lines of code changed in this branch:
```diff
{raw_diff}
```

Generate your final review report. Evaluate line changes, ensure patterns are clean, verify things are getting better and not worse, and generate code corrections where needed. Do not re-fetch context — use only what Pass 1 established.

### Mandatory Rules (violations will be flagged):
1. **CITE DIFF LINES** — For every issue, quote the actual `+` or `-` lines from the diff.
2. **NO FABRICATED EXAMPLES** — Never invent function names, permission codenames, method signatures, or field names. Use only what is visible in the diff, the project map, or the key source file contents from Pass 1.
3. **UNCERTAIN MEANS UNCERTAIN** — If you aren't sure, say `UNCERTAIN: [what you're unsure about]`.
4. **CORRECT IS A FINDING** — If a change looks correct, say "Looks correct" explicitly.
5. **NO GENERIC ADVICE** — Only comment on what the diff actually changes.
6. **FILE-BY-FILE** — Cover each changed file in order: (a) what changed, (b) is it correct, (c) any issues found.
7. **TEST COVERAGE & VALIDITY** — Check tests exist for changed source files and flag weak/tautological assertions.
8. **CONCISION & REUSE (Ponytail lens)** — Flag over-engineering where existing utilities, stdlib, or smaller diffs would have sufficed.
9. **VERIFY CLAIMS** — Cross-reference field attributes against the project map / key source files from Pass 1. Do not assume defaults without evidence.

Follow the markdown schema and headers defined in your system prompt."""

    passes = [pass1, pass2]

    # 7. Execute Reasoning Pass
    print(f"🤖 Step 2: Processing Review via [{REASONING_ARCHITECT}]...")
    pass_start = time.time()

    runner = StatefulHarnessRunner(
        model_name=REASONING_ARCHITECT,
        base_url=ARCHITECT_API_BASE,
        api_key=ARCHITECT_API_KEY,
        fallback_model_name=FALLBACK_REVIEWER,
        local_fallback_model=LOCAL_JUDGE,
        num_ctx=65536,
        request_timeout=request_timeout,
    )
    history = runner.execute_sequence(
        system_prompt=system_agent_prompt,
        passes=passes,
        fallback_prompt=fallback_prompt,
    )
    if not history:
        print("❌ Error: Review produced no output (both primary and fallback failed).")
        return 2
    final_review = history[-1]["output"]
    model_used = runner.model_name

    print(
        f"   [Done] Review generated via {model_used} in {time.time() - pass_start:.2f}s"
    )

    # 8. Verify claims against codebase
    print("🔍 Step 3: Verifying claims against codebase...")
    verify_start = time.time()

    # Restrict claim extraction to known model names so generic prose like
    # "The.first" or "This.is" doesn't pollute the claim count.
    known_models = {
        m.get("name", "") for m in project_map.get("models", []) if m.get("name")
    }
    claims = extract_claims(final_review, known_models=known_models)
    verification_report = verify_claims(
        claims, project_map, key_file_context, target_repo=target_repo
    )
    final_review = annotate_review(final_review, verification_report)

    print(f"   [Done] Claim verification in {time.time() - verify_start:.2f}s")
    print(f"   {verification_report.to_summary()}")

    # 9. Evaluate review quality
    print(f"⚖️ Step 4: Checking review quality via Local Judge [{LOCAL_JUDGE}]...")
    judge_start = time.time()

    # Provide the diff + project map + key file contents as ground truth.
    # The diff is clipped to a generous window; tell the judge explicitly
    # when it cannot see the full diff so it doesn't down-score claims it
    # can't verify.
    diff_visible = raw_diff[:20000]
    diff_truncated = len(raw_diff) > 20000
    judge_diff_note = (
        "\n(Note: diff truncated to first 20000 chars for judge context.)\n"
        if diff_truncated
        else ""
    )
    judge_context = (
        f"Diff ({len(raw_diff)} chars total"
        + (", showing first 20000" if diff_truncated else "")
        + f"):\n```diff\n{diff_visible}\n```{judge_diff_note}\n\n"
        f"Project Map:\n{_clip(project_map_json, 8000, 'project map')}\n\n"
        f"{_clip(key_file_context, 4000, 'key file context')}"
    )
    evaluator = AutomatedEvaluator(judge_model=LOCAL_JUDGE)
    try:
        scores = evaluator.grade_run(
            final_review, "rubrics/code_review_rubric.json", context=judge_context
        )
    except Exception as e:
        print(
            f"   ⚠️  Judge failed ({type(e).__name__}: {e}); continuing without scores."
        )
        scores = {"_judge_error": str(e)[:200]}

    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"📊 Review Reliability Scores: {scores}")

    # 10. Log and Export Artifacts
    print("🗄️ Step 5: Archiving run data...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=model_used,
        agent_role=f"Staff Code Review ({source_branch})",
        raw_output=final_review,
        scores=scores,
    )

    report_filename = "reports/automated_code_review.md"
    os.makedirs("reports", exist_ok=True)
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(final_review)

    if _mcp_orch:
        _mcp_orch.remember(
            f"review:{source_branch}:complete",
            f"Code review completed for {source_branch} -> {target_branch}. Report: {report_filename}",
            tags=["code_review", source_branch, "complete"],
        )

    total_duration = time.time() - start_time
    print(f"\n✅ Report saved to: {report_filename}")
    print(f"⏱️ Total Time: {total_duration:.2f}s  Model: {model_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
