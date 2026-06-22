import os
import json
import time
from core.parser import DjangoTopographer
from core.runner import StatefulHarnessRunner
from core.judge import AutomatedEvaluator
from core.warehouse import HarnessWarehouse

REASONING_ARCHITECT   = "qwen3.6:latest"
ARCHITECT_API_BASE    = "https://generativelanguage.googleapis.com/v1beta/openai" if "gemini" in REASONING_ARCHITECT.lower() else "http://localhost:11434"
ARCHITECT_API_KEY     = os.getenv("GEMINI_API_KEY")

FALLBACK_REVIEWER     = "gemini-2.5-flash"
HEAVY_REVIEWER        = "deepseek-r1:14b"

TARGET_DJANGO_PROJECT = "/Users/dansparkes/memores/memores-api"

def main():
    is_local_mode = "localhost" in ARCHITECT_API_BASE or not ARCHITECT_API_KEY
    if not is_local_mode and not ARCHITECT_API_KEY:
        print("Error: GEMINI_API_KEY environment variable is not set.")
        print("Please run: export GEMINI_API_KEY='your_key_here'")
        return

    print(f"{'='*60}")
    print(f"Launching Staff Onboarding Engine (Hybrid Mode)")
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

    # 2. Load Onboarding Persona
    print("Step 2: Loading staff engineer persona...")
    persona_path = "agents/staff_onboarding.md"
    if not os.path.exists(persona_path):
        print(f"Error: System prompt missing at {persona_path}")
        return
    with open(persona_path, "r", encoding="utf-8") as f:
        system_agent_prompt = f.read()
    print(f"   [Done] Persona loaded")

    # 3. Build prompt context
    project_map_json = json.dumps(project_map, indent=2, default=str)

    PARSER_LIMITATIONS = """### Parser Capabilities & Limitations

The topography is built by static AST parsing. Here's what it CAN and CANNOT resolve:

**CAN resolve:**
- Model fields (name, type, null, default, unique, blank, primary_key, editable)
- Serializer fields (name, type, required, read_only, allow_null, allow_blank)
- Serializer Meta (model, fields, exclude, read_only_fields) — including inherited expressions like `Parent.Meta.fields + [...]`
- View class attributes: `permission_classes`, `authentication_classes`, `serializer_class`, `queryset`, `lookup_field`
- View base classes (e.g., `RetrieveAPIView`, `APIView`, `ModelViewSet`)
- View HTTP methods (derived from non-stub method names: get/post/put/patch/delete)
- View read-only status (`is_read_only: true` if only GET is supported)
- Custom permission class resolution with `has_permission` and `has_object_permission` analysis
- Inline authorization calls: methods list `inline_auth_calls` with function names found in method bodies
- Celery task definitions

**CANNOT resolve:**
- Method bodies beyond stub detection and auth call scanning (no control flow, validation logic, or query filter details)
- URL patterns or route configurations
- ViewSet action-to-HTTP-method mapping beyond function names
- Decorators — parsed as class attributes instead
- Business logic, data flows, or runtime state

### Codebase Inventory
Based on parsing, this project contains:
- {model_count} models
- {serializer_count} serializers
- {view_count} views

### Anti-Hallucination Rules
1. **NEVER attribute a field to a model unless it appears in that model's `fields` list**. Each model entry has its own isolated field list.
2. **`get_queryset` method != `queryset` attribute**: A view may define `get_queryset()` in its methods list but have no `queryset` in its `class_attributes`. These are different DRF patterns.
3. **Each view is independent**: Every view in the topography is a separate entry with its own `class_attributes`, `methods`, and `base_classes`. Do not mix data between views.
4. **Only reference files and classes that appear in the topography map**. Do not invent imports, dependencies, or third-party integrations not visible in the parsed structure."""

    PARSER_LIMITATIONS_FILLED = PARSER_LIMITATIONS.format(
        model_count=len(project_map.get('models', [])),
        serializer_count=len(project_map.get('serializers', [])),
        view_count=len(project_map.get('views', []))
    )

    # Single-pass fallback: used for local-only mode and cloud API failures
    fallback_prompt = f"""Below is the full project topography map including models with their fields, serializers, views, and task definitions.

{PARSER_LIMITATIONS_FILLED}

## Project Topography
```json
{project_map_json}
```

## Instructions
Construct a rigorous, realistic, and highly contextual 90-day onboarding strategy based strictly on the project topography above.

### Mandatory Rules
1. **CITE FILE PATHS** — Every recommendation must reference exact files and classes from the topography.
2. **NO FABRICATED EXAMPLES** — Never reference components, patterns, or dependencies not present in the topography.
3. **CONTEXTUAL ANCHORING** — Avoid generic engineering advice. Recommendations must explicitly target actual files, serializer patterns, views, database structures, or architectural decisions visible in the parsed map.
4. **PRAGMATIC PRIORITIZATION** — Categorize findings by impact vs. effort. Identify immediate quick wins alongside foundational investments.

### Required Report Structure
1. Executive Summary & Core Codebase Impressions
2. Major Technical & Structural Risks
3. Immediate Quick Wins (Weeks 1-3)
4. Strategic Architecture & Database Investments (Months 2-3)
5. Observability, Telemetry, & Testing Enhancements
6. Organizational & Workflow Improvement Recommendations"""

    # Two-pass reasoning for cloud; single-pass with all context for local-only mode
    if is_local_mode:
        passes = [fallback_prompt]
    else:
        pass1 = f"""[Pass 1: Architecture Synthesis & Risk Discovery]
Review this parsed structural layout of your new codebase:

{PARSER_LIMITATIONS_FILLED}

## Project Topography
```json
{project_map_json}
```

Brainstorm a raw ledger of:
1. **Structural bottlenecks** — Coupling patterns, circular dependencies, fat models or views, inconsistent serializer patterns, missing abstractions.
2. **Risk areas** — Views lacking authorization, unscoped querysets, mass assignment exposure, hardcoded values, audit trail gaps, untested paths.
3. **Observability gaps** — No logging or tracing visible in the topography, missing error handling patterns.
4. **Quick win opportunities** — Simple refactors, field constraint improvements, obvious test targets.
5. **Architecture investments** — Database migration needs, service layer extraction, caching opportunities, async task patterns.

Do not structure the 90-day roadmap or write final sections yet. Just map what you see."""

        pass2 = f"""[Pass 2: Timeline Filtering & Production Strategy]
Review your synthesis from Pass 1.

{PARSER_LIMITATIONS_FILLED}

Group, trim, and refine those insights into a concrete, realistic 90-day onboarding strategy.

### Cross-check each finding against the actual topography data:
- **Model field exists?** Confirm every referenced field appears in the specific model's `fields` list.
- **View exists?** Confirm every referenced class appears in the `views` list with its `absolute_path`.
- **Serializer exists?** Confirm every referenced serializer appears in the `serializers` list.
- **Task exists?** Only reference Celery tasks listed in the `celery_tasks` section.

### Required Report Structure
Generate the final report matching the schema defined in your system prompt. Include these sections with exact file references:

1. **Executive Summary & Core Codebase Impressions** — High-level architectural assessment based on parsed structure.
2. **Major Technical & Structural Risks** — Concrete risks traced to specific files and patterns. For each risk, note whether it is a confirmed finding or uncertain due to parser limitations.
3. **Immediate Quick Wins (Weeks 1-3)** — Changes that can be made safely with high confidence from the topography alone. Include the exact file path and class name for each.
4. **Strategic Architecture & Database Investments (Months 2-3)** — Larger initiatives visible from coupling patterns and missing abstractions in the parsed map.
5. **Observability, Telemetry (OpenTelemetry), & Testing Enhancements** — Gap analysis based on what visibility the topography reveals.
6. **Organizational & Workflow Improvement Recommendations** — Process changes, ownership boundaries, code review triggers evident from the project structure."""

        passes = [pass1, pass2]

    # 4. Execute Reasoning Pass
    print(f"Step 3: Processing strategy analysis via [{REASONING_ARCHITECT}]...")
    pass_start = time.time()

    runner = StatefulHarnessRunner(
        model_name=REASONING_ARCHITECT,
        base_url=ARCHITECT_API_BASE,
        api_key=ARCHITECT_API_KEY,
        fallback_model_name=FALLBACK_REVIEWER,
        num_ctx=65536
    )
    history = runner.execute_sequence(
        system_prompt=system_agent_prompt,
        passes=passes,
        fallback_prompt=fallback_prompt
    )
    final_analysis = history[-1]["output"]
    model_used = runner.model_name

    print(f"   [Done] Strategy analysis via {model_used} in {time.time() - pass_start:.2f}s")

    # 5. Evaluate analysis quality
    print(f"Step 4: Evaluating strategy viability via Local Judge [{HEAVY_REVIEWER}]...")
    judge_start = time.time()

    judge_context = f"Project Topography:\n{project_map_json[:5000]}"
    evaluator = AutomatedEvaluator(judge_model=HEAVY_REVIEWER)
    scores = evaluator.grade_run(final_analysis, "rubrics/strategy_rubric.json", context=judge_context)

    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"Strategy Reliability Scores: {scores}")

    # 6. Log and Export Artifacts
    print("Step 5: Archiving run data...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=model_used,
        agent_role="Incoming Staff Engineer (90-Day Strategy)",
        raw_output=final_analysis,
        scores=scores
    )

    report_filename = "reports/staff_90_day_onboarding_roadmap.md"
    os.makedirs("reports", exist_ok=True)
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(final_analysis)

    total_duration = time.time() - start_time
    print(f"\nReport saved to: {report_filename}")
    print(f"Total Time: {total_duration:.2f}s  Model: {model_used}")

if __name__ == "__main__":
    main()
