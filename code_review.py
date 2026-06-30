import argparse
import json
import os
import time

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

# Local Fallback: Used when cloud API is unavailable
FALLBACK_REVIEWER = "gemini-2.5-flash"
# Local Judge: Scores the review against a rubric
HEAVY_REVIEWER = "deepseek-r1:14b"
LOCAL_JUDGE = "qwen3-coder:latest"
# ==============================================================================

TARGET_REPO = os.environ.get("TARGET_REPO")
MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.python.json")

_mcp_orch = None


def init_mcp(repo_path: str | None = None, config_path: str | None = None):
    global _mcp_orch
    if _mcp_orch is not None:
        return _mcp_orch
    cfg_path = config_path or MCP_CONFIG_PATH
    path = repo_path or TARGET_REPO or os.getcwd()
    orch = init_orchestrator(cfg_path, path)
    if orch:
        _mcp_orch = orch
    return orch


def build_mcp_context() -> str:
    orch = _mcp_orch
    if not orch:
        return ""
    return orch.build_mcp_context_block(tags=["code_review", "architectural_rule"])


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
    return parser.parse_args()


def main():
    args = parse_arguments()
    target_branch = args.target
    source_branch = args.source

    target_repo = args.repo or os.environ.get("TARGET_REPO", TARGET_REPO)
    mcp_config_path = args.mcp_config or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)

    is_local_mode = not ARCHITECT_API_KEY
    if not is_local_mode and not ARCHITECT_API_KEY:
        print("❌ Error: USE_GEMINI=true requires GEMINI_API_KEY to be set.")
        print("Please run: export GEMINI_API_KEY='your_key_here'")
        return

    if not target_repo:
        print("Error: No target repository specified.")
        print("Set TARGET_REPO env var or pass --repo /path/to/project")
        return

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
        return

    print(f"   [Git] Detected changes across {len(changed_files)} files.")

    # 3. (Full file context omitted to keep prompt within context window. Project topography map below includes model fields for fact-checking.)

    # 4. Load Reviewer Persona
    persona_path = "agents/code_reviewer.md"
    if not os.path.exists(persona_path):
        print(f"❌ Error: System prompt missing at {persona_path}")
        return

    with open(persona_path, encoding="utf-8") as f:
        system_agent_prompt = f.read()

    # 4b. Initialize MCP workbench for richer context
    print("   Initializing MCP workbench...")
    orch = init_mcp(repo_path=target_repo, config_path=mcp_config_path)
    mcp_block = build_mcp_context() if orch else ""
    if orch:
        print("   [Done] MCP workbench active (tools + git + memory)\n")
    else:
        print("   [Skipped] No MCP config found\n")

    # 4c. Load project-specific context file if provided
    project_context_block = ""
    if args.project_context:
        ctx_path = args.project_context
        if os.path.exists(ctx_path):
            with open(ctx_path, encoding="utf-8") as f:
                project_context_block = minify_markdown(f.read())
            print(f"   [Loaded] Project context from {ctx_path}\n")
        else:
            print(f"   [Warning] Project context file not found: {ctx_path}\n")
    else:
        print("   [Skipped] No project context file specified (-c to add)\n")

    # 5. Build prompt context
    project_map_json = json.dumps(project_map, default=str, separators=(",", ":"))
    changed_files_json = json.dumps(changed_files, separators=(",", ":"))

    # 6. Build prompts for either cloud or local mode
    mcp_prompt_section = (
        f"\n\n### MCP-Augmented Context (Live Project State)\n{mcp_block}"
        if mcp_block
        else ""
    )
    project_context_section = (
        f"\n\n### Project-Specific Context ({os.path.basename(args.project_context)})\n{project_context_block}"
        if project_context_block
        else ""
    )

    # Single-pass fallback: used for local-only mode and cloud API failures
    # Keep project map clipped (it's reference context, not what we review).
    # The diff is kept in full — clipping it would skip reviewing real changes.
    clipped_map = project_map_json[:8000] + (
        "\n... [map truncated]" if len(project_map_json) > 8000 else ""
    )

    fallback_prompt = f"""Below is the project model map (field names for fact-checking) and the git diff.{mcp_prompt_section}{project_context_section}

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
2. **NO FABRICATED EXAMPLES** — Never invent function names, permission codenames, method signatures, or field names. If you cannot see them in the diff or the project map, do not mention them.
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

Format as markdown with file paths as headings."""

    # Two-pass reasoning: split context alignment + detailed review
    pass1 = f"""[Pass 1: Context Alignment & Blast Radius Mapping]
Here is the global system layout of the app (models with their fields, serializers, views):
{project_map_json}

Here are the files changed in this branch:
{changed_files_json}
{mcp_prompt_section}{project_context_section}

Analyze the structural intersection. Which upstream modules, views, or serializers could break or be impacted by changes to these specific files?
Identify potential vulnerabilities or scaling defects introduced by the patch."""

    pass2 = f"""[Pass 2: Detailed Code Review]
Review the raw lines of code changed in this branch:
```diff
{raw_diff}
```
{mcp_prompt_section}{project_context_section}

Generate your final review report. Evaluate line changes, ensure patterns are clean, verify things are getting better and not worse, and generate code corrections where needed.

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
        num_ctx=65536,
    )
    history = runner.execute_sequence(
        system_prompt=system_agent_prompt,
        passes=passes,
        fallback_prompt=fallback_prompt,
    )
    final_review = history[-1]["output"]
    model_used = runner.model_name

    print(
        f"   [Done] Review generated via {model_used} in {time.time() - pass_start:.2f}s"
    )

    # 8. Evaluate review quality
    print(f"⚖️ Step 3: Checking review quality via Local Judge [{LOCAL_JUDGE}]...")
    judge_start = time.time()

    # Provide the diff + project map as ground truth so the judge can detect fabrication
    judge_context = f"Diff:\n```diff\n{raw_diff[:10000]}\n```\n\nProject Map:\n{project_map_json[:5000]}"
    evaluator = AutomatedEvaluator(judge_model=LOCAL_JUDGE)
    scores = evaluator.grade_run(
        final_review, "rubrics/code_review_rubric.json", context=judge_context
    )

    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"📊 Review Reliability Scores: {scores}")

    # 9. Log and Export Artifacts
    print("🗄️ Step 4: Archiving run data...")
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
        _mcp_orch.stop()

    total_duration = time.time() - start_time
    print(f"\n✅ Report saved to: {report_filename}")
    print(f"⏱️ Total Time: {total_duration:.2f}s  Model: {model_used}")


if __name__ == "__main__":
    main()
