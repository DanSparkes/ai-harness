import os
import json
import time
from core.parser import DjangoTopographer
from core.agent import Agent
from core.judge import AutomatedEvaluator
from core.warehouse import HarnessWarehouse

USE_GEMINI = os.getenv("USE_GEMINI", "").lower() in ("1", "true", "yes")

CLOUD_MODEL           = "gemini-2.5-flash"
LOCAL_MODEL           = "qwen3.6:latest"

REASONING_ARCHITECT   = CLOUD_MODEL if USE_GEMINI else LOCAL_MODEL
ARCHITECT_API_BASE    = "https://generativelanguage.googleapis.com/v1beta/openai" if USE_GEMINI else "http://localhost:11434"
ARCHITECT_API_KEY     = os.getenv("GEMINI_API_KEY") if USE_GEMINI else None

FALLBACK_REVIEWER     = "gemini-2.5-flash"
HEAVY_REVIEWER        = "deepseek-r1:14b"

TARGET_DJANGO_PROJECT = "/Users/dansparkes/memores/memores-api"
MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.json")

_mcp_orch = None


def init_mcp():
    global _mcp_orch
    if _mcp_orch is not None:
        return _mcp_orch
    if not os.path.exists(MCP_CONFIG_PATH):
        return None
    from core.mcp_orchestrator import MCPOrchestrator
    orch = MCPOrchestrator(MCP_CONFIG_PATH, target_repo=TARGET_DJANGO_PROJECT)
    started = orch.start()
    if started:
        _mcp_orch = orch
        try:
            orch.call_tool("git", "git_set_repo", {"path": TARGET_DJANGO_PROJECT})
        except Exception:
            pass
        return orch
    return None


def build_mcp_context_block() -> str:
    orch = _mcp_orch
    if not orch:
        return ""
    parts = []
    try:
        status = orch.git_status()
        if status and status != "(no output)":
            parts.append(f"=== Working Tree ===\n{status}")
    except Exception:
        pass
    try:
        recent = orch.git_log(max_count=10)
        if recent and not recent.startswith("("):
            parts.append(f"=== Recent Commits ===\n{recent}")
    except Exception:
        pass
    try:
        memory = orch.recall(tags=["architectural_rule"])
        if memory and memory != "(no memories)":
            parts.append(f"=== Architectural Rules ===\n{memory}")
    except Exception:
        pass
    return "\n\n".join(parts)

def main():
    is_local_mode = not ARCHITECT_API_KEY
    if not is_local_mode and not ARCHITECT_API_KEY:
        print("Error: USE_GEMINI=true requires GEMINI_API_KEY to be set.")
        print("Please run: export GEMINI_API_KEY='your_key_here'")
        return

    print(f"{'='*60}")
    print(f"Launching Architecture Review Engine (Hybrid Mode)")
    print(f"Target Project   : {TARGET_DJANGO_PROJECT}")
    print(f"Cloud Architect  : {REASONING_ARCHITECT}")
    print(f"Local Judge      : {HEAVY_REVIEWER}")
    print(f"{'='*60}\n")

    start_time = time.time()

    # 1. Parse Project Topography
    print("Step 1: Parsing project topography...")
    step_start = time.time()
    topographer = DjangoTopographer(TARGET_DJANGO_PROJECT)
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
    with open(persona_path, "r", encoding="utf-8") as f:
        system_agent_prompt = f.read()
    print(f"   [Done] Persona loaded")

    # 2b. Initialize MCP workbench for richer context
    print("Step 2b: Initializing MCP workbench...")
    orch = init_mcp()
    mcp_block = build_mcp_context_block() if orch else ""
    if orch:
        print("   [Done] MCP workbench active (git context + memory recall)\n")
    else:
        print("   [Skipped] No MCP config found. Use MCP_CONFIG env var or mcp_config.json\n")

    # 3. Build prompt context
    project_map_json = json.dumps(project_map, indent=2, default=str)

    PARSER_LIMITATIONS = f"""### Parser Capabilities & Limitations

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

    # Single-pass fallback: used for local-only mode and cloud API failures
    fallback_prompt = f"""Below is the full project topography map.

{PARSER_LIMITATIONS}

## Project Topography
```json
{project_map_json}
```

## Instructions
Conduct a Staff-level architecture review. Follow the system prompt's core focus areas and operational rules.

### Mandatory Rules
1. **CITE FILE PATHS** — Every observation must reference exact files and classes from the topography.
2. **NO FABRICATED EXAMPLES** — Never reference components not present in the topography.
3. **CONFIDENCE LEVELS** — Tag every finding as Confirmed / Plausible / Speculative. Only Confirmed findings may appear in final recommendations.
4. **PRAGMATIC DJANGO** — Favor incremental, Django-native solutions. Avoid introducing service layers, DTOs, or app decomposition unless evidence shows current approach is failing.
5. **EVIDENCE VS INTERPRETATION** — Separate what the code says from what you infer.

### Required Report Structure
1. Executive Summary
2. Top 5 Prioritized Improvements
3. Deferred Opportunities
4. Concrete Implementation Suggestions"""

    # Build passes
    pass_templates = [
        f"""[Pass 1: Repository Observation]
Analyze this Django repository topography:

{PARSER_LIMITATIONS}

## Project Topography
```json
{project_map_json}
```

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
Confidence:""",

        f"""[Pass 2: Evidence Validation]
Review all observations from Pass 1.

{PARSER_LIMITATIONS}

Categorize each observation as:
- Confirmed
- Plausible
- Speculative

Cross-check each observation against the actual topography:
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
Likely Impact:""",

        """[Pass 3: Staff Prioritization]
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
- Estimated implementation effort.""",

        """[Pass 4: Executive Reporting]
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

Focus on pragmatic Django evolution.""",
    ]

    passes = pass_templates

    # 4. Execute multi-pass architecture review via Agent
    print(f"Step 3: Processing architecture review via [{REASONING_ARCHITECT}]...")
    pass_start = time.time()

    architect = Agent(
        name="Systems_Architect",
        system_prompt=system_agent_prompt,
        model_name=REASONING_ARCHITECT,
        base_url=ARCHITECT_API_BASE,
        api_key=ARCHITECT_API_KEY,
        num_ctx=81920,
    )

    context = ""
    for i, pass_prompt in enumerate(passes):
        combined = f"{context}\n\n{pass_prompt}" if context else pass_prompt
        t0 = time.time()
        output = architect.execute(combined)
        print(f"   [Done] Pass {i+1}/{len(passes)} in {time.time() - t0:.1f}s")
        context += f"\n\n[Pass {i+1} Output]:\n{output}"

    draft_report = output
    model_used = architect.model_name
    print(f"   [Done] Architecture review via {model_used} in {time.time() - pass_start:.2f}s")

    # 5. Adversarial review
    print(f"Step 4: Running adversarial review via [{HEAVY_REVIEWER}]...")
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

{PARSER_LIMITATIONS}

Report:

{draft_report}
"""

    adversary = Agent(
        name="Adversary",
        system_prompt="",
        model_name=HEAVY_REVIEWER,
        num_ctx=32768,
    )
    critique = adversary.execute(adversarial_prompt)
    print(f"   [Done] Adversarial review completed in {time.time() - adv_start:.2f}s")

    # 6. Revision pass
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

    final_report = architect.execute(revision_prompt)
    print(f"   [Done] Revision completed in {time.time() - rev_start:.2f}s")

    # 7. Evaluate final report quality
    print(f"Step 6: Evaluating final report via Local Judge [{HEAVY_REVIEWER}]...")
    judge_start = time.time()

    judge_context = f"Project Topography:\n{project_map_json[:5000]}"
    evaluator = AutomatedEvaluator(judge_model=HEAVY_REVIEWER)
    scores = evaluator.grade_run(final_report, "rubrics/architecture_rubric.json", context=judge_context)

    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"Architecture Review Reliability Scores: {scores}")

    # 8. Log and Export Artifacts
    print("Step 7: Archiving run data...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=model_used,
        agent_role="Staff Architecture Review",
        raw_output=final_report,
        scores=scores
    )

    report_filename = "reports/staff_architecture_review.md"
    os.makedirs("reports", exist_ok=True)
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(final_report)

    if _mcp_orch:
        _mcp_orch.remember(
            "eval:architecture_review:complete",
            f"Architecture review completed. Report: {report_filename}",
            tags=["evaluation", "architecture", "complete"],
        )
        _mcp_orch.stop()

    total_duration = time.time() - start_time
    print(f"\nReport saved to: {report_filename}")
    print(f"Total Time: {total_duration:.2f}s  Model: {model_used}")

if __name__ == "__main__":
    main()
