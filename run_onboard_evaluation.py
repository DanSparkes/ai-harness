import os
import re
import json
import time
from core.parser import DjangoTopographer
from core.agent import Agent
from core.judge import AutomatedEvaluator
from core.warehouse import HarnessWarehouse
from core.mcp_orchestrator import init_orchestrator

os.environ.setdefault("OLLAMA_MLX", "1")

USE_GEMINI = os.getenv("USE_GEMINI", "").lower() in ("1", "true", "yes")

CLOUD_MODEL           = "gemini-2.5-flash"
LOCAL_MODEL           = "qwen3.6:latest"

REASONING_ARCHITECT   = CLOUD_MODEL if USE_GEMINI else LOCAL_MODEL
ARCHITECT_API_BASE    = "https://generativelanguage.googleapis.com/v1beta/openai" if USE_GEMINI else "http://localhost:11434"
ARCHITECT_API_KEY     = os.getenv("GEMINI_API_KEY") if USE_GEMINI else None

FALLBACK_REVIEWER     = "gemini-2.5-flash"
HEAVY_REVIEWER        = "deepseek-r1:14b"
LOCAL_JUDGE           = "qwen2.5-coder:14b"

TARGET_DJANGO_PROJECT = "/Users/dansparkes/memores/memores-api"
MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.json")

_mcp_orch = None


def init_mcp():
    global _mcp_orch
    if _mcp_orch is not None:
        return _mcp_orch
    orch = init_orchestrator(MCP_CONFIG_PATH, TARGET_DJANGO_PROJECT)
    if orch:
        _mcp_orch = orch
    return orch


def build_mcp_context() -> str:
    orch = _mcp_orch
    if not orch:
        return ""
    parts = []
    git_block = orch.build_git_context(max_count=10)
    if git_block:
        parts.append(git_block)
    memory = orch.recall_tagged(tags=["architectural_rule"])
    if memory:
        parts.append(f"Architectural Rules:\n{memory}")
    return "\n\n".join(parts)


def build_django_live_context() -> tuple[str, dict]:
    orch = _mcp_orch
    if not orch:
        return "", {}
    return orch.build_django_live_context()


def build_codebase_memory_context() -> tuple[str, dict]:
    orch = _mcp_orch
    if not orch:
        return "", {}
    return orch.build_codebase_memory_context()

def main():
    is_local_mode = not ARCHITECT_API_KEY
    if not is_local_mode and not ARCHITECT_API_KEY:
        print("Error: USE_GEMINI=true requires GEMINI_API_KEY to be set.")
        print("Please run: export GEMINI_API_KEY='your_key_here'")
        return

    print(f"{'='*60}")
    print(f"Launching Staff Onboarding Engine (Hybrid Mode)")
    print(f"Target Project   : {TARGET_DJANGO_PROJECT}")
    print(f"Cloud Architect  : {REASONING_ARCHITECT}")
    print(f"Local Judge      : {LOCAL_JUDGE}")
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

    # 2b. Initialize MCP workbench for richer context
    print("Step 2b: Initializing MCP workbench...")
    orch = init_mcp()
    mcp_block = build_mcp_context() if orch else ""
    django_block, django_data = build_django_live_context() if orch else ("", {})
    cm_block, cm_data = build_codebase_memory_context() if orch else ("", {})
    live_context = "\n\n".join(filter(None, [mcp_block, django_block, cm_block]))
    if orch:
        statuses = []
        if mcp_block:
            statuses.append("git + memory")
        if django_block:
            statuses.append("django-ai-boost")
        if cm_block:
            statuses.append("codebase-memory")
        print(f"   [Done] MCP workbench active ({' + '.join(statuses)})\n")
    else:
        print("   [Skipped] No MCP config found\n")

    # 3. Build prompt context
    project_map_json = json.dumps(project_map, indent=2, default=str)

    PARSER_LIMITATIONS = """### Parser Capabilities & Limitations

The topography is built by static AST parsing. Here's what it CAN and CANNOT resolve:

**CAN resolve:**
- Model fields (name, type, null, default, unique, blank, primary_key, editable) with `is_abstract` flag for abstract models
- Serializer fields (name, type, required, read_only, allow_null, allow_blank)
- Serializer Meta (model, fields, exclude, read_only_fields) — including inherited expressions like `Parent.Meta.fields + [...]`
- View class attributes: `permission_classes`, `authentication_classes`, `serializer_class`, `queryset`, `lookup_field`
- View base classes (e.g., `RetrieveAPIView`, `APIView`, `ModelViewSet`)
- View HTTP methods (derived from non-stub method names: get/post/put/patch/delete)
- View read-only status (`is_read_only: true` if only GET is supported)
- Custom permission class resolution with `has_permission` and `has_object_permission` analysis
- Inline authorization calls: methods list `inline_auth_calls` with function names found in method bodies
- Celery task definitions
- **Function-based views**: Functions decorated with `@api_view(...)` are now parsed and appear in the views list with `is_function_view: true`, their HTTP methods resolved from the decorator argument, and decorator-level `permission_classes`/`authentication_classes` extracted from sibling `@permission_classes` and `@authentication_classes` decorators
- **Abstract model detection**: Each model entry includes `is_abstract: true/false` to distinguish concrete tables from abstract base classes

**CANNOT resolve (but see MCP-Augmented Context below for live data):**
- Method bodies beyond stub detection and auth call scanning (no control flow, validation logic, or query filter details)
- URL patterns or route configurations — **filled by Live Django URL Patterns** from `django-ai-boost` in the MCP section below
- ViewSet action-to-HTTP-method mapping beyond function names
- Business logic, data flows, or runtime state
- Database relationships, foreign keys, or cascading behavior — **filled by Live Database Schema** from `django-ai-boost`
- Actual settings or runtime configuration values — **filled by Live Django App Info** from `django-ai-boost`

### Codebase Inventory
Based on parsing, this project contains:
- {model_count} total model classes ({concrete_model_count} concrete, {abstract_model_count} abstract)
- {serializer_count} serializers
- {view_count} views ({class_based_view_count} class-based, {function_based_view_count} function-based)

### Anti-Hallucination Rules
1. **NEVER attribute a field to a model unless it appears in that model's `fields` list**. Each model entry has its own isolated field list.
2. **`get_queryset` method != `queryset` attribute**: A view may define `get_queryset()` in its methods list but have no `queryset` in its `class_attributes`. These are different DRF patterns.
3. **Each view is independent**: Every view in the topography is a separate entry with its own `class_attributes`, `methods`, and `base_classes`. Do not mix data between views.
4. **Only reference files and classes that appear in the topography map**. Do not invent imports, dependencies, or third-party integrations not visible in the parsed structure.
5. **Distinguish abstract from concrete models**: Each model entry has an `is_abstract` boolean. Abstract models (like `SoftDeleteModel`) cannot be instantiated directly. Do not count abstract models as concrete data tables. When discussing model counts, state the breakdown clearly (e.g., "36 concrete + 1 abstract").
6. **Never invent enum values**: If you reference status enums (JobStatuses, etc.), use the EXACT values from the constants. Do not substitute synonyms like COMPLETED for FINISHED or FAILED for ERROR.
7. **View names are exact**: Never add or remove suffixes from view class names. If the topography lists `AdminBenefactorRetrieveView`, do NOT refer to it as `AdminBenefactorRetrieveUpdateView`.

### Known Ground Truth (Verified Facts — MUST Match)
The following facts have been manually verified against the codebase. Your report MUST be consistent with them:
- **JobStatuses enum values**: `PENDING`, `IN_PROGRESS`, `FINISHED`, `ERROR` (NOT "COMPLETED" or "FAILED")
- **Admin benefactor view**: `AdminBenefactorRetrieveView` (NOT `AdminBenefactorRetrieveUpdateView`)
- **Soft delete models**: `SoftDeleteModel` is abstract. Concrete soft-delete models: `UserCourseCompletion`, `CourseProgress`, `AnalysisOutput`, `SharingCode`, `CoachEntry`, `JournalEntry`, `EmailReportRequest` (7 total). These use `is_deleted` flag for soft deletion.
- **Admin destroy views**: `AdminAnalysisOutputRetrieveDestroyView` uses `all_objects` (including deleted records). `AdminUserCompletedCourseDestroyView` and `AdminUserCourseProgressDestroyView` correctly implement soft deletes via `perform_destroy`.
- **Course model**: `Course` inherits from `models.Model` directly (NOT `SoftDeleteModel`). `CourseDestroyView` performing hard deletes is EXPECTED behavior, not a risk.

### MCP-Augmented Context (Live Project State)
{live_context}

**Note:** The sections below tagged "Live Django" come from `django-ai-boost` runtime introspection and are fully accurate (actual DB schema, URL configs, settings). Cross-reference these against the static parser data above — when they disagree, the Live Django data is authoritative. Use the URL patterns to understand actual route structure, which the parser cannot resolve statically."""

    models_list = project_map.get('models', [])
    views_list = project_map.get('views', [])
    concrete_model_count = sum(1 for m in models_list if not m.get('is_abstract'))
    abstract_model_count = sum(1 for m in models_list if m.get('is_abstract'))
    class_based_view_count = sum(1 for v in views_list if not v.get('is_function_view'))
    function_based_view_count = sum(1 for v in views_list if v.get('is_function_view'))

    PARSER_LIMITATIONS_FILLED = PARSER_LIMITATIONS.format(
        model_count=len(models_list),
        concrete_model_count=concrete_model_count,
        abstract_model_count=abstract_model_count,
        serializer_count=len(project_map.get('serializers', [])),
        view_count=len(views_list),
        class_based_view_count=class_based_view_count,
        function_based_view_count=function_based_view_count,
        live_context=live_context
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

    # Two-pass reasoning: split analysis + verification for faster per-pass generation
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

    # 4. Execute Reasoning Pass via Agent
    print(f"Step 3: Processing strategy analysis via [{REASONING_ARCHITECT}]...")
    pass_start = time.time()

    analyst = Agent(
        name="Staff_Onboarding",
        system_prompt=system_agent_prompt,
        model_name=REASONING_ARCHITECT,
        base_url=ARCHITECT_API_BASE,
        api_key=ARCHITECT_API_KEY,
        num_ctx=65536,
    )

    context = ""
    for i, pass_prompt in enumerate(passes):
        combined = f"{context}\n\n{pass_prompt}" if context else pass_prompt
        t0 = time.time()
        output = analyst.execute(combined)
        print(f"   [Done] Pass {i+1}/{len(passes)} in {time.time() - t0:.1f}s")
        context += f"\n\n[Pass {i+1} Output]:\n{output}"

    final_analysis = output
    model_used = analyst.model_name

    print(f"   [Done] Strategy analysis via {model_used} in {time.time() - pass_start:.2f}s")

    # 5. Evaluate analysis quality
    print(f"Step 4: Evaluating strategy viability via Local Judge [{LOCAL_JUDGE}]...")
    judge_start = time.time()

    judge_context = f"Project Topography:\n{project_map_json[:5000]}"
    evaluator = AutomatedEvaluator(judge_model=LOCAL_JUDGE)
    scores = evaluator.grade_run(final_analysis, "rubrics/strategy_rubric.json", context=judge_context)

    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"Strategy Reliability Scores: {scores}")

    # 5b. Fact-check report against known ground truth
    print(f"Step 4b: Fact-checking report fidelity...")

    BAD_PATTERNS = [
        (r'\bCOMPLETED\b', '"COMPLETED" (should be FINISHED for JobStatuses)'),
        (r'\bFAILED\b', '"FAILED" (should be ERROR for JobStatuses)'),
        (r'AdminBenefactorRetrieveUpdateView', 'view name (should be AdminBenefactorRetrieveView)'),
    ]
    fidelity_notes = []
    fidelity_penalties = 0
    for pattern, desc in BAD_PATTERNS:
        matches = list(re.finditer(pattern, final_analysis))
        if matches:
            for m in matches:
                line_num = final_analysis[:m.start()].count('\n') + 1
                fidelity_notes.append(f"  ✗ Line {line_num}: {desc}")
                fidelity_penalties += 1

    # Check model count claims in the report
    model_count_patterns = [
        (r'(?:total|overall|approximately|about|contains?|has|have|of|:|\bwith)\s+(\d+)\s+models?\b', 'model count'),
        (r'(?:total|overall|approximately|about|contains?|has|have|of|:|\bwith)\s+(\d+)\s+serializers?\b', 'serializer count'),
        (r'(?:total|overall|approximately|about|contains?|has|have|of|:|\bwith)\s+(\d+)\s+views?\b', 'view count'),
    ]
    parser_model_count = len(project_map.get('models', []))
    parser_serializer_count = len(project_map.get('serializers', []))
    parser_view_count = len(project_map.get('views', []))
    tolerance = 3
    for pattern, label in model_count_patterns:
        for m in re.finditer(pattern, final_analysis, re.IGNORECASE):
            claimed = int(m.group(1))
            actual = {'model': parser_model_count, 'serializer': parser_serializer_count, 'view': parser_view_count}[label.split()[0]]
            if abs(claimed - actual) > tolerance:
                line_num = final_analysis[:m.start()].count('\n') + 1
                fidelity_notes.append(f"  ⚠ Line {line_num}: Claims {claimed} {label} (parser found {actual})")
                fidelity_penalties += 1

    # Check for "concrete" / "abstract" model distinction in the report
    has_concrete_abstract = bool(re.search(r'(concrete|abstract)\s*model', final_analysis, re.IGNORECASE))
    if has_concrete_abstract:
        fidelity_notes.append(f"  ✓ Correctly distinguishes concrete vs abstract models")
    else:
        fidelity_notes.append(f"  ⚠ Does not distinguish concrete vs abstract models")

    fidelity_score = max(0, 10 - fidelity_penalties)
    if fidelity_penalties == 0:
        fidelity_rating = "Excellent"
    elif fidelity_penalties <= 2:
        fidelity_rating = "Good"
    elif fidelity_penalties <= 4:
        fidelity_rating = "Fair"
    else:
        fidelity_rating = "Poor"

    fidelity_report = [
        f"\n{'='*50}",
        f"  Report Fidelity Score: {fidelity_score}/10 — {fidelity_rating}",
        f"  Penalties: {fidelity_penalties}",
    ]
    if fidelity_notes:
        fidelity_report.append("  Details:")
        fidelity_report.extend(fidelity_notes)
    fidelity_report.append(f"{'='*50}\n")
    fidelity_report_str = "\n".join(fidelity_report)
    print(fidelity_report_str)

    # 6. Log and Export Artifacts
    print("Step 5: Archiving run data...")
    full_scores = {**scores, "fidelity": fidelity_score, "fidelity_max": 10, "fidelity_notes": fidelity_notes}
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=model_used,
        agent_role="Incoming Staff Engineer (90-Day Strategy)",
        raw_output=final_analysis,
        scores=full_scores
    )

    report_filename = "reports/staff_90_day_onboarding_roadmap.md"
    os.makedirs("reports", exist_ok=True)
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(final_analysis)
        f.write(f"\n\n---\n{'-'*50}\n")
        f.write(f"## Fidelity Check\n\n")
        f.write(f"**Score:** {fidelity_score}/10 — {fidelity_rating}\n\n")
        f.write(f"**Parser Ground Truth:** {concrete_model_count} concrete models, {abstract_model_count} abstract, {parser_serializer_count} serializers, {parser_view_count} views ({class_based_view_count} class-based, {function_based_view_count} function-based)\n\n")
        if fidelity_notes:
            f.write("**Issues Found:**\n\n")
            for note in fidelity_notes:
                f.write(f"{note}\n\n")

    if _mcp_orch:
        _mcp_orch.remember(
            "eval:onboarding:complete",
            f"Onboarding strategy completed. Report: {report_filename}",
            tags=["evaluation", "onboarding", "complete"],
        )
        _mcp_orch.stop()

    total_duration = time.time() - start_time
    print(f"\nReport saved to: {report_filename}")
    print(f"Fidelity Score: {fidelity_score}/10 ({fidelity_rating})")
    print(f"Total Time: {total_duration:.2f}s  Model: {model_used}")

if __name__ == "__main__":
    main()
