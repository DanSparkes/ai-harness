# ADR 0003: Multi-Provider LLM Integration — Implementation Plan

## 1. Codebase Target Map

### New Files (Create)
| File | Purpose |
|------|---------|
| `memores/external/__init__.py` | Package init for new `external` module |
| `memores/external/llm_provider.py` | BaseProtocol / ABC definition for LLM providers |
| `memores/external/self_hosted_provider.py` | New OpenAI-compatible API client for self-hosted models |
| `memores/jobs/llm_job.py` | New Celery task with `@shared_task(bind=True)` decorator — primary implementation |
| `memores/migrations/0001_add_provider_prefix_to_model.py` | Data migration to prefix existing model values (user will rename to correct number) |

### Modified Files
| File | Change |
|------|--------|
| `memores/settings.py` | Add `SELF_HOSTED_*` env vars and `SELF_HOSTED_MODELS` dict; add to `.env.example` |
| `memores/external/claude_api.py` | Refactor: extract `AnthropicProvider` class logic, keep legacy `ClaudeAPI` as backward-compat delegate |
| `memores/jobs/claude_ai_job.py` | Add legacy wrapper that delegates to `perform_llm_job`; keep `@shared_task(bind=True) claude_ai_job` for rolling deploy compatibility |
| `memores/serializers/prompt_template_serializers.py` | Update `PromptTemplateCreateUpdateSerializer.validate_model()` with provider-aware validation + backward-compat shim |
| `memores/admin.py` | Add `provider_display` derived property and custom `SimpleListFilter` to `PromptTemplateAdmin` |
| `memores/views/admin/config.py` | Add new `/api/v1/admin/models/config/` endpoint returning nested structure; keep legacy `get_claude_models_config` with deprecation header |
| `memores/urls/admin.py` (or main URL conf) | Add URL route for new models config endpoint |

### Test Files (Update Existing)
| File | Change |
|------|--------|
| `tests/external/test_claude_api.py` | Update to test `AnthropicProvider.make_request` instead of direct `ClaudeAPI` instantiation |
| `tests/serializers/test_prompt_template_serializers.py` | Add tests for self-hosted model validation, legacy bare-name fallback |
| `tests/views/admin/test_prompt_template.py` | Update model list construction to use nested provider structure |
| `tests/views/admin/test_claude_models_config.py` | Test new nested response shape and deprecation header on legacy endpoint |

### Test Files (Create New)
| File | Purpose |
|------|---------|
| `tests/external/test_llm_provider.py` | Unit tests for `parse_model_string()` edge cases (~50 lines) |
| `tests/external/test_provider_factory.py` | Tests for `get_provider()` factory dispatch, bare-name fallback (~80 lines) |
| `tests/external/test_self_hosted_provider.py` | Unit tests for `SelfHostedProvider` request building, response parsing (~150 lines) |
| `tests/jobs/test_llm_job_routing.py` | Integration test: Celery job routes to correct provider based on `prompt_template.model` (~60 lines) |
| `tests/integration/test_provider_routing_e2e.py` | End-to-end: API request → serializer → Celery task → factory dispatch → provider execution (~120 lines) |
| `tests/migrations/test_data_migration.py` | Migration idempotency, edge cases (NULL, empty, already-prefixed), rollback test (~80 lines) |

---

## 2. Architecture & Design

### Provider Abstraction Layer

```python
# memores/external/llm_provider.py
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from memores.models import PromptTemplate, User


class MessageType(TypedDict):
    role: str
    content: str


class LLMProvider(Protocol):
    """Protocol defining the interface for all LLM providers."""

    def make_request(
        self,
        system_prompt: str,
        prompt: str,
        job_id: str,
        stream: bool = False,
        previous_messages: list[MessageType] | None = None,
    ) -> tuple[str | None, "AnalysisOutput"]: ...


class AnthropicProvider:
    """Wraps existing ClaudeAPI logic as a provider implementation."""

    def __init__(self, user: User, prompt_template: PromptTemplate):
        self.user = user
        self.prompt_template = prompt_template

    async def make_request(
        self,
        system_prompt: str,
        prompt: str,
        job_id: str,
        stream: bool = False,
        previous_messages: list[MessageType] | None = None,
    ) -> tuple[str | None, "AnalysisOutput"]:
        # Delegates to existing ClaudeAPI.make_request logic
        ...


class SelfHostedProvider:
    """OpenAI-compatible API client for self-hosted LLM servers."""

    def __init__(self, user: User, prompt_template: PromptTemplate):
        self.user = user
        self.prompt_template = prompt_template

    async def make_request(
        self,
        system_prompt: str,
        prompt: str,
        job_id: str,
        stream: bool = False,
        previous_messages: list[MessageType] | None = None,
    ) -> tuple[str | None, "AnalysisOutput"]:
        # Translates Anthropic-style messages to OpenAI chat completions format
        ...


PROVIDER_REGISTRY: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "self_hosted": SelfHostedProvider,
}


def get_provider(user: User, prompt_template: PromptTemplate) -> LLMProvider:
    """Resolve the LLM provider for a given prompt template.

    Supports legacy bare model names (e.g. "claude-sonnet-4-6") by
    defaulting to the "anthropic" provider. This backward-compat shim
    can be removed after the data migration prefixes all rows.
    """
    if not prompt_template.model:
        raise ValueError("PromptTemplate has no model set.")

    model_value = prompt_template.model.strip()

    # Backward-compat: bare model names default to anthropic
    if "/" not in model_value:
        provider_key = "anthropic"
        model_name = model_value
    else:
        provider_key, model_name = parse_model_string(model_value)

    provider_cls = PROVIDER_REGISTRY.get(provider_key)
    if provider_cls is None:
        raise ValueError(f"Unknown provider: '{provider_key}'")
    return provider_cls(user, prompt_template)


def parse_model_string(model: str) -> tuple[str, str]:
    """Parse 'provider/model-name' into (provider, model_name).

    Splits on the first '/' only, so model names containing slashes
    (e.g. 'self_hosted/my-org/llama-3') are handled correctly — the
    provider is 'self_hosted' and the model name is 'my-org/llama-3'.

    Raises ValueError if the format is invalid.
    """
    model = model.strip()
    if "/" not in model:
        raise ValueError(
            f"Invalid model format '{model}'. Expected 'provider/model-name'."
        )
    provider, model_name = model.split("/", 1)
    provider = provider.strip()
    model_name = model_name.strip()
    if not provider or not model_name:
        raise ValueError(
            f"Invalid model format '{model}'. Both provider and model name must be non-empty."
        )
    return provider, model_name
```

### Celery Job Layer Update

```python
# memores/jobs/llm_job.py (new file)
import logging
from typing import TYPE_CHECKING, NamedTuple

from celery import shared_task
from django.contrib.auth import get_user_model

from memores.external.llm_provider import get_provider
from memores.models import AnalysisOutput, PromptTemplate
from memores.services.ws_push import publish_job_update

if TYPE_CHECKING:
    from memores.models import User
else:
    User = get_user_model()


class LLMJobResult(NamedTuple):
    response: str
    analysis_output: AnalysisOutput


def perform_llm_job(
    job_id: str,
    name: str,
    user_id: str,
    prompt_template_id: str,
    system_prompt: str,
    prompt: str,
    stream: bool = False,
    previous_messages: list | None = None,
) -> LLMJobResult:
    """Sends the given prompt to the appropriate LLM provider and parses the response."""
    import time

    start_time = time.perf_counter()
    logging.info(f"[JOB] [{name}] job started {job_id} by {user_id=}")

    user = User.objects.get(pk=user_id)
    prompt_template = PromptTemplate.objects.get(id=prompt_template_id)
    prompt_name = prompt_template.name

    logging.info(f"[JOB] [{name}] job started {job_id}, {prompt_name=} by {user=}")
    logging.info(
        f"[JOB] [{name}] system_prompt={len(system_prompt)}, prompt={len(prompt)}"
    )

    provider = get_provider(user, prompt_template)

    try:
        response, analysis_output = provider.make_request(
            system_prompt,
            prompt,
            job_id,
            stream,
            previous_messages,
        )
    except Exception as e:
        logging.error(f"[JOB] [{name}] exception {prompt_name=}, {user=}: {str(e)}")
        raise

    elapsed_seconds = round(time.perf_counter() - start_time, 2)
    logging.info(f"[JOB] [{name}] job {job_id} finished in {elapsed_seconds}s")
    logging.info(f"[JOB] [{name}] job {job_id} {analysis_output.id=}")

    if not response:
        logging.error(f"[JOB] [{name}] no response from LLM, {prompt_name=}, {user=}")
        raise Exception("No response from LLM")

    return LLMJobResult(response=response, analysis_output=analysis_output)


@shared_task(bind=True)
def llm_job(
    self,
    user_id: str,
    prompt_template_id: str,
    system_prompt: str,
    prompt: str,
    stream: bool = False,
    previous_messages: list | None = None,
):
    """Primary Celery task for LLM inference — routes to appropriate provider."""
    job_id = self.request.id
    name = "LLM"

    try:
        result = perform_llm_job(
            job_id, name, user_id, prompt_template_id, system_prompt, prompt, stream, previous_messages,
        )

        publish_job_update(job_id, name, result=result.analysis_output.id)
        return {"analysis_output_id": result.analysis_output.id}

    except Exception as e:
        logging.error(f"[JOB] [{name}] job {job_id} failed for {user_id=}: {str(e)}")
        publish_job_update(job_id, name, error=str(e))
        raise
```

### Legacy Wrapper (Backward Compatibility)

```python
# memores/jobs/claude_ai_job.py — add at end of file
@shared_task(bind=True, name="claude_ai_job")
def claude_ai_job_legacy(
    self,
    user_id: str,
    prompt_template_id: str,
    system_prompt: str,
    prompt: str,
    stream: bool = False,
    previous_messages: list | None = None,
):
    """Legacy task name — delegates to llm_job. Remove after all workers upgraded."""
    from memores.jobs.llm_job import perform_llm_job

    job_id = self.request.id
    name = "CLAUDE"

    try:
        result = perform_llm_job(
            job_id, name, user_id, prompt_template_id, system_prompt, prompt, stream, previous_messages,
        )

        publish_job_update(job_id, name, result=result.analysis_output.id)
        return {"analysis_output_id": result.analysis_output.id}

    except Exception as e:
        logging.error(f"[JOB] [{name}] legacy job {job_id} failed for {user_id=}: {str(e)}")
        publish_job_update(job_id, name, error=str(e))
        raise
```

### Serializer Validation Update

```python
# memores/serializers/prompt_template_serializers.py — update validate_model()
def validate_model(self, value):
    if not value:
        return value

    # Backward-compat shim: accept bare model names (pre-migration)
    if "/" not in value:
        provider, model_name = "anthropic", value
    else:
        try:
            from memores.external.llm_provider import parse_model_string
            provider, model_name = parse_model_string(value)
        except ValueError as e:
            raise serializers.ValidationError(str(e))

    # Look up correct model dictionary based on provider
    settings = django_settings  # import at top of file
    model_dictionaries = {
        "anthropic": settings.CLAUDE_MODELS,
        "self_hosted": getattr(settings, 'SELF_HOSTED_MODELS', {}),
    }

    allowed_models = model_dictionaries.get(provider)
    if allowed_models is None:
        raise serializers.ValidationError(
            f"Unknown provider '{provider}'. Allowed providers: {list(model_dictionaries.keys())}"
        )

    if model_name not in allowed_models:
        allowed_names = list(allowed_models.keys())
        raise serializers.ValidationError(
            f"Invalid model '{model_name}' for provider '{provider}'. "
            f"Allowed models: {allowed_names}"
        )

    return value
```

### Data Migration Pattern

```python
# memores/migrations/00XX_add_provider_prefix_to_model.py
from django.db import migrations
from django.db.models import F, Value


def prefix_anthropic_models(apps, schema_editor):
    """Prefix all existing non-null model values with 'anthropic/'.

    Uses a single bulk UPDATE instead of row-by-row iteration for performance.
    print() is intentional — Django migrations log to stdout.
    """
    PromptTemplate = apps.get_model('memores', 'PromptTemplate')

    updated_count = (
        PromptTemplate.objects
        .exclude(model__isnull=True)
        .exclude(model='')
        .exclude(model__startswith='anthropic/')
        .update(model=Concat(Value('anthropic/'), F('model')))
    )

    print(f"Updated {updated_count} PromptTemplate rows with 'anthropic/' prefix")


def reverse_prefix(apps, schema_editor):
    """Reverse: strip 'anthropic/' prefix from all model values.

    Uses Substring to strip the prefix in a single bulk UPDATE.
    print() is intentional — Django migrations log to stdout.
    """
    from django.db.models.functions import Substr

    PromptTemplate = apps.get_model('memores', 'PromptTemplate')

    updated_count = (
        PromptTemplate.objects
        .exclude(model__isnull=True)
        .exclude(model='')
        .filter(model__startswith='anthropic/')
        .update(model=Substr(F('model'), len('anthropic/') + 1))
    )

    print(f"Reversed prefix on {updated_count} PromptTemplate rows")


class Migration(migrations.Migration):

    dependencies = [
        ('memores', 'XXXX_previous_migration'),  # Update to actual latest migration
    ]

    operations = [
        migrations.RunPython(prefix_anthropic_models, reverse_prefix),
    ]
```

---

## 3. Risk Assessment & Mitigations

### Backwards Compatibility Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Legacy bare model names break validation** | Existing templates with `model="claude-sonnet-4-6"` fail serializer validation after migration | Backward-compat shim in `validate_model()` accepts bare names and defaults to `anthropic` provider until data migration runs |
| **Data migration fails mid-execution** | Mixed state (some rows prefixed, some not) causes inconsistent routing | Migration is idempotent — re-run works on already-unprefixed rows; `get_provider()` shim handles both formats during transition |
| **Celery task rename breaks workers** | Old workers still expect `claude_ai_job` task name | Keep legacy wrapper with `@shared_task(bind=True, name="claude_ai_job")` that delegates to new implementation; remove after 2 deployment cycles |

### Migration Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Migration touches all rows** | Potential downtime or slow execution on large datasets | Run in staging first with ≥100 templates; use `update_fields` to minimize DB load; monitor execution time |
| **Rollback data corruption** | Reverse migration fails partway through | Backup `model` column before migration: `SELECT id, model FROM memores_prompttemplate INTO OUTFILE '/tmp/model_backup.csv'`; reverse migration is idempotent |

### Performance Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Self-hosted latency increase** | p95 latency >30s causes user-facing timeouts | Configure `SELF_HOSTED_TIMEOUT_SECONDS=60`; set Celery `soft_time_limit=120` for LLM jobs; monitor and alert on latency thresholds |
| **Token counting inaccuracy** | Self-hosted APIs may return different token counts or none | Implement provider-specific token extraction in each provider class; normalize to common `TokenUsage` dataclass; add integration test asserting non-zero counts |

### Security Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Self-hosted endpoint exposed** | Network exposure of internal API | Require mTLS or network policy restricting access to API pods; document auth strategy in runbook; audit log all requests |
| **Credential management** | `SELF_HOSTED_API_KEY` stored insecurely | Use env vars (not DB); if key is optional, document when it's required vs. not needed for local auth |

### Operational Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Team lacks GPU ops capacity** | Assumption #4 fails — cannot maintain self-hosted infrastructure | Validate with infra lead before Step 7 gate; require documented runbook covering deployment, health checks, scaling, logs, backup, security patching |
| **Self-hosted model version drift** | Model weights change on update, making results non-reproducible | Pin model versions in `SELF_HOSTED_MODELS` settings (e.g., `llama-3-70b-v1.2`); record resolved version in `AnalysisOutput.metadata.model_version` at request time; enforce via CI check |
| **Structured output unsupported** | Self-hosted server lacks OpenAI-compatible structured output | Implement schema support as optional; fall back to prompt-based JSON extraction using `json_repair`; log which templates use fallback for monitoring |

### Celery Retry Safety (CRITICAL)

**Rule:** If adding `autoretry_for` to a Celery task, check the task's error handler. AUTORETRY_FOR IS UNSAFE if the except block: (1) sets status=ERROR on a model, (2) calls `.save()`, (3) re-raises. On retry the task re-executes from the top, hits `if status != PENDING: raise ValueError`, and permanently fails.

**Current State:** None of the existing Celery tasks in this codebase use `autoretry_for`. They use manual retry patterns or no retries. The new `llm_job` task does not need `autoretry_for` — it uses tenacity retry on just the API call in the task body instead (for self-hosted provider).

**Decision:** Use **Option B** — tenacity retry on just the API call in the task body, not Celery-level `autoretry_for`. This avoids the state-modification safety issue entirely. The self-hosted provider will use tenacity for HTTP retries, while Anthropic provider uses existing ClaudeAPI retry logic.

---

## 4. Implementation Pipeline

```json
{
  "feature_name": "ADR-0003-Multi-Provider-LLM",
  "target_workspace": "/Users/dansparkes/memores/memores-api",
  "pipeline": [
    {
      "step": 1,
      "name": "Add SELF_HOSTED settings to memores/settings.py",
      "target_file": "memores/settings.py",
      "task": "Import env and env.json from django-environ at the top of the file. Add new environment variable definitions after the existing ANTHROPIC_* settings block: SELF_HOSTED_API_BASE_URL = env('SELF_HOSTED_API_BASE_URL', default=None), SELF_HOSTED_API_KEY = env('SELF_HOSTED_API_KEY', default=None), SELF_HOSTED_MODELS = env.json('SELF_HOSTED_MODELS', default={}), SELF_HOSTED_MAX_RETRIES = env.int('SELF_HOSTED_MAX_RETRIES', default=3). Note: SELF_HOSTED_MODELS is a JSON dict mapping model IDs to display names (e.g. {\"llama-3-70b\": \"Llama 3 70B\"}). There is no single SELF_HOSTED_MODEL setting — the default model is determined at request time by the prompt_template.model field. Also add to .env.example file: SELF_HOSTED_API_BASE_URL=http://localhost:8000/v1, SELF_HOSTED_API_KEY=optional-key, SELF_HOSTED_MODELS={\"llama-3-70b\": \"Llama 3 70B\", \"mistral-7b\": \"Mistral 7B\"}, SELF_HOSTED_MAX_RETRIES=3.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 3
    },
    {
      "step": 2,
      "name": "Create LLM provider base protocol and factory in memores/external/llm_provider.py",
      "target_file": "memores/external/llm_provider.py",
      "task": "Create new file memores/external/llm_provider.py with the following content: Import Protocol, TypedDict from typing. Define MessageType TypedDict with role (str) and content (str). Define LLMProvider Protocol class with make_request method signature: def make_request(self, system_prompt: str, prompt: str, job_id: str, stream: bool = False, previous_messages: list[MessageType] | None = None) -> tuple[str | None, AnalysisOutput]: ... . Define parse_model_string(model: str) function that splits on first '/', strips whitespace, validates non-empty provider and model_name, raises ValueError for invalid format. Define PROVIDER_REGISTRY dict mapping 'anthropic' to AnthropicProvider class and 'self_hosted' to SelfHostedProvider class (these classes will be defined in separate files). Define get_provider(user: User, prompt_template: PromptTemplate) function that parses the model string from prompt_template.model, applies backward-compat shim for bare names (defaults to 'anthropic'), looks up PROVIDER_REGISTRY, instantiates and returns provider. Add TYPE_CHECKING guards for User and PromptTemplate imports.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 3
    },
    {
      "step": 3,
      "name": "Refactor ClaudeAPI to extract AnthropicProvider in memores/external/claude_api.py",
      "target_file": "memores/external/claude_api.py",
      "task": "Open memores/external/claude_api.py. Create new class AnthropicProvider that wraps existing ClaudeAPI logic: move __init__ to take user and prompt_template as parameters, move make_request method (preserving all internal _build_data_and_metadata, _perform_request, _parse_request_data helpers), add async def make_request with same signature as LLMProvider Protocol. Keep existing ClaudeAPI class as thin delegate: claude_api.py should define ClaudeAPI(user, prompt_template) = AnthropicProvider(user, prompt_template). Add logging to AnthropicProvider.make_request to log provider='anthropic' before request. Ensure MessageType TypedDict is still defined in this file (or moved to llm_provider.py if circular imports arise — prefer keeping here per ADR note).",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax", "run_mypy"],
      "max_attempts": 4
    },
    {
      "step": 4,
      "name": "Create SelfHostedProvider in memores/external/self_hosted_provider.py",
      "target_file": "memores/external/self_hosted_provider.py",
      "task": "Create new file memores/external/self_hosted_provider.py. Import AnthropicProvider from claude_api for MessageType reference, import settings from django.conf, import tenacity and httpx (or requests). Define SelfHostedProvider class with __init__(self, user, prompt_template) storing user and prompt_template. Implement async def make_request with same signature as LLMProvider Protocol: translate system_prompt + prompt to OpenAI chat completions format (messages array with role='system' and role='user'), map output_schema to response_format if supported (try POST /v1/chat/completions with response_format parameter, fall back to json_repair if 400 returned), use tenacity.retry for HTTP retries with max_attempts=settings.SELF_HOSTED_MAX_RETRIES, extract token counts from response usage field and pass to track_usage(), return tuple of (response_text, AnalysisOutput). Add logging for provider='self_hosted' before request. Handle SELF_HOSTED_API_KEY in headers if set.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 5,
      "name": "Update PromptTemplateCreateUpdateSerializer.validate_model() in memores/serializers/prompt_template_serializers.py",
      "target_file": "memores/serializers/prompt_template_serializers.py",
      "task": "Open memores/serializers/prompt_template_serializers.py. Locate the validate_model method on PromptTemplateCreateUpdateSerializer class. Replace existing validation logic with: if not value: return value. Add backward-compat shim: if '/' not in value: provider, model_name = 'anthropic', value else: try: from memores.external.llm_provider import parse_model_string; provider, model_name = parse_model_string(value) except ValueError as e: raise serializers.ValidationError(str(e)). Look up correct model dictionary: settings = django_settings (import at top); model_dictionaries = {'anthropic': settings.CLAUDE_MODELS, 'self_hosted': getattr(settings, 'SELF_HOSTED_MODELS', {})}; allowed_models = model_dictionaries.get(provider). If allowed_models is None: raise serializers.ValidationError(f\"Unknown provider '{provider}'. Allowed providers: {list(model_dictionaries.keys())}\"). If model_name not in allowed_models: raise serializers.ValidationError(f\"Invalid model '{model_name}' for provider '{provider}'. Allowed models: {list(allowed_models.keys())}\"). Return value.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 6,
      "name": "Create data migration to prefix model values in memores/migrations/",
      "target_file": "memores/migrations/0001_add_provider_prefix_to_model.py",
      "task": "Create new file memores/migrations/0001_add_provider_prefix_to_model.py. Import F and Value from django.db.models, Substr and Concat from django.db.models.functions. Define forward function prefix_anthropic_models(apps, schema_editor): use a single bulk UPDATE with PromptTemplate.objects.exclude(model__isnull=True).exclude(model='').exclude(model__startswith='anthropic/').update(model=Concat(Value('anthropic/'), F('model'))); print the updated_count. Define reverse function reverse_prefix(apps, schema_editor): use PromptTemplate.objects.exclude(model__isnull=True).exclude(model='').filter(model__startswith='anthropic/').update(model=Substr(F('model'), len('anthropic/') + 1)); print the updated_count. Note: print() is intentional — Django migrations log to stdout. Define Migration class with dependencies = [('memores', '0001_initial')], operations = [migrations.RunPython(prefix_anthropic_models, reverse_prefix)]. Use 0001 as placeholder — the user will rename to the correct number before running.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 7,
      "name": "Create new llm_job Celery task in memores/jobs/llm_job.py",
      "target_file": "memores/jobs/llm_job.py",
      "task": "Create new file memores/jobs/llm_job.py. Import logging, time from standard lib, TYPE_CHECKING and NamedTuple from typing, shared_task from celery, get_user_model from django.contrib.auth. Define ClaudeAIStringResult NamedTuple with response (str) and analysis_output (AnalysisOutput). Implement perform_llm_job(job_id: str, name: str, user_id: str, prompt_template_id: str, system_prompt: str, prompt: str, stream: bool = False, previous_messages: list | None = None) -> ClaudeAIStringResult function that: gets User and PromptTemplate objects, logs job start, calls get_provider(user, prompt_template).make_request(system_prompt, prompt, job_id, stream, previous_messages), handles exception logging, calculates elapsed time, validates response is not empty, returns ClaudeAIStringResult. Define @shared_task(bind=True) def llm_job(self, user_id: str, prompt_template_id: str, system_prompt: str, prompt: str, stream: bool = False, previous_messages: list | None = None): extracts job_id from self.request.id, calls perform_llm_job, publishes job update via publish_job_update(job_id, name, result=analysis_output.id), returns dict with analysis_output_id. Add logging for provider routing decision.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 8,
      "name": "Add legacy wrapper to memores/jobs/claude_ai_job.py for backward compatibility",
      "target_file": "memores/jobs/claude_ai_job.py",
      "task": "Open memores/jobs/claude_ai_job.py. Locate the existing @shared_task(bind=True) def claude_ai_job function. Add new function at end of file: @shared_task(bind=True, name='claude_ai_job_legacy') def claude_ai_job_legacy(self, user_id: str, prompt_template_id: str, system_prompt: str, prompt: str, stream: bool = False, previous_messages: list | None = None): 'Legacy task name — delegates to llm_job. Remove after all workers upgraded.' Extract job_id from self.request.id, import perform_llm_job from memores.jobs.llm_job, call perform_llm_job(job_id, 'CLAUDE', user_id, prompt_template_id, system_prompt, prompt, stream, previous_messages), publish_job_update(job_id, 'CLAUDE', result=analysis_output.id), return dict with analysis_output_id. Handle exceptions with logging and publish_job_update error. Keep original claude_ai_job function unchanged for backward compatibility during rolling deploy. Add comment: '# NOTE: Original claude_ai_job kept for backward compat — new workers use llm_job'.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 3
    },
    {
      "step": 9,
      "name": "Update memores/admin.py with provider_display and list_filter",
      "target_file": "memores/admin.py",
      "task": "Open memores/admin.py. Locate PromptTemplateAdmin class registration (@admin.register(PromptTemplate)). Add 'provider_display' to list_display tuple (insert after 'model'). Add @admin.display(description='Provider') decorator method: def provider_display(self, obj: PromptTemplate) -> str: if obj.model and '/' in obj.model: return obj.model.split('/', 1)[0]; return 'anthropic'. Create custom SimpleListFilter class ProviderFilter(admin.SimpleListFilter): title = 'Provider'; parameter_name = 'provider'; def lookups(self, request, model_admin): return (('anthropic', 'Anthropic'), ('self_hosted', 'Self-Hosted')); def queryset(self, request, queryset): if self.value() == 'anthropic': return queryset.filter(model__startswith='anthropic/'); elif self.value() == 'self_hosted': return queryset.filter(model__startswith='self_hosted/'); return queryset. Note: the filter value 'self_hosted' uses underscore to match the PROVIDER_REGISTRY key, not the DB prefix which may vary. Add ProviderFilter to list_filter tuple in PromptTemplateAdmin class.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 10,
      "name": "Add new models config endpoint in memores/views/admin/config.py",
      "target_file": "memores/views/admin/config.py",
      "task": "Open memores/views/admin/config.py. Locate existing get_claude_models_config function. Add @api_view(['GET']) decorator if not present. Rename original function to get_claude_models_config_legacy and add deprecation header: response = Response(legacy_format); response['Deprecation'] = 'true'; response['Link'] = '</api/v1/admin/models/config/>; rel=\"successor-version\"'. Create new function get_models_config @api_view(['GET']): build nested dict structure iterating over settings.CLAUDE_MODELS with key format 'anthropic/{model_name}' and label from CLAUDE_MODELS values, then iterate over getattr(settings, 'SELF_HOSTED_MODELS', {}) with key format 'self_hosted/{model_name}'. Return Response({'anthropic': {...}, 'self_hosted': {...}}). Add URL route in memores/urls/admin.py (or the project's main URL conf if admin URLs are centralized): path('api/v1/admin/models/config/', get_models_config, name='admin-models-config').",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 11,
      "name": "Update existing test files for new provider abstraction",
      "target_file": "memores/tests/external/test_claude_api.py",
      "task": "Open memores/tests/external/test_claude_api.py. Locate all test methods that instantiate ClaudeAPI directly (e.g., claude = ClaudeAPI(user, prompt_template)). Replace with: from memores.external.llm_provider import get_provider; provider = get_provider(user, prompt_template); response, analysis_output = await provider.make_request(system_prompt, prompt, job_id). Update assertions to verify provider type is AnthropicProvider.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 12,
      "name": "Create unit tests for llm_provider.py",
      "target_file": "memores/tests/external/test_llm_provider.py",
      "task": "Create memores/tests/external/test_llm_provider.py with tests for parse_model_string function: test_parse_model_string_valid_format, test_parse_model_string_missing_slash_raises_value_error, test_parse_model_string_empty_provider_raises_value_error, test_parse_model_string_empty_model_name_raises_value_error, test_parse_model_string_whitespace_stripped. Use pytest. Import parse_model_string from memores.external.llm_provider.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 13,
      "name": "Create integration test for Celery job routing in memores/tests/jobs/test_llm_job_routing.py",
      "target_file": "memores/tests/jobs/test_llm_job_routing.py",
      "task": "Create memores/tests/jobs/test_llm_job_routing.py with ~60 lines of tests: test_llm_job_routes_to_anthropic_for_anthropic_model (mock AnthropicProvider.make_request, assert called), test_llm_job_routes_to_self_hosted_for_self_hosted_model (mock SelfHostedProvider.make_request, assert called), test_legacy_claude_ai_job_delegates_to_perform_llm_job. Use pytest-mock to patch get_provider or provider classes. Verify that the correct provider is instantiated based on prompt_template.model field value. Run pytest tests/jobs/test_llm_job_routing.py -v to verify routing logic works correctly.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 14,
      "name": "Create end-to-end integration test for provider routing in memores/tests/integration/test_provider_routing_e2e.py",
      "target_file": "memores/tests/integration/test_provider_routing_e2e.py",
      "task": "Create memores/tests/integration/test_provider_routing_e2e.py with ~120 lines of tests: test_api_request_to_prompt_template_create_with_anthropic_model_routes_correctly, test_api_request_to_prompt_template_create_with_self_hosted_model_validates_correctly, test_celery_task_execution_calls_correct_provider. Use Django test client to POST to /api/v1/admin/prompt-templates/ with model field set to 'anthropic/claude-sonnet-4-6' and 'self-hosted/llama-3-70b'. Mock provider.make_request in each test to verify correct dispatch. Verify serializer validation accepts both formats, data migration prefixes correctly, Celery task routes based on model prefix. Run pytest tests/integration/test_provider_routing_e2e.py -v to verify end-to-end flow.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 15,
      "name": "Verify imports work correctly",
      "target_file": "memores/external/__init__.py",
      "task": "Create or update memores/external/__init__.py to export the public API: from memores.external.llm_provider import get_provider, parse_model_string, LLMProvider, PROVIDER_REGISTRY. This ensures all imports work correctly after the refactoring.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax"],
      "max_attempts": 3
    }
  ]
}
```

---

## Execution Notes

**Order Dependency:** Steps are ordered so earlier steps create dependencies later steps rely on:
1. Settings (Step 1) → needed by serializer validation (Step 5) and self-hosted provider (Step 4)
2. Provider protocol/factory (Steps 2-3) → needed by job layer (Steps 7-8) and tests (Steps 11-14)
3. Data migration (Step 6) → validates before deployment, requires settings to exist
4. Celery task rename (Steps 7-8) → depends on provider abstraction being complete
5. Admin/config updates (Steps 9-10) → independent but require serializer changes to work correctly
6. Tests (Steps 11-14) → validate all previous steps
7. Final validation (Step 15) → ensures everything works together

**Validation Gates:** Before proceeding to production deployment, verify:
- All unit tests pass with >90% coverage on new provider layer
- Data migration runs successfully in staging with ≥100 templates
- Self-hosted health endpoint returns 200 OK (Step 7 gate from ADR)
- Smoke test prompts return valid JSON within 15s p95 latency
- No breaking changes to existing Anthropic-only workflows
