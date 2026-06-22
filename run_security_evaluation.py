import os
import sys
import json
import time
from core.parser import DjangoTopographer
from core.agent import Agent
from core.judge import AutomatedEvaluator
from core.warehouse import HarnessWarehouse

# ==============================================================================
# MODEL & API CONFIGURATION
# ==============================================================================
USE_GEMINI = os.getenv("USE_GEMINI", "").lower() in ("1", "true", "yes")

CLOUD_MODEL           = "gemini-2.5-flash"
LOCAL_MODEL           = "qwen3.6:latest"

REASONING_ARCHITECT   = CLOUD_MODEL if USE_GEMINI else LOCAL_MODEL
ARCHITECT_API_BASE    = "https://generativelanguage.googleapis.com/v1beta/openai" if USE_GEMINI else "http://localhost:11434"
ARCHITECT_API_KEY     = os.getenv("GEMINI_API_KEY") if USE_GEMINI else None

FALLBACK_REVIEWER     = "gemini-2.5-flash"
HEAVY_REVIEWER        = "deepseek-r1:14b"
# ==============================================================================

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
        memory = orch.recall(tags=["security", "architectural_rule"])
        if memory and memory != "(no memories)":
            parts.append(f"Security Context:\n{memory}")
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
    print(f"Launching Security Evaluation Engine (Hybrid Mode)")
    print(f"Target Project   : {TARGET_DJANGO_PROJECT}")
    print(f"Cloud Architect  : {REASONING_ARCHITECT}")
    print(f"Local Judge      : {HEAVY_REVIEWER}")
    print(f"{'='*60}\n")

    start_time = time.time()

    # 1. Gather Global Structural Picture
    print("Step 1: Parsing Django topography for attack surface analysis...")
    step_start = time.time()
    topographer = DjangoTopographer(TARGET_DJANGO_PROJECT)
    project_map = topographer.scan_project()
    print(f"   [Done] Topography scan in {time.time() - step_start:.2f}s")

    if not project_map.get("serializers") and not project_map.get("views"):
        print(f"   Warning: No Django views or serializers detected. Attack surface may be incomplete.")

    # 2. Load Security Engineer Persona
    print("Step 2: Loading security engineer persona...")
    persona_path = "agents/security.md"
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
    if orch:
        print("   [Done] MCP workbench active (git context + memory recall)\n")
    else:
        print("   [Skipped] No MCP config found\n")

    # 3. Build prompt context
    project_map_json = json.dumps(project_map, indent=2, default=str)

    PARSER_LIMITATIONS = """### Parser Capabilities & Limitations

The topography is built by static AST parsing. Here's what it CAN and CANNOT resolve:

**CAN resolve:**
- Model fields (name, type, null, default, unique, blank, primary_key, editable)
- Serializer fields (name, type, required, read_only, allow_null, allow_blank) — including nested serializers like `SimpleProfileSerializer(read_only=True)`
- Serializer Meta (model, fields, exclude, read_only_fields) — including inherited expressions like `Parent.Meta.fields + [...]`
- View class attributes: `permission_classes`, `authentication_classes`, `serializer_class`, `queryset`, `lookup_field`
- View base classes (e.g., `RetrieveAPIView`, `APIView`, `ModelViewSet`)
- View HTTP methods (derived from non-stub method names: get/post/put/patch/delete)
- View read-only status (`is_read_only: true` if only GET is supported)
- ALL methods including DRF hooks: `perform_create`, `perform_destroy`, `perform_update`, `get_queryset`, `get_serializer_class` — each with stub detection and inline auth call scanning
- Inline authorization calls: methods list `inline_auth_calls` with function names found in method bodies (e.g., `["authorize_superuser"]`)

**CANNOT resolve:**
- Method bodies beyond stub detection and auth call scanning (no control flow, validation logic, or queryset filtering details)
- URL patterns or route configurations
- ViewSet action-to-HTTP-method mapping beyond function names
- Decorators (@permission_classes, @authentication_classes) — these are parsed as class attributes instead

### ANTI-HALLUCINATION RULES — read these before touching any view data

1. **NEVER attribute a class_attributes field to a view unless it explicitly appears in that view's `class_attributes` dict**. Each view entry has its own isolated dict. If `queryset` is not listed in a view's `class_attributes`, that view does NOT have a class-level queryset — do NOT invent one.
2. **`get_queryset` method ≠ `queryset` attribute**: A view may define `get_queryset()` in its methods list but have no `queryset` in its class_attributes. These are different DRF patterns — `get_queryset()` at runtime takes precedence over the class-level `queryset` attribute. Never claim a view has `queryset = Model.objects.all` just because it has a `get_queryset` method.
3. **Each view is independent**: Every view in the topography is a separate entry with its own `class_attributes`, `methods`, and `base_classes`. Do not mix data between views, even if they share a file.

### First, Do No Harm — Severity Calibration Rules

**Authorization:**
1. **Class-level auth**: Check `class_attributes.permission_classes`. If set, auth IS configured — do NOT flag. Superuser-only permission classes with `.objects.all()` querysets are expected DRF admin patterns — superusers have cross-tenant access by design. Flag at LOW unless there is evidence of missing audit logging.
2. **Inline auth in methods**: Check `inline_auth_calls` on each method. A method with `["authorize_superuser"]` has method-level authorization — do NOT flag.
3. **DRF delegation ordering**: DRF calls `destroy()` → `get_object()` (read-only lookup, no side effects) → `perform_destroy()` (where mutation + auth happen). `get_object()` itself cannot modify data. Auth in `perform_destroy()` gates the MUTATION, so the endpoint is protected. Do NOT treat an unscoped `get_object()` as a vulnerability if `perform_destroy()` has auth — downgrade to LOW/INFO.
4. **APIView subclasses**: Views inheriting from `APIView` (not `GenericAPIView`/`ModelViewSet`) don't use `get_queryset()` or `serializer_class` by default. Check `base_classes` before assuming DRF generic view patterns.

**Mass Assignment:**
5. **Field must exist in Meta.fields**: Before flagging a field as writable, confirm it appears in the serializer's `Meta.fields` list. If NOT in the list, it cannot be mass-assigned — retract entirely.
6. **Field-level read_only**: Check each field's `read_only` attribute. `read_only: true` on a field declaration takes precedence over `Meta.fields`. If a field is inherited from a parent serializer, look up the parent serializer's field declarations for `read_only`. Nested serializer fields like `SimpleProfileSerializer(read_only=True)` are captured correctly.
7. **Writable PK fields**: Check field attributes: `required: false` = optional (reduces risk), `read_only: true` = not writable. If neither attribute is present, assume constrained and downgrade to MEDIUM.

**View Role — Critical for Disambiguation:**
8. **Read-only views**: Check `is_read_only: true` on the view. A serializer used in a read-only view is never written to by DRF — writable field declarations are harmless. Do NOT flag.
9. **Read serializer ≠ write serializer**: A view may use DIFFERENT serializers for reads vs writes. For mass assignment analysis, you MUST check the WRITE view's `serializer_class` (the view with POST/PUT/PATCH methods). A GET-only view's serializer (e.g., `CourseFullListSerializer` on `CourseRetrieveView`) may include representation-only fields like `content_creator` that do NOT exist in the write serializer's `Meta.fields`. If a field exists in a read serializer but NOT in the write serializer's `Meta.fields`, it is NOT mass-assignable — do NOT flag it.
10. **Method stubs**: Methods with `is_stub: true` contain no real logic. If a finding relies on a stubbed method, retract it.
11. **`get_queryset` with auth**: If a view has `get_queryset` with `inline_auth_calls` (e.g., `["authorize_creator"]`), the view enforces object-level authorization at the query level — serializer-level mass assignment is less risky.

**Classification:**
12. **Hardcoded vs user-supplied data**: Static analysis cannot trace data flows from request → API calls. If the only evidence for an "unvalidated input" finding is the endpoint existing (no visible user-controlled data flow from request parameters to API calls), flag at INFO not LOW. An endpoint using hardcoded values with no request data consumption is a code quality concern, not a vulnerability.
13. **Endpoint misclassification**: A class mentioning "Stripe" or "Webhook" is not necessarily a webhook handler. Check `base_classes` and `http_methods`. A Stripe Checkout Session creation endpoint makes outbound API calls — no webhook signature verification applies.
14. **Inherited Meta fields**: Resolved `Meta.fields` lists are complete. A field not in this list cannot be mass-assigned.

### MCP-Augmented Context (Live Project State)
{mcp_block}

### Penetration Test Framing
Treat the analysis as a manual penetration test. Think about how an attacker would chain weaknesses together into a realistic exploit path. Each step must trace back to visible evidence. Do NOT invent endpoints, data flows, or trust boundaries to satisfy a narrative. An attack path predicated on a hallucinated `queryset` or `class_attributes` value is invalid — verify every attribute against the specific view's entry."""

    # Single-pass fallback: used for local-only mode and cloud API failures
    fallback_prompt = f"""Below is the full project topography map including models with their fields, serializers, views, and URL routes.

{PARSER_LIMITATIONS}

## Project Model Map
```json
{project_map_json}
```

## Review Instructions
Conduct a thorough security audit of the codebase represented above. Apply the severity calibration rules above before making any finding.

### Mandatory Rules (violations will be flagged):
1. **CITE FILE PATHS** — For every vulnerability, reference the exact `absolute_path` from the topography map. Example: `profiles/models.py`
2. **NO FABRICATED EXAMPLES** — Never invent permission codenames, method signatures, fields, endpoints, or components not present in the provided context.
3. **UNCERTAIN MEANS UNCERTAIN** — If you cannot verify a finding with confidence, say `UNCERTAIN: [what you're unsure about]`. Do not hedge with vague wording.
4. **CLEAN IS A FINDING** — If a module appears secure, say "Looks secure" explicitly. A report that finds no issues is valid.
5. **NO GENERIC ADVICE** — Do not give generic security lectures or OWASP re-education. Only analyze the actual code present.
6. **SEVERITY TRACKING** — Every finding must be labeled CRITICAL / HIGH / MEDIUM / LOW / UNCERTAIN.

### Required Coverage
- **Authorization Boundaries**: Multi-tenancy isolation, object-level permissions, role leakage
- **Mass Assignment**: Serializer fields vs model fields, `fields = '__all__'`, writable nested serializers
- **Authentication**: Missing or weak auth on views, hardcoded tokens, session exposure
- **Secrets Management**: Hardcoded API keys, connection strings, or credentials in code/config
- **OWASP API Top 10**: BOLA, BFLA, injection vectors, unrestricted resource consumption

### Attack Path Analysis (Evidence-Driven)
Frame your findings as a manual penetration test. Identify any realistic attack paths where evidence in the topography suggests confidentiality, integrity, or availability could be compromised.

DO NOT force a specific number of attack paths. If the evidence supports 0, 1, or 3 attack paths, that is the right answer. Only include an attack path if you can trace each step to concrete evidence in the topography.

For each attack path, structure as:
- **Prerequisites** (attacker position, required access level)
- **Exploitation Steps** (sequence of API calls or data flows)
- **Affected Endpoints** (file paths + class names)
- **Business Impact** (what CIA pillar is compromised and how)
- **Mitigations** (specific, not generic)

If no attack path can be fully traced from evidence, include a section titled `No Evidence-Based Attack Paths Found` and explain which controls prevent the most likely threat models.

### Output Format

Generate the final report using this three-section structure:

## Vulnerability Findings
List each verified finding with severity, evidence, and confidence.

## Attack Path Analysis
Chain findings into attack narratives, or state that none could be traced.

## Secure Areas
List modules that appear well-configured, noting which security controls are visibly in place."""

    # Two-pass reasoning for cloud; single-pass with all context for local-only mode
    if is_local_mode:
        passes = [fallback_prompt]
    else:
        pass1 = f"""[Pass 1: Attack Surface Discovery & Mapping]
Review this extracted codebase topography structure for security vulnerabilities:

{PARSER_LIMITATIONS}

## Project Model Map
```json
{project_map_json}
```

Adopt a manual penetration testing mindset. Map the attack surface by chaining weaknesses together, not just listing them in isolation.

Identify potential vulnerabilities across these categories:
- **Mass Assignment**: Compare serializer field lists against model field lists. Check `Meta.fields`, `Meta.read_only_fields`, `Meta.exclude`. Flag `fields = '__all__'` only after checking if `read_only_fields` covers sensitive fields.
- **Authorization Gaps**: Note views that lack `permission_classes` in the topography. First check if the view might use custom helpers (the parser can't see these) — mark as UNCERTAIN, not HIGH.
- **Secrets & Credentials**: Identify any hardcoded secrets in the topography (API keys, tokens, connection strings).
- **Exposed Endpoints**: Flag classes suggesting broad CRUD. Before flagging, note whether the methods might be stubs — mark method-only findings as LOW unless you see body evidence.
- **Audit Trail Gaps**: Identify destructive operations (DELETE, PATCH) that lack logging.

For each finding, reference the exact `absolute_path` from the topography and apply the calibration rules above. Note which findings could chain together into an attack path (e.g., mass assignment + missing auth on a destroy endpoint = data exfiltration). If a category appears clean, note that explicitly. Do not make recommendations yet — just discover and map."""

        pass2 = f"""[Pass 2: Strict Verification & Final Report]
Review your findings from Pass 1.

{PARSER_LIMITATIONS}

CRITICAL MANDATE: For every single vulnerability or risk you retain in your final report, you MUST:
1. Reference the exact `absolute_path` from the topography map.
2. Quote the specific field name, class name, or pattern as evidence.
3. If you cannot map a finding to an exact file path from the context, discard the finding entirely.
4. Re-apply ALL severity calibration rules before finalizing.

Cross-check each finding against the actual topography data. For every attribute reference, VERIFY it exists in that specific view's entry — do not confuse views.

**Mass assignment checks:**
- **Field is in Meta.fields?** Confirm the flagged field appears in the WRITE serializer's `Meta.fields` list. The write serializer is the `serializer_class` on the view that has POST/PUT/PATCH methods. A READ serializer (on GET-only views) may include representation-only fields not present in the write serializer — those fields are NOT mass-assignable.
- **Field has read_only?** Check the field's `read_only` attribute. If a field is inherited from a parent serializer, look up the parent serializer's field declarations — `read_only: true` may be defined there.
- **View is read-only?** If the view only serves GET requests (`is_read_only: true`), DRF never calls `save()` — retract entirely.
- **Nested serializer read_only?** A field using a nested serializer with `read_only=True` is entirely read-only — retract.

**Authorization checks:**
- **Self-scoped view?** Check `self_scoped: true` on the handler method. An APIView handler (patch/post) with no inline auth calls but `self_scoped: true` exclusively operates on `request.user` — it never accepts a user-controllable resource ID. Downgrade to INFO — it is inherently scoped by authentication with no mechanism for cross-user targeting.
- **Class-level auth set?** Check `class_attributes.permission_classes` on the specific view. If present, auth IS configured — do NOT flag.
- **Custom permission class inspection?** Check `permission_class_analysis` on the view entry. If a custom permission class has `has_permission: true` but `has_object_permission: false`, it CANNOT enforce object-level authorization — DRF's default delegates object checks to `has_permission`, which only checks role, never ownership. If the view also uses an unscoped queryset (`.all`), this is a demontrable HIGH BOLA finding — do NOT rate as UNCERTAIN. The permission class provides role-based access control but zero object-level scoping.
- **DRF delegation chain?** Check `inline_auth_calls` on BOTH the HTTP handler (`destroy`, `create`, `update`) AND its `perform_*` delegate (`perform_destroy`, `perform_create`, `perform_update`). Auth in `perform_destroy()` gates the mutation even if `destroy()` lacks it — `get_object()` before that point is read-only. Downgrade to LOW/INFO.
- **Neither visible?** If a view lacks auth on both the handler and its `perform_*` delegate, state UNCERTAIN.

**Endpoint checks:**
- **Method is a stub?** Check `is_stub: true`. If flagged, retract any finding relying on it.
- **User-supplied data?** Static analysis cannot trace data flows. If the only evidence for an "unvalidated input" finding is the endpoint existing, flag at INFO not LOW. An endpoint using hardcoded values that never passes client data to an external service (e.g., hardcoded price ID in Stripe checkout) is even lower — rate NEGLIGIBLE, as the abuse surface is creating checkout sessions with a fixed price and zero client-controlled parameters.
- **No fabricated querysets**: Only reference `queryset` from a view's `class_attributes` if explicitly listed there. `get_queryset` method ≠ `queryset` attribute — but note that `get_queryset()` DOES gate single-object access in DRF: `GenericAPIView.get_object()` calls `self.get_queryset()` internally before filtering by PK. If a view defines `get_queryset()` with authorization filters (e.g., `authorize_creator`), those filters apply to `update()`, `destroy()`, and `retrieve()` via `get_object()`. Do NOT claim the `queryset` is unscoped just because only `get_queryset()` exists — the authorization in `get_queryset()` IS enforced for all single-object operations.
- **No fabricated fields**: If a field is in a read serializer's field list but NOT in the write serializer's `Meta.fields`, it is NOT mass-assignable. Do not flag it.

Generate the final report using this structure:

## Vulnerability Findings
List each verified finding with:
### [SEVERITY] - Finding Title
- **Target File:** `absolute_path`
- **Vulnerability Type:** (Mass Assignment / BOLA / Secrets / Auth Gap / Audit Gap)
- **Confidence:** (HIGH / MEDIUM / LOW)
- **Evidence:** (exact class/field/pattern from topography)

## Attack Path Analysis
Chain findings into realistic attack narratives. Only include an attack path if every step is backed by topography evidence. The number of attack paths should match the evidence — 0 is a valid answer.

For each attack path:
- **Prerequisites**: What position does the attacker need? (unauthenticated, low-privilege user, staff)
- **Exploitation Steps**: Sequence of concrete API calls, field manipulations, or data flows
- **Affected Endpoints**: Specific file paths and class names
- **Business Impact**: What is compromised (confidentiality, integrity, availability) and what data/operations are at risk
- **Mitigations**: Specific, implementable changes to existing code

If no attack path can be fully traced from evidence, state "No evidence-based attack paths found." and explain which controls (class-level auth, field constraints, method stubs, queryset scoping) block the most plausible threat models.

## Secure Areas
List modules that appear well-configured, noting which security controls are visibly in place.

If all findings are low-confidence or parser-limited, state "All potential findings are constrained by parser limitations — manual review recommended for high-signal areas listed below." and itemize the areas that warrant human attention."""

        passes = [pass1, pass2]

    # 4. Execute Reasoning Pass via Agent
    print(f"Step 3: Processing security analysis via [{REASONING_ARCHITECT}]...")
    pass_start = time.time()

    analyst = Agent(
        name="Security_Auditor",
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
        output = analyst.execute(combined)
        print(f"   [Done] Pass {i+1}/{len(passes)} in {time.time() - t0:.1f}s")
        context += f"\n\n[Pass {i+1} Output]:\n{output}"

    final_analysis = output
    model_used = analyst.model_name

    print(f"   [Done] Security analysis via {model_used} in {time.time() - pass_start:.2f}s")

    # 5. Evaluate analysis quality
    print(f"Step 4: Evaluating analysis quality via Local Judge [{HEAVY_REVIEWER}]...")
    judge_start = time.time()

    # Provide the project map as ground truth so the judge can detect fabrication
    judge_context = f"Project Map:\n{project_map_json[:5000]}"
    evaluator = AutomatedEvaluator(judge_model=HEAVY_REVIEWER)
    scores = evaluator.grade_run(final_analysis, "rubrics/security_rubric.json", context=judge_context)

    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"Analysis Reliability Scores: {scores}")

    # 6. Log and Export Artifacts
    print("Step 5: Archiving run data...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=model_used,
        agent_role="Staff Security Engineer",
        raw_output=final_analysis,
        scores=scores
    )

    report_filename = "reports/automated_security_review.md"
    os.makedirs("reports", exist_ok=True)
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(final_analysis)

    if _mcp_orch:
        _mcp_orch.remember(
            "eval:security_review:complete",
            f"Security evaluation completed. Report: {report_filename}",
            tags=["evaluation", "security", "complete"],
        )
        _mcp_orch.stop()

    total_duration = time.time() - start_time
    print(f"\nReport saved to: {report_filename}")
    print(f"Total Time: {total_duration:.2f}s  Model: {model_used}")

if __name__ == "__main__":
    main()
