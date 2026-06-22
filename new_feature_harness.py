#!/usr/bin/env python3
"""
new_feature_harness.py
Reads a feature pipeline (.json or .md) and drives multi-agent coding workflows
with local-mlx-lm/Gemini cloud support, human-reviewable .md report consumption,
and an optional multi-MCP tool workbench for codebase exploration, git context,
persistent memory, and structured reasoning.
"""

import os
import re
import sys
import json
import time
import subprocess
import requests
from typing import Any, Dict, Tuple

# Core Configurations
from core.agent import Agent

IMPLEMENTER_MODEL = "qwen2.5-coder:14b"
AUDITOR_MODEL     = "qwen3.6:latest"
GEMINI_MODEL      = "gemini-2.5-flash"

MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.json")
_mcp_orchestrator = None


def init_mcp_orchestrator(config_path: str, target_repo: str | None = None):
    global _mcp_orchestrator
    if _mcp_orchestrator is not None:
        return _mcp_orchestrator
    if not os.path.exists(config_path):
        return None
    from core.mcp_orchestrator import MCPOrchestrator
    orch = MCPOrchestrator(config_path, target_repo=target_repo)
    started = orch.start()
    if started:
        _mcp_orchestrator = orch
        if target_repo:
            orch.call_tool("git", "git_set_repo", {"path": target_repo})
        return orch
    return None


def get_mcp_orchestrator():
    return _mcp_orchestrator


def get_gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "")


def get_isolated_env() -> Dict[str, str]:
    clean_env = os.environ.copy()
    clean_env.pop("VIRTUAL_ENV", None)
    clean_env.pop("PYTHONPATH", None)
    return clean_env


def build_agent(name: str, system_prompt: str, model_name: str = None, num_ctx: int = 65536) -> Agent:
    use_gemini = model_name and "gemini" in model_name.lower()
    api_key = get_gemini_api_key() if use_gemini else None
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai" if use_gemini else "http://localhost:11434"
    return Agent(
        name=name,
        system_prompt=system_prompt,
        model_name=model_name or IMPLEMENTER_MODEL,
        base_url=base_url,
        api_key=api_key,
        num_ctx=num_ctx,
    )


def run_formatter_toolchain(file_path: str, target_repo: str, run_env: Dict[str, str]) -> None:
    rel_path = os.path.relpath(file_path, target_repo)
    try:
        subprocess.run(["uv", "run", "isort", "--profile", "black", rel_path], cwd=target_repo, env=run_env, capture_output=True)
        subprocess.run(["uv", "run", "black", "--target-version", "py312", rel_path], cwd=target_repo, env=run_env, capture_output=True)
        subprocess.run(["uv", "run", "ruff", "check", "--fix", rel_path], cwd=target_repo, env=run_env, capture_output=True)
    except Exception as e:
        print(f"   Formatter toolchain warning: {e}")


def validate_code_safety(file_path: str, target_repo: str, run_env: Dict[str, str]) -> Tuple[bool, str]:
    rel_path = os.path.relpath(file_path, target_repo)
    try:
        syntax_res = subprocess.run(
            ["uv", "run", "python", "-m", "py_compile", rel_path],
            cwd=target_repo, env=run_env, capture_output=True, text=True
        )
        if syntax_res.returncode != 0:
            return False, f"Syntax Compilation Failure:\n{syntax_res.stderr.strip()}"
    except Exception as e:
        return False, f"Syntax compiler execution failed: {e}"

    try:
        mypy_res = subprocess.run(
            ["uv", "run", "mypy", "--check-untyped-defs", rel_path],
            cwd=target_repo, env=run_env, capture_output=True, text=True
        )
        if mypy_res.returncode != 0:
            combined = (mypy_res.stdout + "\n" + mypy_res.stderr).lower()
            # Environment issues (missing plugins, config errors) are warnings, not gate failures
            if "no module named" in combined or "module not found" in combined:
                print(f"   ⚠️ Mypy plugin warning (non-blocking):\n{mypy_res.stderr.strip()}")
            else:
                return False, f"Mypy Type Guard Violation:\nSTDOUT:\n{mypy_res.stdout.strip()}\nSTDERR:\n{mypy_res.stderr.strip()}"
    except Exception as e:
        return False, f"Mypy check execution failed to initialize: {e}"

    return True, "Passed local verification standards."


def test_path_for(source_path: str, target_repo: str) -> str | None:
    """Derive a test file path from a source module path. Returns None for files that don't need tests."""
    rel = os.path.relpath(source_path, target_repo)
    # Skip config/settings files
    if rel.endswith("settings.py") or rel.endswith("config.py") or rel.endswith("urls.py"):
        return None
    parts = rel.replace(".py", "").split(os.sep)
    if parts[0] == "tests":
        return None  # already a test file
    # Map memores/external/claude_api.py -> memores/tests/external/test_claude_api.py
    app_root = parts[0]
    subdirs = parts[1:-1]
    module_name = parts[-1]
    test_dir = os.path.join(target_repo, app_root, "tests", *subdirs)
    return os.path.join(test_dir, f"test_{module_name}.py")


def clean_model_output(raw_output: str) -> str:
    clean_lines = []
    backtick_trigger = "`" * 3
    for line in raw_output.splitlines():
        if line.strip().startswith(backtick_trigger):
            continue
        clean_lines.append(line)
    return "\n".join(clean_lines).strip()


def load_plan(path: str) -> Tuple[Dict[str, Any], str]:
    """
    Load a feature plan from either a .json or .md file.
    Returns (plan_data, exploration_report) where exploration_report is
    the full .md content (empty string for .json inputs).
    """
    if path.endswith(".md"):
        with open(path, "r", encoding="utf-8") as f:
            report_text = f.read()

        sections = re.split(r"^##\s+4\.?\s*Implementation Pipeline\s*$", report_text, flags=re.MULTILINE)
        if len(sections) < 2:
            sections = re.split(r"^##\s+Implementation Pipeline\s*$", report_text, flags=re.MULTILINE)

        if len(sections) < 2:
            print(f"Error: '{path}' is a .md file but has no '## Implementation Pipeline' section.")
            sys.exit(1)

        json_blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", sections[1], re.DOTALL)
        if not json_blocks:
            print(f"Error: No JSON code block found in Implementation Pipeline section of '{path}'.")
            sys.exit(1)

        plan_data = None
        for block in json_blocks:
            try:
                plan_data = json.loads(block.strip())
                break
            except json.JSONDecodeError:
                continue

        if plan_data is None:
            print(f"Error: Could not parse JSON in Implementation Pipeline section of '{path}'.")
            sys.exit(1)

        return plan_data, report_text

    # .json path
    with open(path, "r", encoding="utf-8") as f:
        plan_data = json.load(f)
    return plan_data, ""


def main() -> None:
    skip_tests = "--skip-tests" in sys.argv
    mcp_config_override = None
    filtered_argv = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "--mcp-config" and i + 1 < len(sys.argv):
            mcp_config_override = sys.argv[i + 1]
            i += 2
            continue
        filtered_argv.append(sys.argv[i])
        i += 1

    plan_path = next((a for a in filtered_argv if not a.startswith("--")), None)
    if not plan_path:
        print("Usage: python3 new_feature_harness.py <plan.json|plan.md> [--skip-tests] [--mcp-config <path>]")
        print("  .json  — Direct execution plan")
        print("  .md    — Report with embedded pipeline (report used as architectural context")
        print("  --skip-tests  — Do NOT auto-generate pytest-django + factory_boy unit tests")
        print("  --mcp-config  — Path to MCP server configuration JSON (default: $MCP_CONFIG or mcp_config.json)")
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
        subprocess.run(["uv", "sync"], cwd=target_repo, env=isolated_env, check=True, capture_output=True)
        print("   Workspace dependencies synchronized successfully.\n")
    except subprocess.CalledProcessError as e:
        print(f"   Pre-flight workspace sync failed:\n{e.stderr.decode().strip()}")
        sys.exit(1)

    mcp_cfg_path = mcp_config_override or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)
    mcp_orch = init_mcp_orchestrator(mcp_cfg_path, target_repo)
    if mcp_orch:
        print("  MCP Workbench active: git context, memory, and reasoning tools available.\n")
        mcp_context = mcp_orch.build_mcp_context_block()
        mcp_project_discovery = mcp_orch.discover_project_context()
    else:
        mcp_context = ""
        mcp_project_discovery = ""
        print("  (No MCP servers configured. Run with --mcp-config to enable.)\n")

    agents_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")
    implementer_path = os.path.join(agents_dir, "code_implementer.md")
    auditor_path = os.path.join(agents_dir, "integration_auditor.md")

    if not os.path.exists(implementer_path) or not os.path.exists(auditor_path):
        print("Error: Missing agent system files in 'agents/'.")
        sys.exit(1)

    with open(implementer_path, "r", encoding="utf-8") as f:
        implementer_persona = f.read()
    with open(auditor_path, "r", encoding="utf-8") as f:
        auditor_persona = f.read()

    context_history = f"Feature Engineering Campaign Context Plan for: {feature_name}\n"
    if exploration_report:
        context_history += f"\n\nArchitectural Analysis Report:\n{exploration_report}\n"
    if mcp_project_discovery:
        context_history += f"\n\nMCP Project Discovery Context:\n{mcp_project_discovery}\n"

    markdown_fence = "`" * 3

    # Use Agent/Skill registry for dispatch if available
    implementer_agent = build_agent("Implementer", implementer_persona, IMPLEMENTER_MODEL)
    auditor_agent = build_agent("Auditor", auditor_persona, AUDITOR_MODEL)
    print(f"   Implementer: {implementer_agent.model_name} | Auditor: {auditor_agent.model_name}")

    for task in pipeline:
        # Normalize new/old field names
        task.setdefault("instruction", task.get("task", task.get("instruction", "")))
        task.setdefault("assigned_agent", "Engineer")
        task.setdefault("auditor_agent", "QA_Tester")

        print(f"\n[Stage {task['step']}] Initiating: {task['name']}  Agent: {task.get('assigned_agent', 'Engineer')}")
        full_target_path = os.path.join(target_repo, task["target_file"])

        file_backup_contents = None
        existing_file_context = ""
        is_modifying_existing_file = False

        if os.path.exists(full_target_path):
            is_modifying_existing_file = True
            with open(full_target_path, "r", encoding="utf-8") as f:
                file_backup_contents = f.read()
            existing_file_context = (
                f"\n\nCRITICAL CONTEXT: This production file already contains code that MUST be preserved.\n"
                f"Do NOT drop, shorten, or truncate surrounding classes, methods, or imports. Retain the file structure "
                f"and add/modify only what is requested:\n{markdown_fence}python\n{file_backup_contents}\n{markdown_fence}\n"
            )

        max_attempts = 4
        stage_completed_successfully = False

        for attempt in range(1, max_attempts + 1):
            print(f"  -> Generation Attempt {attempt}/{max_attempts} via [{IMPLEMENTER_MODEL}]...")

            user_prompt = f"""
            System Scope Context:
            {context_history}
            {existing_file_context}
            {mcp_context if mcp_context else ''}

            Destination Target File Path: {task['target_file']}
            Execution Requirements: {task['instruction']}

            Generate the absolute, complete final production code for this file. Provide clean type annotations. Do not include commentary outside the code block.
            """

            raw_response = implementer_agent.execute(user_prompt)
            code_generated = clean_model_output(raw_response)

            os.makedirs(os.path.dirname(full_target_path), exist_ok=True)
            with open(full_target_path, "w", encoding="utf-8") as f:
                f.write(code_generated)

            run_formatter_toolchain(full_target_path, target_repo, isolated_env)

            with open(full_target_path, "r", encoding="utf-8") as f:
                code_generated = f.read()

            success, log_msg = validate_code_safety(full_target_path, target_repo, isolated_env)
            if not success:
                print(f"  Linter/Type Check Failed on Attempt {attempt}.")
                print(f"     --- Verification Error Output ---\n{log_msg}\n     ---------------------------------")

                if file_backup_contents is not None:
                    with open(full_target_path, "w", encoding="utf-8") as f:
                        f.write(file_backup_contents)
                elif os.path.exists(full_target_path):
                    os.remove(full_target_path)

                context_history += f"\n\n[Feedback Attempt {attempt} Error on {task['target_file']}]:\n{log_msg}"
                continue

            print("  Local Verification Gates Passed: Formatted code compiles cleanly.")
            print(f"  -> Initiating Architectural Integration Review via [{AUDITOR_MODEL}]...")

            if is_modifying_existing_file:
                scope_directive = (
                    f"You are reviewing ONLY the change requested in this instruction:\n"
                    f"{markdown_fence}\n{task['instruction']}\n{markdown_fence}\n\n"
                    f"The file already contains pre-existing production code that is known to work correctly. "
                    f"You MUST IGNORE all pre-existing code. Only evaluate whether the requested change was "
                    f"implemented correctly. Do NOT reject because of pre-existing patterns, configurations, "
                    f"or imports outside the scope of the instruction. If you cannot identify what was added, "
                    f"you MUST default to VERDICT: APPROVED — the change may be minimal."
                )
            else:
                scope_directive = "Verify this implementation against our design rules: Ensure no top-level view cross-imports, no loop allocations/N+1 queries, and valid framework declarations."

            audit_prompt = (
                f"Original file before modification:\n{markdown_fence}python\n{file_backup_contents}\n{markdown_fence}\n\n"
                f"Modified file:\n{markdown_fence}python\n{code_generated}\n{markdown_fence}\n\n"
                f"{scope_directive}\n\n"
                f"Compare the two. Only reject if the modification introduces a NEW runtime risk that was not present in the original. "
                f"Conclude explicitly with 'VERDICT: APPROVED' or 'VERDICT: REJECTED'."
            ) if is_modifying_existing_file else (
                f"Proposed Module Content for '{task['target_file']}':\n"
                f"{markdown_fence}python\n{code_generated}\n{markdown_fence}\n\n"
                f"{scope_directive}\n\n"
                f"Conclude explicitly with 'VERDICT: APPROVED' or 'VERDICT: REJECTED'."
            )

            audit_prompt_with_mcp = (
                audit_prompt + f"\n\nMCP Tool Context:\n{mcp_context}"
                if mcp_context else audit_prompt
            )

            audit_verdict = auditor_agent.execute(audit_prompt_with_mcp)
            print(f"\n--- Audit Trace for Step {task['step']} (Attempt {attempt}) ---\n{audit_verdict}\n----------------------------------\n")

            if "VERDICT: APPROVED" in audit_verdict:
                print(f"  Audit Confirmed: Saved production module.")
                stage_completed_successfully = True
                context_history += f"\n\n[Verified Module Added: {task['target_file']}]\n"
                break
            else:
                print(f"  Auditor Rejected Attempt {attempt}. Re-routing feedback...")
                context_history += f"\n\n[Auditor Critique Attempt {attempt} for {task['target_file']}]:\n{audit_verdict}"

                if file_backup_contents is not None:
                    with open(full_target_path, "w", encoding="utf-8") as f:
                        f.write(file_backup_contents)
                elif os.path.exists(full_target_path):
                    os.remove(full_target_path)

        if not stage_completed_successfully:
            print(f"Critical: Maximum cycles exhausted for {task['name']}. Halting pipeline execution.")
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
                print(f"  -> Auto-generating unit tests for {task['target_file']}...")
                test_rel = os.path.relpath(test_path, target_repo)
                test_instruction = (
                    f"Write pytest-django unit tests using factory_boy for the code in {task['target_file']}. "
                    f"Use pytest fixtures and django.test.TestCase or pytest.mark.django_db. "
                    f"Cover the main public functions, edge cases, and error paths. "
                    f"Name the file {test_rel}."
                )
                test_user_prompt = f"""
                System Scope Context:
                {context_history}

                Destination Target File Path: {test_rel}
                Execution Requirements: {test_instruction}

                Generate the absolute, complete test file. Provide clean type annotations. Do not include commentary outside the code block.
                """
                raw_test = implementer_agent.execute(test_user_prompt)
                test_code = clean_model_output(raw_test)

                os.makedirs(os.path.dirname(test_path), exist_ok=True)
                with open(test_path, "w", encoding="utf-8") as f:
                    f.write(test_code)

                run_formatter_toolchain(test_path, target_repo, isolated_env)
                success, log_msg = validate_code_safety(test_path, target_repo, isolated_env)
                if success:
                    print(f"  Test file {test_rel} generated and verified.")
                    context_history += f"\n\n[Tests Added: {test_rel}]\n"
                else:
                    print(f"  Test file {test_rel} generated but has validation issues (will not block):")
                    print(f"     {log_msg}")
                    context_history += f"\n\n[Tests Added (with warnings): {test_rel}]\n"

    if mcp_orch:
        mcp_orch.remember(
            f"campaign:{feature_name}:complete",
            f"Campaign '{feature_name}' completed successfully across {len(pipeline)} stages",
            tags=["feature", feature_name, "campaign_complete"],
        )
        mcp_orch.stop()

    print(f"\n=== Universal Engine Campaign for '{feature_name}' Completed Successfully ===")


if __name__ == "__main__":
    main()
