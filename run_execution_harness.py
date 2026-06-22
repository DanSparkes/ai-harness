#!/usr/bin/env python3
"""
run_execution_harness.py (Agent/Skill Architecture)
Reads a pipeline JSON, dispatches each step to the assigned Agent
with its allowed Skills, and iterates on failures.
"""

import os
import sys
import json
import time
import subprocess

from core.agent import AgentRegistry, build_default_registry, build_dependency_graph, skill_get_affected_files

TARGET_REPO       = "/Users/dansparkes/memores/memores-api"

USE_GEMINI = os.getenv("USE_GEMINI", "").lower() in ("1", "true", "yes")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


def get_isolated_env() -> dict[str, str]:
    clean_env = os.environ.copy()
    clean_env.pop("VIRTUAL_ENV", None)
    clean_env.pop("PYTHONPATH", None)
    return clean_env


def clean_model_output(raw_output: str) -> str:
    clean_lines = []
    for line in raw_output.splitlines():
        if line.strip().startswith("```"):
            continue
        clean_lines.append(line)
    return "\n".join(clean_lines).strip()


def load_pipeline(path: str) -> dict:
    if path.endswith(".md"):
        import re
        with open(path) as f:
            text = f.read()
        blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
        for block in blocks:
            try:
                return json.loads(block.strip())
            except json.JSONDecodeError:
                continue
        raise ValueError("No valid JSON pipeline block found in .md file")
    with open(path) as f:
        return json.load(f)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 run_execution_harness.py <plan.json|plan.md>")
        sys.exit(1)

    plan_path = sys.argv[1]
    print(f"=== Loading plan: {plan_path} ===")
    plan = load_pipeline(plan_path)

    target_workspace = plan.get("target_workspace", TARGET_REPO)
    pipeline = plan.get("pipeline", [])
    if not pipeline:
        print("❌ No pipeline steps found in plan.")
        sys.exit(1)

    print(f"Target Workspace: {target_workspace}")
    print(f"Pipeline Steps: {len(pipeline)}")

    # Build registry with all agents + skills
    registry = build_default_registry(target_workspace, GEMINI_API_KEY if USE_GEMINI else None)
    print(f"   Agents loaded: {registry.list_agents()}")
    print(f"   Skills loaded: {registry.list_skills()}")

    isolated_env = get_isolated_env()

    # Pre-flight sync
    print("\n🔄 Pre-flight: Synchronizing dependencies...")
    try:
        subprocess.run(["uv", "sync"], cwd=target_workspace, env=isolated_env, check=True, capture_output=True)
        print("   ✅ Dependencies synchronized.\n")
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Sync failed:\n{e.stderr.decode().strip()}")
        sys.exit(1)

    exploration_report = ""
    if os.path.exists("reports/access_gates_architectural_exploration.md"):
        with open("reports/access_gates_architectural_exploration.md") as f:
            exploration_report = f.read()

    # Build dependency graph once for cascading-breakage detection
    print("\n🔍 Building dependency graph...")
    dep_graph = build_dependency_graph(target_workspace)
    print(f"   {len(dep_graph)} modules tracked for cascading breakage.")

    for step in pipeline:
        step_num = step.get("step", pipeline.index(step) + 1)
        task_desc = step.get("task", step.get("name", step.get("instruction", "(unnamed step)")))
        agent_name = step.get("assigned_agent", "Engineer")
        skills_override = step.get("allowed_skills", None)

        print(f"\n[Stage {step_num}] {task_desc}")
        print(f"   Assigned Agent: {agent_name}")

        full_target_path = None
        target_file = step.get("target_file")
        if target_file:
            full_target_path = os.path.join(target_workspace, target_file)

        file_backup_contents = None
        existing_file_context = ""
        is_modifying = False

        if full_target_path and os.path.exists(full_target_path):
            is_modifying = True
            with open(full_target_path) as f:
                file_backup_contents = f.read()
            existing_file_context = (
                f"\n\nCRITICAL CONTEXT: This file already contains existing code.\n"
                f"Do NOT remove other classes, methods, or imports. Modify/extend only what is requested:\n"
                f"```python\n{file_backup_contents}\n```\n"
            )

            # Check for downstream dependents (cascading breakage detection)
            affected = skill_get_affected_files(target_file, graph=dep_graph)
            if affected and not affected.startswith("(no"):
                existing_file_context += f"\n\nDOWNSTREAM DEPENDENTS (files that import this module):\n{affected}\n"
                existing_file_context += "IMPORTANT: Ensure your changes don't break these files.\n"

        max_attempts = step.get("max_attempts", step.get("retry", 4))
        stage_completed = False

        for attempt in range(1, max_attempts + 1):
            print(f"   -> Attempt {attempt}/{max_attempts} via [{agent_name}]...")

            user_prompt = f"""
System Exploratory Analysis Context:
{exploration_report}
{existing_file_context}

Destination Target File Path: {target_file or '(not specified)'}
Execution Requirements: {task_desc}

Generate the absolute, complete final production code for this file. Preserve all existing code segments if provided. Use clean type annotations. Do not include commentary outside the code block.
"""

            agent = registry.get_agent(agent_name)
            t0 = time.time()
            raw_response = agent.execute(user_prompt, registry._skills, stream=True)
            elapsed = time.time() - t0
            print(f"   -> Generated in {elapsed:.1f}s")

            # Check for syntax error caught during streaming
            if raw_response.startswith("__SYNTAX_ERROR__:"):
                _, err, partial = raw_response.split(":", 2)
                print(f"   ❌ Streaming Syntax Error: {err[:200]}")
                if file_backup_contents:
                    with open(full_target_path, "w") as f:
                        f.write(file_backup_contents)
                exploration_report += f"\n\n[Attempt {attempt} Syntax Error for {target_file}]:\n{err}"
                continue

            code_generated = clean_model_output(raw_response)

            # If a target file is specified, write it
            if full_target_path:
                os.makedirs(os.path.dirname(full_target_path), exist_ok=True)
                with open(full_target_path, "w") as f:
                    f.write(code_generated)

                # Run local verification skills if available
                if skills_override and any(s in skills_override for s in ["run_formatter", "validate_syntax", "run_mypy"]):
                    if "run_formatter" in (skills_override or []):
                        registry.get_skill("run_formatter").fn(full_target_path)
                        with open(full_target_path) as f:
                            code_generated = f.read()

                    if "validate_syntax" in (skills_override or []):
                        syntax_result = registry.get_skill("validate_syntax").fn(full_target_path)
                        if "Error" in syntax_result:
                            print(f"   ❌ Syntax Error: {syntax_result}")
                            if file_backup_contents:
                                with open(full_target_path, "w") as f:
                                    f.write(file_backup_contents)
                            exploration_report += f"\n\n[Attempt {attempt} Syntax Error for {target_file}]:\n{syntax_result}"
                            continue

                    if "run_mypy" in (skills_override or []):
                        mypy_result = registry.get_skill("run_mypy").fn(full_target_path)
                        if "mypy OK" not in mypy_result:
                            print(f"   ❌ Mypy Violation:\n{mypy_result}")
                            if file_backup_contents:
                                with open(full_target_path, "w") as f:
                                    f.write(file_backup_contents)
                            exploration_report += f"\n\n[Attempt {attempt} Mypy Error for {target_file}]:\n{mypy_result}"
                            continue

                print("   ✅ Local verification passed.")

            # Audit step for existing-file modifications
            if is_modifying:
                auditor_name = step.get("auditor_agent", "QA_Tester")
                print(f"   -> Audit via [{auditor_name}]...")

                scope_directive = (
                    "CRITICAL: This is an active production file update. Focus your zero-tolerance checks "
                    "EXCLUSIVELY on newly added code. Do NOT reject due to pre-existing legacy patterns."
                )

                audit_prompt = f"""
Proposed Module Content for '{target_file}':
```python
{code_generated}
```

{scope_directive}

Output your analysis and conclude with 'VERDICT: APPROVED' or 'VERDICT: REJECTED'.
"""
                auditor = registry.get_agent(auditor_name)
                t0 = time.time()
                audit_verdict = auditor.execute(audit_prompt)
                print(f"   -> Audit completed in {time.time() - t0:.1f}s")
                print(f"   --- Audit ---\n{audit_verdict}\n   ------------")

                if "VERDICT: APPROVED" in audit_verdict:
                    print(f"   🎉 Audit approved.")
                    stage_completed = True
                    break
                else:
                    print(f"   🛑 Auditor rejected. Feeding critique back to implementer...")
                    exploration_report += f"\n\n[Auditor Rejection Attempt {attempt} for {target_file}]:\n{audit_verdict}"
                    if file_backup_contents:
                        with open(full_target_path, "w") as f:
                            f.write(file_backup_contents)
            else:
                stage_completed = True
                break

        if not stage_completed:
            print(f"❌ Stage {step_num} failed after {max_attempts} attempts. Halting.")
            sys.exit(1)

        print(f"   ✅ Stage {step_num} complete.")

    print("\n=== Pipeline Complete ===")


if __name__ == "__main__":
    main()
