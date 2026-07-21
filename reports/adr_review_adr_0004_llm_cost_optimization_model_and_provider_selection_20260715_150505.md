# ADR Review Report

**Source:** ADR 0004: LLM Cost Optimization — Model and Provider Selection
**Date:** 2026-07-15 15:05
**Model:** ornith:35b

## Rubric Scores

| Metric | Score (1-5) | Weight |
|--------|-------------|--------|

## Review

# ADR Review (Multi-Pass)

## Assumption Validity

# Assumption Review for ADR 0004: LLM Cost Optimization — Model and Provider Selection

## Findings

1. **Assumption:** *"Templates default to 1,000 output tokens per request (not the 8K-10K assumed in earlier drafts). Verify: Query `PromptTemplate.output_limit` across all active templates to confirm no template exceeds 1,000 tokens."*
   - **Testable before decision point?** Yes — explicitly self-verifying via SQL query. However, the verification is deferred to Migration Step 0 (after the ADR is approved), not validated *before* the decision point as part of this review. The assumption should be confirmed prior to approval.
   - **Missing assumptions:** None critical. One related gap: no mention of whether `output_limit` is enforced at the API level or only as a soft cap in templates — if it's soft, actual output tokens could exceed 1,000 regardless.
   - **Impact if False accurate?** Partially. If some templates exceed 1,000 tokens, cost projections for Haiku and OpenRouter models would be understated (longer outputs = more tokens), but the qualitative finding ("Haiku may not handle long-form analysis") remains valid. The impact should note that cost savings could be partially eroded by longer-than-expected outputs.
   - **Hidden assumptions:** Assumes `output_limit` is a reliable proxy for actual output length. It does not account for models that produce shorter outputs than their limit (e.g., Haiku truncating early), which would make the 1,000-token baseline optimistic for cost calculations.

2. **Assumption:** *"Monthly spend: ~$40/month (near-zero production traffic)"*
   - **Testable before decision point?** Yes — query `LlmUseSummary` and `PromptSummary` for actual token counts × known pricing. The ADR itself calls this out in Step 0.
   - **Missing assumptions:** No assumption about cost trajectory. If the platform is growing (new users, more journal entries), $40/month may be a floor, not a ceiling. This affects whether OpenRouter's savings justify integration effort at scale.
   - **Impact if False accurate?** Yes — if actual spend is significantly higher than $40, the cost-reduction imperative strengthens and the comparison becomes more urgent. If lower, it weakens. The impact statement correctly identifies this as a calibration variable.
   - **Hidden assumptions:** Assumes "near-zero production traffic" means costs are stable. Does not account for batch job spikes (e.g., email report cron firing on many pending reports simultaneously) which could cause burst costs not reflected in monthly averages.

3. **Assumption:** *"Templates using Sonnet 4.6 at $15/1K output tokens"*
   - **Testable before decision point?** Yes — Anthropic's published pricing is public and stable. Verifiable via Anthropic console or documentation.
   - **Missing assumptions:** Does not account for Anthropic's tiered pricing (higher volumes get discounts). At current $40/month, you're in the lowest tier, so this is a minor gap but worth noting if spend grows.
   - **Impact if False accurate?** Yes — the $15/1K output figure is correct per Anthropic's public pricing for Claude Sonnet 4.6.
   - **Hidden assumptions:** Assumes input token cost ($3/1K) is negligible relative to output cost. For prompts with very long system prompts + user context, input costs could be material (e.g., a journal entry with 2,000 input tokens × $3 = $6 vs. 500 output tokens × $15 = $7.50 — input is ~44% of cost).

4. **Assumption:** *"Haiku 4.5 at $0.25/1K is 60× cheaper"*
   - **Testable before decision point?** Yes — Anthropic publishes Haiku pricing publicly.
   - **Missing assumptions:** None significant. The 60× figure compares output-only costs; a full cost comparison should include input tokens (Haiku input is ~$0.25/1K vs Sonnet $3/1K, so input savings are even larger).
   - **Impact if False accurate?** Yes — the ratio is correct for output tokens. Impact statement correctly identifies this as the primary cost driver.
   - **Hidden assumptions:** Assumes Haiku's pricing remains stable. Anthropic has historically changed model pricing on short notice (e.g., Sonnet 4 → Sonnet 4.5 transition). No mention of price lock-in or contract options.

5. **Assumption:** *"Quality drops to ~70-80% of Sonnet"* (for Claude Haiku)
   - **Testable before decision point?** Partially — the ADR acknowledges this is an estimate and calls for empirical validation on actual prompts. However, the 70-80% figure itself is not validated here; it's cited from general benchmarks.
   - **Missing assumptions:** No assumption about *which dimension* of quality drops. Haiku may be 90% as good at extraction/classification but only 50% as good at nuanced coaching tone. The "70-80%" aggregate masks dimension-specific variance that is critical for the hybrid routing decision (Option 1c).
   - **Impact if False accurate?** Partially. If Haiku quality is actually 90%+ on simple tasks, Option 1b becomes viable without needing OpenRouter at all. The impact statement correctly notes this as a gate condition but doesn't quantify how much the estimate could be wrong in either direction.
   - **Hidden assumptions:** Assumes quality degradation is uniform across task types. In reality, smaller models often degrade non-uniformly — they may handle structured extraction well but fail on open-ended reasoning. This directly impacts whether Option 1c (hybrid) or Option 2 (single provider) is optimal.

6. **Assumption:** *"GLM-5.2: ~80-90% of Sonnet"* / *"Qwen3-235B-A22B: ~85-95% of Sonnet"* / *"gpt-oss-120b: ~75-85% of Sonnet"*
   - **Testable before decision point?** No — these are explicitly labeled as estimates derived from general benchmarks. The ADR's own disclaimer correctly notes this. However, the assumption that benchmark quality transfers to domain-specific health/wellness prompts is unvalidated and critical to the decision.
   - **Missing assumptions:** No assumption about how health/wellness language tasks differ from coding/general chat (the ADR does mention this in the disclaimer but doesn't quantify the gap). Missing: assumption about whether these models support the specific output schemas required by `output_schema` templates — a hard blocker if unsupported.
   - **Impact if False accurate?** Partially. The disclaimer correctly frames these as directional. However, if actual quality on domain prompts is <60% of Sonnet (possible for GLM/Qwen on nuanced coaching), the cost savings are irrelevant because the model fails the P0 quality requirement. Impact should note that a 20-30 point quality gap could be catastrophic for user-facing templates.
   - **Hidden assumptions:** Assumes OpenRouter routes to the exact model versions listed (GLM-5.2, Qwen3-235B-A22B). OpenRouter may route to newer/older versions depending on availability, and version changes can significantly affect quality. The ADR mentions pinning versions in risks but doesn't address whether OpenRouter's routing is deterministic.

7. **Assumption:** *"If 60% of templates are simple and 40% are complex, blended cost is ~$0.55/1K output tokens — 96% cheaper than Sonnet-only."*
   - **Testable before decision point?** Partially — template classification by complexity can be done (Step 3 mentions "classifying templates by complexity"), but the specific 60/40 split is an estimate, not measured data.
   - **Missing assumptions:** No assumption about *which* templates are "simple" vs "complex." The ADR gives examples (quiz explanations = simple; journal analysis = complex) but doesn't provide a classification rubric or decision tree. This is the single most operationally important assumption in Option 1c, and it's unvalidated.
   - **Impact if False accurate?** Yes — if the split is actually 40/60 (more complex), blended cost rises to ~$0.85/1K. If 80/20, it drops further. The impact correctly identifies this as a sensitivity variable but doesn't show the range of possible outcomes.
   - **Hidden assumptions:** Assumes template classification is stable over time. New templates added in the future may not fit neatly into "simple" or "complex," requiring ongoing maintenance of routing rules. Also assumes all "complex" templates benefit from OpenRouter models — some may need Sonnet specifically (e.g., personality reports requiring high reasoning depth).

8. **Assumption:** *"At $40/month spend, the warm-worker tax alone ($720/month for 2× H100 at ~$1.00/hr on-demand) is 18× current spend"*
   - **Testable before decision point?** Yes — RunPod pricing is public and verifiable. The math (2 × $1.00/hr × 730 hrs/month = $1,460, not $720) appears to use a different calculation. Let me check: 2 H100 at $1.00/hr = $2.00/hr × 24 × 30 = $1,440/month. The ADR says $720, which implies either 1 GPU or $0.50/hr. This is a **math error**.
   - **Missing assumptions:** Assumes always-on GPUs even at near-zero traffic. Does not account for RunPod's "serverless" mode (cold-start penalty) vs persistent instances. The $720 figure may be for serverless, but the comparison to "warm-worker tax" implies persistent.
   - **Impact if False accurate?** No — the math is incorrect. If actual cost is ~$1,440/month (2× H100 persistent), it's 36× current spend, not 18×. This strengthens the case against self-hosting but doesn't change the conclusion. The error should be corrected for accuracy.
   - **Hidden assumptions:** Assumes GPU pricing is stable and that H100 availability is guaranteed on RunPod. Spot/preemptible instances could be cheaper but introduce reliability risk not addressed.

9. **Assumption:** *"Break-even vs OpenRouter requires sustained volume where per-token GPU cost < OpenRouter markup — rough estimate: ~$300-500/month spend"*
   - **Testable before decision point?** No — this is a forward-looking projection dependent on future usage growth, which is unknown.
   - **Missing assumptions:** Does not account for the *time value* of the $720+/month GPU cost during the ramp-up period. If it takes 6 months to reach $300/month spend, you've spent ~$3,600 in GPU costs before breaking even vs OpenRouter's zero fixed cost.
   - **Impact if False accurate?** Partially. The break-even range is directionally reasonable but doesn't include the payback period. If growth stalls at $100/month for a year, self-hosting is permanently unviable. Impact should note that the $300-500 threshold assumes linear growth and no alternative cost reductions.
   - **Hidden assumptions:** Assumes OpenRouter pricing remains constant while GPU costs are fixed. If OpenRouter raises prices (acknowledged as a risk), break-even shifts downward, making self-hosting more attractive sooner.

10. **Assumption:** *"Known PII-bearing fields include: user profile data (name, age, gender), journal entry text, course responses, and coaching interaction content."*
    - **Testable before decision point?** Partially — can be verified by auditing `PromptTemplate` system prompts and `JournalEntry`/`CoachEntry`/`UserResponse` model fields. However, the ADR doesn't specify *which* templates inject which PII fields, making it impossible to determine if all OpenRouter traffic contains PII or only some.
    - **Missing assumptions:** No assumption about whether PII can be safely redacted without degrading quality. For journal analysis, removing the user's name may not matter; for coaching, context from past entries (which contain PII) is essential. The ADR mandates synthetic data but doesn't address whether redaction preserves task utility.
    - **Impact if False accurate?** Yes — if prompts contain PII and go to a third party without DPA, there's legal/compliance risk. The impact correctly identifies this as a gating condition for production use.
    - **Hidden assumptions:** Assumes OpenRouter's data processing terms are acceptable even with redacted data. Some providers (e.g., certain Chinese models via OpenRouter) may have different data retention policies than others routed through the same aggregator.

11. **Assumption:** *"OpenRouter provides per-token pricing immediately"*
    - **Testable before decision point?** Yes — create an account and check the dashboard/API response format. This is a factual capability claim.
    - **Missing assumptions:** None significant for this assumption. One minor gap: OpenRouter's per-request cost reporting may lag by hours, not be real-time. The ADR implies immediate visibility but doesn't clarify latency of cost data availability.
    - **Impact if False accurate?** Partially. If cost data is delayed, it doesn't affect the comparison phase (Step 4) which uses token counts × known pricing, but it does affect ongoing monitoring. Impact should note that "immediately" may mean "within 24 hours" rather than real-time.
    - **Hidden assumptions:** Assumes OpenRouter's reported token counts match actual Anthropic/underlying provider counts. Discrepancies between aggregator-reported and provider-reported tokens could make cost tracking unreliable — a risk the ADR acknowledges but doesn't quantify.

12. **Assumption:** *"The `PromptTemplate` system prompts are template-based, but user data is injected at runtime."*
    - **Testable before decision point?** Yes — can be verified by inspecting `PromptTemplate.system_prompt` fields and the code path in `memores/external/claude_api.py` or relevant service layer that substitutes variables.
    - **Missing assumptions:** No assumption about *how* user data is substituted. If substitution happens via string interpolation (e.g., `{user_name}`), PII is clearly present. If it's via structured context injection, some fields may be non-PII (e.g., course progress percentages). The binary "template vs runtime" framing oversimplifies — some templates may have hardcoded sensitive content.
    - **Impact if False accurate?** Yes — the distinction between template-level and runtime-injected data is correctly identified as the PII boundary. Impact statement accurately reflects that synthetic data must be used during comparison.
    - **Hidden assumptions:** Assumes all runtime-injected data can be replaced with synthetic equivalents without breaking prompt structure. Some templates may rely on specific data formats (e.g., date ranges, numerical scores) that synthetic data might not replicate faithfully.

13. **Assumption:** *"If no cheaper model matches quality, you've lost nothing — the `openrouter` provider just stays disabled"*
    - **Testable before decision point?** Yes — verify that disabling the provider reverts all traffic to Anthropic (already documented in rollback triggers).
    - **Missing assumptions:** Assumes template model assignments can remain unchanged when OpenRouter is disabled. But if templates are assigned to `openrouter/glm-5.2` and the provider is disabled, those templates will fail — not silently revert. The ADR acknowledges this in the rollback section but the assumption here implies a cleaner fallback than reality.
    - **Impact if False accurate?** Partially. The "lost nothing" claim is true for *routing* (instant revert) but false for *template state* (templates pointing at OpenRouter models need reassignment). Impact should distinguish between routing revert and template reversion.
    - **Hidden assumptions:** Assumes the comparison phase doesn't permanently modify templates. If Step 6 routes templates to the winner before full validation, reverting requires manual admin work (acknowledged in rollback but not in this assumption's impact statement).

14. **Assumption:** *"Direct providers like Google Gemini or Mistral would triple the integration work for the comparison phase"*
    - **Testable before decision point?** Partially — can be estimated by reviewing ADR 0003's `OpenRouterProvider` abstraction and assessing adapter complexity for Gemini/Mistral. However, this is an estimate, not a measured fact.
    - **Missing assumptions:** Assumes the `OpenRouterProvider` abstraction cannot be extended to support direct provider routing without significant changes. If OpenRouter itself uses an OpenAI-compatible format (which it does), the "triple the work" claim may overstate the cost of direct APIs — some providers (Gemini, Mistral) also offer OpenAI-compatible endpoints.
    - **Impact if False accurate?** Partially. If direct APIs are actually 1.5× not 3× the integration cost, Option 5 becomes more viable as a follow-up optimization. The impact correctly identifies this as a scope decision but doesn't quantify the actual adapter effort.
    - **Hidden assumptions:** Assumes OpenRouter's aggregator markup is always positive (i.e., OpenRouter is always more expensive than direct). In some cases, OpenRouter may offer volume discounts or promotional pricing that beats direct API rates for low-volume users.

---

## Summary of Critical Issues

- **#8 contains a math error** ($720 vs actual ~$1,440 for 2× H100 persistent) — must be corrected.
- **Assumptions #5, #6 (quality estimates)** are the highest-risk unvalidated assumptions — they directly determine whether cheaper models pass the P0 quality bar. The ADR correctly flags them as directional but doesn't quantify the validation effort needed to reduce uncertainty.
- **Assumption #7 (60/40 template split)** is operationally critical for Option 1c but entirely unmeasured — no classification rubric or data source is identified.
- **Assumption #13 ("lost nothing")** oversimplifies the rollback reality by conflating routing revert with template state reversion.

## Alternative Completeness

# ADR Review: Alternatives Considered Section

## Findings

1. **Option numbering creates false hierarchy** — Options are labeled "1", "1b", "1c", "2", "3", "4", "5", implying 1b/1c are sub-variants of Option 1. However, Option 1c (hybrid) explicitly combines elements of both Option 1b AND Option 2, making it a cross-option hybrid rather than an Anthropic variant. This obscures the fact that the recommended approach IS a phased hybrid strategy, not a single alternative. The numbering should be flattened to A, B, C, D, E, F, G with clear labels for what each represents.

2. **Missing major alternatives in the comparison** — The ADR evaluates GLM, Qwen3, and gpt-oss families but omits:
   - **OpenAI GPT-4o-mini** via OpenRouter (dominant cost-quality leader at ~$0.15/1K output)
   - **Meta Llama 3.1 70B/405B** via OpenRouter (explicitly mentioned in Option 2's "model diversity" but never included in the comparison table)
   - **Cohere Command R+** or **Perplexity** as provider options
   These are significant gaps given the P0 cost reduction requirement — GPT-4o-mini alone could undercut all listed options.

3. **Option 5 (Direct APIs) dismissed without full criteria comparison** — The ADR states direct providers "offers potentially better pricing and lower latency" but dismisses them on integration simplicity grounds alone. It does NOT compare Option 5 against the P0 cost reduction criterion with concrete evidence. If Gemini 1.5 Flash or Mistral Large offer 20-30% better pricing than OpenRouter's aggregated rates, the integration tradeoff may not be worth it at scale. The dismissal should include a quantified cost comparison (e.g., "OpenRouter markup is ~15%; direct API savings would need to exceed X hours of adapter maintenance to justify").

4. **Option 3 (Bedrock) lacks pricing parity analysis** — The ADR compares Bedrock Llama 3.3 70B ($0.72/1K) against OpenRouter options but doesn't address whether the same Llama model is cheaper via OpenRouter directly. If OpenRouter offers Llama 3.3 70B at $0.55/1K, Bedrock's "fully managed" advantage must be weighed against a ~30% cost premium. The comparison matrix treats Bedrock as only relevant for VPC sovereignty but doesn't quantify the cost tradeoff for the same model across providers.

5. **Option 4 (RunPod) dismissal is evidence-based but incomplete** — The $720/month warm-worker estimate is reasonable and justifies the dismissal at current scale. However, it doesn't address the **break-even analysis** more rigorously: if monthly spend grows to $300-500 (as the ADR acknowledges), self-hosted becomes viable. The "Why not" should include a specific trigger condition (e.g., "Revisit if 6-month projected spend exceeds $400/month based on Step 0 measurements") rather than leaving it open-ended.

6. **No hybrid alternative combining Option 2 + Option 5** — The ADR presents OpenRouter and Direct APIs as mutually exclusive, but a valid hybrid would be: "Use OpenRouter for comparison phase (3+ model families), then migrate winning model family to direct API for production." This is mentioned in passing ("When to reconsider") but not evaluated as a formal alternative with cost/complexity tradeoffs. Given that the recommendation already includes escalation logic, this should have been Option 2b or a distinct hybrid option.

7. **Comparison matrix doesn't weight P0 vs P1 criteria** — The matrix treats all criteria equally in its visual comparison, but the Requirements section explicitly prioritizes cost reduction and integration simplicity as P0 (must-have) while structured output and latency are P1 (nice-to-have). Option 5 (Direct APIs) fails on "Integration simplicity" but might pass on cost — without weighting, it's unclear whether a P0 failure is fatal. The matrix should include a "P0 blocker" column or use different shading for P0 vs P1 criteria.

8. **Option 2 quality claim is aspirational, not validated** — The comparison matrix lists Option 2's model quality as "✅ Validated per-model," but validation hasn't occurred yet (it's part of Steps 3-4). This should be marked as "⚠️ To be validated" or similar. The disclaimer in the option text correctly notes estimates are directional, but the matrix contradicts this by using a checkmark.

9. **Missing alternative: Tiered routing within single provider** — The ADR considers hybrid routing (Option 1c) but doesn't evaluate it as a standalone alternative against the simpler options. For example: "Route journal analysis to Opus, quiz explanations to Haiku" is a valid strategy that requires no new provider. This should be Option 1d or explicitly compared in the matrix, since it achieves cost reduction without OpenRouter complexity.

10. **Recommendation contradicts alternatives structure** — The recommendation says "Start with Option 1b... then escalate to Option 1c or Option 2 based on results." This is a phased decision tree, not a single alternative choice. The ADR should have presented this as the primary decision (e.g., "Option H: Phased Hybrid") rather than treating it as a post-hoc recommendation that combines three options. This makes it unclear whether the team is committing to Option 1c or just using it as a fallback if Option 2 fails.

## Decision Coherence

# ADR Review: Coherence Between Decision Criteria and Chosen Decision

## 1. Does the chosen decision actually satisfy all 6 stated criteria?

**Finding 1:** The hybrid approach (Option 1c) does not fully satisfy the "Integration simplicity" criterion, despite being recommended as a likely outcome.

The ADR explicitly acknowledges this contradiction in Option 1c's cons:
> "Two providers to manage (Anthropic + OpenRouter). Requires classifying templates by complexity. Slightly more operational overhead than a single-provider approach."

Yet the recommendation states:
> "Start with Option 1b... then escalate to Option 1c (hybrid) or Option 2 (OpenRouter) based on results."

The hybrid approach is positioned as the "likely outcome" while simultaneously being flagged as having more operational overhead than simpler alternatives. This creates a coherence gap between the stated P0 criterion of "Integration simplicity" and the recommended path forward.

**Finding 2:** The decision does not guarantee satisfaction of the "Model quality" criterion (P0), only that it will be empirically validated.

The ADR states:
> "Must match or approach Claude Sonnet quality on our actual prompts (journal analysis, coaching, personality reports)"

But in Option 2's disclaimer:
> "Quality estimates are derived from general benchmarks... and may not transfer to domain-specific prompts... These numbers are directional — the entire point of this ADR's comparison phase is to measure actual quality on our prompts. Do not treat these as validated."

The decision is conditional on validation, not guaranteed to meet the P0 requirement. If no model clears the quality bar, the fallback is "stay with Anthropic," which means the cost reduction goal (P0) cannot be met. This creates a logical contradiction where two P0 criteria are mutually exclusive in the failure case.

**Finding 3:** The "Latency" criterion (P1) has an unresolved tension with OpenRouter routing.

The comparison matrix marks Option 1c and Option 2 as:
> "⚠️ OpenRouter adds hop" / "⚠️ Add network hop"

Yet the requirement states:
> "Acceptable for async Celery jobs (< 60s for typical requests)"

The ADR does not quantify how much latency OpenRouter's aggregation layer adds, nor does it establish a measurement baseline. The migration plan's Step 4 includes measuring latency, but there's no explicit gate stating that if latency exceeds the <60s threshold, those templates must remain on Anthropic regardless of cost savings.

## 2. Are there contradictions between stated goals and implementation details?

**Finding 4:** The goal states "simplest integration path possible" but recommends a hybrid approach that is explicitly more complex than single-provider alternatives.

Goal statement:
> "Find a cheaper model that matches Claude Sonnet quality for our specific prompts, using the simplest integration path possible."

Recommendation:
> "Start with Option 1b (assign more templates to Haiku), then escalate to Option 1c (hybrid) or Option 2 (OpenRouter) based on results."

Option 1c is acknowledged as having "Slightly more operational overhead than a single-provider approach" and requiring "Two providers to manage." The recommendation path moves from simpler (Option 1b) to more complex (Option 1c), contradicting the stated preference for simplicity.

**Finding 5:** There's an implicit contradiction between "Data sovereignty is not a constraint" and the PII mitigation requirements.

The context states:
> "Data sovereignty is not a constraint — third-party APIs are acceptable."

But Option 2 includes extensive PII mitigation:
> "During the comparison phase (Steps 3-4), all prompts sent to OpenRouter MUST use synthetic/redacted user data — this is mandatory, not optional. Real PII-bearing prompts may be tested in production only after: (a) model quality is confirmed, (b) the team reviews OpenRouter's data processing terms, and (c) a production migration path is chosen."

If data sovereignty isn't a constraint, why is there such extensive PII protection required? This suggests an unstated compliance or ethical concern that contradicts the explicit statement.

## 3. Is the decision truly reversible as claimed, or are there hidden lock-in effects?

**Finding 6:** The ADR claims reversibility but acknowledges a partial lock-in effect in the Rollback Triggers section itself.

Claimed reversibility (Option 2 pros):
> "Reversible — Disable openrouter provider to revert to Anthropic instantly"

Acknowledged limitation (Rollback Triggers):
> "Note: This reverts routing instantly, but does not revert template model assignments. Templates pointing at OpenRouter models (e.g., openrouter/glm-5.2) will fail if the provider is disabled."

The rollback procedure requires two steps:
1. Set `OPENROUTER_ENABLED=False` (instant)
2. "Reassign affected templates back to Anthropic models... via Django admin"

This means the decision is only partially reversible — you can disable routing, but your template assignments are broken and require manual cleanup. The ADR attempts to mitigate this by noting:
> "Document which templates were changed during the comparison phase so reversion is a known list, not a discovery exercise."

But this still represents a lock-in effect: if you don't document the changes, rollback becomes a discovery exercise rather than a defined procedure.

**Finding 7:** There's an implicit lock-in through cost tracking dependencies.

The ADR adds new fields to track costs:
> "Add `cost_usd` field to `PromptSummary` and `total_cost_usd` to `LlmUseSummary`."

If you rollback, these fields remain in the database schema (Django migrations are rarely reversed). More importantly, if your cost projections and budget decisions were based on OpenRouter pricing data, reverting to Anthropic means those cost models become invalid. The ADR doesn't address what happens to dashboards, alerts, or reporting that depend on `cost_usd` fields populated with OpenRouter pricing.

## 4. Does the migration plan have a realistic rollback that doesn't cut corners?

**Finding 8:** The rollback triggers are defined but lack explicit "experiment failed" criteria with measurable thresholds for early termination.

The Rollback Triggers section defines:
- Quality regression: >2 user-facing quality complaints per week
- Structured output failure rate: >5% JSON parse errors
- Provider outage: OpenRouter unreachable for >5 minutes during business hours

But there's no trigger for the core hypothesis failing: "No model matches Sonnet quality." The ADR mentions this in Option 1's "When to choose":
> "If quality cannot be matched by anything cheaper, or if the team has no capacity for evaluation."

And in Step 5 (Decision Gate):
> "Pick cheapest model that clears quality bar. If none clear, stay with Anthropic."

But there's no explicit rollback trigger tied to this outcome. The comparison phase could complete without any model passing, but the ADR doesn't define what happens next — does the team re-evaluate in 6 months? Does it escalate to leadership? This is a gap in the rollback framework.

**Finding 9:** The migration plan's Step 7 assumes successful production validation without defining what "production validation passes" means beyond quality and latency.

Step 7 states:
> "If production validation passes: route remaining templates. Disable Anthropic fallback for OpenRouter templates."

But there's no explicit pass/fail criteria for production validation in Step 6 beyond:
- "No quality regressions"
- "Latency < 60s p95"
- "Cost reduction confirmed vs baseline"

What about structured output reliability? What about error rates? The Rollback Triggers section defines failure thresholds, but the migration plan doesn't define success thresholds for the full rollout decision. This creates ambiguity: if you're at 4% JSON parse errors (below the >5% rollback trigger), do you proceed to Step 7 or stay in Step 6?

**Finding 10:** The rollback procedure doesn't address data consistency between old and new cost tracking.

The migration adds `cost_usd` fields to track costs. If you rollback from OpenRouter back to Anthropic:
- Historical `cost_usd` values were calculated using OpenRouter pricing
- Future `cost_usd` values will use Anthropic pricing
- Dashboards and reports comparing "before" and "after" become misleading

The ADR doesn't address whether historical cost data should be purged, annotated with the provider that generated it, or if reporting queries need to filter by provider. This is a data integrity gap in the rollback plan.

## Implementation Readiness

# ADR Review: LLM Cost Optimization — Model and Provider Selection

## Verdict
**REQUEST CLARIFICATION**

## Executive Summary
The ADR has a solid strategic framework but critical implementation gaps in the migration plan. The cost tracking code snippet has logical issues, missing files/modules aren't identified, and several validation gates lack pass/fail criteria. The integration point with existing infrastructure (`track_usage`) isn't clearly specified.

## Dimension Scores

| Dimension | Score (1-5) | Key Finding |
|-----------|-------------|-------------|
| Assumption Validity | 4 | Good assumption table, but missing validation for OpenRouter API compatibility |
| Alternative Completeness | 5 | Comprehensive options analysis with clear tradeoffs |
| Decision Coherence | 3 | Migration plan has dependency gaps; cost tracking location unspecified |
| Implementation Readiness | 2 | Missing files/modules not identified; code snippet has logical issues |
| Risk Coverage | 4 | Good risk table, but PII mitigation lacks automation criteria |
| Testability | 3 | Validation gates partially specific; synthetic data testing approach unclear |

## Critical Gaps (Must Fix)

1. **Missing integration file**: The ADR references `OpenRouterProvider` from ADR 0003 but doesn't specify the new file to create (e.g., `memores/external/openrouter_provider.py`). The existing `memores/external/claude_api.py` is shown, but no OpenRouter equivalent exists in the file tree.

2. **Cost calculation location unspecified**: The `calculate_cost()` function in "Cost Tracking" section is standalone. It must be integrated into `track_usage()` in `memores/external/usage_tracker.py`, but this integration isn't shown. The current signature is:
   ```python
   def track_usage(
       user,
       analysis_output: AnalysisOutput,
       prompt_template: PromptTemplate,
       input_tokens: int,
       output_tokens: int,
       elapsed_seconds: int,
   ) -> None:
   ```
   Where does `cost_usd` get calculated and stored?

3. **Code snippet logical bug**: The pricing dict structure silently zeros costs when a model isn't found:
   ```python
   rates = pricing.get(provider, {}).get(model, {"input": 0, "output": 0})
   ```
   If `provider="anthropic"` and `model="claude-opus-4-8"` (not in dict), cost becomes $0 instead of raising an error. Should use explicit lookup with validation.

4. **Settings location ambiguous**: Step 2 says "Add `OPENROUTER_API_KEY` to `.env.example` and settings" but doesn't specify which settings file (`memores/settings.py`, `loadtest_settings.py`, or environment-only). The project uses Django settings, so this needs clarification.

5. **Validation gate vagueness**: Step 0 gate says "Dashboard showing actual tokens/month" — what dashboard? Django admin custom view? Management command output? Needs specific tool/command reference.

6. **PII compliance verification manual**: Step 4 gate requires "PII compliance verified (no real user data in OpenRouter requests)" but provides no automated check. How is this validated? Manual review? Log scanning?

7. **Structured output confirmation missing test criteria**: Step 3 gate says "structured output confirmed for each candidate" but doesn't specify the test payload or expected response format to validate `response_format` support.

## Strengths
- Migration plan has clear decision gates (Step 5) that prevent over-engineering if Haiku works
- Cost tracking addition is well-motivated and addresses real opacity in current spend
- PII mitigation strategy with synthetic data requirement is appropriately cautious
- Rollback procedure distinguishes between routing revert (`OPENROUTER_ENABLED=False`) and template reassignment

## Suggestions (Nice to Have)
- Add a Step 2.5: "Create `memores/external/openrouter_provider.py` following `claude_api.py` patterns"
- Specify the exact settings path for new env vars (e.g., `memores/settings.py` line X)
- Include a sample synthetic prompt in an appendix to clarify "synthetic user data" expectations
- Add automated PII scan as part of Step 4 gate (e.g., regex check on prompts before sending to OpenRouter)

## Missing Questions from Reviewer
1. Does ADR 0003's `OpenRouterProvider` actually exist in the codebase, or is this ADR also implementing it?
2. Where exactly should the cost calculation logic live — modify `track_usage()` or create a new service function?
3. What's the exact format for `OPENROUTER_MODELS` configuration (JSON string? Python list? Database field)?
4. How will Step 0 "dashboard" be implemented — Django admin custom view, management command, or external tool?
5. What's the automated PII verification mechanism for Step 4 gate?

## Risk Coverage

# ADR Review: Risk Analysis Findings

## 1. Unmitigated or Weakly Mitigated Risks

**Risk #1: "No model matches Sonnet quality"** — **UNMITIGATED**
- The mitigation ("Stay with Anthropic; re-evaluate in 6 months") is an acceptance, not a mitigation. There's no process defined for *how* to determine if quality is insufficient (beyond the vague ">2 user complaints" rollback trigger), and no fallback model strategy if all OpenRouter candidates fail.
- **Missing:** A clear "stop condition" for the comparison phase with documented decision criteria for when to abandon the experiment entirely.

**Risk #9: "Haiku quality insufficient for nuanced templates"** — **WEAKLY MITIGATED**
- The mitigation ("Test Haiku on actual prompts in Step 0") is a validation step, not a risk reduction strategy. It doesn't address what happens if Haiku fails on *all* templates (not just the nuanced ones).
- **Missing:** A contingency plan for the scenario where no model within budget clears quality bar — does the team accept degraded quality, or revert to Sonnet-only?

**Risk #5: "Model quality degrades silently (provider updates)"** — **PARTIALLY MITIGATED**
- References monitoring `resolved_model_version` but doesn't specify *how* version changes are detected (manual check? automated alert? drift detection?). The phrase "Set alert if version changes between requests" lacks implementation specificity.
- **Missing:** A concrete monitoring mechanism (e.g., "Add OpenTelemetry span attribute for resolved model; alert via PagerDuty on version mismatch").

**Risk #8: "Cost tracking pipeline breaks"** — **WEAKLY MITIGATED**
- The mitigation ("Validate `cost_usd` population in Step 2") is a one-time check, not ongoing monitoring. The phrase "fire PagerDuty/alert" doesn't specify the tool or threshold.
- **Missing:** A specific alerting rule (e.g., "Alert if >5% of LLM calls have null `cost_usd` over 1-hour window").

---

## 2. Missing Risk Categories

**3. Data Loss / Corruption During Migration**
- The migration plan involves reassigning templates to new models. If a template is reassigned mid-generation (e.g., user starts a journal analysis, then the template switches to Haiku), could this cause inconsistent outputs or orphaned data?
- **Missing:** Risk assessment for partial migrations and data integrity during cutover.

**4. Rollback Failure Under Pressure**
- The rollback procedure requires manual template reassignment via Django admin ("reassigning affected templates back to Anthropic models"). If the team is under time pressure (e.g., quality complaints flooding in), this manual step could fail or be forgotten.
- **Missing:** An automated rollback mechanism (e.g., a management command that reverts all OpenRouter-assigned templates in one click) and a documented "rollback drill" to validate it works.

**5. Third-Party Compliance / Legal Risk**
- The ADR mentions PII exposure but doesn't address the legal/compliance implications of sending health/wellness data (journal entries, personality responses) to OpenRouter as a third-party aggregator. OpenRouter routes traffic to underlying providers (GLM, Qwen, etc.) — does OpenRouter's ToS allow this? Does the team need a DPA with OpenRouter specifically?
- **Missing:** A legal review gate before production PII is sent to OpenRouter, beyond the vague "DPA review completion" mention.

**6. Cost Tracking Accuracy Risk**
- The `calculate_cost` function uses hardcoded pricing that may not match actual OpenRouter pricing (which varies by model, region, and request type). If rates are wrong, cost projections are meaningless.
- **Missing:** A validation step to compare calculated costs against OpenRouter's actual billing dashboard monthly.

---

## 3. Mitigation Specificity Gaps

**Risk #6: "OpenRouter provider outage"** — **SPECIFIC THRESHOLDS, VAGUE IMPLEMENTATION**
- References `tenacity` retry and a circuit breaker ("if 3+ consecutive requests fail with 5xx"), but doesn't specify *where* the circuit breaker lives (provider wrapper? middleware?). The phrase "automatically disable `openrouter` provider for 5 minutes" implies state management that isn't described.
- **Missing:** A code-level description of the circuit breaker implementation (e.g., "Use `tenacity.stop_after_attempt(3)` with a custom exception handler that sets `OPENROUTER_ENABLED=False` in Redis for 5 minutes").

**Risk #7: "Rate limiting on free/low-tier OpenRouter"** — **SPECIFIC METRIC, VAGUE ACTION**
- References monitoring `LLM_REQUEST_ERRORS` metric but doesn't define what action to take when rate limits are hit beyond retrying. The phrase "Monitor 429 rates" is passive.
- **Missing:** An escalation path (e.g., "If >5% of requests hit 429, switch to paid tier; if persistent, reduce concurrent Celery workers").

**Risk #3: "Structured output failure on cheaper models"** — **VAGUE FALLBACK MECHANISM**
- The mitigation ("Fallback to Anthropic for templates requiring `output_schema`") doesn't specify *how* the fallback is triggered (manual template reassignment? automated routing rule?).
- **Missing:** A code-level description of the fallback logic (e.g., "If JSON parse fails, retry with `anthropic/claude-sonnet-4-6`; log failure rate per template").

---

## 4. Rollback Trigger Measurability Issues

**Trigger #1: "Quality regression"** — **MEASURABLE BUT AMBIGUOUS AUTOMATED SCORE**
- The threshold ">2 user-facing quality complaints per week" is measurable but slow (relies on user feedback). The automated score "<3.5/5" lacks definition — how is it calculated? What dimensions are scored? Who validates the scoring methodology?
- **Missing:** A defined automated quality evaluation pipeline (e.g., "Run 20-sample blind comparison weekly; score via LLM-as-judge with prompt template `quality_eval_v1`").

**Trigger #3: "Cost tracking failure"** — **OVERLY SENSITIVE THRESHOLD**
- The threshold "if any LLM call has null `cost_usd`" would fire constantly during migration if not all code paths are updated. This could cause alert fatigue or false rollbacks.
- **Missing:** A grace period or percentage-based threshold (e.g., "Alert if >5% of LLM calls have null `cost_usd` over 1-hour window").

**Trigger #4: "Provider outage"** — **SPECIFIC BUT MISSING AUTOMATION**
- The thresholds (>5 min business hours, >15 min outside) are measurable and appropriate. However, the action ("Fallback to Anthropic automatically") implies automation that isn't described in the mitigation for Risk #6.
- **Missing:** Confirmation that the circuit breaker (Risk #6 mitigation) implements this exact timeout logic.

---

## Summary of Critical Gaps

1. **No stop condition** for the comparison phase if all models fail quality bar.
2. **Manual rollback procedure** lacks automation and validation drill.
3. **Legal/compliance review** for OpenRouter's data handling practices is undefined.
4. **Cost tracking accuracy** isn't validated against actual billing.
5. **Rollback trigger #3** (cost tracking failure) is too sensitive for production use.

## Testability

# ADR Review: LLM Cost Optimization — Model and Provider Selection

## Verdict
REQUEST CLARIFICATION

## Executive Summary
The ADR lacks an explicit testing strategy for the **software implementation** of the provider routing change, conflating empirical model quality evaluation (manual blind comparison) with automated test coverage. No specific test files are enumerated, no integration tests for cross-provider routing are specified, and migration gates lack measurable automation criteria.

## Dimension Scores

| Dimension | Score (1-5) | Key Finding |
|-----------|-------------|-------------|
| Assumption Validity | 3 | Quality estimates explicitly labeled as directional; PII risk mitigated but DPA path undefined |
| Alternative Completeness | 4 | 6 options evaluated with clear rejection criteria; hybrid option (1c) well-justified |
| Decision Coherence | 3 | Recommendation contradicts stated P0 integration simplicity goal (two providers to manage) |
| Implementation Readiness | 2 | **No test files enumerated**; migration gates lack pass/fail automation criteria |
| Risk Coverage | 4 | Comprehensive risk table with mitigations; rollback triggers measurable |
| Testability | 1 | **Zero specificity** on which tests to create/modify; quality evaluation is manual, not automated |

## Critical Gaps (Must Fix)

1. **No test files enumerated.** The ADR does not list the "4 test files" referenced in the review request. It mentions testing only in the quality evaluation methodology (blind comparison of outputs), which is an empirical evaluation process, not a software testing strategy. Must specify: which existing tests to update, what new tests to create, and expected line counts for each.

2. **No end-to-end provider routing tests.** The core behavioral change is switching LLM traffic between Anthropic and OpenRouter. No test file is specified to verify:
   - `OpenRouterProvider` correctly routes requests when enabled/disabled
   - Template model assignments (`anthropic/claude-sonnet-4-6` vs `openrouter/glm-5.2`) resolve to correct providers
   - Circuit breaker triggers on 5xx responses from OpenRouter

3. **No unit tests for cost tracking logic.** The ADR introduces a new `calculate_cost()` function with hardcoded pricing dictionaries. No test file is specified to verify:
   - Cost calculation accuracy across all provider/model combinations
   - Graceful handling of unknown providers/models (returns $0)
   - Token count normalization (/1000 divisor)

4. **No structured output validation tests.** Option 2 explicitly lists "structured output" as a P1 requirement and notes edge cases for `response_format`. No test file is specified to verify JSON mode works across GLM, Qwen3, and gpt-oss families.

5. **Migration gates lack automation criteria.** Step 0 gate: "Dashboard showing actual tokens/month" — how is this validated? Step 4 gate: "Quality scores recorded per model" — where are they stored? What format? Without automated validation, gates become checkbox exercises.

6. **No mocking strategy for external LLM calls.** The existing test suite (`memores/tests/`) uses factory_boy and in-memory databases. No mention of how to mock `OpenRouterProvider` or `ClaudeAPI` during tests to avoid real API calls and costs.

## Strengths
- Explicit PII mitigation with mandatory synthetic data requirement (Step 4 gate)
- Measurable rollback triggers (>5% JSON parse errors, >10% 429 rates)
- Two-step rollback procedure (env var + template reassignment) prevents silent failures
- Cost tracking fields (`cost_usd`, `total_cost_usd`) enable future decision-making

## Suggestions (Nice to Have)
- Add a "Test Plan" section specifying which of the ~35 existing test files need updates and what new files to create under `memores/tests/external/` or `memores/tests/services/`
- Define automated quality scoring (e.g., LLM-as-judge pipeline) rather than relying on manual blind evaluation
- Specify expected line counts for each test file (e.g., "test_provider_routing.py: ~120 lines, 8 test methods")

## Missing Questions from Reviewer
1. Which specific existing test files must be updated to cover the provider routing change?
2. What new test file(s) should be created under `memores/tests/external/` for OpenRouter integration?
3. How will "quality scores recorded per model" (Step 4 gate) be stored and validated automatically?
4. Will existing Celery job tests (`memores/tests/jobs/`) need to mock the new provider, or is a new test module required?
5. What is the rollback trigger for the cost tracking pipeline if `cost_usd` remains null after migration?
