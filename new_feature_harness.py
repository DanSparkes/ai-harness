#!/usr/bin/env python3
"""
new_feature_harness.py
Unified execution harness — merges the Agent/Skill and MCP-based approaches.

Reads a feature pipeline (.json or .md) from generate_feature_plan.py and drives
a three-phase loop per pipeline step:

  1. Engineer Loop (inner) — generates code and iterates with the Python compiler
     (format → py_compile → mypy) until type-checking passes perfectly.

  2. Auditor Loop (outer)  — reviews the diff against structural design criteria.
     If rejected, feeds critique back to the Engineer and loops again.

  3. (Optional)            — auto-generates pytest-django + factory_boy tests
     and persists completion in MCP memory for cross-session recall.
"""

import ast
import contextlib
import json
import os
import re
import subprocess
import sys
from typing import Any

from core.agent import Agent, build_dependency_graph, skill_get_affected_files
from core.headroom import CompressionManager
from core.mcp_orchestrator import init_orchestrator
from core.parser import minify_markdown

IMPLEMENTER_MODEL = os.environ.get("IMPLEMENTER_MODEL", "ornith:35b")
AUDITOR_MODEL = os.environ.get("AUDITOR_MODEL", "qwen3-coder:latest")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

DEFAULT_MAX_ENGINEER_ATTEMPTS = 4
DEFAULT_MAX_AUDITOR_ROUNDS = 3
MAX_PRECOMMIT_ROUNDS = 5

MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.json")
_mcp_orchestrator = None

# ── Ponytail structural constraint settings ──────────────────────────
PONYTAIL_ENABLED = os.environ.get("PONYTAIL_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
PONYTAIL_MAX_DIFF_LINES = int(os.environ.get("PONYTAIL_MAX_DIFF_LINES", "100"))
PONYTAIL_SHRINK_TARGET = float(os.environ.get("PONYTAIL_SHRINK_TARGET", "0.4"))


def init_mcp_orchestrator(config_path: str, target_repo: str | None = None):
    global _mcp_orchestrator
    if _mcp_orchestrator is not None:
        return _mcp_orchestrator
    if not target_repo:
        return None
    orch = init_orchestrator(config_path, target_repo)
    if orch:
        _mcp_orchestrator = orch
    return orch


def get_mcp_orchestrator():
    return _mcp_orchestrator


def get_gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "")


def get_isolated_env() -> dict[str, str]:
    clean_env = os.environ.copy()
    clean_env.pop("VIRTUAL_ENV", None)
    clean_env.pop("PYTHONPATH", None)
    return clean_env


def build_agent(
    name: str, system_prompt: str, model_name: str | None = None, num_ctx: int = 65536
) -> Agent:
    use_gemini = model_name and "gemini" in model_name.lower()
    api_key = get_gemini_api_key() if use_gemini else None
    base_url = (
        "https://generativelanguage.googleapis.com/v1beta/openai"
        if use_gemini
        else "http://localhost:11434"
    )
    return Agent(
        name=name,
        system_prompt=system_prompt,
        model_name=model_name or IMPLEMENTER_MODEL,
        base_url=base_url,
        api_key=api_key,
        num_ctx=num_ctx,
    )


def run_formatter_toolchain(
    file_path: str, target_repo: str, run_env: dict[str, str]
) -> None:
    rel_path = os.path.relpath(file_path, target_repo)
    try:
        subprocess.run(
            ["uv", "run", "isort", "--profile", "black", rel_path],
            cwd=target_repo,
            env=run_env,
            capture_output=True,
        )
        subprocess.run(
            ["uv", "run", "black", "--target-version", "py312", rel_path],
            cwd=target_repo,
            env=run_env,
            capture_output=True,
        )
        subprocess.run(
            ["uv", "run", "ruff", "check", "--fix", rel_path],
            cwd=target_repo,
            env=run_env,
            capture_output=True,
        )
    except Exception as e:
        print(f"   Formatter toolchain warning: {e}")


def _run_mypy(
    rel_path: str, target_repo: str, run_env: dict[str, str]
) -> tuple[int, str, str]:
    mypy_res = subprocess.run(
        ["uv", "run", "mypy", "--check-untyped-defs", rel_path],
        cwd=target_repo,
        env=run_env,
        capture_output=True,
        text=True,
    )
    return mypy_res.returncode, mypy_res.stdout, mypy_res.stderr


def _parse_mypy_output(stdout: str, stderr: str) -> set[str]:
    errors = set()
    for line in (stdout + "\n" + stderr).splitlines():
        if ": error:" in line or ": warning:" in line:
            parts = line.split(": ", 1)
            if len(parts) == 2:
                errors.add(parts[1].strip())
    return errors


def validate_code_safety(
    file_path: str,
    target_repo: str,
    run_env: dict[str, str],
    baseline_mypy_errors: set[str] | None = None,
) -> tuple[bool, str]:
    rel_path = os.path.relpath(file_path, target_repo)
    try:
        syntax_res = subprocess.run(
            ["uv", "run", "python", "-m", "py_compile", rel_path],
            cwd=target_repo,
            env=run_env,
            capture_output=True,
            text=True,
        )
        if syntax_res.returncode != 0:
            return False, f"Syntax Compilation Failure:\n{syntax_res.stderr.strip()}"
    except Exception as e:
        return False, f"Syntax compiler execution failed: {e}"

    rc, out, err = _run_mypy(rel_path, target_repo, run_env)
    if rc != 0:
        combined = (out + "\n" + err).lower()
        if "no module named" in combined or "module not found" in combined:
            print(f"   \u26a0\ufe0f Mypy plugin warning (non-blocking):\n{err.strip()}")
            return True, "Passed local verification standards."
        if baseline_mypy_errors is not None:
            current_errors = _parse_mypy_output(out, err)
            new_errors = current_errors - baseline_mypy_errors
            if not new_errors:
                return (
                    True,
                    "Passed local verification standards (no new mypy violations)",
                )
            return False, "Mypy New Violations:\n" + "\n".join(sorted(new_errors))
        return (
            False,
            f"Mypy Type Guard Violation:\nSTDOUT:\n{out.strip()}\nSTDERR:\n{err.strip()}",
        )

    return True, "Passed local verification standards."


def test_path_for(source_path: str, target_repo: str) -> str | None:
    """Map a source file to its test file path using project conventions."""
    rel = os.path.relpath(source_path, target_repo)
    if (
        rel.endswith("settings.py")
        or rel.endswith("config.py")
        or rel.endswith("urls.py")
    ):
        return None
    parts = rel.replace(".py", "").split(os.sep)
    if parts[0] == "tests":
        return None

    conventions = detect_test_conventions(target_repo)
    if conventions.get("test_layout") == "flat":
        test_dir = os.path.join(target_repo, "tests")
        return os.path.join(test_dir, f"test_{parts[-1]}.py")
    app_root = parts[0]
    subdirs = parts[1:-1]
    module_name = parts[-1]
    test_dir = os.path.join(target_repo, app_root, "tests", *subdirs)
    return os.path.join(test_dir, f"test_{module_name}.py")


def detect_test_conventions(target_repo: str, target_file: str | None = None) -> dict:
    """Auto-discover project test conventions by inspecting the repo.

    Returns a dict with keys:
      - is_django: bool — whether the target file belongs to a Django project
      - settings_module: str | None — Django settings module
      - test_layout: "app-level" | "flat" | "unknown"
      - has_factories: bool — whether tests/factories.py or factories.py exists
      - factory_path: str | None
      - base_class_paths: list[str] — test base class file paths
      - base_class_names: list[str]
      - pytest_args: str — extra pytest args from config
    """
    conv: dict = {
        "is_django": False,
        "settings_module": None,
        "test_layout": "unknown",
        "has_factories": False,
        "factory_path": None,
        "base_class_paths": [],
        "base_class_names": [],
        "pytest_args": "",
    }

    # ── Check if target file is under a non-Django sub-project ─────────
    if target_file:
        full_path = (
            os.path.join(target_repo, target_file)
            if not os.path.isabs(target_file)
            else target_file
        )
        dir_path = os.path.dirname(full_path)
        # Walk up from target dir to repo root looking for sub-project markers
        while dir_path.startswith(target_repo) and dir_path != target_repo:
            req_file = os.path.join(dir_path, "requirements.txt")
            if os.path.isfile(req_file):
                with (
                    contextlib.suppress(Exception),
                    open(req_file, encoding="utf-8") as f,
                ):
                    content = f.read().lower()
                    if "django" not in content:
                        # Non-Django sub-project (e.g. Locust, scripts, etc.)
                        return conv
                    else:
                        break  # Django detected in sub-project, proceed
            dir_path = os.path.dirname(dir_path)

    # ── Detect settings module ──────────────────────────────────────
    manage_py = os.path.join(target_repo, "manage.py")
    if os.path.isfile(manage_py):
        with contextlib.suppress(Exception), open(manage_py, encoding="utf-8") as f:
            for m in re.finditer(
                r'DJANGO_SETTINGS_MODULE\s*=\s*[\'"](.+?)[\'"]', f.read()
            ):
                conv["settings_module"] = m.group(1)
                break

    if not conv["settings_module"]:
        for cfg_file in ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"):
            cfg_path = os.path.join(target_repo, cfg_file)
            if os.path.isfile(cfg_path):
                with (
                    contextlib.suppress(Exception),
                    open(cfg_path, encoding="utf-8") as f,
                ):
                    content = f.read()
                    for m in re.finditer(
                        r"DJANGO_SETTINGS_MODULE\s*=\s*[\"']?(.+?)(?:[\"'\s]|$)",
                        content,
                    ):
                        conv["settings_module"] = m.group(1)
                        break

    if conv["settings_module"]:
        conv["is_django"] = True

    # ── Detect test layout ──────────────────────────────────────────
    app_has_local_tests = False
    for entry in os.listdir(target_repo):
        app_dir = os.path.join(target_repo, entry)
        if os.path.isdir(app_dir) and os.path.isfile(
            os.path.join(app_dir, "tests", "__init__.py")
        ):
            app_has_local_tests = True
            break
    has_flat_tests = os.path.isdir(os.path.join(target_repo, "tests"))
    conv["test_layout"] = (
        "app-level"
        if app_has_local_tests
        else ("flat" if has_flat_tests else "unknown")
    )

    # ── Detect factory files ────────────────────────────────────────
    for candidate in ("tests/factories.py", "tests/factory.py", "factories.py"):
        path = os.path.join(target_repo, candidate)
        if os.path.isfile(path):
            conv["has_factories"] = True
            conv["factory_path"] = candidate
            break

    # ── Detect test base classes via AST ─────────────────────────────
    base_keywords = {"TestCase", "APITestCase", "APITestCaseBase"}
    for root, _dirs, files in os.walk(os.path.join(target_repo, "tests")):
        for filename in files:
            if not filename.endswith(".py") or filename.startswith("test_"):
                continue
            if filename == "__init__.py":
                continue
            base_path = os.path.join(root, filename)
            try:
                with open(base_path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                for node in tree.body:
                    if isinstance(node, ast.ClassDef):
                        base_names = {
                            getattr(b, "id", getattr(b, "attr", "")) for b in node.bases
                        }
                        if base_names & base_keywords or any(
                            k in node.name for k in ("Base", "Mixin", "TestCase")
                        ):
                            conv["base_class_paths"].append(
                                os.path.relpath(base_path, target_repo)
                            )
                            conv["base_class_names"].append(node.name)
            except Exception:
                pass

    # ── Detect pytest args from pyproject.toml ───────────────────────
    pyproject = os.path.join(target_repo, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            with open(pyproject, encoding="utf-8") as f:
                content = f.read()
            for m in re.finditer(r'addopts\s*=\s*"(.+?)"', content):
                conv["pytest_args"] = m.group(1)
                break
        except Exception:
            pass

    return conv


def _compute_diff_stats(original: str, modified: str) -> dict[str, int]:
    """Compute diff statistics between original and modified file contents."""
    import difflib

    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)
    diff = list(difflib.unified_diff(original_lines, modified_lines, n=0))
    added = sum(
        1 for line in diff if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1 for line in diff if line.startswith("-") and not line.startswith("---")
    )
    return {"added": added, "removed": removed, "total": added + removed}


def _build_ponytail_refinement(
    diff_stats: dict[str, int] | None, audit_verdict: str, project_context_block: str
) -> str:
    keywords = [
        "over-engineer",
        "abstraction",
        "reuse",
        "unnecessary",
        "YAGNI",
        "decision ladder",
        "rung",
        "ponytail",
        "bloated",
    ]
    triggered = any(k in audit_verdict.lower() for k in keywords)

    if not triggered and (
        not diff_stats or diff_stats["total"] < PONYTAIL_MAX_DIFF_LINES
    ):
        return ""

    lines = ["Rejection: You over-engineered this file."]
    if diff_stats and diff_stats["total"] > PONYTAIL_MAX_DIFF_LINES:
        target = max(int(diff_stats["total"] * (1 - PONYTAIL_SHRINK_TARGET)), 10)
        lines.append(
            f"Diff size ({diff_stats['total']} lines) exceeds threshold "
            f"({PONYTAIL_MAX_DIFF_LINES}). Shrink by at least "
            f"{int(PONYTAIL_SHRINK_TARGET * 100)}% (target <= {target} lines)."
        )

    lines.append("Climb rungs 1-2-4 of the Decision Ladder again:")
    lines.append(
        "  1. YAGNI — remove speculative abstractions not required by the instruction."
    )
    lines.append(
        "  2. Codebase Reuse — check existing helpers before writing new code."
    )
    lines.append("  4. Native Framework — prefer Django/DRF built-in mechanisms.")

    if project_context_block:
        lines.append(
            "Reuse the existing utilities and patterns in the project context above."
        )

    return "\n".join(lines)


def _build_retry_feedback(
    diff_stats: dict[str, int] | None, audit_verdict: str, task: dict
) -> str:
    """Build actionable retry feedback for the engineer after an auditor rejection."""
    parts = ["--- PREVIOUS ROUND FEEDBACK — address ALL points below ---"]

    if diff_stats and diff_stats["total"] > PONYTAIL_MAX_DIFF_LINES:
        target = max(int(diff_stats["total"] * (1 - PONYTAIL_SHRINK_TARGET)), 10)
        parts.append(
            f"[PONYTAIL] Your previous diff was {diff_stats['total']} lines "
            f"(+{diff_stats['added']}/-{diff_stats['removed']}). "
            f"Max allowed: {PONYTAIL_MAX_DIFF_LINES}. "
            f"Reduce to ≤{target} lines by making targeted edits, "
            f"not rewriting the entire file."
        )

    task_instr = task.get("instruction", "").lower()
    if "read_only_fields" in task_instr and (
        "read_only_fields" in audit_verdict.lower()
        and "missing" in audit_verdict.lower()
    ):
        parts.append(
            "[CONFLICT] The auditor flagged removed read_only_fields, but the "
            "task INSTRUCTION explicitly says to remove them (inherited from base). "
            "Follow the task instruction."
        )

    parts.append(f"[AUDITOR CRITIQUE]\n{audit_verdict.strip()}")
    parts.append(
        "[DIRECTIVE] Address each point above. "
        "Make MINIMAL edits to the existing file. "
        "Do NOT rewrite code sections that are not part of the change."
    )
    parts.append("--- END FEEDBACK ---")

    return "\n".join(parts)


def _prune_old_feedback(
    context_history: str, target_file: str, current_round: int
) -> str:
    """Remove stale audit critique and ponytail entries older than the current round."""
    for tag in ("Auditor Critique", "Ponytail Structural Refinement"):
        # Remove entries for rounds < current_round
        context_history = re.sub(
            rf"\n\n\[{tag} Round \d+ for {re.escape(target_file)}\].*?"
            rf"(?=\n\n\[|\Z)",
            "",
            context_history,
            flags=re.DOTALL,
        )
    return context_history


def clean_model_output(raw_output: str) -> str:
    clean_lines = []
    backtick_trigger = "`" * 3
    for line in raw_output.splitlines():
        stripped = line.strip()
        if stripped.startswith(backtick_trigger):
            continue
        # Strip MCP tool output artifacts (XML-like tags) that leak into code.
        # No valid Python expression starts with '<'.
        if stripped.startswith("<") and ">" in stripped:
            continue
        clean_lines.append(line)
    return "\n".join(clean_lines).strip()


def load_plan(path: str) -> tuple[dict[str, Any], str]:
    if path.endswith(".md"):
        with open(path, encoding="utf-8") as f:
            report_text = minify_markdown(f.read())

        sections = re.split(
            r"^##\s+4\.?\s*Implementation Pipeline\s*$", report_text, flags=re.MULTILINE
        )
        if len(sections) < 2:
            sections = re.split(
                r"^##\s+Implementation Pipeline\s*$", report_text, flags=re.MULTILINE
            )

        if len(sections) < 2:
            print(
                f"Error: '{path}' is a .md file but has no '## Implementation Pipeline' section."
            )
            sys.exit(1)

        json_blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", sections[1], re.DOTALL)
        if not json_blocks:
            print(
                f"Error: No JSON code block found in Implementation Pipeline section of '{path}'."
            )
            sys.exit(1)

        plan_data = None
        for block in json_blocks:
            try:
                plan_data = json.loads(block.strip())
                break
            except json.JSONDecodeError:
                continue

        if plan_data is None:
            print(
                f"Error: Could not parse JSON in Implementation Pipeline section of '{path}'."
            )
            sys.exit(1)

        return plan_data, report_text

    with open(path, encoding="utf-8") as f:
        plan_data = json.load(f)
    return plan_data, ""


def main() -> None:
    skip_tests = "--skip-tests" in sys.argv
    mcp_config_override = None
    project_context = None
    filtered_argv = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ("--mcp-config", "-m") and i + 1 < len(sys.argv):
            mcp_config_override = sys.argv[i + 1]
            i += 2
            continue
        if a in ("--project-context", "-c") and i + 1 < len(sys.argv):
            project_context = sys.argv[i + 1]
            i += 2
            continue
        filtered_argv.append(sys.argv[i])
        i += 1

    plan_path = next((a for a in filtered_argv if not a.startswith("--")), None)
    if not plan_path:
        print(
            "Usage: python3 new_feature_harness.py <plan.json|plan.md> [--skip-tests] [--mcp-config <path>] [--project-context <path>]"
        )
        print("  .json  \u2014 Direct execution plan")
        print(
            "  .md    \u2014 Report with embedded pipeline (report used as architectural context"
        )
        print(
            "  --skip-tests  \u2014 Do NOT auto-generate pytest-django + factory_boy unit tests"
        )
        print(
            "  --mcp-config  \u2014 Path to MCP server configuration JSON (default: $MCP_CONFIG or mcp_config.json)"
        )
        print(
            "  --project-context, -c  \u2014 Path to project-specific context file (markdown)"
        )
        sys.exit(1)
    if not os.path.exists(plan_path):
        print(f"Error: Plan file not found at '{plan_path}'")
        sys.exit(1)

    plan_data, exploration_report = load_plan(plan_path)

    feature_name = plan_data.get("feature_name", "Unnamed Feature")
    target_repo = plan_data.get("target_workspace")
    pipeline = plan_data.get("pipeline", [])

    if not target_repo or not os.path.exists(target_repo):
        print(f"Error: Target workspace directory '{target_repo}' does not exist.")
        sys.exit(1)

    use_gemini = "gemini" in AUDITOR_MODEL.lower()
    if use_gemini and not get_gemini_api_key():
        print("Setup Error: GEMINI_API_KEY is not set.")
        print("Run: export GEMINI_API_KEY='your_api_key_here'")
        sys.exit(1)

    print("=== Launching Universal Hybrid Execution Workflow ===")
    print(f"Active Feature Campaign : {feature_name}")
    print(f"Target System Workspace : {target_repo}")
    print(f"Implementer Model       : [{IMPLEMENTER_MODEL}]")
    print(f"Auditor Model           : [{AUDITOR_MODEL}]\n")

    isolated_env = get_isolated_env()

    print("Pre-Flight: Synchronizing workspace virtual environment dependencies...")
    try:
        subprocess.run(
            ["uv", "sync"],
            cwd=target_repo,
            env=isolated_env,
            check=True,
            capture_output=True,
        )
        print("   Workspace dependencies synchronized successfully.\n")
    except subprocess.CalledProcessError as e:
        print(f"   Pre-flight workspace sync failed:\n{e.stderr.decode().strip()}")
        sys.exit(1)

    mcp_cfg_path = mcp_config_override or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)
    mcp_orch = init_mcp_orchestrator(mcp_cfg_path, target_repo)
    if mcp_orch:
        print(
            "  MCP Workbench active: git context, memory, and reasoning tools available.\n"
        )
        mcp_context = mcp_orch.build_mcp_context_block()
        mcp_project_discovery = mcp_orch.discover_project_context()
        django_context, _django_raw = mcp_orch.build_django_live_context()
    else:
        mcp_context = ""
        mcp_project_discovery = ""
        django_context = ""
        print("  (No MCP servers configured. Run with --mcp-config to enable.)\n")

    # ── Load project context file ─────────────────────────────────
    project_context_block = ""
    if project_context:
        ctx_path = project_context
        if os.path.exists(ctx_path):
            with open(ctx_path, encoding="utf-8") as f:
                project_context_block = minify_markdown(f.read())
            print(f"   [Loaded project context: {ctx_path}]")
        else:
            print(f"   [Warning: project context file not found: {ctx_path}]")

    agents_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")
    implementer_path = os.path.join(agents_dir, "code_implementer.md")
    auditor_path = os.path.join(agents_dir, "integration_auditor.md")

    if not os.path.exists(implementer_path) or not os.path.exists(auditor_path):
        print("Error: Missing agent system files in 'agents/'.")
        sys.exit(1)

    with open(implementer_path, encoding="utf-8") as f:
        implementer_persona = f.read()
    with open(auditor_path, encoding="utf-8") as f:
        auditor_persona = f.read()

    context_history = f"Feature Engineering Campaign Context Plan for: {feature_name}\n"
    if exploration_report:
        context_history += f"\n\nArchitectural Analysis Report:\n{exploration_report}\n"
    if project_context_block:
        context_history += f"\n\nProject Conventions:\n{project_context_block}\n"
    if django_context:
        context_history += f"\n\n{django_context}\n"
    if mcp_project_discovery:
        context_history += f"\n\n{mcp_project_discovery}\n"

    # Load standalone exploration report if present (from earlier pipeline runs)
    standalone_exploration = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "reports",
        "access_gates_architectural_exploration.md",
    )
    if os.path.exists(standalone_exploration):
        with open(standalone_exploration, encoding="utf-8") as f:
            extra_context = minify_markdown(f.read())
        context_history += f"\n\nArchitectural Exploration Report:\n{extra_context}\n"

    markdown_fence = "`" * 3

    implementer_agent = build_agent(
        "Implementer", implementer_persona, IMPLEMENTER_MODEL
    )
    auditor_agent = build_agent("Auditor", auditor_persona, AUDITOR_MODEL)
    print(
        f"   Implementer: {implementer_agent.model_name} | Auditor: {auditor_agent.model_name}"
    )

    headroom = CompressionManager(
        target_ratio=0.4, compress_user_messages=True, protect_recent=0
    )
    print(f"   Headroom compression active (target ratio: {headroom.target_ratio})")

    if PONYTAIL_ENABLED:
        print(
            f"   Ponytail structural constraints active "
            f"(max diff: {PONYTAIL_MAX_DIFF_LINES} lines, "
            f"shrink target: {PONYTAIL_SHRINK_TARGET:.0%})"
        )
    else:
        print("   Ponytail structural constraints: DISABLED")

    # Build dependency graph for cascading breakage detection
    print("\n\U0001f50d Building dependency graph...")
    dep_graph = build_dependency_graph(target_repo)
    print(f"   {len(dep_graph)} modules tracked for cascading breakage.\n")

    for task in pipeline:
        task.setdefault("instruction", task.get("task", task.get("instruction", "")))

        print(f"\n[Stage {task['step']}] {task['name']}")
        full_target_path = os.path.join(target_repo, task["target_file"])

        file_backup_contents = None
        existing_file_context = ""
        is_modifying_existing_file = False

        if os.path.exists(full_target_path):
            is_modifying_existing_file = True
            with open(full_target_path, encoding="utf-8") as f:
                file_backup_contents = f.read()
            existing_file_context = (
                f"\n\nExisting file (preserve structure, add/modify only what's requested):\n"
                f"{markdown_fence}python\n{file_backup_contents}\n{markdown_fence}\n"
            )

            affected = skill_get_affected_files(task["target_file"], graph=dep_graph)
            if affected and not affected.startswith("(no"):
                existing_file_context += (
                    f"\n\nFiles importing this module (don't break):\n{affected}\n"
                )

        max_engineer_attempts = task.get(
            "max_engineer_attempts", DEFAULT_MAX_ENGINEER_ATTEMPTS
        )
        max_auditor_rounds = task.get("max_auditor_rounds", DEFAULT_MAX_AUDITOR_ROUNDS)
        stage_completed_successfully = False

        # Compress accumulated context before this stage
        if len(context_history) > 5000:
            compressed, cr = headroom.compress_context(context_history)
            if cr.tokens_saved > 0:
                context_history = compressed
                print(
                    f"   Headroom: context compressed {cr.tokens_before:,} -> {cr.tokens_after:,} tok ({cr.compression_ratio:.1%} saved)"
                )
        if len(existing_file_context) > 5000:
            compressed, cr = headroom.compress_context(existing_file_context)
            if cr.tokens_saved > 0:
                existing_file_context = compressed
                print(
                    f"   Headroom: file context compressed {cr.tokens_before:,} -> {cr.tokens_after:,} tok ({cr.compression_ratio:.1%} saved)"
                )

        # Verify-only tasks: skip write-validate loop, just run auditor on existing file
        if task.get("verify_only"):
            print(
                "   Verify-only task: skipping engineer loop, running auditor on existing file"
            )
            if is_modifying_existing_file:
                existing_content = file_backup_contents
            else:
                existing_content = ""
            # Run auditor directly
            for auditor_round in range(1, max_auditor_rounds + 1):
                audit_prompt = f"Review existing file:\n{markdown_fence}python\n{existing_content}\n{markdown_fence}\n\nTask: {task['instruction']}\nConclude: VERDICT: APPROVED or VERDICT: REJECTED."
                if mcp_context:
                    audit_prompt += f"\n\nMCP Tool Context:\n{mcp_context}"
                audit_verdict = auditor_agent.execute(audit_prompt)
                print(
                    f"\n--- Audit Trace for Step {task['step']} (Round {auditor_round}) ---\n{audit_verdict}\n----------------------------------\n"
                )
                if "VERDICT: APPROVED" in audit_verdict:
                    print("  \U0001f389 Audit Approved: Verify-only task complete.")
                    stage_completed_successfully = True
                    break
                else:
                    print(f"  \U0001f6d1 Auditor Rejected Round {auditor_round}.")
            if not stage_completed_successfully:
                print(
                    f"Critical: Maximum auditor rounds exhausted for verify-only task {task['name']}. Halting."
                )
                sys.exit(1)
            context_history += f"\n\n[Verified (verify-only): {task['target_file']}]\n"
            continue

        # Capture baseline mypy errors from original file (if modifying existing)
        baseline_mypy_errors = set()
        if is_modifying_existing_file and file_backup_contents is not None:
            tmp_path = os.path.join(
                target_repo, f".baseline_{os.path.basename(task['target_file'])}"
            )
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(file_backup_contents)
                rc, out, err = _run_mypy(
                    os.path.basename(tmp_path), target_repo, isolated_env
                )
                if rc != 0:
                    baseline_mypy_errors = _parse_mypy_output(out, err)
                    if baseline_mypy_errors:
                        print(
                            f"   Baseline mypy violations ({len(baseline_mypy_errors)}): will not block"
                        )
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

        # ── Auditor Outer Loop ────────────────────────────────────────────
        last_diff_stats: dict[str, int] | None = None
        last_audit_verdict: str | None = None

        for auditor_round in range(1, max_auditor_rounds + 1):
            engineer_succeeded = False
            engineer_log = ""
            diff_stats: dict[str, int] | None = None

            # ── Engineer Inner Loop (compiler loop) ───────────────────────
            for engineer_attempt in range(1, max_engineer_attempts + 1):
                print(
                    f"  \U0001f527 Engineer attempt {engineer_attempt}/{max_engineer_attempts}"
                    f" (auditor round {auditor_round}/{max_auditor_rounds})..."
                )

                retry_feedback = ""
                if auditor_round > 1 and last_audit_verdict:
                    retry_feedback = _build_retry_feedback(
                        last_diff_stats, last_audit_verdict, task
                    )

                user_prompt = f"""
Architectural Plan:
{context_history}
{existing_file_context}

Target: {task['target_file']}
Task: {task['instruction']}

{retry_feedback}

Generate complete production code for this file. Type-annotated. No commentary.
"""

                engineer_temp = 0.3 if auditor_round > 1 else None
                raw_response = implementer_agent.execute(
                    user_prompt, stream=True, temperature=engineer_temp
                )

                # Streaming syntax error detection
                if raw_response.startswith("__SYNTAX_ERROR__:"):
                    _, err, _partial = raw_response.split(":", 2)
                    print(f"   \u274c Streaming Syntax Error: {err[:200]}")
                    if file_backup_contents is not None:
                        with open(full_target_path, "w", encoding="utf-8") as f:
                            f.write(file_backup_contents)
                    context_history += f"\n\n[Attempt {engineer_attempt} Syntax Error for {task['target_file']}]:\n{err}"
                    continue

                code_generated = clean_model_output(raw_response)

                os.makedirs(os.path.dirname(full_target_path), exist_ok=True)
                with open(full_target_path, "w", encoding="utf-8") as f:
                    f.write(code_generated)

                run_formatter_toolchain(full_target_path, target_repo, isolated_env)

                with open(full_target_path, encoding="utf-8") as f:
                    code_generated = f.read()

                success, log_msg = validate_code_safety(
                    full_target_path, target_repo, isolated_env, baseline_mypy_errors
                )
                if not success:
                    print(
                        f"  \u274c Linter/Type Check Failed on Engineer attempt {engineer_attempt}."
                    )
                    print(
                        f"     --- Verification Error Output ---\n{log_msg}\n     ---------------------------------"
                    )

                    if file_backup_contents is not None:
                        with open(full_target_path, "w", encoding="utf-8") as f:
                            f.write(file_backup_contents)
                    elif os.path.exists(full_target_path):
                        os.remove(full_target_path)

                    context_history += f"\n\n[Engineer Attempt {engineer_attempt} Error on {task['target_file']}]:\n{log_msg}"
                    engineer_log = log_msg
                    continue

                # Engineer succeeded — compiler passed
                engineer_succeeded = True
                engineer_log = ""

                # ── Ponytail: compute diff stats ─────────────────────────
                diff_stats = None
                if PONYTAIL_ENABLED and file_backup_contents is not None:
                    diff_stats = _compute_diff_stats(
                        file_backup_contents, code_generated
                    )
                    if diff_stats["total"] > PONYTAIL_MAX_DIFF_LINES:
                        print(
                            f"  \U0001f4ad Ponytail: diff size {diff_stats['total']} lines "
                            f"(+{diff_stats['added']}/-{diff_stats['removed']}) "
                            f"exceeds threshold {PONYTAIL_MAX_DIFF_LINES}"
                        )
                    else:
                        print(
                            f"  \U0001f4ad Ponytail: diff size {diff_stats['total']} lines "
                            f"(+{diff_stats['added']}/-{diff_stats['removed']}) within threshold"
                        )

                print(
                    "  \u2705 Local Verification Gates Passed: Formatted code compiles cleanly."
                )
                break  # exit engineer inner loop

            if not engineer_succeeded:
                print(
                    f"  \u274c Engineer failed after {max_engineer_attempts} attempts"
                    f" in auditor round {auditor_round}. "
                    + (
                        "Passing to auditor with last error for context..."
                        if engineer_log
                        else "Halting."
                    )
                )
                if not engineer_log:
                    print(
                        f"Critical: Could not generate valid code for {task['name']}. Halting pipeline."
                    )
                    sys.exit(1)
                # Let the auditor see the last error; don't halt yet

            # ── Auditor Review ─────────────────────────────────────────────
            print(
                f"  \U0001f50d Initiating Architectural Integration Review via [{AUDITOR_MODEL}]..."
                + (
                    f" (auditor round {auditor_round}/{max_auditor_rounds})"
                    if not engineer_succeeded
                    else ""
                )
            )

            # ── Ponytail: prepend structural constraint section ──────────
            ponytail_section = ""
            if (
                PONYTAIL_ENABLED
                and diff_stats
                and diff_stats["total"] > PONYTAIL_MAX_DIFF_LINES
            ):
                ponytail_section = (
                    f"\n\n### Structural Constraint Check (Ponytail)\n"
                    f"This change alters {diff_stats['total']} lines "
                    f"(+{diff_stats['added']}/-{diff_stats['removed']}) — "
                    f"exceeds the {PONYTAIL_MAX_DIFF_LINES}-line threshold.\n"
                    f"Flag with OVER-ENGINEERED if the diff could reasonably be smaller. "
                    f"Reference which Decision Ladder rungs the implementer skipped."
                )

            if is_modifying_existing_file:
                scope_directive = (
                    f"Review ONLY this instruction's change:\n"
                    f"{markdown_fence}\n{task['instruction']}\n{markdown_fence}\n\n"
                    f"Ignore all pre-existing code. Judge only whether the requested change was implemented correctly. "
                    f"If you cannot identify the change, default VERDICT: APPROVED."
                )
            else:
                scope_directive = "Check for: no cross-view imports, no N+1 queries, valid framework declarations."

            audit_prompt = (
                (
                    f"ORIGINAL:\n{markdown_fence}python\n{file_backup_contents}\n{markdown_fence}\n\n"
                    f"MODIFIED:\n{markdown_fence}python\n{code_generated}\n{markdown_fence}\n\n"
                    f"{ponytail_section}\n\n"
                    f"{scope_directive}\n\n"
                    f"Reject only if new code introduces a runtime risk absent in the original. "
                    f"Conclude: VERDICT: APPROVED or VERDICT: REJECTED."
                )
                if is_modifying_existing_file
                else (
                    f"Proposed module for '{task['target_file']}':\n"
                    f"{markdown_fence}python\n{code_generated}\n{markdown_fence}\n\n"
                    f"{ponytail_section}\n\n"
                    f"{scope_directive}\n\n"
                    f"Conclude: VERDICT: APPROVED or VERDICT: REJECTED."
                )
            )

            # Include engineer failure context if present
            if not engineer_succeeded and engineer_log:
                audit_prompt += (
                    f"\n\nEngineer could not produce clean compilation. Last error:\n{engineer_log}\n"
                    f"Accept? Or provide guidance for next round."
                )

            audit_verdict = auditor_agent.execute(audit_prompt)
            print(
                f"\n--- Audit Trace for Step {task['step']} (Round {auditor_round}) ---\n{audit_verdict}\n----------------------------------\n"
            )

            if "VERDICT: APPROVED" in audit_verdict:
                print("  \U0001f389 Audit Approved: Saved production module.")
                stage_completed_successfully = True
                context_history += (
                    f"\n\n[Verified Module Added: {task['target_file']}]\n"
                )
                break  # exit auditor outer loop — stage done
            else:
                print(
                    f"  \U0001f6d1 Auditor Rejected Round {auditor_round}. Feeding critique back to Engineer..."
                )

                # Save for next round's retry feedback
                last_diff_stats = diff_stats
                last_audit_verdict = audit_verdict

                # Prune stale feedback older than current round
                context_history = _prune_old_feedback(
                    context_history, task["target_file"], auditor_round
                )
                context_history += (
                    f"\n\n[Auditor Critique Round {auditor_round} for "
                    f"{task['target_file']}]:\n{audit_verdict}"
                )

                # ── Ponytail: inject structural refinement token ─────────
                if PONYTAIL_ENABLED:
                    ponytail_refinement = _build_ponytail_refinement(
                        diff_stats, audit_verdict, project_context_block
                    )
                    if ponytail_refinement:
                        print(
                            "  \U0001f4ad Ponytail: injecting structural refinement token"
                        )
                        # Remove stale ponytail entry before appending
                        context_history = _prune_old_feedback(
                            context_history, task["target_file"], auditor_round
                        )
                        context_history += (
                            f"\n\n[Ponytail Structural Refinement Round {auditor_round}]:\n"
                            f"{ponytail_refinement}\n"
                        )

                # Restore file to original state for next engineer attempt
                if file_backup_contents is not None:
                    with open(full_target_path, "w", encoding="utf-8") as f:
                        f.write(file_backup_contents)
                elif os.path.exists(full_target_path):
                    os.remove(full_target_path)

        if not stage_completed_successfully:
            print(
                f"Critical: Maximum auditor rounds ({max_auditor_rounds}) exhausted for {task['name']}. Halting pipeline execution."
            )
            sys.exit(1)

        # Store implementation knowledge in MCP memory for cross-session recall
        if mcp_orch:
            mcp_orch.remember(
                f"feature:{feature_name}:step:{task['step']}",
                f"Implemented {task['name']} in {task['target_file']}",
                tags=["feature", feature_name, "active"],
            )

        # Auto-generate unit tests after a successful stage
        if not skip_tests:
            test_path = test_path_for(full_target_path, target_repo)
            if test_path and not os.path.exists(test_path):
                conventions = detect_test_conventions(target_repo, task["target_file"])
                if not conventions["is_django"]:
                    print(
                        f"  Skipping test generation for {task['target_file']}: "
                        "non-Django project or sub-project (e.g. Locust)."
                    )
                else:
                    print(
                        f"  -> Auto-generating unit tests for {task['target_file']}..."
                    )
                    test_rel = os.path.relpath(test_path, target_repo)
                    test_instruction = (
                        f"Write pytest-django unit tests for the code in {task['target_file']}. "
                        f"Name the file {test_rel}."
                    )

                    conv_parts = []

                    if conventions["settings_module"]:
                        conv_parts.append(
                            f"DJANGO_SETTINGS_MODULE={conventions['settings_module']}\n"
                        )

                    if conventions["test_layout"] == "app-level":
                        conv_parts.append(
                            "Test layout: app-level — tests/ dir inside each app, mirrors source structure."
                        )
                    elif conventions["test_layout"] == "flat":
                        conv_parts.append(
                            "Test layout: flat — all tests under a single tests/ directory."
                        )

                    if conventions["has_factories"] and conventions["factory_path"]:
                        conv_parts.append(
                            f"Factories: {conventions['factory_path']} — "
                            "factory_boy DjangoModelFactory per model. "
                            "SubFactory for FK/MTM. "
                            "Sequence for unique fields, Faker for data, "
                            "LazyFunction for timestamps."
                        )

                    if conventions["base_class_names"]:
                        cls_list = ", ".join(conventions["base_class_names"])
                        paths = ", ".join(conventions["base_class_paths"])
                        conv_parts.append(
                            f"Base test classes ({paths}): {cls_list} — "
                            "inherit these in view tests."
                        )

                    if conventions["pytest_args"]:
                        conv_parts.append(f"pytest args: {conventions['pytest_args']}")

                    conv_block = "\n".join(
                        f"  {c}"
                        for c in conv_parts or ["  Standard pytest-django conventions."]
                    )

                    test_user_prompt = f"""
                    Context:
                    {context_history}

                    Target: {test_rel}
                    Task: {test_instruction}

                    Project test conventions:

                    {conv_block}

                    === GENERIC RULES ===
                    @patch from unittest.mock, target the usage module path.
                    side_effect for exceptions.
                    assertRaises / assertEqual / assertTrue / assertFalse / assertIn.
                    setUp calls super(). setUpTestData for class-level data.
                    No conftest fixtures (define in test file).
                    No bare assert (Bandit B101).
                    snake_case method names.
                    Type-annotated.
                    No commentary.

                    Generate a complete, runnable test file with edge cases.
                    """
                    raw_test = implementer_agent.execute(test_user_prompt, stream=True)
                    test_code = clean_model_output(raw_test)

                    os.makedirs(os.path.dirname(test_path), exist_ok=True)
                    with open(test_path, "w", encoding="utf-8") as f:
                        f.write(test_code)

                    run_formatter_toolchain(test_path, target_repo, isolated_env)
                    success, log_msg = validate_code_safety(
                        test_path, target_repo, isolated_env, set()
                    )
                    if success:
                        print(f"  Test file {test_rel} generated and verified.")
                        context_history += f"\n\n[Tests Added: {test_rel}]\n"
                    else:
                        print(
                            f"  Test file {test_rel} generated but has validation issues (will not block):"
                        )
                        print(f"     {log_msg}")
                        context_history += (
                            f"\n\n[Tests Added (with warnings): {test_rel}]\n"
                        )

    # Final pre-commit gauntlet: iterate until all hooks pass or max rounds exhausted
    print("\n=== Running pre-commit gauntlet ===")
    precommit_ok = False
    pre_result = None
    for pc_round in range(1, MAX_PRECOMMIT_ROUNDS + 1):
        print(f"   pre-commit round {pc_round}/{MAX_PRECOMMIT_ROUNDS}...")
        try:
            subprocess.run(
                ["git", "add", "."],
                cwd=target_repo,
                capture_output=True,
                text=True,
                check=True,
            )
            pre_result = subprocess.run(
                ["pre-commit", "run", "--all-files"],
                cwd=target_repo,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if pre_result.returncode == 0:
                print("   \u2713 All pre-commit hooks passed.")
                precommit_ok = True
                break
            print(
                f"   \u2717 Hooks failed (round {pc_round}). Re-staging auto-fixes and retrying..."
            )
        except subprocess.TimeoutExpired:
            print(f"   \u26a0 pre-commit timed out on round {pc_round}.")
            break
        except FileNotFoundError:
            print("   \u26a0 pre-commit not found in PATH (non-blocking).")
            precommit_ok = True
            break
        except subprocess.CalledProcessError:
            print(f"   \u26a0 git add failed on round {pc_round}.")
            break

    if not precommit_ok:
        print("   \u2717 pre-commit did not pass after all rounds.")
        if pre_result:
            print(pre_result.stdout.strip()[-3000:])

    if mcp_orch:
        mcp_orch.remember(
            f"campaign:{feature_name}:complete",
            f"Campaign '{feature_name}' completed successfully across {len(pipeline)} stages",
            tags=["feature", feature_name, "campaign_complete"],
        )
        mcp_orch.stop()

    print(
        f"\n=== Universal Engine Campaign for '{feature_name}' Completed Successfully ==="
    )


if __name__ == "__main__":
    main()
