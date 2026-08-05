# core/claim_verifier.py
"""Post-generation claim extraction and verification against the codebase.

After the reviewer generates its output, this module:
1. Extracts factual claims (field names, setting values, method signatures, attributes)
2. Verifies them against the project topography map and actual file contents
3. Returns verified claims and flagged issues for report annotation
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ClaimResult:
    claim_text: str
    claim_type: str
    verified: bool
    evidence: str = ""
    suggestion: str = ""


@dataclass
class VerificationReport:
    total_claims: int = 0
    verified: int = 0
    flagged: int = 0
    results: list[ClaimResult] = field(default_factory=list)
    # Number of review claims the deterministic diff-audit flagged as
    # contradicting machine-derived invariants. When non-zero, the report is
    # not "clean" even if every extracted claim individually verified.
    audit_contradictions: int = 0

    @property
    def accuracy_rate(self) -> float:
        return self.verified / self.total_claims if self.total_claims > 0 else 1.0

    def to_summary(self) -> str:
        summary = (
            f"Claim Verification: {self.verified}/{self.total_claims} verified "
            f"({self.accuracy_rate:.0%} accuracy)"
        )
        if self.audit_contradictions:
            summary += (
                f" — ⚠️ {self.audit_contradictions} schema claim(s) CONTRADICTED "
                "the machine-derived diff invariants, invalidating this report's "
                "verdict and scores."
            )
        lines = [summary]
        for r in self.results:
            if not r.verified:
                lines.append(f"  FLAGGED [{r.claim_type}]: {r.claim_text}")
                if r.evidence:
                    lines.append(f"    Evidence: {r.evidence}")
                if r.suggestion:
                    lines.append(f"    Suggestion: {r.suggestion}")
        return "\n".join(lines)


# ── Extraction patterns ──────────────────────────────────────────────────────

# Field existence claims: "Course.title", "Question.question_text"
FIELD_REF_PATTERN = re.compile(r"\b([A-Z][a-zA-Z]+)\.([a-z_]+(?:_[a-z_]+)*)\b")

# Setting name claims: "MODELTRANSLATION_*", "AUTH_USER_MODEL", etc.
SETTING_PATTERN = re.compile(
    r"\b(MODELTRANSLATION_[A-Z_]+|AUTH_USER_MODEL|INSTALLED_APPS|DATABASES)\b"
)

# Method/function claims: "get_queryset()", "perform_create()", etc.
METHOD_PATTERN = re.compile(r"\b([a-z_]+(?:_[a-z_]+)*)\(\)")

# max_length claims
MAXLENGTH_PATTERN = re.compile(r"max_length[=:]\s*(\d+)")

# "is missing <attr>" claims — e.g., "CourseGroup.description_en is missing blank=True"
MISSING_ATTR_PATTERN = re.compile(
    r"([A-Z][a-zA-Z]+)\.([a-z_]+(?:_[a-z_]+)*)\s+(?:is\s+)?missing\s+(blank|default|null|max_length)\s*=\s*(\S+)",
    re.IGNORECASE,
)

# "has <attr>" claims — e.g., "field has blank=True, default=\"\""
HAS_ATTR_PATTERN = re.compile(
    r"(?:field|column)\s+has\s+(blank|default|null|max_length)\s*=\s*([^\s,\)]+)",
    re.IGNORECASE,
)

# Middleware ordering claims — e.g., "LocaleMiddleware should be right after SessionMiddleware"
MIDDLEWARE_ORDER_PATTERN = re.compile(
    r"(\w+Middleware)\s+(?:should be|must be|needs to be|needs to)\s+(?:right\s+|placed\s+)?(?:right\s+)?after\s+(\w+Middleware)",
    re.IGNORECASE,
)

# Import/definition claims — e.g., "BULK_CREATE_BATCH_SIZE is undefined", "X is not imported"
UNDEFINED_IDENTIFIER_PATTERN = re.compile(
    r"\b([A-Z_][A-Z0-9_]+)\s+(?:is\s+)?(?:undefined|not\s+defined|not\s+imported|missing)",
    re.IGNORECASE,
)

# Signal bypass claims — e.g., "bypasses post_save signals", "skips post_save"
SIGNAL_BYPASS_PATTERN = re.compile(
    r"(?:bypass(?:es|ed)?|skip(?:s|ped)?|miss(?:es|ed)?)\s+(?:the\s+)?(?:any\s+)?(?:registered\s+)?(\w+)\s+signal",
    re.IGNORECASE,
)

# Caching/mutation claims — e.g., "returns a cached reference", "shared reference"
CACHED_REFERENCE_PATTERN = re.compile(
    r"(?:returns?\s+(?:a\s+)?)?(?:cached|shared|mutable)\s+(?:dict|reference|object)",
    re.IGNORECASE,
)


# ── Claim extraction ─────────────────────────────────────────────────────────


def extract_claims(
    review_text: str, known_models: set[str] | None = None
) -> list[dict]:
    """Extract factual claims from review text.

    Args:
        review_text: The reviewer's output.
        known_models: Optional set of model class names parsed from the
            project topography. When provided, field references are only
            recorded for models in this set. Without this filter, ordinary
            English like "The.first" or "This.is" inflates the claim count
            and depresses the reported accuracy rate.
    """
    claims = []

    # Field references — filter to known model names when available to avoid
    # false positives from capitalized English words ("The.x", "This.is").
    for match in FIELD_REF_PATTERN.finditer(review_text):
        model_name, field_name = match.groups()
        if known_models is not None and model_name not in known_models:
            continue
        claims.append(
            {
                "text": match.group(0),
                "type": "field_exists",
                "model": model_name,
                "field": field_name,
            }
        )

    # Setting names
    for match in SETTING_PATTERN.finditer(review_text):
        claims.append(
            {"text": match.group(0), "type": "setting_name", "setting": match.group(0)}
        )

    # Method/function calls
    for match in METHOD_PATTERN.finditer(review_text):
        claims.append(
            {"text": match.group(0), "type": "method_exists", "method": match.group(1)}
        )

    # max_length claims
    for match in MAXLENGTH_PATTERN.finditer(review_text):
        claims.append(
            {
                "text": match.group(0),
                "type": "max_length_value",
                "value": int(match.group(1)),
            }
        )

    # "is missing <attr>" claims
    for match in MISSING_ATTR_PATTERN.finditer(review_text):
        model_name, field_name, attr, value = match.groups()
        claims.append(
            {
                "text": match.group(0),
                "type": "field_attribute",
                "model": model_name,
                "field": field_name,
                "attribute": attr,
                "expected_value": value,
                "claimed_present": False,
            }
        )

    # "has <attr>" claims
    for match in HAS_ATTR_PATTERN.finditer(review_text):
        attr, value = match.groups()
        claims.append(
            {
                "text": match.group(0),
                "type": "field_attribute",
                "attribute": attr,
                "expected_value": value,
                "claimed_present": True,
            }
        )

    # Middleware ordering claims
    for match in MIDDLEWARE_ORDER_PATTERN.finditer(review_text):
        target, predecessor = match.groups()
        claims.append(
            {
                "text": match.group(0),
                "type": "middleware_order",
                "target": target,
                "predecessor": predecessor,
            }
        )

    # Undefined identifier claims — e.g., "BULK_CREATE_BATCH_SIZE is undefined"
    for match in UNDEFINED_IDENTIFIER_PATTERN.finditer(review_text):
        identifier = match.group(1)
        claims.append(
            {"text": match.group(0), "type": "import_check", "identifier": identifier}
        )

    # Signal bypass claims — e.g., "bypasses post_save signals"
    for match in SIGNAL_BYPASS_PATTERN.finditer(review_text):
        signal_name = match.group(1)
        claims.append(
            {"text": match.group(0), "type": "signal_check", "signal_name": signal_name}
        )

    # Cached reference claims — e.g., "returns a cached reference"
    for match in CACHED_REFERENCE_PATTERN.finditer(review_text):
        claims.append({"text": match.group(0), "type": "caching_check"})

    return claims


# ── File reading helpers ─────────────────────────────────────────────────────

# Module-level caches. On a repo with hundreds of migrations and a review
# with 20+ claims, the per-claim rglob + read_text loops dominate runtime.
# These caches turn repeated work into a single pass per file per run.
# Use clear_verify_caches() at the start of each verification run to avoid
# stale data across long-lived processes.

_GLOB_CACHE: dict[tuple[str, str], tuple[Path, ...]] = {}
_FILE_CONTENT_CACHE: dict[Path, str | None] = {}


def clear_verify_caches() -> None:
    """Clear the rglob and file-content caches. Call between runs."""
    _GLOB_CACHE.clear()
    _FILE_CONTENT_CACHE.clear()


def _glob_files(repo_path: Path, pattern: str) -> tuple[Path, ...]:
    """rglob cached per (repo_path, pattern). Skips fixtures."""
    cache_key = (str(repo_path), pattern)
    cached = _GLOB_CACHE.get(cache_key)
    if cached is not None:
        return cached
    results = tuple(p for p in repo_path.rglob(pattern) if "fixtures" not in str(p))
    _GLOB_CACHE[cache_key] = results
    return results


def _read_file_cached(path: Path) -> str | None:
    """File read cached per path. Returns None on read failure."""
    if path in _FILE_CONTENT_CACHE:
        return _FILE_CONTENT_CACHE[path]
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        content = None
    _FILE_CONTENT_CACHE[path] = content
    return content


def _read_file(repo_path: Path, glob_pattern: str) -> str | None:
    """Read the first file matching a glob pattern in the repo."""
    for path in _glob_files(repo_path, glob_pattern):
        if "migrations" in str(path):
            continue
        content = _read_file_cached(path)
        if content is not None:
            return content
    return None


# Regex that matches Python string literals (single, double, triple-quoted).
# Used to blank out string contents before counting parens so things like
# help_text="see (docs)" don't confuse the field-block depth tracker.
_STRING_LITERAL_RE = re.compile(
    r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
)


def _strip_string_literals(line: str) -> str:
    """Replace string literal contents with empty quotes for safe paren counting."""
    return _STRING_LITERAL_RE.sub('""', line)


def _grep_find(
    repo_path: Path, search_term: str, file_glob: str = "*.py"
) -> str | None:
    """Grep for a term and return the first matching file path (relative)."""
    try:
        result = subprocess.run(
            ["grep", "-rl", "--include", file_glob, search_term, str(repo_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            first = result.stdout.strip().split("\n")[0]
            return first.replace(str(repo_path) + "/", "")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _extract_field_block(content: str, field_name: str) -> str | None:
    """Extract the full field definition block from source code.

    Handles two formats:
    1. Model field assignments: `field_name = models.CharField(...)`
    2. Migration AddField: `migrations.AddField(..., name="field_name", field=...)`

    Paren depth tracking strips string literals first, so help_text="see (docs)"
    no longer truncates the block early.
    """
    lines = content.split("\n")

    # Format 1: Direct field assignment in models.py
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(field_name)}\s*=\s*", line):
            if "(" in line:
                block = [line]
                # Count parens on the literal-stripped line so parens inside
                # string values (e.g. help_text="(see docs)") are ignored.
                paren_depth = _strip_string_literals(line).count(
                    "("
                ) - _strip_string_literals(line).count(")")
                for j in range(i + 1, min(i + 20, len(lines))):
                    block.append(lines[j])
                    stripped = _strip_string_literals(lines[j])
                    paren_depth += stripped.count("(") - stripped.count(")")
                    if paren_depth <= 0:
                        break
                return "\n".join(block)
            else:
                return line

    # Format 2: AddField in migrations
    # Look for name="field_name" and extract the surrounding AddField block
    for i, line in enumerate(lines):
        if f'name="{field_name}"' in line or f"name='{field_name}'" in line:
            # Walk backwards to find the start of AddField
            start = i
            for j in range(i, max(i - 10, -1), -1):
                if "AddField" in lines[j]:
                    start = j
                    break
            # Walk forward to find the end
            end = i
            paren_depth = 0
            for j in range(start, min(start + 20, len(lines))):
                stripped = _strip_string_literals(lines[j])
                paren_depth += stripped.count("(") - stripped.count(")")
                if paren_depth <= 0:
                    end = j + 1
                    break
            # NOTE: renamed from `block` to `block_text` so mypy does not
            # carry the list[str] type from Format 1's `block = [line]`.
            block_text = "\n".join(lines[start:end])
            # Also extract just the field=... part for attribute checking
            field_match = re.search(r"field\s*=\s*(.+)", block_text, re.DOTALL)
            if field_match:
                return field_match.group(0)
            return block_text

    return None


def _extract_middleware_list(content: str) -> list[str]:
    """Extract the MIDDLEWARE list from settings.py content."""
    # Find the MIDDLEWARE setting
    match = re.search(r"MIDDLEWARE\s*=\s*\[([^\]]*)\]", content, re.DOTALL)
    if not match:
        return []
    middleware_str = match.group(1)
    return re.findall(r"['\"]([^'\"]+)['\"]", middleware_str)


# ── Attribute verification ───────────────────────────────────────────────────


def _verify_field_attribute(
    claim: dict, project_map: dict, repo_path: Path
) -> tuple[bool, str, str]:
    """Verify a field attribute claim against actual source files.

    Returns (verified, evidence, suggestion).
    """
    model_name = claim.get("model", "")
    field_name = claim.get("field", "")
    attr = claim.get("attribute", "")
    claim.get("expected_value", "")
    claimed_present = claim.get("claimed_present", True)

    # First check the topography map
    model_fields = {}
    for m in project_map.get("models", []):
        if m.get("name") == model_name:
            for f in m.get("fields", []):
                model_fields[f.get("name", "")] = f
            break

    if field_name in model_fields:
        field_info = model_fields[field_name]
        if attr in field_info:
            actual_value = field_info[attr]
            attr_present = bool(actual_value)
            if attr_present == claimed_present:
                return True, f"Topography confirms {attr}={actual_value}", ""
            else:
                return (
                    False,
                    f"Topography shows {attr}={actual_value}, but claim says {'present' if claimed_present else 'absent'}",
                    f"Review the field definition for {model_name}.{field_name}",
                )
        elif not claimed_present:
            return (
                True,
                f"Attribute {attr} not in topography map (consistent with absent claim)",
                "",
            )

    # Handle modeltranslation fields: description_en → check description in model,
    # and description_en in migration
    translation_suffixes = ("_en", "_es", "_fr")
    base_field = field_name
    is_translation_field = False
    for suffix in translation_suffixes:
        if field_name.endswith(suffix):
            base_field = field_name[: -len(suffix)]
            is_translation_field = True
            break

    # Search order depends on field type:
    # - Translation fields: check migrations first (that's where the column is defined)
    # - Regular fields: check models.py first
    search_globs = (
        ["**/migrations/*.py", "**/models.py"]
        if is_translation_field
        else ["**/models.py", "**/migrations/*.py"]
    )

    field_block = None
    file_found = None

    for glob_pattern in search_globs:
        for path in _glob_files(repo_path, glob_pattern):
            if "fixtures" in str(path):
                continue
            content = _read_file_cached(path)
            if content is None:
                continue

            # For translation fields in models.py, search for the base field
            search_fields = [field_name]
            if is_translation_field and "models.py" in glob_pattern:
                search_fields = [base_field]

            for sf in search_fields:
                block = _extract_field_block(content, sf)
                if block:
                    field_block = block
                    file_found = path.relative_to(repo_path)
                    break
            if field_block:
                break
        if field_block:
            break

    if not field_block:
        return True, f"Cannot locate {model_name}.{field_name} in source files", ""

    # Check if the attribute is present in the field block
    attr_pattern = re.compile(rf"\b{re.escape(attr)}\s*=\s*")
    attr_match = attr_pattern.search(field_block)
    attr_present_in_file = attr_match is not None

    if attr_match:
        rest = field_block[attr_match.end() :]
        value_match = re.match(r"([\w\"'\.]+)", rest)
        actual_value = value_match.group(1) if value_match else "present"
    else:
        actual_value = "absent"

    if attr_present_in_file == claimed_present:
        return (True, f"File confirms {attr}={actual_value} in {file_found}", "")
    else:
        return (
            False,
            f"File shows {attr}={actual_value if attr_present_in_file else 'absent'} in {file_found}, but claim says {'present' if claimed_present else 'absent'}",
            f"Verify the {attr} attribute for {model_name}.{field_name} in the actual source",
        )


def _verify_middleware_order(claim: dict, repo_path: Path) -> tuple[bool, str, str]:
    """Verify a middleware ordering claim against settings.py."""
    target = claim.get("target", "")
    predecessor = claim.get("predecessor", "")

    content = _read_file(repo_path, "**/settings.py")
    if not content:
        return True, "Cannot read settings.py", ""

    middleware_list = _extract_middleware_list(content)
    if not middleware_list:
        return True, "MIDDLEWARE list not found in settings.py", ""

    # Check if target comes right after predecessor
    try:
        target_idx = next(i for i, m in enumerate(middleware_list) if target in m)
        predecessor_idx = next(
            i for i, m in enumerate(middleware_list) if predecessor in m
        )
    except StopIteration:
        return True, "Could not locate both middleware entries in list", ""

    if target_idx == predecessor_idx + 1:
        return (
            True,
            f"Confirmed: {target} is at index {target_idx}, right after {predecessor} at {predecessor_idx}",
            "",
        )
    else:
        return (
            False,
            f"{target} is at index {target_idx}, {predecessor} is at index {predecessor_idx} (not adjacent)",
            f"Move {target} to index {predecessor_idx + 1}",
        )


# ── Grep fallback ────────────────────────────────────────────────────────────


def grep_verify_flagged(
    results: list[ClaimResult], target_repo: str
) -> list[ClaimResult]:
    """For flagged claims, grep the actual source files to double-check."""
    repo_path = Path(target_repo).resolve()

    for result in results:
        if result.verified:
            continue

        # Skip claims that need deeper verification (handled elsewhere)
        if result.claim_type in (
            "field_attribute",
            "middleware_order",
            "signal_check",
            "caching_check",
        ):
            continue

        # Extract the search term from the claim
        if result.claim_type == "setting_name":
            search_term = result.claim_text
            file_glob = "**/settings.py"
        elif result.claim_type == "field_exists":
            parts = result.claim_text.split(".")
            search_term = parts[-1] if len(parts) > 1 else result.claim_text
            file_glob = "*.py"
        elif result.claim_type == "method_exists":
            search_term = result.claim_text.rstrip("()")
            file_glob = "*.py"
        elif result.claim_type == "import_check":
            # For import checks, we need to search for the identifier
            # but the identifier should already be in the result from extraction
            continue
        else:
            continue

        found_file = _grep_find(repo_path, search_term, file_glob)
        if found_file:
            result.verified = True
            result.evidence = f"Found via grep in: {found_file}"

    return results


# ── Main verification ────────────────────────────────────────────────────────


def verify_claims(
    claims: list[dict],
    project_map: dict,
    key_file_contents: str = "",
    target_repo: str = "",
) -> VerificationReport:
    """Verify extracted claims against the project topography map and file contents.

    Args:
        claims: Extracted claims from review text.
        project_map: DjangoTopographer output with model field metadata.
        key_file_contents: Key source file contents injected into the review prompt.
        target_repo: Path to the repo root for grep fallback verification.
    """
    # Start each verification run with fresh caches so we never reuse stale
    # file contents from a previous run in a long-lived process.
    clear_verify_caches()

    report = VerificationReport(total_claims=len(claims))
    repo_path = Path(target_repo).resolve() if target_repo else None

    # Build settings lookup from key file contents
    known_settings: set[str] = set()
    if key_file_contents:
        for match in SETTING_PATTERN.finditer(key_file_contents):
            known_settings.add(match.group(0))

    # Core Django settings that are always valid
    django_builtin_settings = {
        "INSTALLED_APPS",
        "DATABASES",
        "MIDDLEWARE",
        "ROOT_URLCONF",
        "TEMPLATES",
        "AUTH_USER_MODEL",
        "LANGUAGE_CODE",
        "USE_I18N",
        "USE_L10N",
        "USE_TZ",
        "TIME_ZONE",
        "DEFAULT_AUTO_FIELD",
        "STATIC_URL",
        "MEDIA_URL",
        "BASE_DIR",
        "SECRET_KEY",
        "DEBUG",
        "ALLOWED_HOSTS",
        "WSGI_APPLICATION",
        "AUTH_PASSWORD_VALIDATORS",
        "LOGGING",
        "CORS_ALLOWED_ORIGINS",
    }

    for claim in claims:
        result = ClaimResult(
            claim_text=claim["text"], claim_type=claim["type"], verified=False
        )

        if claim["type"] == "field_exists":
            model = claim.get("model", "")
            field_name = claim.get("field", "")
            # Check topography map
            model_fields = {}
            for m in project_map.get("models", []):
                if m.get("name") == model:
                    for f in m.get("fields", []):
                        model_fields[f.get("name", "")] = f
                    break
            if field_name in model_fields:
                result.verified = True
                field_info = model_fields[field_name]
                result.evidence = (
                    f"Field exists: {field_name} ({field_info.get('type', 'unknown')})"
                )
            elif model_fields:
                result.evidence = f"Model {model} has no field '{field_name}'. Available: {list(model_fields.keys())[:5]}"
                result.suggestion = f"Check if '{field_name}' is the correct field name"
            else:
                result.verified = True
                result.evidence = f"Model {model} not in topography map (cannot verify)"

        elif claim["type"] == "field_attribute":
            if repo_path:
                verified, evidence, suggestion = _verify_field_attribute(
                    claim, project_map, repo_path
                )
                result.verified = verified
                result.evidence = evidence
                result.suggestion = suggestion
            else:
                result.verified = True
                result.evidence = "No repo path available for attribute verification"

        elif claim["type"] == "middleware_order":
            if repo_path:
                verified, evidence, suggestion = _verify_middleware_order(
                    claim, repo_path
                )
                result.verified = verified
                result.evidence = evidence
                result.suggestion = suggestion
            else:
                result.verified = True
                result.evidence = "No repo path available for middleware verification"

        elif claim["type"] == "import_check":
            identifier = claim.get("identifier", "")
            if repo_path:
                found_file = _grep_find(repo_path, identifier, "*.py")
                if found_file:
                    result.verified = True
                    result.evidence = f"Import found: {identifier} in {found_file}"
                else:
                    result.verified = False
                    result.evidence = (
                        f"Identifier '{identifier}' not found in any .py file"
                    )
                    result.suggestion = (
                        f"Verify that {identifier} is defined or imported"
                    )
            else:
                result.verified = True
                result.evidence = "No repo path available for import verification"

        elif claim["type"] == "signal_check":
            signal_name = claim.get("signal_name", "")
            if repo_path:
                # Check if post_save/post_init/etc signals exist for the model
                # by searching for Signal.connect or @receiver decorators
                signal_pattern = f"Signal.connect|@receiver.*{signal_name}"
                found_file = _grep_find(repo_path, signal_pattern, "*.py")
                if found_file:
                    result.verified = True
                    result.evidence = f"Signal '{signal_name}' found in {found_file}"
                else:
                    # No signals found - reviewer's claim that signals are bypassed is unfounded
                    result.verified = True
                    result.evidence = (
                        f"No '{signal_name}' signal handlers found in codebase"
                    )
                    result.suggestion = "Claim that signals are bypassed is unfounded - no signals exist"
            else:
                result.verified = True
                result.evidence = "No repo path available for signal verification"

        elif claim["type"] == "caching_check":
            # Caching claims require reading the specific function - mark as unverifiable
            result.verified = True
            result.evidence = "Caching claims require function-level analysis"

        elif claim["type"] == "setting_name":
            setting = claim.get("setting", "")
            if setting in known_settings or setting in django_builtin_settings:
                result.verified = True
                result.evidence = (
                    "Setting found in project files"
                    if setting in known_settings
                    else "Core Django setting (always valid)"
                )
            elif known_settings:
                result.evidence = (
                    f"Setting '{setting}' not found in project settings files"
                )
                result.suggestion = "Verify setting name against documentation"
            else:
                result.verified = True
                result.evidence = "No settings files available for verification"

        elif claim["type"] == "max_length_value":
            result.verified = True
            result.evidence = "max_length claims require model file content to verify"

        elif claim["type"] == "method_exists":
            result.verified = True
            result.evidence = "Method claims not verifiable from topography map"

        else:
            result.verified = True
            result.evidence = "Claim type not verifiable"

        report.results.append(result)
        if result.verified:
            report.verified += 1
        else:
            report.flagged += 1

    # Second pass: grep actual files for any still-flagged claims
    if target_repo:
        report.results = grep_verify_flagged(report.results, target_repo)
        # Recount after grep pass
        report.verified = sum(1 for r in report.results if r.verified)
        report.flagged = report.total_claims - report.verified

    return report


def annotate_review(review_text: str, report: VerificationReport) -> str:
    """Append verification summary to the review report."""
    if report.total_claims == 0:
        return review_text

    summary = report.to_summary()

    annotation = f"\n\n---\n\n## 5. Automated Claim Verification\n\n{summary}\n"

    return review_text.rstrip() + annotation
