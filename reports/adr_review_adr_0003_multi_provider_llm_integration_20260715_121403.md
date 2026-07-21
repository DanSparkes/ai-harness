# ADR Review Report

**Source:** ADR 0003: Multi-Provider LLM Integration
**Date:** 2026-07-15 12:14
**Model:** ornith:35b

## Rubric Scores

| Metric | Score (1-5) | Weight |
|--------|-------------|--------|

## Review

# ADR Review (Multi-Pass)

## Assumption Validity

# ADR Assumptions Review: Multi-Provider LLM Integration

## Findings

1. **Assumption: "Per-template routing is sufficient — We do not need per-request or per-user provider selection."**
   - **Testable before decision point?** Partially. You could audit existing code paths for per-request/per-user routing needs, but the ADR doesn't document this analysis. The assumption should have been validated by searching `ClaudeAPI` call sites for any dynamic provider logic.
   - **Missing assumptions:** No consideration of content-based routing (e.g., PII detection → route to self-hosted/VPC-only). No consideration of multi-tenant scenarios where different tenants require different providers. No consideration of templates that might need *multiple* providers in a single pipeline (chaining).
   - **Impact if False inaccurate:** The impact says "Simpler approach may suffice" — this is backwards. If the assumption is FALSE (we DO need per-request/per-user routing), the chosen design is inadequate and would require significant rework, not simplification. The impact should state: "Per-template routing cannot satisfy requirements; must add provider selection at request/user level."
   - **Hidden assumptions:** Assumes all templates can be evaluated in isolation without cross-template provider coordination. Assumes a single provider per template is sufficient for all use cases (journaling, coaching, reports, result explanations).

2. **Assumption: "Structured output (`output_schema`) is not universally required — Some templates may not use `output_schema`, so the self-hosted provider can be deployed for those first."**
   - **Testable before decision point?** Yes. This should have been validated by running `PromptTemplate.objects.filter(output_schema__isnull=False).count()` against production data before making this assumption. The ADR doesn't document this query or its results.
   - **Missing assumptions:** Treats structured output as binary (supported/not supported) when in reality there's a spectrum — some providers support flat JSON but not nested schemas, or function calling but not `response_format` with JSON Schema. No consideration of schema complexity (simple key-value vs deeply nested with constraints).
   - **Impact if False accurate:** Correctly identifies that all-templates-requiring-schema would block the self-hosted provider until schema support is confirmed.
   - **Hidden assumptions:** Assumes `output_schema` compatibility can be mapped uniformly across providers, when in practice each provider's "structured output" implementation differs (Anthropic `output_schema`, OpenAI `response_format`, function calling).

3. **Assumption: "Latency is not a hard SLA — The jobs (journaling, coaching, reports) are async via Celery, so a latency increase of 2-5x from self-hosted inference is acceptable during the comparison phase."**
   - **Testable before decision point?** Partially. You can verify Celery task definitions confirm async processing, but you cannot test the *compound* latency impact (e.g., chained jobs, worker pool saturation) without running the experiment. The ADR doesn't enumerate all `ClaudeAPI` consumers to confirm none have implicit latency expectations.
   - **Missing assumptions:** No consideration of Celery worker pool exhaustion if self-hosted requests take 5x longer — this could starve other jobs even if individual latency is "acceptable." No consideration of timeout cascading in dependent job chains (e.g., `detailed_report_job` calling multiple LLM tasks sequentially). No consideration of Redis queue backlog impact.
   - **Impact if False partially accurate:** Identifies that latency-sensitive user-facing paths would disqualify self-hosted, but doesn't enumerate which paths exist or address resource exhaustion as a consequence of increased latency.
   - **Hidden assumptions:** Assumes 2-5x latency increase is uniform across all models/templates, when in reality it may vary wildly (e.g., small models fast, large models slow). Assumes Celery worker count is sufficient to absorb latency variance without degradation.

4. **Assumption: "Team has capacity for self-hosted infrastructure — GPU server provisioning, container orchestration, and monitoring are feasible within the team's existing skills and time."**
   - **Testable before decision point?** Yes. This was testable via team skills audit and capacity review. The ADR even notes it was validated: "Status: FALSE per ADR 0004."
   - **Missing assumptions:** No consideration of *ongoing* maintenance burden (GPU driver updates, model security patches, vLLM/TGI upgrades). Assumes deferring self-hosted doesn't create technical debt in the abstraction design (e.g., `SelfHostedProvider` designed for GPU inference may not map cleanly to other deployment models later).
   - **Impact if False accurate:** Correctly identifies that without capacity, self-hosted should be deferred or outsourced. Notes that the provider abstraction layer remains valuable even without self-hosted (via OpenRouter).
   - **Hidden assumptions:** Assumes that deferring self-hosted indefinitely doesn't require re-architecting the `SelfHostedProvider` if it's later needed for a different deployment model (e.g., serverless GPU, managed inference).

5. **Assumption: "The Anthropic API contract is stable — Anthropic's Messages API format and `output_schema` support will not change in ways that break the abstraction layer."**
   - **Testable before decision point?** No. This is inherently a future-looking assumption that cannot be fully tested beforehand. You can review Anthropic's deprecation notices and changelog, but cannot guarantee stability.
   - **Missing assumptions:** No consideration of pricing model changes (e.g., per-token vs per-request billing affecting cost tracking). No consideration of feature deprecations (e.g., Anthropic removing a field used in the abstraction). No consideration of Anthropic's rate of innovation vs. stability — they may change APIs more frequently than other providers.
   - **Impact if False accurate:** Correctly states that major API changes would require updating `AnthropicProvider` regardless of this ADR (i.e., the abstraction doesn't protect against upstream API changes).
   - **Hidden assumptions:** Assumes the team can respond to API changes quickly without significant cost. Doesn't account for partial deprecations (e.g., Anthropic deprecates a field but keeps it functional for N versions).

6. **Assumption: "OpenRouter availability is sufficient for the comparison phase — OpenRouter's uptime, rate limits, and model catalog are reliable enough to run the A/B comparison without frequent interruptions."**
   - **Testable before decision point?** Partially. You can check OpenRouter's status page history, SLA documentation, and rate limit policies before committing. The ADR doesn't document this due diligence.
   - **Missing assumptions:** No consideration of OpenRouter's model availability (can they actually route to the specific models needed?). No consideration of pricing stability (OpenRouter may change per-token costs mid-comparison). No consideration of terms of service regarding data handling — OpenRouter is a third-party egress point for potentially PII-bearing prompts.
   - **Impact if False accurate:** Correctly identifies that outages/rate limits would block comparison. Mitigation (Anthropic fallback) is sound.
   - **Hidden assumptions:** Assumes OpenRouter's "OpenAI-compatible" endpoint is truly compatible with all models in their catalog — some providers behind OpenRouter may use custom response formats. Assumes rate limits won't be hit during parallel A/B testing of multiple templates simultaneously.

7. **Assumption: "Data format compatibility across providers — OpenRouter's OpenAI-compatible endpoint produces responses normalizable to `AnalysisOutput` without loss of fidelity (thinking content, usage metadata, stop reasons)."**
   - **Testable before decision point?** Yes. This should have been validated by sending a sample prompt to OpenRouter and inspecting the response structure against `AnalysisOutput` requirements. The ADR doesn't document this validation.
   - **Missing assumptions:** No consideration of error response formats — providers return errors in different structures, and the abstraction needs to normalize these too. No consideration of streaming responses (if supported). No quantification of adapter work scope if incompatibilities are found (is it 10 lines or 500?).
   - **Impact if False accurate:** Correctly identifies that response field mismatches require provider-specific adapter logic. Mitigation (validate during Step 4) is appropriate.
   - **Hidden assumptions:** Assumes `AnalysisOutput` schema is flexible enough to absorb provider-specific fields without loss. Doesn't address which specific fields might be lost (e.g., Anthropic's `thinking` content, usage metadata structure differences).

8. **Assumption: "Token counts are comparable across providers — Token counts from OpenRouter are accurate and directly comparable to Anthropic's counts for quality-per-token analysis. Different tokenizers (tiktoken vs. Anthropic's internal tokenizer) may produce different counts for the same text."**
   - **Testable before decision point?** Yes. This should have been validated by running identical prompts through both tokenizers and comparing outputs. The ADR acknowledges the risk but doesn't document that validation was performed.
   - **Missing assumptions:** No consideration of system prompt vs user message tokenization differences across providers. No consideration of special tokens (e.g., Anthropic's `<|begin_of_text|>`, OpenAI's BOS tokens). No consideration of whether "tokens" mean the same thing semantically (e.g., subword units vs character-level counts).
   - **Impact if False accurate:** Correctly identifies that divergent token counts invalidate cross-provider quality-per-token comparisons. Mitigation ("evaluate on output quality, not tokens-per-dollar") is sound and addresses the core concern.
   - **Hidden assumptions:** Assumes "quality per token" is a meaningful comparison metric when it may not be — one provider might use more tokens for formatting but produce better output. Doesn't address whether token count normalization (e.g., mapping to a common tokenizer) would be feasible if needed.

## Alternative Completeness

# ADR Review: Alternatives Considered Section

## Findings

1. **Missing reasonable alternative: Hybrid of E + F (Convention-based routing with schema-enforced validation)**
   The ADR treats options E and F as mutually exclusive, but a hybrid approach — using `provider/model-name` strings for routing (convention alignment) while adding a separate `provider` column for Django admin filtering and serializer validation — was not considered. This would preserve the industry convention benefit of E while addressing the drift risk and admin-filtering concerns raised against it in F's cons. The ADR should explicitly evaluate this hybrid or explain why it doesn't fit (e.g., "dual representation increases maintenance burden without proportional benefit at current scale").

2. **Alternative B dismissal lacks concrete evaluation evidence**
   The "Why not" for the gateway approach states LiteLLM "requires custom configuration that negates much of the convenience advantage," but provides no specific details about what configuration was required, what failed, or why it negated the benefit. This reads as a post-hoc justification rather than an evidence-based evaluation. A stronger dismissal would cite: (a) which specific self-hosted endpoint feature LiteLLM didn't support natively, (b) what custom adapter code would have been needed, and (c) how that adapter complexity compared to the proposed in-house abstraction. The update about OpenRouter being used as a direct provider rather than gateway partially addresses this but doesn't retroactively strengthen the original B evaluation.

3. **Alternatives A, C, and D were not evaluated against all six decision criteria**
   - Alternative A (Feature Flag) was dismissed solely on criterion #1 (per-template routing). It was not evaluated for backward compatibility (#2), extensibility (#3), complexity (#4), cost comparison capability (#5), or implementation risk (#6). While the dismissal is valid, a brief note on why it fails other criteria would strengthen the analysis.
   - Alternative C (Conditional Branching) was dismissed on extensibility grounds only. It was not evaluated for backward compatibility — in fact, conditional branching might have *better* backward compatibility than E since no data migration is needed. This omission weakens the comparison.
   - Alternative D (Plugin Registry) was dismissed as over-engineering without evaluating implementation risk (#6) or cost comparison capability (#5). A plugin registry could theoretically support cost tracking per provider more cleanly than string parsing.

4. **No explicit criteria matrix for alternatives B, C, and D**
   The "Criteria Traceability" table only maps the chosen alternative (E) against all six criteria. Alternatives B, C, and D lack this structured comparison. While their pros/cons touch on some criteria implicitly, a side-by-side evaluation would make it clearer why E wins across *all* dimensions rather than just excelling at extensibility while being acceptable elsewhere.

5. **Missing alternative: Per-request provider selection via API parameter**
   The ADR assumes per-template routing is the requirement (criterion #1), but does not consider whether per-request provider selection (e.g., a `provider` query parameter on the analysis endpoint) might satisfy the comparison goal with less architectural change. This would allow ad-hoc A/B testing without template-level configuration changes. The ADR dismisses this implicitly by stating "per-template routing is sufficient," but should explicitly evaluate why per-request selection doesn't meet the stated goals (e.g., "per-request selection requires caller awareness of provider capabilities, which violates the abstraction goal").

6. **Alternative F's drift risk claim is unsupported**
   The dismissal of option F cites "Risk of drift: `provider` column says `anthropic` but `model` column says `gpt-4` — schema doesn't prevent this." However, this drift scenario could be prevented with a database-level check constraint or a Django model `clean()` method that validates provider-model compatibility. The ADR treats the drift risk as inherent to separate columns rather than addressable through validation logic, which weakens the dismissal.

7. **No hybrid of B + E explored**
   LiteLLM (option B) natively supports the `provider/model-name` convention and could handle request/response translation while the application layer uses option E's naming for template configuration. This would eliminate provider-specific adapter code while preserving the convention alignment benefit. The ADR treats B and E as alternatives rather than potentially complementary layers, missing an opportunity to reduce implementation risk (criterion #6) by leveraging a battle-tested abstraction.

8. **Alternative C's backward compatibility advantage is underweighted**
   Conditional branching (option C) requires zero data migration and zero schema changes — it has the strongest backward compatibility of all alternatives. The ADR dismisses it primarily on extensibility grounds without acknowledging that this comes at the cost of violating criterion #2 (backward compatibility). A more balanced evaluation would note: "C satisfies #2 best but fails #3 most severely; E is acceptable on #2 with migration overhead but strongest on #3."

9. **No comparison of alternatives against criterion #5 (cost comparison capability)**
   Only the chosen alternative (E) explicitly addresses cost tracking ("Per-request token counts enable quality-per-token analysis"). Alternatives A, B, C, D, and F are not evaluated for their ability to support cost comparison. Option B (gateway) might actually have superior cost tracking out of the box. This omission means criterion #5 was not used as a differentiator between alternatives.

10. **Alternative E's "industry convention" claim is asserted without evidence**
    The pros list "Aligns with industry conventions that engineers already recognize" and cites LiteLLM, OpenRouter, and LLM CLIs. However, the ADR doesn't provide evidence that this convention reduces onboarding time or maintenance burden for *this specific team*. If the team has no prior experience with `provider/model-name` patterns, the "recognizable to engineers" benefit may not materialize. This is a soft point but worth noting — the convention alignment claim would be stronger if supported by team familiarity data or a brief comparison of how other teams in the organization handle multi-provider routing.

## Decision Coherence

# ADR Review: Coherence Between Decision Criteria and Chosen Decision

## 1. Criterion Satisfaction Analysis

### Criterion 1: Per-template provider routing — **PARTIALLY SATISFIED**
The chosen approach supports per-template routing, but only after migration completes. The validation shim creates a dual-path system where legacy templates remain Anthropic-only until explicitly migrated:

> "If the team prefers not to migrate existing data immediately, the validation can also accept bare model names (without a `/`) and default the provider to `anthropic`."

**Finding:** Per-template routing is gated behind manual template updates. Templates created before migration cannot be routed without code changes or admin intervention. The ADR claims "Per-template routing is sufficient" in assumptions, but doesn't address how templates get migrated post-deployment (admin UI? API endpoint? bulk script?).

### Criterion 2: Backward compatibility — **CONTRADICTED BY IMPLEMENTATION**
The traceability table claims backward compatibility via a shim, but the implementation introduces breaking changes to external contracts:

> "Breaking change: This changes the response shape from a flat dict to a nested dict, and model keys now include the provider prefix. Frontend consumers of `/api/v1/admin/claude-models/config/` must be updated..."

**Finding:** Criterion 2 states "No breakage for existing templates, API consumers, or frontend clients." The ADR explicitly acknowledges breaking the models config endpoint response shape. Calling this "mitigated via versioned endpoint" does not satisfy the criterion — it acknowledges failure and proposes a workaround.

### Criterion 3: Extensibility — **WEAKENED BY HARDCODED DICTIONARIES**
The traceability claims "New providers = new settings dict + new provider class; no Django model changes." However, `validate_model()` contains hardcoded provider mappings that must be updated per new provider:

```python
model_dictionaries = {
    "anthropic": settings.CLAUDE_MODELS,
    "openrouter": settings.OPENROUTER_MODELS,
    "self_hosted": settings.SELF_HOSTED_MODELS,
}
```

**Finding:** Adding a fourth provider (e.g., `bedrock`) requires modifying `validate_model()` — a code change, not configuration. This contradicts the extensibility claim of "no modifying core dispatch logic." The ADR should document this as a known limitation or refactor to dynamic discovery.

### Criterion 4: Minimized code complexity — **AMBIGUOUS TRADEOFF**
The traceability states "Factory + registry = ~50 LOC; replaces growing if/else in `ClaudeAPI`." However, the abstraction adds cognitive overhead without eliminating conditional logic entirely:

> "Adds a new module (`llm_provider.py`) and parsing logic"

**Finding:** The complexity is shifted from inline conditionals to dispatch machinery. Whether this reduces total system complexity depends on whether you count the cost of understanding `parse_model_string()` + registry lookup vs. reading `if provider == "anthropic":`. The ADR doesn't quantify this tradeoff or provide evidence that engineers find the new pattern simpler than conditional branches.

### Criterion 5: Cost comparison capability — **ACKNOWLEDGED AS POTENTIALLY INVALID**
The traceability claims cost comparison is enabled, but the assumptions table explicitly undermines this:

> "Token counts are comparable across providers" → Impact if False: "If token counts diverge significantly, cross-provider quality-per-token comparisons are invalid. Mitigation: During comparison testing, evaluate on output quality (not tokens-per-dollar) as the primary metric."

**Finding:** Criterion 5 is satisfied only conditionally. The ADR acknowledges that the primary cost comparison mechanism (tokens-per-dollar analysis) may be invalid due to tokenizer differences across providers. This should be elevated from an assumption to a known limitation with explicit scope boundaries.

### Criterion 6: Low implementation risk — **ROLLBACK PLAN HAS GAPS**
The traceability claims "Each step is independently reversible." The rollback procedure states:

> "If any gate fails, revert the data migration (prefix removal), disable feature flag, and route all templates back to Anthropic via `ANTHROPIC_MODEL` fallback."

**Finding:** The rollback plan doesn't specify *how* prefix removal works. Is it a reverse Django migration? Manual SQL? The ADR doesn't define validation gates for the rollback itself (e.g., "verify all templates resolve to `anthropic/` after revert"). Without explicit rollback criteria, "independently reversible" is aspirational rather than verified.

---

## 2. Contradictions Between Goals and Implementation

### Contradiction 1: Provider flexibility vs. model-name coupling
**Goal (Primary Motivation #2):** "Establish a foundation that supports additional providers... without further architectural changes."

**Implementation:** The `provider/model-name` convention couples provider identity to the model string format. If Anthropic changes their API contract, the abstraction layer must adapt regardless:

> "The Anthropic API contract is stable" → Impact if False: "A major Anthropic API version change would require updating `AnthropicProvider` regardless of this ADR."

**Finding:** The chosen approach doesn't eliminate vendor lock-in — it just moves it from hardcoded endpoints to a provider-specific implementation class. The ADR overstates the flexibility benefit by implying the abstraction removes dependency on any single vendor's format.

### Contradiction 2: Backward compatibility vs. frontend breaking change
**Criterion 2:** "No breakage for existing templates, API consumers, or frontend clients."

**Implementation (Section 7):**
> "Breaking change: This changes the response shape from a flat dict to a nested dict... Frontend consumers of `/api/v1/admin/claude-models/config/` must be updated..."

**Finding:** The ADR acknowledges this is a breaking change but doesn't reconcile it with Criterion 2. Either the criterion should be relaxed to "No breakage for backend-only consumers" or the implementation should provide backward-compatible response shapes (e.g., dual-format endpoint during transition).

### Contradiction 3: Extensibility vs. hardcoded validation
**Criterion 3:** "Easy addition of future providers without modifying core dispatch logic."

**Implementation:** `validate_model()` requires manual updates for each new provider:
```python
model_dictionaries = {
    "anthropic": settings.CLAUDE_MODELS,
    "openrouter": settings.OPENROUTER_MODELS,
    "self_hosted": settings.SELF_HOSTED_MODELS,
}
```

**Finding:** The validation logic is not extensible without code changes. This contradicts the criterion's claim of "without modifying core dispatch logic." A truly extensible approach would discover providers dynamically (e.g., via Django apps, entry points, or configuration-driven registration).

---

## 3. Reversibility Assessment

### Claimed Reversibility
The ADR states: "If any gate fails, revert the data migration (prefix removal), disable feature flag, and route all templates back to Anthropic via `ANTHROPIC_MODEL` fallback."

### Hidden Lock-in Effects

1. **Data migration reversal is non-trivial**
   The ADR doesn't specify how prefix removal works. If it's a Django reverse migration, it must handle:
   - Rows where `model` was already prefixed (e.g., `anthropic/claude-sonnet-4-6` → `claude-sonnet-4-6`)
   - Rows that were never migrated (`NULL` or empty values)
   - Concurrent writes during revert

   **Finding:** No rollback validation gate is defined. The ADR should specify: "After prefix removal, verify all `PromptTemplate.model` values match the legacy format (no `/` present)."

2. **Frontend breaking change persists after rollback**
   If the frontend has been updated to handle nested response shapes, rolling back the backend without reverting the frontend leaves both systems incompatible. The ADR mentions versioning but doesn't define a rollback path for the frontend:

   > "Support both response shapes via `Accept` header or query param (`?format=v1|v2`) for 2 release cycles."

   **Finding:** This is a forward-looking mitigation, not a rollback plan. If you revert to v1, the frontend must also revert — but the ADR doesn't document this dependency.

3. **Code module cleanup is undefined**
   The rollback procedure retains the legacy `ClaudeAPI` class:

   > "The legacy `ClaudeAPI` class is retained as a delegate for backward compatibility."

   **Finding:** If rolling back, why retain the new modules (`llm_provider.py`, `AnthropicProvider`, etc.)? The ADR doesn't specify whether rollback includes removing the abstraction layer entirely or keeping it dormant. This creates ambiguity about what "revert" means.

4. **Celery task rename coordination**
   The Celery task rename (`claude_ai_job` → `llm_job`) requires rolling workers through two deployment cycles:

   > "Legacy task imports and delegates to new implementation. Remove alias after all workers upgraded (2 deployment cycles)."

   **Finding:** If rolling back mid-rename, the legacy wrapper must be restored immediately. The ADR doesn't define this rollback trigger or procedure.

---

## 4. Migration Plan Rollback Assessment

### Stated Rollback Procedure
> "If any gate fails, revert the data migration (prefix removal), disable feature flag, and route all templates back to Anthropic via `ANTHROPIC_MODEL` fallback."

### Missing Rollback Components

1. **No rollback validation gates**
   The ADR defines forward validation gates (e.g., "Unit tests pass") but doesn't define what success looks like after a revert:

   **Finding:** Add a rollback gate: "After prefix removal, verify `PromptTemplate.objects.filter(model__contains='/').count() == 0` and all templates resolve to Anthropic via fallback."

2. **No data integrity checks**
   The migration prefixes existing values but doesn't validate that the resulting strings are valid model names:

   **Finding:** Add a pre-migration validation step: "Before prefixing, verify all non-null `model` values match expected legacy format (e.g., `claude-sonnet-4-6`, not `anthropic/claude-sonnet-4-6`)."

3. **No rollback trigger criteria**
   The ADR says "If any gate fails" but doesn't define what constitutes a failure warranting rollback:

   **Finding:** Define explicit rollback triggers: "Rollback if: (a) error rate > 5% for 10 minutes, (b) p95 latency exceeds 30s, (c) token counts are zero for any provider."

4. **No frontend rollback coordination**
   The models config endpoint change is acknowledged as breaking but no frontend rollback procedure exists:

   **Finding:** Add a frontend rollback step: "If backend reverts, restore `/api/v1/admin/claude-models/config/` to flat response shape and revert frontend model selector component."

5. **No cleanup of deferred modules**
   The migration plan includes `SelfHostedProvider` (deferred per ADR 0004) but doesn't specify whether it should be removed if self-hosted is never adopted:

   **Finding:** Add a post-migration cleanup step: "If self-hosted is deferred indefinitely, remove `SelfHostedProvider` and `SELF_HOSTED_*` settings to reduce surface area."

---

## Summary of Findings

1. **Criterion 1 (Per-template routing)** is partially satisfied — only works for migrated templates; no migration path defined for new templates.
2. **Criterion 2 (Backward compatibility)** is contradicted by the breaking change to `/api/v1/admin/claude-models/config/` response shape.
3. **Criterion 3 (Extensibility)** is weakened by hardcoded provider dictionaries in `validate_model()` that require code changes per new provider.
4. **Criterion 4 (Minimized complexity)** has an ambiguous tradeoff — complexity is shifted, not eliminated; no evidence provided that the new pattern reduces cognitive load.
5. **Criterion 5 (Cost comparison)** is acknowledged as potentially invalid due to tokenizer differences across providers.
6. **Criterion 6 (Low implementation risk)** rollback plan lacks validation gates and explicit failure criteria.
7. **Hidden lock-in effects** include non-trivial data migration reversal, frontend breaking change persistence, undefined code module cleanup, and Celery task rename coordination gaps.
8. **Migration plan rollback** is missing: rollback validation gates, data integrity checks, explicit rollback trigger criteria, frontend rollback coordination, and cleanup of deferred modules.

## Implementation Readiness

# ADR Review: Implementation Readiness Assessment

## Verdict
**REQUEST CLARIFICATION / ELABORATION** — 5 critical gaps must be resolved before implementation can proceed safely.

## Executive Summary
The ADR demonstrates strong architectural thinking with a well-reasoned provider abstraction design. However, there are **syntactic errors in code snippets**, **missing file paths for migrations and admin updates**, and **vague validation gates** that could cause implementation drift or testing ambiguity. The migration plan ordering is mostly correct but has one dependency sequencing issue that needs explicit handling.

## Dimension Scores

| Dimension | Score (1-5) | Key Finding |
|-----------|-------------|-------------|
| Assumption Validity | 4 | Assumptions are well-stated; #4 (team capacity) correctly marked FALSE per ADR 0004 |
| Alternative Completeness | 5 | Comprehensive analysis of 6 alternatives with clear tradeoffs |
| Decision Coherence | 4 | One dependency ordering issue in migration plan (Step 3 vs Step 4) |
| Implementation Readiness | 2 | Syntactic error in Prometheus import; missing file paths for migrations/admin |
| Risk Coverage | 4 | Risks well-documented; OpenRouter volatility risk adequately mitigated |
| Testability | 3 | Validation gates #5, #7, #9, #10 are too vague for pass/fail automation |

## Critical Gaps (Must Fix)

### 1. Syntactic Error in Prometheus Metrics Snippet
**Location:** Section "Observability gap" mitigation code block
**Issue:** Duplicate import of `Histogram`
```python
from prometheus_client import Counter, Histogram, Histogram  # ❌ DUPLICATE IMPORT
```
**Fix:** Change to:
```python
from prometheus_client import Counter, Histogram  # ✅ CORRECT
```

### 2. Missing File Path for Data Migration
**Location:** Step 4 in Migration Plan table and Section "Data migration"
**Issue:** The ADR references creating a data migration but does not specify the file path. Given the project structure (`memores/migrations/`), this should be explicitly stated.
**Fix:** Add explicit file path: `memores/migrations/00XX_add_model_provider_prefix.py` (use next available migration number per project convention).

### 3. Missing File Path for Admin Updates
**Location:** Step 6 in Migration Plan table and Section "Update serializers and admin"
**Issue:** The ADR states to update `PromptTemplateAdmin` but does not specify which file contains it. Based on the file tree, this is `memores/admin.py`.
**Fix:** Explicitly reference: `memores/admin.py` (or `memores/apps.py` if using `@admin.register()` decorator pattern).

### 4. Migration Plan Dependency Ordering Issue
**Location:** Steps 3 and 4 in Migration Plan table
**Issue:** Step 3 updates validation to require `provider/model-name` format, but Step 4 runs the data migration to prefix existing rows. If Step 3 is deployed before Step 4 completes, **existing templates with bare model names will fail validation**. The ADR mentions backward compatibility but doesn't explicitly order this correctly.
**Fix:** Reorder or add explicit gate:
- **Option A (Recommended):** Deploy Step 1-2 first, then Step 4 (data migration), then Step 3 (strict validation). This ensures all rows are prefixed before validation rejects bare names.
- **Option B:** Keep current order but explicitly state that Step 3's backward compatibility shim must remain active until Step 5 completes and all legacy data is verified migrated.

### 5. Vague Validation Gates (#5, #7, #9, #10)
**Location:** Migration Plan table, Steps 5, 7, 9, 10
**Issue:** Four gates use subjective language ("works", "verify", "validated") that cannot be automated or pass/fail tested.

| Step | Current Gate (Vague) | Required Specificity |
|------|---------------------|---------------------|
| 5 | "Admin shows `provider` derived property; filter by provider works" | "Admin list view displays `provider_display` column; `list_filter` returns expected results for sample data with mixed providers" |
| 7 | "Quality scores recorded; cheapest model meeting quality bar identified" | "≥4/5 average across dimensions on blind comparison of N prompts (define N and dimensions in ADR 0004)" |
| 9 | "Endpoints return 200; structured output validated" | "Automated test asserts `output_schema` produces valid JSON matching schema for N templates (define N)" |
| 10 | "Canary: route 1-2 non-critical templates to self-hosted via admin; verify output quality, latency, cost tracking" | "Canary runs for 24h with <1% error rate and p95 latency <30s (matches Step 11 criteria)" |

## Strengths
- **Backward compatibility shim** in `validate_model()` is well-designed and explicitly handles legacy bare model names.
- **Provider registry pattern** (`PROVIDER_REGISTRY`) makes extensibility trivial — adding a new provider is a single-line change.
- **Data migration approach** (prefix existing values with `anthropic/`) is low-risk and reversible.
- **Feature flag strategy** (`SELF_HOSTED_ENABLED=False` default) provides instant rollback capability.
- **Celery task rename wrapper** pattern (`claude_ai_job` delegating to `llm_job`) correctly handles rolling deployment coordination.

## Suggestions (Nice to Have)
1. **Add architecture diagram** showing Protocol → Registry → Factory → Provider flow (mentioned in Review Summary but not included in ADR body).
2. **Specify migration number** for the data migration file (e.g., `0015_add_model_provider_prefix.py`) to avoid conflicts with existing migrations.
3. **Define "quality bar" threshold** explicitly in this ADR or link to ADR 0004's evaluation framework (currently referenced but not defined here).
4. **Add import statements** for all code snippets (e.g., `from django.conf import settings`, `from rest_framework import serializers`) to ensure copy-paste readiness.

## Missing Questions from Reviewer
1. **What is the exact migration number** for the data migration file? The project has gaps in migration numbering (0001 → 0002 → 0006), so using `00XX` is ambiguous.
2. **Does `memores.external.claude_api.MessageType` exist?** The ADR imports it but doesn't verify its existence in the current codebase.
3. **How will the backward compatibility shim be removed?** Once all legacy data is migrated, the shim adds unnecessary complexity. Define a deprecation timeline or removal trigger.
4. **What happens if `OPENROUTER_MODELS` is empty during comparison phase?** The ADR doesn't specify fallback behavior when no OpenRouter models are configured.
5. **Is `SELF_HOSTED_ENABLED` feature flag stored in Django settings, database, or environment variable?** Clarify the storage mechanism for operational clarity.

## Risk Coverage

# ADR Review: Risk Analysis Findings

## 1. Unmitigated or Weakly Mitigated Risks

| # | Finding | Reference | Gap |
|---|---------|-----------|-----|
| 1 | **Model quality mitigation lacks evaluation methodology** — The risk states "start with a comparison phase" and "document quality differences," but provides no pass/fail criteria, scoring rubric, or automated validation. Without measurable quality thresholds, the experiment has no defined failure condition. | Risk #1: Model quality | Mitigation is descriptive, not operational. Needs specific evaluation framework (e.g., blind A/B scoring ≥4/5 on X dimensions) before any production routing decision. |
| 2 | **Latency mitigation is unspecified** — "Configure appropriate timeouts" does not define timeout values per provider, retry budgets, or p95 latency thresholds that would trigger rollback. | Risk #2: Latency | No concrete configuration references (e.g., `OPENROUTER_TIMEOUT=30s`, `SELF_HOSTED_TIMEOUT=60s`). Cannot be validated without explicit values. |
| 3 | **Operational complexity gate criteria undefined** — "Gate production rollout behind a manual approval step" does not specify who approves, what documentation is required, or what constitutes operational readiness. | Risk #5: Operational complexity | Missing: runbook review checklist, on-call coverage confirmation, incident response drill requirement. |
| 4 | **Schema compatibility fallback behavior unspecified** — "Fall back to prompt-based JSON extraction when unsupported" does not define how the fallback is detected, logged, or monitored. No metric for fallback frequency. | Risk #3: Schema compatibility | Needs: detection mechanism (e.g., `response_format` rejection), fallback logging tag, alert threshold for fallback usage >10% of requests. |
| 5 | **Credential rotation failure not addressed** — Mitigation says "rotate keys quarterly" but does not cover: what happens if rotation fails mid-cycle, how expired keys are detected, or whether key expiry is monitored via OpenRouter dashboard API. | Risk #16: Credential lifecycle | Missing: automated key expiry monitoring (e.g., OpenRouter `/v1/status` endpoint check), alert on key age >60 days, documented rotation runbook with rollback to previous key. |

## 2. Missing Risk Categories

| # | Finding | Reference | Gap |
|---|---------|-----------|-----|
| 6 | **Data migration corruption risk not identified** — The ADR prefixes all existing `model` values with `anthropic/` but does not address: what happens if the migration is interrupted mid-run, how partial prefixing is detected, or whether a pre-migration backup exists. | Migration Plan Step 1 & Risk #6: Reversibility | Missing: transactional migration strategy, pre-migration snapshot, validation query to detect rows with inconsistent format post-migration (e.g., `SELECT COUNT(*) FROM prompt_template WHERE model NOT LIKE 'anthropic/%' AND model IS NOT NULL`). |
| 7 | **Rollback failure scenario not covered** — The ADR states "revert the data migration (prefix removal)" but does not address: what if the reverse migration fails due to concurrent writes, how to handle rows that were partially migrated, or whether a database restore point exists. | Migration Plan Rollback & Risk #6: Reversibility | Missing: rollback validation query (`SELECT COUNT(*) FROM prompt_template WHERE model LIKE 'anthropic/%'`), database snapshot before migration, explicit rollback success criteria. |
| 8 | **Coordination timing risk not quantified** — Frontend coordination and Celery task rename are identified as risks but no timeline or dependency mapping is provided. What if frontend update is delayed by 2 sprints? What if legacy Celery workers cannot be upgraded in time? | Risk #13: Frontend coordination & Risk #12: Celery task rename | Missing: Gantt-style dependency map, fallback plan if frontend update is blocked (e.g., versioned endpoint with backward-compatible shape), explicit deprecation deadline for `claude_ai_job` task name. |
| 9 | **OpenRouter rate limiting / quota exhaustion not identified** — No risk entry addresses what happens if OpenRouter enforces rate limits during comparison phase, exceeds monthly quotas, or throttles specific models. | Missing from Risk table | Needs: rate limit monitoring (e.g., `429` response tracking), quota alerting (OpenRouter dashboard webhook), fallback to Anthropic when rate-limited >5% of requests in 1-hour window. |
| 10 | **Multi-provider response format drift not identified** — If OpenRouter silently updates model versions or changes response schema, the abstraction layer may produce malformed `AnalysisOutput` without detection. | Missing from Risk table | Needs: response schema validation (e.g., Pydantic model for OpenAI-compatible responses), alert on schema mismatch frequency >1%, automated regression test suite for each provider's response format. |
| 11 | **Cost comparison validity risk not identified** — The ADR acknowledges token counting accuracy but does not address: what if OpenRouter and Anthropic tokenize the same prompt differently, how to normalize cross-provider token counts, or whether cost-per-token comparisons are valid without a unified tokenizer. | Risk #9: Token counting accuracy (incomplete) | Needs: documented tokenization normalization strategy, example of divergent tokenization case, acceptance criteria for "comparable" token counts (e.g., <10% variance on 100-sample test set). |

## 3. Mitigation Strategies Lacking Specific Tool/Command/Config References

| # | Finding | Reference | Gap |
|---|---------|-----------|-----|
| 12 | **Grafana dashboard not specified** — Risk #10 mitigation says "Dashboard in Grafana before Step 7" but does not name the dashboard, define queries, or specify alert rules. | Risk #10: Observability gap | Missing: dashboard name (e.g., `LLM Provider Health`), specific panel definitions, alert rule for error rate >5% (who is paged?). |
| 13 | **Data migration rollback command not defined** — Risk #6 mitigation says "revert the data migration" but does not specify the management command or SQL to execute. | Risk #6: Reversibility | Missing: explicit command (e.g., `python manage.py reverse_model_prefix_migration`), validation query post-rollback, estimated rollback duration for production dataset size. |
| 14 | **Feature flag implementation not specified** — Risk #6 mentions `SELF_HOSTED_ENABLED=False` but does not define the feature flag library (Django Flags? Unleash?), rollout percentage mechanism, or how to verify the flag is respected across all code paths. | Risk #6: Reversibility & Migration Plan Step 10 | Missing: feature flag package selection, configuration in `settings.py`, test asserting flag disables self-hosted provider even if model string says `openrouter/...`. |
| 15 | **Retry configuration not specified for OpenRouter** — Risk #2 mitigation mentions "configure appropriate timeouts" but does not define retry behavior (tenacity config) for OpenRouter vs. Anthropic, which may have different latency profiles. | Risk #2: Latency & Risk #3: Schema compatibility | Missing: `OPENROUTER_MAX_RETRIES=3`, `SELF_HOSTED_MAX_RETRIES=5`, backoff strategy per provider, circuit breaker threshold (e.g., 10 consecutive failures → disable provider for 5 minutes). |

## 4. 'Experiment Failed' Rollback Trigger Analysis

| # | Finding | Reference | Gap |
|---|---------|-----------|-----|
| 16 | **No measurable failure criteria defined** — The ADR's "Decision Gate" (Step 8) states "If a cheaper model passes quality bar on OpenRouter" but does not define what constitutes the quality bar, cost threshold, or latency constraint that would trigger rollback. | Migration Plan Step 8 & Risk #1: Model quality | Missing: explicit failure triggers such as:<br>- Quality score <4/5 across X dimensions<br>- p95 latency >30s for self-hosted<br>- Error rate >2% for OpenRouter<br>- Cost savings <20% vs. Anthropic Sonnet 4.6<br>- Token count variance >15% between providers |
| 17 | **Rollback trigger not tied to monitoring alerts** — The ADR defines Prometheus metrics but does not connect them to automated rollback triggers. Manual review is implied but not operationalized. | Risk #10: Observability gap & Migration Plan Step 8 | Missing: alert rule that pages on-call when failure criteria met, automated feature flag disable via PagerDuty webhook or similar, documented runbook for manual rollback within 15 minutes of alert. |
| 18 | **Graduated response not defined** — The ADR implies a binary outcome (stay on OpenRouter vs. revert to Anthropic) but does not address intermediate states: partial rollout reduction, model-specific routing changes, or temporary disablement of specific models while keeping others active. | Migration Plan Step 9 & Risk #15: OpenRouter API volatility | Missing: decision tree for partial failures (e.g., "if GLM-5.2 fails quality but Qwen3 passes, keep Qwen3 in production and remove GLM-5.2"), per-model rollback capability via Django admin toggle. |

## Summary of Critical Gaps

The risk analysis identifies 16 risks but leaves **5 categories unaddressed** (data migration corruption, rollback failure, coordination timing, rate limiting/quota exhaustion, response format drift) and provides **unmeasurable failure criteria** for the experiment itself. Mitigation strategies lack specific tool/command references in **4 areas** (Grafana dashboard, data migration rollback command, feature flag implementation, retry configuration). The "experiment failed" trigger is the most critical gap: without explicit, measurable rollback criteria tied to monitoring alerts, the team cannot objectively determine when to revert and may continue routing traffic to a failing provider indefinitely.

**Recommendation:** Add a dedicated "Experiment Rollback Criteria" subsection to the Migration Plan with explicit thresholds for quality, latency, error rate, and cost savings. Define automated alert rules that trigger feature flag disablement. Specify data migration rollback command and validation queries. Address the 5 missing risk categories with concrete mitigations referencing specific tools or configurations.

## Testability

# ADR Review: Testing Strategy for Multi-Provider LLM Integration

## Findings

1. **The 4 listed test files are insufficient — they miss core new components.** The ADR lists `test_claude_api.py` (261 lines), `test_prompt_template_serializers.py` (82 lines), `test_prompt_template.py` (189 lines), and `test_claude_models_config.py` (51 lines). These cover legacy surface-level changes but omit test files for:
   - **`parse_model_string()`** — the core utility function that's entirely new. No dedicated unit tests are specified.
   - **Provider factory/registry** (`get_provider()`, `PROVIDER_REGISTRY`) — dispatch logic, unknown provider handling, and edge cases (empty model strings) need their own test file.
   - **New provider implementations** (`AnthropicProvider`, `OpenRouterProvider`) — each requires tests for request building, response parsing, and error handling.
   - **Data migration** — no `test_migrations.py` is mentioned. The ADR's "spot-check 10 rows pre/post" (Step 4) is manual, not automated.

2. **Missing integration tests for end-to-end provider routing.** The validation gate in Step 6 states "Integration test: job routes to correct provider based on `prompt_template.model`" but doesn't specify which file or pass/fail criteria. Missing tests include:
   - **Celery job dispatch verification** — a test that asserts `perform_llm_job` calls the correct provider (Anthropic vs OpenRouter) based on `prompt_template.model`, using mocked HTTP responses (e.g., via `responses` library).
   - **OpenRouter response translation** — the ADR mentions translating Anthropic-style messages to OpenAI format; this translation needs integration tests with mock OpenRouter responses.
   - **Cross-provider token normalization** — no test specified for verifying that `track_usage` correctly extracts token counts from both Anthropic and OpenRouter response formats.

3. **Validation gates are not specific enough to automate.** Several gates lack programmatic pass/fail criteria:
   - **Step 2**: "`SELF_HOSTED_API_BASE_URL` reachable" — no HTTP status code, timeout threshold, or DNS resolution check specified.
   - **Step 7 (OpenRouter comparison)**: "Quality scores recorded; cheapest model meeting quality bar identified" — lacks measurement methodology (automated metric vs human eval) and quantitative thresholds.
   - **Step 9**: "Endpoints return 200; structured output validated" — no endpoint path, response schema validation, or Pydantic/JSON Schema assertions defined.

4. **Data migration cannot be fully tested in staging without production data unless explicit migration tests are added.** The ADR's approach relies on manual verification:
   - Step 4: "Migration dry-run succeeds; spot-check 10 rows pre/post" — this is a manual checklist item, not an automated test.
   - Step 5: "Run data migration in staging; verify `PromptTemplate` admin list/filter works" — verifies UI behavior but doesn't validate row-level correctness (e.g., NULL models remain NULL, empty strings handled correctly).

   To enable testing without production data, the ADR should specify a `test_migrations.py` file that:
   - Creates synthetic test data covering edge cases (NULL values, empty strings, already-prefixed models for idempotency)
   - Asserts row counts and model value transformations post-migration
   - Tests the reverse migration (rollback procedure)

5. **No test coverage for error handling and fallback paths.** The ADR mentions rollback procedures but doesn't specify tests for:
   - What happens when OpenRouter returns a 5xx or times out?
   - Does the factory/registry correctly fall back to Anthropic when `OPENROUTER_ENABLED=False`?
   - Are feature flags (`SELF_HOSTED_ENABLED`) properly respected in dispatch logic?

6. **The "4 test files totaling ~580 lines" claim is misleading.** The ADR states these tests "directly exercise `ClaudeAPI` or validate against `CLAUDE_MODELS` and will require updates." However, the behavioral change introduces entirely new components (provider abstraction, factory, data migration) that have no corresponding test files. The 580 lines represent legacy coverage that needs updating, not comprehensive coverage of the new system.
