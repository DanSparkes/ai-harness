#!/usr/bin/env python3
"""
run_execution_harness.py (Delta Auditing Layout)
Automates a multi-agent generation loop featuring environmental isolation,
in-place formatting, and delta-scoped audits to protect against legacy debt blocks.
"""

import os
import sys
import json
import subprocess
import requests

# Local Core Hardware Matrix
IMPLEMENTER_MODEL = "mlx-community/Qwen3-14B-4bit"
AUDITOR_MODEL     = "mlx-community/Qwen3-32B-4bit"
TARGET_REPO       = "/Users/dansparkes/memores/memores-api"
MLX_LM_URL        = "http://localhost:8080/v1/chat/completions"
MCP_CONFIG_PATH   = os.environ.get("MCP_CONFIG", "mcp_config.json")

_mcp_orch = None


def init_mcp():
    global _mcp_orch
    if _mcp_orch is not None:
        return _mcp_orch
    if not os.path.exists(MCP_CONFIG_PATH):
        return None
    from core.mcp_orchestrator import MCPOrchestrator
    orch = MCPOrchestrator(MCP_CONFIG_PATH, target_repo=TARGET_REPO)
    started = orch.start()
    if started:
        _mcp_orch = orch
        try:
            orch.call_tool("git", "git_set_repo", {"path": TARGET_REPO})
        except Exception:
            pass
        return orch
    return None


def build_mcp_context() -> str:
    orch = _mcp_orch
    if not orch:
        return ""
    parts = []
    try:
        status = orch.git_status()
        if status and status != "(no output)":
            parts.append(f"Working Tree:\n{status}")
    except Exception:
        pass
    try:
        recent = orch.git_log(max_count=10)
        if recent and not recent.startswith("("):
            parts.append(f"Recent Commits:\n{recent}")
    except Exception:
        pass
    try:
        memory = orch.recall(tags=["architectural_rule", "active"])
        if memory and memory != "(no memories)":
            parts.append(f"Memory Context:\n{memory}")
    except Exception:
        pass
    return "\n\n".join(parts)

# Complete End-to-End Task Pipeline with Fine-Tuned System Parameters
EXECUTION_PIPELINE = [
    {
        "step": 1,
        "name": "Access Gate Registry Configuration",
        "target_file": "memores/config/access_gates.py",
        "instruction": "Create a pure data configuration registry dictionary named ACCESS_GATE_REGISTRY mapping frontend feature slugs to Django permission codenames. Include standard fallback messages and fallback_grant_options arrays. Provide explicit type annotations utilizing a TypedDict to satisfy strict mypy settings. Do not import views or models."
    },
    {
        "step": 2,
        "name": "DRF Custom Permission Gatekeeper",
        "target_file": "memores/permissions.py",
        "instruction": "Implement a reusable AccessGatePermission class extending rest_framework.permissions.BasePermission. Read the view's 'required_feature_slug' attribute at runtime. Safeguard request.user access against AnonymousUser type configurations. Call profile.resolve_feature_access(codename) if authenticated. Raise PermissionDenied with a standardized message on failure."
    },
    {
        "step": 3,
        "name": "Dynamic Access Gate Metadata Serializer",
        "target_file": "memores/serializers/access_gates.py",
        "instruction": "Implement DynamicAccessGateSerializer extending serializers.Serializer. Contain a single permissions field mapped via SerializerMethodField. Call user.get_all_permissions() to prime Django's permission cache in RAM before looping over the configuration registry to construct a flat camelCased output map. Ensure NO invalid 'Meta' class block is included."
    },
    {
        "step": 4,
        "name": "User Payload Schema Integration",
        "target_file": "memores/serializers/user_serializers.py",
        "instruction": (
            "Locate the existing UserSerializer class within the provided file context. Do not modify or alter any other sibling classes. "
            "1. Inside the UserSerializer class body, declare: permissions = serializers.SerializerMethodField().\n"
            "2. Inside the UserSerializer Meta.fields tuple, append the 'permissions' string element cleanly.\n"
            "3. Implement the 'get_permissions(self, obj: User) -> dict[str, Any]:' method on the UserSerializer class.\n"
            "4. Inside the get_permissions method body, use a function-local lazy import for DynamicAccessGateSerializer to isolate dependencies.\n"
            "5. Apply a strict guard: 'request = self.context.get(\"request\"); if request is None or not request.user.is_authenticated: return {}'.\n"
            "6. Invoke the serializer directly, passing self.context, and return its results wrapped in an explicit type cast: 'from typing import cast, Any; return cast(dict[str, Any], serializer.get_permissions(obj))'."
        )
    },
    {
        "step": 5,
        "name": "Comprehensive Access Gates Unit Test Suite",
        "target_file": "memores/tests/test_access_gates.py",
        "instruction": (
            "Write a robust suite of Django unit tests targeting the new access gates infrastructure inside 'memores/tests/test_access_gates.py'. "
            "1. NO PRODUCTION VIEW IMPORTS: Declare a local 'class MockView(views.APIView):' inside the test file and assign a 'required_feature_slug = \"reflections_journal\"' class property to it dynamically.\n"
            "2. VALID CONTENT TYPE: Resolve a valid model ContentType from the database via 'from django.contrib.contenttypes.models import ContentType; ct = ContentType.objects.get_for_model(User)'. Never pass content_type=None.\n"
            "3. CORRECT REQUEST MOCKING: Use 'rest_framework.test.APIRequestFactory' to build a mock request object. Attach the user instance directly to it via 'request.user = self.user' before passing it to '.has_permission(request, view)' manually.\n"
            "4. METRIC SCENARIOS: Verify behavior across premium tiers, free tiers with fallback structures, and PermissionDenied enforcement paths."
        )
    }
]

def get_isolated_env() -> dict[str, str]:
    clean_env = os.environ.copy()
    clean_env.pop("VIRTUAL_ENV", None)  
    clean_env.pop("PYTHONPATH", None)   
    return clean_env

def call_mlx_lm(model: str, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "stream": False,
        "temperature": 0.0,
    }
    try:
        response = requests.post(MLX_LM_URL, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"mlx-lm connection error: {e}")
        sys.exit(1)

def run_formatter_toolchain(file_path: str, run_env: dict[str, str]):
    rel_path = os.path.relpath(file_path, TARGET_REPO)
    try:
        subprocess.run(["uv", "run", "isort", "--profile", "black", rel_path], cwd=TARGET_REPO, env=run_env, capture_output=True)
        subprocess.run(["uv", "run", "black", "--target-version", "py312", rel_path], cwd=TARGET_REPO, env=run_env, capture_output=True)
        subprocess.run(["uv", "run", "ruff", "check", "--fix", rel_path], cwd=TARGET_REPO, env=run_env, capture_output=True)
    except Exception:
        pass 

def validate_code_safety(file_path: str, run_env: dict[str, str]) -> tuple[bool, str]:
    rel_path = os.path.relpath(file_path, TARGET_REPO)
    try:
        syntax_res = subprocess.run([sys.executable, "-m", "py_compile", file_path], capture_output=True, text=True)
        if syntax_res.returncode != 0:
            return False, f"Syntax Compilation Failure:\n{syntax_res.stderr.strip()}"
    except Exception as e:
        return False, f"Syntax validator error: {str(e)}"

    try:
        mypy_res = subprocess.run(["uv", "run", "mypy", "--check-untyped-defs", rel_path], cwd=TARGET_REPO, env=run_env, capture_output=True, text=True)
        if mypy_res.returncode != 0:
            error_log = f"STDOUT:\n{mypy_res.stdout.strip()}\nSTDERR:\n{mypy_res.stderr.strip()}"
            return False, f"Mypy Type Guard Violation:\n{error_log.strip()}"
    except Exception as e:
        return False, f"Mypy execution failed to start: {str(e)}"
        
    return True, "Passed local verification standards."

def clean_model_output(raw_output: str) -> str:
    clean_lines = []
    for line in raw_output.splitlines():
        if line.strip().startswith("```"):
            continue
        clean_lines.append(line)
    return "\n".join(clean_lines).strip()

def main():
    print("=== Launching Pre-Commit Aligned Execution Workflow ===")
    print(f"Target System Workspace: {TARGET_REPO}\n")

    isolated_env = get_isolated_env()

    # Pre-Flight Sync
    print("🔄 Pre-Flight: Synchronizing target project virtual environment dependencies...")
    try:
        subprocess.run(["uv", "sync"], cwd=TARGET_REPO, env=isolated_env, check=True, capture_output=True)
        print("   ✅ Workspace dependencies synchronized successfully.\n")
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Pre-flight workspace sync failed:\n{e.stderr.decode().strip()}")
        sys.exit(1)

    orch = init_mcp()
    mcp_block = build_mcp_context() if orch else ""
    if orch:
        print("   MCP Workbench active (git context + memory recall)\n")
    else:
        print("   (No MCP servers configured)\n")

    with open("agents/code_implementer.md", "r", encoding="utf-8") as f:
        implementer_persona = f.read()
    with open("agents/integration_auditor.md", "r", encoding="utf-8") as f:
        auditor_persona = f.read()

    exploration_report = ""
    if os.path.exists("reports/access_gates_architectural_exploration.md"):
        with open("reports/access_gates_architectural_exploration.md", "r", encoding="utf-8") as f:
            exploration_report = f.read()

    for task in EXECUTION_PIPELINE:
        print(f"\n[Stage {task['step']}] Initiating: {task['name']}")
        full_target_path = os.path.join(TARGET_REPO, task["target_file"])
        
        file_backup_contents = None
        existing_file_context = ""
        is_modifying_existing_file = False
        
        if os.path.exists(full_target_path):
            is_modifying_existing_file = True
            with open(full_target_path, "r", encoding="utf-8") as f:
                file_backup_contents = f.read()
            existing_file_context = (
                f"\n\nCRITICAL CONTEXT: This production file already contains existing code that MUST be preserved.\n"
                f"Do NOT remove or shorten any other classes, methods, or imports. Retain the entire content layout "
                f"and modify or extend only what is requested:\n"
                f"```python\n{file_backup_contents}\n```\n"
            )

        code_generated = ""
        max_attempts = 4
        stage_completed_successfully = False
        
        for attempt in range(1, max_attempts + 1):
            print(f"  -> Generation Attempt {attempt}/{max_attempts} via [{IMPLEMENTER_MODEL}]...")
            
            user_prompt = f"""
            System Exploratory Analysis Context:
            {exploration_report}
            {existing_file_context}
            {mcp_block}

            Destination Target File Path: {task['target_file']}
            Execution Requirements: {task['instruction']}

            Generate the absolute, complete final production code for this file. Preserve all existing code segments if provided. Provide clean type annotations. Do not include commentary outside the code block.
            """
            
            raw_response = call_mlx_lm(IMPLEMENTER_MODEL, implementer_persona, user_prompt)
            code_generated = clean_model_output(raw_response)
            
            os.makedirs(os.path.dirname(full_target_path), exist_ok=True)
            with open(full_target_path, "w", encoding="utf-8") as f:
                f.write(code_generated)

            run_formatter_toolchain(full_target_path, isolated_env)
            
            with open(full_target_path, "r", encoding="utf-8") as f:
                code_generated = f.read()

            success, log_msg = validate_code_safety(full_target_path, isolated_env)
            if not success:
                print(f"  ❌ Formatting/Type Check Failed on Attempt {attempt}.")
                print(f"     --- Verification Error Output ---\n{log_msg}\n     ---------------------------------")
                
                if file_backup_contents is not None:
                    with open(full_target_path, "w", encoding="utf-8") as f:
                        f.write(file_backup_contents)
                elif os.path.exists(full_target_path):
                    os.remove(full_target_path)
                    
                exploration_report += f"\n\n[Feedback Loop Attempt {attempt} Error Log for {task['target_file']}]:\n{log_msg}"
                continue

            print("  ✅ Local Verification Gates Passed: Formatted code compiles cleanly.")
            
            print(f"  -> Initiating Architectural Integration Review via [{AUDITOR_MODEL}]...")
            
            # FIXED: Scope the auditor instructions based on whether we are auditing a delta or a fresh module
            if is_modifying_existing_file:
                scope_directive = (
                    "CRITICAL DIRECTION: This is an active production file update. Focus your zero-tolerance checks "
                    "EXCLUSIVELY on the newly added 'permissions' field and 'get_permissions' method logic within the UserSerializer class. "
                    "Do NOT reject this submission because of pre-existing legacy code patterns, missing user hashes, or structural anti-patterns "
                    "found in adjacent legacy classes (such as ForgotPasswordSerializer or CreateUpdateProfileSerializer), as those are part of the baseline codebase debt."
                )
            else:
                scope_directive = "Verify this implementation against our design rules: Ensure no top-level view cross-imports, no loop allocations/N+1 queries, and valid framework declarations."

            audit_prompt = f"""
            Proposed Module Content for '{task['target_file']}':
            ```python
            {code_generated}
            ```

            {scope_directive}

            Live Project Context:
            {mcp_block}
            
            Output your analysis and conclude explicitly with 'VERDICT: APPROVED' or 'VERDICT: REJECTED'.
            """
            
            audit_verdict = call_mlx_lm(AUDITOR_MODEL, auditor_persona, audit_prompt)
            print(f"\n--- Audit Trace for Step {task['step']} (Attempt {attempt}) ---\n{audit_verdict}\n----------------------------------\n")
            
            if "VERDICT: APPROVED" in audit_verdict:
                print(f"  🎉 Audit Confirmed: Safely writing production module to target workspace.")
                stage_completed_successfully = True
                break
            else:
                print(f"  🛑 Auditor Rejected Attempt {attempt}. Feeding critique back to implementer context...")
                exploration_report += f"\n\n[Auditor Failure Feedback Loop Attempt {attempt} for {task['target_file']}]:\n{audit_verdict}"
                
                if file_backup_contents is not None:
                    with open(full_target_path, "w", encoding="utf-8") as f:
                        f.write(file_backup_contents)
                elif os.path.exists(full_target_path): 
                    os.remove(full_target_path)

        if not stage_completed_successfully:
            print(f"Critical: Maximum generation and auditing cycles exhausted for {task['name']}. Halting pipeline execution.")
            sys.exit(1)

        if orch:
            orch.remember(
                f"exec:access_gates:step:{task['step']}",
                f"Implemented {task['name']} in {task['target_file']}",
                tags=["execution", "access_gates", "active"],
            )

    if orch:
        orch.remember(
            "exec:access_gates:complete",
            "All 5 Access Gates pipeline stages completed successfully",
            tags=["execution", "access_gates", "complete"],
        )
        orch.stop()

    print("\n=== All 5 Stages of the Agentic Pipeline Completed Successfully ===")

if __name__ == "__main__":
    main()
