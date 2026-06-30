import argparse
import json
import os
import sys
import threading
import time

import requests

from core.agent import Agent
from core.judge import AutomatedEvaluator
from core.mcp_orchestrator import init_orchestrator
from core.parser import DjangoTopographer
from core.warehouse import HarnessWarehouse

# ==============================================================================
# MODEL & API CONFIGURATION
# ==============================================================================
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

FALLBACK_REVIEWER = "gemini-2.5-flash"
HEAVY_REVIEWER = "deepseek-r1:14b"
LOCAL_JUDGE = os.getenv("LOCAL_JUDGE", "qwen3-coder:latest")
# Ensure judge is from a different model family than architect
_MODEL_FAMILIES = {
    "qwen": {"qwen", "qwen2", "qwen3", "qwen14b", "qwen2.5"},
    "deepseek": {"deepseek"},
    "gemma": {"gemma", "gemini"},
    "codestral": {"codestral", "mistral"},
}


def _model_family(name: str) -> str:
    lower = name.lower().split(":")[0]
    for family, prefixes in _MODEL_FAMILIES.items():
        if any(lower.startswith(p) for p in prefixes):
            return family
    return lower.split("-")[0] if "-" in lower else lower.split(":")[0]


# ==============================================================================

MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.json")

_mcp_orch = None


def parse_arguments():
    parser = argparse.ArgumentParser(description="Security Evaluation Engine")
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


def init_mcp(repo_path: str | None = None, config_path: str | None = None):
    global _mcp_orch
    if _mcp_orch is not None:
        return _mcp_orch
    cfg_path: str = config_path or MCP_CONFIG_PATH
    path: str = repo_path or os.environ.get("TARGET_REPO") or os.getcwd()
    orch = init_orchestrator(cfg_path, path)
    if orch:
        _mcp_orch = orch
    return orch


def build_mcp_context() -> str:
    orch = _mcp_orch
    if not orch:
        return ""
    return orch.build_mcp_context_block(tags=["security", "architectural_rule"])


def _progress_indicator(interval: int = 30):
    """Print a dot every `interval` seconds while the LLM generates."""
    done = threading.Event()

    def _dot():
        while not done.wait(interval):
            print(f"   ...still running ({int(time.time() - t0)}s)", flush=True)

    t0 = time.time()
    t = threading.Thread(target=_dot, daemon=True)
    t.start()
    return done


def trim_for_security(pm: dict) -> dict:
    """Strip project-map fields the security evaluator doesn't use.

    Keeps everything the PARSER_LIMITATIONS calibration rules reference;
    drops noise that inflates token count without affecting analysis quality.
    """
    # Models: keep only name + absolute_path for mass-assignment cross-ref
    # Serializers: keep field names + read_only + Meta model/fields/exclude
    # Views: keep everything security-relevant, drop relative_path, skip stubs
    out: dict[str, list] = {"models": [], "serializers": [], "views": []}

    for m in pm.get("models", []):
        out["models"].append(
            {
                "p": m.get("absolute_path"),
                "c": m.get("class"),
                "f": [f["name"] for f in m.get("fields", []) if "name" in f],
            }
        )

    for s in pm.get("serializers", []):
        fields = [
            {k: f[k] for k in ("name", "read_only") if k in f}
            for f in s.get("fields", [])
        ]
        trimmed = {"p": s.get("absolute_path"), "c": s.get("class"), "f": fields}
        if "meta" in s:
            meta = {
                k: v
                for k, v in s["meta"].items()
                if k in ("model", "fields", "exclude", "read_only_fields")
            }
            if meta:
                trimmed["m"] = meta
        out["serializers"].append(trimmed)

    for v in pm.get("views", []):
        methods = v.get("methods", [])
        filtered_methods = []
        for m in methods:
            if m.get("is_stub"):
                continue
            entry = {}
            for k in ("n", "h", "s", "i", "a", "d", "D"):
                val = m.get(
                    {
                        "n": "name",
                        "h": "http_method",
                        "s": "self_scoped",
                        "i": "inline_auth_calls",
                        "a": "auth_fully_trusted",
                        "d": "delegated_auth_calls",
                        "D": "delegate_auth_fully_trusted",
                    }[k]
                )
                if val:
                    entry[k] = val
            filtered_methods.append(entry)

        view_entry: dict = {}
        for k, vv in v.items():
            if k == "relative_path":
                continue
            if k == "methods":
                if filtered_methods:
                    view_entry["m"] = filtered_methods
                continue
            if k == "class_attributes":
                ca = {}
                for ca_k in (
                    "permission_classes",
                    "authentication_classes",
                    "serializer_class",
                    "queryset",
                    "lookup_field",
                ):
                    if ca_k in vv:
                        ca[ca_k] = vv[ca_k]
                if ca:
                    view_entry["c"] = ca
                continue
            if k == "base_classes" and vv:
                view_entry["b"] = vv
                continue
            if k == "http_methods" and vv:
                view_entry["h"] = vv
                continue
            if k == "is_read_only" and vv:
                view_entry["r"] = True
                continue
            if k == "get_object_overridden" and vv:
                view_entry["g"] = True
                continue
            if k == "queryset_auth_chain" and vv != "unknown":
                view_entry["q"] = vv
                continue
            if k == "permission_class_analysis" and vv:
                view_entry["pa"] = vv
                continue
            if k == "absolute_path":
                view_entry["p"] = vv
                continue
            if k == "class":
                view_entry["c"] = vv
                continue
        out["views"].append(view_entry)

    return out


def main():
    args = parse_arguments()

    target_repo = args.repo or os.environ.get("TARGET_REPO")
    mcp_config_path = args.mcp_config or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)

    is_local_mode = not ARCHITECT_API_KEY
    if not is_local_mode and not ARCHITECT_API_KEY:
        print("Error: USE_GEMINI=true requires GEMINI_API_KEY to be set.")
        print("Please run: export GEMINI_API_KEY='your_key_here'")
        return

    if not target_repo:
        print("Error: No target repository specified.")
        print("Set TARGET_REPO env var or pass --repo /path/to/project")
        return

    judge_model = LOCAL_JUDGE if is_local_mode else HEAVY_REVIEWER
    if _model_family(judge_model) == _model_family(REASONING_ARCHITECT):
        judge_model = HEAVY_REVIEWER
    print(f"{'=' * 60}")
    print("Launching Security Evaluation Engine (Hybrid Mode)")
    print(f"Target Project   : {target_repo}")
    print(f"Cloud Architect  : {REASONING_ARCHITECT}")
    print(f"Local Judge      : {judge_model}")
    print(f"{'=' * 60}\n")

    start_time = time.time()

    # 1. Gather Global Structural Picture
    print("Step 1: Parsing Django topography for attack surface analysis...")
    step_start = time.time()
    topographer = DjangoTopographer(target_repo)
    project_map = topographer.scan_project()
    print(f"   [Done] Topography scan in {time.time() - step_start:.2f}s")

    if not project_map.get("serializers") and not project_map.get("views"):
        print(
            "   Warning: No Django views or serializers detected. Attack surface may be incomplete."
        )

    # 2. Load Security Engineer Persona
    print("Step 2: Loading security engineer persona...")
    persona_path = "agents/security.md"
    if not os.path.exists(persona_path):
        print(f"Error: System prompt missing at {persona_path}")
        return
    with open(persona_path, encoding="utf-8") as f:
        system_agent_prompt = f.read()
    print("   [Done] Persona loaded")

    # 2b. Initialize MCP workbench for richer context
    print("Step 2b: Initializing MCP workbench...")
    orch = init_mcp(repo_path=target_repo, config_path=mcp_config_path)
    mcp_block = build_mcp_context() if orch else ""
    if orch:
        print("   [Done] MCP workbench active (tools + git + memory)\n")
    else:
        print("   [Skipped] No MCP config found\n")

    # 3. Build prompt context
    project_map = trim_for_security(project_map)
    project_map_json = json.dumps(project_map, default=str, separators=(",", ":"))

    parser_limitations = (
        "Parser Capabilities & Limitations\n"
        "The topography is built by static AST parsing. Here's what it CAN and CANNOT resolve:\n"
        "CAN resolve:\n"
        "- Model fields (name, type, null, default, unique, blank, primary_key, editable)\n"
        "- Serializer fields (name, type, required, read_only, allow_null, allow_blank) — including nested serializers like SimpleProfileSerializer(read_only=True)\n"
        "- Serializer Meta (model, fields, exclude, read_only_fields) — including inherited expressions like Parent.Meta.fields + [...]\n"
        "- View class attributes: permission_classes, authentication_classes, serializer_class, queryset, lookup_field\n"
        "- View base classes (e.g., RetrieveAPIView, APIView, ModelViewSet)\n"
        "- View HTTP methods (derived from non-stub method names: get/post/put/patch/delete)\n"
        "- View read-only status (is_read_only: true if only GET is supported)\n"
        "- ALL methods including DRF hooks: perform_create, perform_destroy, perform_update, get_queryset, get_serializer_class, get_object — each with stub detection and inline auth call scanning\n"
        '- Inline authorization calls: methods list inline_auth_calls with function names found in method bodies (e.g., ["authorize_superuser"])\n'
        "- Serializer field keyword arguments: required, read_only, allow_null, allow_blank, many, allow_empty, queryset, source, child\n"
        "- DRF delegation chain resolution: When an HTTP handler (e.g., destroy) has a corresponding perform_* delegate (e.g., perform_destroy), any inline_auth_calls found in the delegate are propagated to the handler via the delegated_auth_calls field. Check delegated_auth_calls in addition to inline_auth_calls — auth in perform_destroy() gates the mutation even if destroy() lacks it.\n"
        '- get_object/get_queryset relationship: Each view has get_object_overridden (True if the view defines its own get_object) and queryset_auth_chain (one of "scoped", "overridden", or "unknown"). When get_object_overridden is False and get_queryset has inline_auth_calls, the chain is "scoped" — authorization in get_queryset() cascades to all single-object operations (retrieve, update, destroy, partial_update) via DRF\'s inherited get_object(). When get_object IS overridden, the chain is "overridden" — the custom get_object() replaces DRF\'s default; check it for its own authorization. When get_queryset is not defined or has no auth, the chain is "unknown".\n'
        "- auth_fully_trusted flag: Each method entry has an auth_fully_trusted boolean. true means every inline_auth_calls function in that method belongs to the manually-verified trusted set (authorize_app_user, authorize_benefactor, authorize_creator, authorize_superuser, authorize_staff_or_superuser, authorize_benefactor_or_creator, authorize_benefactor_scope). These functions enforce strict user or tenant scoping. Methods with auth_fully_trusted: true have verified authorization — do NOT flag them as vulnerable. Delegated auth also propagates this: delegate_auth_fully_trusted: true means the perform_* delegate's auth is fully trusted.\n"
        "CANNOT resolve:\n"
        "- Method bodies beyond stub detection and auth call scanning (no control flow, validation logic, or queryset filtering details)\n"
        "- URL patterns or route configurations\n"
        "- ViewSet action-to-HTTP-method mapping beyond function names\n"
        "- Decorators (@permission_classes, @authentication_classes) — these are parsed as class attributes instead\n"
        "ANTI-HALLUCINATION RULES — read these before touching any view data:\n"
        "1. NEVER attribute a class_attributes field to a view unless it explicitly appears in that view's class_attributes dict. Each view entry has its own isolated dict. If queryset is not listed in a view's class_attributes, that view does NOT have a class-level queryset — do NOT invent one.\n"
        "2. get_queryset method ≠ queryset attribute: A view may define get_queryset() in its methods list but have no queryset in its class_attributes. These are different DRF patterns — get_queryset() at runtime takes precedence over the class-level queryset attribute. Never claim a view has queryset = Model.objects.all just because it has a get_queryset method.\n"
        "3. Each view is independent: Every view in the topography is a separate entry with its own class_attributes, methods, and base_classes. Do not mix data between views, even if they share a file.\n"
        "4. Empty inline_auth_calls ≠ no authorization: The scanner finds authorization calls in method bodies by name matching. If get_queryset() or a handler method delegates to a service/helper function (e.g., get_accessible_courses_queryset(profile)), the scanner cannot trace into that call. Empty inline_auth_calls means no directly visible auth calls were found — it does NOT mean authorization is absent. Do NOT assume a get_queryset() with empty inline_auth_calls is unscoped; the scoping may be encapsulated in a delegated service function. However, if auth_fully_trusted: true, the authorization IS verified — do NOT flag.\n"
        '5. queryset = .none() + custom get_object() is a standard DRF pattern: A view may set class_attributes.queryset = "Model.objects.none()" while overriding get_object() with custom ownership filtering. The .none() queryset intentionally prevents DRF\'s inherited get_object() from running — the custom get_object() does its own query with user filtering. Check whether get_object appears in the methods list. If it does and has inline_auth_calls (e.g., user=user filtering), the class-level queryset is irrelevant for authorization. Do NOT flag this as an unscoped queryset.\n'
        '6. permission_classes values are resolved Python objects, not strings: The parser captures permission_classes from class-level assignments like permission_classes = [permissions.IsAuthenticated] or permission_classes = [IsAuthenticated]. These are Python class references (via module attribute or direct import) that resolve at class definition time — there is zero resolution failure risk. This is NOT the DRF dotted-string permission path pattern (e.g., "rest_framework.permissions.IsAuthenticated"). Do NOT invent "string resolution failure" findings based on these values.\n'
        "7. Serializer field queryset is captured when present: If a serializer field like PrimaryKeyRelatedField(queryset=Model.objects.all()) has a queryset argument, it appears in the field's dict. Check the field entry for a queryset key before claiming scoping is missing. If the key is absent, the field genuinely lacks queryset=. Do NOT hallucinate missing queryset= — verify against the actual field data.\n"
        "First, Do No Harm — Severity Calibration Rules:\n"
        "Authorization:\n"
        "1. Class-level auth: Check class_attributes.permission_classes. If set, auth IS configured — do NOT flag. Values like IsAuthenticated or permissions.IsAuthenticated are resolved Python class objects (not strings) — there is no resolution failure mechanism in Python's import system. Superuser-only permission classes with .objects.all() querysets are expected DRF admin patterns — superusers have cross-tenant access by design. Flag at LOW unless there is evidence of missing audit logging.\n"
        '2. Inline auth in methods: Check inline_auth_calls on each method. A method with ["authorize_superuser"] has method-level authorization — do NOT flag.\n'
        '3. DRF delegation ordering (check delegated_auth_calls): DRF calls destroy() → get_object() (read-only lookup, no side effects) → perform_destroy() (where mutation + auth happen). The parser now propagates auth calls from perform_* delegates to their HTTP handler via the delegated_auth_calls field. Check delegated_auth_calls on the handler method — if it\'s non-empty, auth exists in the perform_* delegate and gates the mutation. Do NOT treat an unscoped get_object() as a vulnerability if perform_destroy() has auth — downgrade to LOW/INFO. Example: destroy() with inline_auth_calls: [] but delegated_auth_calls: ["authorize_superuser"] means the mutation IS protected.\n'
        "4. auth_fully_trusted / delegate_auth_fully_trusted: If a method has auth_fully_trusted: true, ALL its auth calls are from the manually-verified trusted set (authorize_app_user, authorize_benefactor, authorize_creator, authorize_superuser, authorize_staff_or_superuser, authorize_benefactor_or_creator, authorize_benefactor_scope). These functions enforce strict user or tenant scoping. This pattern is considered PROTECTED — report it in the Protected Areas section with the evidence, NOT in Vulnerability Findings. The same applies to delegate_auth_fully_trusted: true on a handler with delegated_auth_calls.\n"
        '5. get_object/get_queryset chain (check get_object_overridden and queryset_auth_chain): When get_object_overridden is False and queryset_auth_chain is "scoped", authorization in get_queryset() cascades to all single-object operations — report in Protected Areas, NOT in Vulnerability Findings. When get_object_overridden is True ("overridden"), the custom get_object() replaces DRF\'s default. Check if get_object() has auth_fully_trusted: true — if so, report in Protected Areas. If get_object() has empty inline_auth_calls, the auth is invisible — flag at LOW max, as UNCERTAIN.\n'
        "6. Self-scoped handlers (self_scoped: true): If a handler method has self_scoped: true, it exclusively operates on request.user and accepts no user-controllable resource ID. This is inherently scoped by authentication — report in Protected Areas, NOT in Vulnerability Findings.\n"
        "7. is_read_only: true + class-level permission_classes: If a view is read-only AND has class_attributes.permission_classes set, report in Protected Areas, NOT above LOW in Vulnerability Findings.\n"
        "8. APIView subclasses: Views inheriting from APIView (not GenericAPIView/ModelViewSet) don't use get_queryset() or serializer_class by default. Check base_classes before assuming DRF generic view patterns.\n"
        "9. .none() queryset with overridden get_object(): If a view has queryset = Model.objects.none() AND overrides get_object() in its methods list, the queryset is a safety guard, not the authorization mechanism. DRF's normal get_object() → get_queryset() chain is broken by the override. Check the overridden get_object() for ownership filtering instead. Do NOT flag as BOLA.\n"
        "10. DRF view lifecycle — initial() runs BEFORE handler methods: DRF's dispatch() calls initial() before dispatching to the HTTP handler (get(), post(), etc.). The execution order is: dispatch() → initial() (called, sets up self._profile etc.) → get() → retrieve() → get_object() → get_queryset(). A view that calls authorize_app_user() in initial() and caches the result in self._profile, then uses self._profile in get_queryset(), is correctly ordered — initial() always completes before any handler method runs. Do NOT flag initial() as an anti-pattern.\n"
        "Mass Assignment:\n"
        "11. Field must exist in Meta.fields: Before flagging a field as writable, confirm it appears in the serializer's Meta.fields list. If NOT in the list, it cannot be mass-assigned — retract entirely.\n"
        "12. Field-level read_only: Check each field's read_only attribute. read_only: true on a field declaration takes precedence over Meta.fields. If a field is inherited from a parent serializer, look up the parent serializer's field declarations for read_only. Nested serializer fields like SimpleProfileSerializer(read_only=True) are captured correctly.\n"
        "13. Writable PK fields: Check field attributes: required: false = optional (reduces risk), read_only: true = not writable. If neither attribute is present, assume constrained and downgrade to MEDIUM.\n"
        "View Role — Critical for Disambiguation:\n"
        "14. Read-only views: Check is_read_only: true on the view. A serializer used in a read-only view is never written to by DRF — writable field declarations are harmless. Do NOT flag.\n"
        "15. Read serializer ≠ write serializer: A view may use DIFFERENT serializers for reads vs writes. For mass assignment analysis, you MUST check the WRITE view's serializer_class (the view with POST/PUT/PATCH methods). A GET-only view's serializer (e.g., CourseFullListSerializer on CourseRetrieveView) may include representation-only fields like content_creator that do NOT exist in the write serializer's Meta.fields. If a field exists in a read serializer but NOT in the write serializer's Meta.fields, it is NOT mass-assignable — do NOT flag it.\n"
        "16. Method stubs: Methods with is_stub: true contain no real logic. If a finding relies on a stubbed method, retract it.\n"
        '17. get_queryset with auth: If a view has get_queryset with inline_auth_calls (e.g., ["authorize_creator"]), the view enforces object-level authorization at the query level — serializer-level mass assignment is less risky. If get_queryset exists with EMPTY inline_auth_calls, do NOT assume it is unscoped — the method may delegate to a service function (e.g., get_accessible_courses_queryset(profile)) that the parser cannot trace. Flag at MEDIUM max, as UNCERTAIN.\n'
        "Classification:\n"
        '18. Hardcoded vs user-supplied data: Static analysis cannot trace data flows from request → API calls. If the only evidence for an "unvalidated input" finding is the endpoint existing (no visible user-controlled data flow from request parameters to API calls), flag at INFO not LOW. An endpoint using hardcoded values with no request data consumption is a code quality concern, not a vulnerability.\n'
        '19. Endpoint misclassification: A class mentioning "Stripe" or "Webhook" is not necessarily a webhook handler. Check base_classes and http_methods. A Stripe Checkout Session creation endpoint makes outbound API calls — no webhook signature verification applies.\n'
        "20. Inherited Meta fields: Resolved Meta.fields lists are complete. A field not in this list cannot be mass-assigned.\n"
        "MCP-Augmented Context (Live Project State):\n"
        f"{mcp_block}\n"
        "Penetration Test Framing:\n"
        "Treat the analysis as a manual penetration test. Think about how an attacker would chain weaknesses together into a realistic exploit path. Each step must trace back to visible evidence. Do NOT invent endpoints, data flows, or trust boundaries to satisfy a narrative. An attack path predicated on a hallucinated queryset or class_attributes value is invalid — verify every attribute against the specific view's entry."
    )

    # Single-pass fallback: used for local-only mode and cloud API failures
    fallback_prompt = (
        "Below is the full project topography map including models with their fields, serializers, views, and URL routes.\n"
        "\n"
        f"{parser_limitations}\n"
        "\n"
        "Project Model Map:\n"
        "```json\n"
        f"{project_map_json}\n"
        "```\n"
        "\n"
        "Review Instructions:\n"
        "Conduct a thorough security audit of the codebase represented above. Apply the severity calibration rules above before making any finding.\n"
        "\n"
        "Mandatory Rules (violations will be flagged):\n"
        "1. CITE FILE PATHS — For every vulnerability, reference the exact absolute_path from the topography map. Example: profiles/models.py\n"
        "2. NO FABRICATED EXAMPLES — Never invent permission codenames, method signatures, fields, endpoints, or components not present in the provided context.\n"
        "3. UNCERTAIN MEANS UNCERTAIN — If you cannot verify a finding with confidence, say UNCERTAIN: [what you're unsure about]. Do not hedge with vague wording.\n"
        '4. CLEAN IS A FINDING — If a module appears secure, say "Looks secure" explicitly. A report that finds no issues is valid.\n'
        "5. NO GENERIC ADVICE — Do not give generic security lectures or OWASP re-education. Only analyze the actual code present.\n"
        "6. SEVERITY TRACKING — Every finding must be labeled CRITICAL / HIGH / MEDIUM / LOW / UNCERTAIN.\n"
        "\n"
        "Required Coverage:\n"
        "- Authorization Boundaries: Multi-tenancy isolation, object-level permissions, role leakage\n"
        "- Mass Assignment: Serializer fields vs model fields, fields = '__all__', writable nested serializers\n"
        "- Authentication: Missing or weak auth on views, hardcoded tokens, session exposure\n"
        "- Secrets Management: Hardcoded API keys, connection strings, or credentials in code/config\n"
        "- OWASP API Top 10: BOLA, BFLA, injection vectors, unrestricted resource consumption\n"
        "\n"
        "Attack Path Analysis (Evidence-Driven):\n"
        "Frame your findings as a manual penetration test. Identify any realistic attack paths where evidence in the topography suggests confidentiality, integrity, or availability could be compromised.\n"
        "DO NOT force a specific number of attack paths. If the evidence supports 0, 1, or 3 attack paths, that is the right answer. Only include an attack path if you can trace each step to concrete evidence in the topography.\n"
        "For each attack path, structure as:\n"
        "- Prerequisites (attacker position, required access level)\n"
        "- Exploitation Steps (sequence of API calls or data flows)\n"
        "- Affected Endpoints (file paths + class names)\n"
        "- Business Impact (what CIA pillar is compromised and how)\n"
        "- Mitigations (specific, not generic)\n"
        "If no attack path can be fully traced from evidence, include a section titled No Evidence-Based Attack Paths Found and explain which controls prevent the most likely threat models.\n"
        "\n"
        "Output Format:\n"
        "Generate the final report using this four-section structure:\n"
        "## Vulnerability Findings\n"
        "List each verified finding with severity, evidence, and confidence.\n"
        "## Protected Areas (Verified — Not Vulnerabilities)\n"
        'List every view or method that triggered a rule-mandated exclusion (e.g., auth_fully_trusted: true, self_scoped: true, queryset_auth_chain: "scoped", read-only + class-level auth). Each entry must include the specific evidence from the topography that satisfied the rule. If no views triggered any exclusion rule, state "No protected areas identified."\n'
        "## Attack Path Analysis\n"
        "Chain findings into attack narratives, or state that none could be traced.\n"
        "## Secure Areas\n"
        "REQUIRED — List ALL modules that appear well-configured, noting which security controls are visibly in place. Include at minimum: every view with permission_classes set, every view with self_scoped: true methods, every view with auth in get_queryset, and every view using authorize_superuser or authorize_staff. This section must NOT be empty — even a report with findings has areas that are correctly secured. If truly none exist, state the specific controls that are missing across all views."
    )

    # Two-pass reasoning: split analysis + verification for faster per-pass generation
    pass1 = (
        "[Pass 1: Attack Surface Discovery & Mapping]\n"
        "Review this extracted codebase topography structure for security vulnerabilities:\n"
        "\n"
        f"{parser_limitations}\n"
        "\n"
        "Project Model Map:\n"
        "```json\n"
        f"{project_map_json}\n"
        "```\n"
        "\n"
        "Adopt a manual penetration testing mindset. Map the attack surface by chaining weaknesses together, not just listing them in isolation.\n"
        "Identify potential vulnerabilities across these categories:\n"
        "- Mass Assignment: Compare serializer field lists against model field lists. Check Meta.fields, Meta.read_only_fields, Meta.exclude. Flag fields = '__all__' only after checking if read_only_fields covers sensitive fields.\n"
        '- Authorization Gaps: Note views that lack permission_classes in the topography. First check if the view might use custom helpers or auth_fully_trusted methods (the parser can\'t see service function bodies, but auth_fully_trusted: true means verified auth is present) — mark as UNCERTAIN, not HIGH. Do NOT flag views with self_scoped: true or queryset_auth_chain: "scoped".\n'
        "- Secrets & Credentials: Identify any hardcoded secrets in the topography (API keys, tokens, connection strings).\n"
        "- Exposed Endpoints: Flag classes suggesting broad CRUD. Before flagging, note whether the methods might be stubs — mark method-only findings as LOW unless you see body evidence.\n"
        "- Audit Trail Gaps: Identify destructive operations (DELETE, PATCH) that lack logging.\n"
        "For each finding, reference the exact absolute_path from the topography and apply the calibration rules above. Note which findings could chain together into an attack path (e.g., mass assignment + missing auth on a destroy endpoint = data exfiltration). If a category appears clean, note that explicitly. Do not make recommendations yet — just discover and map."
    )

    pass2 = (
        "[Pass 2: Strict Verification & Final Report]\n"
        "Review your findings from Pass 1.\n"
        "\n"
        f"{parser_limitations}\n"
        "\n"
        "CRITICAL MANDATE: For every single vulnerability or risk you retain in your final report, you MUST:\n"
        "1. Reference the exact absolute_path from the topography map.\n"
        "2. Quote the specific field name, class name, or pattern as evidence.\n"
        "3. If you cannot map a finding to an exact file path from the context, discard the finding entirely.\n"
        "4. Re-apply ALL severity calibration rules before finalizing.\n"
        "Cross-check each finding against the actual topography data. For every attribute reference, VERIFY it exists in that specific view's entry — do not confuse views.\n"
        "\n"
        "Mass assignment checks:\n"
        "- Field is in Meta.fields? Confirm the flagged field appears in the WRITE serializer's Meta.fields list. The write serializer is the serializer_class on the view that has POST/PUT/PATCH methods. A READ serializer (on GET-only views) may include representation-only fields not present in the write serializer — those fields are NOT mass-assignable.\n"
        "- Field has read_only? Check the field's read_only attribute. If a field is inherited from a parent serializer, look up the parent serializer's field declarations — read_only: true may be defined there.\n"
        "- View is read-only? If the view only serves GET requests (is_read_only: true), DRF never calls save() — retract entirely.\n"
        "- Nested serializer read_only? A field using a nested serializer with read_only=True is entirely read-only — retract.\n"
        "\n"
        "Authorization checks (applied in order; first match wins):\n"
        "- auth_fully_trusted: true on handler or its delegate? Move to Protected Areas with evidence. NOT a vulnerability.\n"
        "- self_scoped: true on handler? Move to Protected Areas with evidence. NOT a vulnerability.\n"
        '- queryset_auth_chain: "scoped"? Move to Protected Areas with evidence. NOT a vulnerability.\n'
        "- get_object_overridden: true and get_object has auth_fully_trusted: true? Move to Protected Areas with evidence. NOT a vulnerability.\n"
        "- is_read_only: true and class_attributes.permission_classes is set? Move to Protected Areas with evidence. NOT a vulnerability above LOW.\n"
        "- DRF delegation chain? Check delegated_auth_calls on the HTTP handler (destroy, create, update). If delegated_auth_calls is non-empty, auth gates the mutation — downgrade to LOW/INFO.\n"
        "- Custom permission class inspection? Check permission_class_analysis on the view entry. If a custom permission class has has_permission: true but has_object_permission: false, it CANNOT enforce object-level authorization — DRF's default delegates object checks to has_permission, which only checks role, never ownership. If the view also uses an unscoped queryset (.all), this is a demonstrable HIGH BOLA finding — do NOT rate as UNCERTAIN. The permission class provides role-based access control but zero object-level scoping.\n"
        "- get_object_overridden: true but get_object has empty inline_auth_calls? Auth is invisible inside the custom get_object(). Flag at LOW max, as UNCERTAIN.\n"
        "- Neither visible? If a view lacks auth on both the handler and its perform_* delegate, state UNCERTAIN.\n"
        "\n"
        "Endpoint checks:\n"
        "- Method is a stub? Check is_stub: true. If flagged, retract any finding relying on it.\n"
        '- User-supplied data? Static analysis cannot trace data flows. If the only evidence for an "unvalidated input" finding is the endpoint existing, flag at INFO not LOW. An endpoint using hardcoded values that never passes client data to an external service (e.g., hardcoded price ID in Stripe checkout) is even lower — rate NEGLIGIBLE, as the abuse surface is creating checkout sessions with a fixed price and zero client-controlled parameters.\n'
        "- No fabricated querysets: Only reference queryset from a view's class_attributes if explicitly listed there. get_queryset method ≠ queryset attribute — but note that get_queryset() DOES gate single-object access in DRF: GenericAPIView.get_object() calls self.get_queryset() internally before filtering by PK. If a view defines get_queryset() with authorization filters (e.g., authorize_creator), those filters apply to update(), destroy(), and retrieve() via get_object(). Do NOT claim the queryset is unscoped just because only get_queryset() exists — the authorization in get_queryset() IS enforced for all single-object operations.\n"
        "- No fabricated fields: If a field is in a read serializer's field list but NOT in the write serializer's Meta.fields, it is NOT mass-assignable. Do not flag it.\n"
        "\n"
        "Generate the final report using this four-section structure:\n"
        "## Vulnerability Findings\n"
        "List each verified finding with:\n"
        "### [SEVERITY] - Finding Title\n"
        "- Target File: absolute_path\n"
        "- Vulnerability Type: (Mass Assignment / BOLA / Secrets / Auth Gap / Audit Gap)\n"
        "- Confidence: (HIGH / MEDIUM / LOW)\n"
        "- Evidence: (exact class/field/pattern from topography)\n"
        "## Protected Areas (Verified — Not Vulnerabilities)\n"
        'List every view or method that triggered a rule-mandated exclusion (e.g., auth_fully_trusted: true, self_scoped: true, queryset_auth_chain: "scoped", read-only + class-level auth). Each entry must include the specific evidence from the topography that satisfied the rule.\n'
        "Format:\n"
        "### [PROTECTED] - View/Method Name\n"
        "- Target File: absolute_path\n"
        "- Protection Rule: Which rule applied (e.g., auth_fully_trusted, self_scoped, queryset_auth_chain: scoped, is_read_only + permission_classes)\n"
        "- Evidence: Exact field/value from topography that proves the protection\n"
        "This section documents the security baseline. If future modifications break any of these protections, they will migrate from this section to Vulnerability Findings — flagging the regression.\n"
        'If no views triggered any exclusion rule, state "No protected areas identified."\n'
        "## Attack Path Analysis\n"
        "Chain findings into realistic attack narratives. Only include an attack path if every step is backed by topography evidence. The number of attack paths should match the evidence — 0 is a valid answer.\n"
        "For each attack path:\n"
        "- Prerequisites: What position does the attacker need? (unauthenticated, low-privilege user, staff)\n"
        "- Exploitation Steps: Sequence of concrete API calls, field manipulations, or data flows\n"
        "- Affected Endpoints: Specific file paths and class names\n"
        "- Business Impact: What is compromised (confidentiality, integrity, availability) and what data/operations are at risk\n"
        "- Mitigations: Specific, implementable changes to existing code\n"
        'If no attack path can be fully traced from evidence, state "No evidence-based attack paths found." and explain which controls (class-level auth, field constraints, method stubs, queryset scoping) block the most plausible threat models.\n'
        "## Secure Areas\n"
        "REQUIRED — List ALL modules that appear well-configured, noting which security controls are visibly in place. Include at minimum: every view with permission_classes set, every view with self_scoped: true methods, every view with auth in get_queryset, and every view using authorize_superuser or authorize_staff. This section must NOT be empty — even a report with findings has areas that are correctly secured. If truly none exist, state the specific controls that are missing across all views."
    )

    # Single-pass for local (faster); two-pass for cloud (redundancy)
    passes = [fallback_prompt] if is_local_mode else [pass1, pass2]

    # 4. Execute Reasoning Pass via Agent
    print(f"Step 3: Processing security analysis via [{REASONING_ARCHITECT}]...")
    pass_start = time.time()

    analyst = Agent(
        name="Security_Auditor",
        system_prompt=system_agent_prompt,
        model_name=REASONING_ARCHITECT,
        base_url=ARCHITECT_API_BASE,
        api_key=ARCHITECT_API_KEY,
        num_ctx=49152,
    )

    context_parts: list[str] = []
    for i, pass_prompt in enumerate(passes):
        combined = (
            "\n\n".join([*context_parts, pass_prompt]) if context_parts else pass_prompt
        )
        t0 = time.time()
        _progress_indicator()
        try:
            output = analyst.execute(combined)
        except requests.exceptions.ConnectionError as e:
            print(
                "\n   [ERROR] LLM connection failed — Ollama may have run out of memory."
            )
            print(
                "   Try a smaller model, reduce num_ctx, or close other applications."
            )
            print(f"   Details: {e}")
            sys.exit(1)
        print(f"   [Done] Pass {i + 1}/{len(passes)} in {time.time() - t0:.1f}s")
        context_parts.append(f"[Pass {i + 1} Output]:\n{output}")

    final_analysis = output
    model_used = analyst.model_name

    print(
        f"   [Done] Security analysis via {model_used} in {time.time() - pass_start:.2f}s"
    )

    # 5. Evaluate analysis quality
    print(f"Step 4: Evaluating analysis quality via Local Judge [{judge_model}]...")
    judge_start = time.time()

    # Provide the project map as ground truth so the judge can detect fabrication
    judge_context = f"Project Map:\n{project_map_json[:5000]}"
    evaluator = AutomatedEvaluator(judge_model=judge_model)
    scores = evaluator.grade_run(
        final_analysis, "rubrics/security_rubric.json", context=judge_context
    )

    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"Analysis Reliability Scores: {scores}")

    # 6. Log and Export Artifacts
    print("Step 5: Archiving run data...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=model_used,
        agent_role="Staff Security Engineer",
        raw_output=final_analysis,
        scores=scores,
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
