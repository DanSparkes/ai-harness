import argparse
import os
import sys
import time

import requests

from core import harness
from core.config import get_config
from core.forge_proxy import ForgeProxy
from core.parser import DjangoTopographer


def _abort_on_conn_error(e: Exception) -> None:
    """Surface an actionable message for LLM connection failures, then exit.

    Mirrors run_security_evaluation.py: an OOM'd Ollama model or a downed
    server would otherwise propagate a raw ConnectionError traceback.
    """
    print(
        f"\n❌ LLM request failed ({type(e).__name__}). "
        "If local, Ollama may be down or the model OOM'd during generation. "
        "Check `ollama ps` / GPU memory and retry."
    )
    sys.exit(1)


MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.json")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Architecture Review Evaluation Engine"
    )
    parser.add_argument(
        "--repo",
        "-r",
        default=None,
        help="Path to the target repository (overrides TARGET_REPO env var)",
    )
    parser.add_argument(
        "--project-context",
        "-c",
        default=None,
        help="Path to a project-specific context file (markdown) with domain knowledge",
    )
    parser.add_argument(
        "--mcp-config",
        "-m",
        default=None,
        help="Path to MCP server config file (overrides MCP_CONFIG env var)",
    )
    return parser.parse_args()


def main():
    args = parse_arguments()
    cfg = get_config()

    target_repo = harness.resolve_target_repo(args.repo)
    mcp_config_path = args.mcp_config or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)

    if cfg.is_cloud and not cfg.api_key:
        print("Error: USE_GEMINI=true requires GEMINI_API_KEY to be set.")
        print("Please run: export GEMINI_API_KEY='your_key_here'")
        return

    if not target_repo:
        print("Error: No target repository specified.")
        print("Set TARGET_REPO env var or pass --repo /path/to/project")
        return

    harness.banner(
        "Launching Architecture Review Engine (Hybrid Mode)",
        cfg,
        target_project=target_repo,
        local_judge=cfg.resolve_judge(),
    )

    start_time = time.time()

    # 1. Parse Project Topography
    print("Step 1: Parsing project topography...")
    step_start = time.time()
    topographer = DjangoTopographer(target_repo)
    project_map = topographer.scan_project()
    print(f"   [Done] Topography scan in {time.time() - step_start:.2f}s")

    if not project_map.get("models"):
        print("   Warning: No Django models detected. Topography may be incomplete.")

    # 2. Load Architecture Review Persona
    print("Step 2: Loading architecture review persona...")
    persona_path = "agents/architecture_review.md"
    if not os.path.exists(persona_path):
        print(f"Error: System prompt missing at {persona_path}")
        return
    with open(persona_path, encoding="utf-8") as f:
        system_agent_prompt = f.read()
    print("   [Done] Persona loaded")

    with harness.mcp_context(mcp_config_path, target_repo) as orch:
        mcp_block = (
            orch.build_mcp_context_block(tags=["architectural_rule"]) if orch else ""
        )
        if orch:
            print("   [Done] MCP workbench active (tools + git + memory)\n")
        else:
            print(
                "   [Skipped] No MCP config found. "
                "Use MCP_CONFIG env var or mcp_config.json\n"
            )

        # 3. Build shared context (injected into Pass 1 only; threaded onward)
        import json

        project_map_json = json.dumps(project_map, default=str, separators=(",", ":"))

        parser_limitations = f"""### Parser Capabilities & Limitations

The topography is built by static AST parsing. Here's what it CAN and CANNOT resolve:

**CAN resolve:**
- Model fields (name, type, null, default, unique, blank, primary_key, editable)
- Serializer fields (name, type, required, read_only, allow_null, allow_blank)
- Serializer Meta (model, fields, exclude, read_only_fields) — including inherited expressions like `Parent.Meta.fields + [...]`
- View class attributes: `permission_classes`, `authentication_classes`, `serializer_class`, `queryset`, `lookup_field`
- View base classes (e.g., `RetrieveAPIView`, `APIView`, `ModelViewSet`)
- View HTTP methods (derived from non-stub method names)
- View read-only status (`is_read_only: true`)
- Custom permission class resolution with `has_permission` / `has_object_permission` analysis
- Inline authorization calls found in method bodies
- Celery task definitions

**CANNOT resolve:**
- Method bodies beyond stub detection and auth call scanning (no control flow, validation logic, or query filter details)
- URL patterns or route configurations
- ViewSet action-to-HTTP-method mapping beyond function names
- Decorators — parsed as class attributes instead
- Business logic, data flows, or runtime state

### Codebase Inventory
Based on parsing, this project contains:
- {len(project_map.get('models', []))} models
- {len(project_map.get('serializers', []))} serializers
- {len(project_map.get('views', []))} views

### Anti-Hallucination Rules
1. **NEVER attribute a field to a model unless it appears in that model's `fields` list**.
2. **`get_queryset` method != `queryset` attribute**: A view may define `get_queryset()` in its methods but have no `queryset` in its `class_attributes`.
3. **Each view/serializer/model is independent**: Every entry has its own isolated attributes. Do not mix data between entries.
4. **Only reference files and classes that appear in the topography map**. Do not invent imports, dependencies, or third-party integrations not visible in the parsed structure.
5. **Large files alone are insufficient evidence** for maintainability concerns — check the actual module structure.
6. **Do not infer database indexes, missing constraints, or workflow complexity** from model field definitions alone.

### MCP-Augmented Context (Live Project State)
{mcp_block}"""

        shared_context = (
            f"{parser_limitations}\n\n## Project Topography\n```json\n"
            f"{project_map_json}\n```"
        )

        # Passes reference the topography provided above (in shared_context /
        # Pass 1), which the runner threads into subsequent passes via history.
        pass1 = """[Pass 1: Repository Observation]
Analyze the Django repository topography provided above.

Your task is ONLY to identify observations.

For each observation:
- describe what exists,
- identify the relevant files,
- explain why it may matter operationally,
- assign a confidence score (High / Medium / Low).

Rules:
- Do NOT propose solutions.
- Do NOT infer missing structures.
- Do NOT speculate.
- Do NOT introduce architectural patterns.

Output format:

Observation:
Evidence:
Operational Significance:
Confidence:"""

        pass2 = """[Pass 2: Evidence Validation]
Review all observations from Pass 1.

Categorize each observation as:
- Confirmed
- Plausible
- Speculative

Cross-check each observation against the actual topography provided above:
- **Model field exists?** Confirm every referenced field appears in the specific model's `fields` list.
- **View exists?** Confirm every referenced class appears in the `views` list with its `absolute_path`.
- **Serializer exists?** Confirm every referenced serializer appears in the `serializers` list.
- **Task exists?** Only reference Celery tasks listed in the `celery_tasks` section.

Definitions:
Confirmed: supported directly by repository evidence.
Plausible: partially supported but requires additional inspection.
Speculative: insufficient evidence.

Rules:
- Discard speculative findings.
- Preserve only confirmed findings.
- Do NOT recommend fixes.

Output format:

Finding:
Category:
Evidence:
Reasoning Chain:
Likely Impact:"""

        pass3 = """[Pass 3: Staff Prioritization]
Assume you are the Staff Engineer responsible for this system.

Constraints:
- Two engineers.
- One quarter.
- Existing feature commitments remain unchanged.

Using ONLY confirmed findings:
Select EXACTLY five initiatives.

Rank them by:
1. Operational impact,
2. Engineering effort,
3. Developer productivity impact,
4. Incident prevention potential.

For each initiative provide:
- Why it was selected,
- Why alternatives were deferred,
- Estimated implementation effort."""

        pass4 = """[Pass 4: Executive Reporting]
Generate the final report.

Requirements:
- Separate evidence from interpretation.
- Introduce NO new findings.
- Preserve prioritization rationale.
- Explicitly identify assumptions.

Avoid recommending:
- service layers,
- DTO layers,
- command buses,
- app decomposition,

unless repository evidence demonstrates that the current approach is failing.

Focus on pragmatic Django evolution."""

        # 4. Execute multi-pass architecture review
        print(f"Step 3: Processing architecture review via [{cfg.reasoning_model}]...")
        pass_start = time.time()

        try:
            draft_report, model_used, _hist = harness.run_multipass(
                system_prompt=system_agent_prompt,
                passes=[pass1, pass2, pass3, pass4],
                shared_context=shared_context,
                role="reasoning",
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            _abort_on_conn_error(e)
        print(
            f"   [Done] Architecture review via {model_used} "
            f"in {time.time() - pass_start:.2f}s"
        )

        # 5. Adversarial review (separate model family)
        print(f"Step 4: Running adversarial review via [{cfg.heavy_reviewer}]...")
        adv_start = time.time()

        adversarial_prompt = f"""
Act as a skeptical Staff Django Engineer.

Review this report.

Your job is NOT to improve it.

Your job is to identify:
- unsupported claims,
- over-engineering,
- recommendations lacking evidence,
- Django anti-patterns introduced by the reviewer.

For each criticism provide:
- Severity,
- Confidence,
- Supporting rationale.

{parser_limitations}

Report:

{draft_report}
"""
        adversary = harness.build_agent(
            "Adversary",
            system_prompt="",
            model_override=cfg.heavy_reviewer,
            num_ctx=32768,
        )
        try:
            critique = adversary.execute(adversarial_prompt)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            _abort_on_conn_error(e)
        print(
            f"   [Done] Adversarial review completed in {time.time() - adv_start:.2f}s"
        )

        # 6. Revision pass (architect revises)
        print("Step 5: Final revision pass...")
        rev_start = time.time()

        revision_prompt = f"""
Revise the report using the critique below.

Critique:
{critique}

Rules:
- Remove unsupported findings.
- Reduce unnecessary complexity.
- Preserve evidence-backed recommendations.
- Preserve prioritization rationale.
- Explicitly state uncertainty.

Return the revised report only.

Original Report:
{draft_report}
"""
        architect = harness.build_agent("Systems_Architect", system_agent_prompt)
        try:
            final_report = architect.execute(revision_prompt)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            _abort_on_conn_error(e)
        print(f"   [Done] Revision completed in {time.time() - rev_start:.2f}s")

        # 7. Evaluate final report quality + archive
        print(f"Step 6: Evaluating final report via Judge [{cfg.resolve_judge()}]...")
        judge_start = time.time()
        judge_context = f"Project Topography:\n{project_map_json[:8000]}"
        scores = harness.grade_and_archive(
            output=final_report,
            rubric_path="rubrics/architecture_rubric.json",
            agent_role="Staff Architecture Review",
            model_used=model_used,
            config=cfg,
            judge_context=judge_context,
            report_path="reports/staff_architecture_review.md",
        )
        print(f"   [Done] Judging in {time.time() - judge_start:.2f}s")
        print(f"Architecture Review Reliability Scores: {scores}")

        if orch:
            orch.remember(
                "eval:architecture_review:complete",
                "Architecture review completed. Report: reports/staff_architecture_review.md",
                tags=["evaluation", "architecture", "complete"],
            )

    total_duration = time.time() - start_time
    print("\nReport saved to: reports/staff_architecture_review.md")
    print(f"Total Time: {total_duration:.2f}s  Model: {model_used}")


if __name__ == "__main__":
    with ForgeProxy(get_config()):
        main()
