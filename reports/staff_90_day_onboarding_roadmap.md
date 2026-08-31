# 90-Day Onboarding Strategy — Memores API

---

## 1. Executive Summary & Core Codebase Impressions

The Memores API is a Django 5.2 / DRF backend for a multi-tenant personality-assessment and coaching platform. The single `memores` app contains **36 concrete models + 1 abstract (`SoftDeleteModel`)**, **75 serializers**, and **106 views** (82 class-based, 24 function-based). The domain spans course delivery, LLM-powered analysis (via `PromptTemplate` → `AnalysisOutput` with Celery-backed async processing), multi-tenant benefactor scoping, Stripe billing, and a simulated-data pipeline for benchmarking.

**Core impressions:**

- **Monolithic app, modular intent.** The `memores` app is the sole custom app, yet its internal structure (`views/app/`, `views/admin/`, `views/management/`, `views/public/`, `views/payment/`, `services/results_analysis/`) shows a team that has been *organizing by domain* within a single app. This is a transitional state: the boundaries are drawn in directory structure but not enforced by module boundaries, import discipline, or separate Django apps.

- **LLM integration is the active frontier.** Recent commits (`ef6d33c Add json-repair`, `37cbc4e Optimize output schema for effective system prompt caching`, `2f5f0e2 Support temperature and stopSequences in inferenceConfig`, `13eabb5 respect the stream parameter`) show the team is mid-flight on LLM pipeline hardening. `PromptTemplate` now carries `model`, `temperature`, `stop_sequences`, `output_schema` — fields that were likely added in the last 2–3 sprints. The `LlmUseSummary` and `PromptSummary` models track token economics, but the service layer that orchestrates calls is not visible in the topography.

- **Authorization is the weakest structural layer.** 10+ custom permission classes exist in `memores/permissions.py`, but 8 of them have `has_permission: false, has_object_permission: false` in the parser output, meaning they perform a single boolean check (likely `request.user.is_staff` or `request.user.user_type`). No permission class implements `has_object_permission`. Object-level security depends entirely on `get_queryset` scoping, which the parser marks `self_scoped: true` for many views but cannot verify the filter logic.

- **The soft-delete pattern is half-applied.** 7 of 36 concrete models inherit `SoftDeleteModel` (`UserCourseCompletion`, `CourseProgress`, `AnalysisOutput`, `SharingCode`, `CoachEntry`, `JournalEntry`, `EmailReportRequest`). `Course` does not. This asymmetry creates a cascade-delete hazard: deleting a `Course` hard-deletes child `UserCourseCompletion` and `CourseProgress` rows, bypassing their `is_deleted` flags.

- **i18n is a pervasive multiplier.** `modeltranslation` is installed; `QuestionGroup`, `ResponseOption`, `ResponseGroup`, `Course`, `Audio`, `BenefactorCohort` all carry `_en`, `_es`, `_fr`, `_pt` translation fields. Every serializer, every migration, and every query must account for the active language. This is invisible complexity that compounds with every new model or field.

- **The CI/CD pipeline is in active flux.** Five workflow files are modified in the working tree (`ci.yml`, `deploy.yml`, `load-test-after-deploy.yml`, `release.yml`, `dependabot.yml`), and an untracked `.opencode/` directory exists. The team is restructuring automation.

---

## 2. Major Technical & Structural Risks

### Risk 2.1 — Unauthenticated / Under-Authenticated Admin Data Endpoints (CONFIRMED)

**Severity: CRITICAL**

Five function-based views in `memores/views/admin/data.py` and `memores/views/management/content.py` are gated only by `IsAuthenticated` with no role or scope check:

| View | File | Permission Classes | Exposes |
|---|---|---|---|
| `get_unstructured_interactions` | `memores/views/admin/data.py` | `["IsAuthenticated"]` | All `UnstructuredUserInteraction` rows across all users — PII (audio timestamps, response types) |
| `get_user_responses` | `memores/views/admin/data.py` | `["IsAuthenticated"]` | All `UserResponse` rows — PII (question answers, sentiment) |
| `get_course_durations` | `memores/views/admin/data.py` | `["IsAuthenticated"]` | Course analytics data |
| `CourseKeysListView` | `memores/views/management/content.py` | `["IsAuthenticated"]` | All `Course.course_key` values |
| `CoursePathsListView` | `memores/views/management/content.py` | `["IsAuthenticated"]` | All `Course.course_path` values |

Additionally, `check_job_status` in `memores/views/app/jobs.py` and `sharing_code_validation` in `memores/views/app/sharing_code.py` lack `IsAppUser`, and `handle_s3_uploads` in `memores/views/management/content.py` lacks any role gate.

**Why this matters:** Any authenticated user — including a standard app user with `user_type` set to a non-staff value — can enumerate all user responses and unstructured interactions. This is a cross-tenant PII leak.

### Risk 2.2 — IDOR on `SharingCodeRetrieveUpdateDestroyView` (CONFIRMED)

**Severity: HIGH**

`memores/views/app/sharing_code.py` → `SharingCodeRetrieveUpdateDestroyView`:
- `queryset: "SharingCode.objects.all"` (the default manager, which filters `is_deleted=False` via `SoftDeleteModel`)
- `lookup_field: "code"` — a `CharField(unique=True)`
- No `get_object` override, no `get_queryset` override in the methods list
- `permission_classes: ["IsAuthenticated", "IsAppUser"]`

Any authenticated app user can `PUT` or `DELETE` any `SharingCode` by supplying its `code` string. The `code` field is a `CharField` with no visible length constraint in the model definition (the live schema shows `CharField` with no `max_length`), making brute-force enumeration feasible if codes are short.

### Risk 2.3 — Unauthenticated Stripe Webhook Path (UNCERTAIN — Parser Limitation)

**Severity: CRITICAL if unverified**

`memores/views/payment/stripe.py` → `create_registration_code_from_checkout_session`:
- `permission_classes: []`, `authentication_classes: []`
- `http_methods: ["POST"]`
- No `inline_auth_calls` detected

This view creates a `RegistrationCode` from a Stripe checkout session. The topography shows zero authentication and zero permission classes. If Stripe signature verification is performed via a decorator, middleware, or inline call that the parser cannot resolve (the parser explicitly cannot inspect method bodies beyond stub detection and auth-call scanning), this is a false alarm. **However, the absence of any visible signature check is a confirmed gap in the static analysis.** This must be verified by reading the file.

### Risk 2.4 — Mass Assignment via `CreateUpdateUserSerializer` (CONFIRMED)

**Severity: HIGH**

`memores/serializers/user_serializers.py` → `CreateUpdateUserSerializer`:
- `Meta.fields` includes: `"is_active"`, `"is_deleted"`, `"stripe_customer_id"`, `"meta"`, `"password"`
- No `read_only_fields` declared in the Meta

If this serializer is used in any view without an explicit `read_only_fields` override or a separate admin-only serializer, a user can:
- Set `is_active=True` to resurrect a deactivated account
- Set `is_deleted=False` to bypass soft-delete
- Overwrite `stripe_customer_id` to redirect billing
- Inject arbitrary JSON into `meta`

The `UserSerializer` (same file) does declare `read_only_fields: ["id"]` but does **not** protect `is_active`, `is_deleted`, or `stripe_customer_id`.

### Risk 2.5 — `CoachEntryCreateSerializer` Exposes `is_deleted` (CONFIRMED)

**Severity: MEDIUM**

`memores/serializers/coach_serializers.py` → `CoachEntryCreateSerializer`:
- `Meta.fields` includes `"is_deleted"`
- No `read_only_fields` for `is_deleted`

A user creating a `CoachEntry` via `CoachListCreateView` (`memores/views/app/coach.py`) can set `is_deleted=True` to hide their own entry from list views, or `is_deleted=False` to resurrect a soft-deleted entry.

### Risk 2.6 — `EmailReportRequestCreateSerializer` Exposes `status` (CONFIRMED)

**Severity: MEDIUM**

`memores/serializers/email_report_request_serializers.py` → `EmailReportRequestCreateSerializer`:
- `Meta.fields` includes `"status"`
- The model default is `JobStatuses.PENDING`

A user can set `status` to `FINISHED` at creation time, bypassing the Celery task pipeline that generates the actual report. The `AdminStartEmailReportView` and `AdminResetEmailReportView` in `memores/views/admin/email_report_request.py` exist to manage this state, but the create path does not enforce it.

### Risk 2.7 — `DEBUG = True` in Live Environment (CONFIRMED)

**Severity: CRITICAL if production**

The live Django app info reports `"debug_mode": true`. If this reflects the production or staging environment, it exposes:
- Full stack traces to end users
- SQL query logging
- `django_extensions` management commands (installed in `INSTALLED_APPS`)
- Potential template source disclosure

### Risk 2.8 — Cascade Hard-Delete Bypasses Soft-Delete (CONFIRMED, EXPECTED BUT RISKY)

**Severity: MEDIUM**

`Course` inherits from `models.Model` (confirmed: no `is_deleted` field, no `SoftDeleteModel` base). `CourseDestroyView` in `memores/views/admin/admin.py` uses `queryset: "Course.objects.all"` and performs a hard delete. Per ground truth, this is expected behavior. However, the cascade will hard-delete:
- `UserCourseCompletion` rows (soft-delete model)
- `CourseProgress` rows (soft-delete model)
- `AnalysisResult` rows (no soft-delete, but no audit trail)
- `CourseSession`, `QuestionMap`, `CourseMap` rows

The `is_deleted` flags on `UserCourseCompletion` and `CourseProgress` are silently bypassed. If any reporting or analytics query filters on `is_deleted=False`, the cascade creates an inconsistency: the parent `Course` is gone, but the child soft-delete flags are irrelevant because the rows are physically deleted.

### Risk 2.9 — `PromptSummary` Unbounded Growth (CONFIRMED)

**Severity: MEDIUM (long-term)**

`PromptSummary` has a `OneToOneField` to `AnalysisOutput`. Every LLM analysis call creates a `PromptSummary` row. With no visible archival or aggregation job, this table grows linearly with usage. `LlmUseSummary` (1:1 with `PromptTemplate`) aggregates the same metrics at the template level, making `PromptSummary` redundant for reporting purposes after a retention window.

### Risk 2.10 — No Object-Level Permission Checks (CONFIRMED)

**Severity: HIGH**

All 10+ custom permission classes in `memores/permissions.py` show `has_object_permission: false` in the parser output. This means:
- `AdminBenefactorRetrieveView` (ground-truth name; topography lists `AdminBenefactorRetrieveUpdateView` at `memores/views/admin/benefactor.py`) relies on `get_queryset` scoping via `IsStaffOrBenefactorScopeOwner`
- `AdminUserPermissionUpdateView` at `memores/views/admin/user.py` uses `queryset: "User.objects.select_related.all"` with `IsStaffOrSuperUser` — any staff user can update any user's permissions
- `AdminAnalysisOutputRetrieveDestroyView` at `memores/views/admin/analysis_output.py` uses `AnalysisOutput.all_objects.all.select_related` — includes soft-deleted records, gated by `IsLowerEnv`

The absence of `has_object_permission` means a misconfigured `get_queryset` silently grants cross-tenant access.

---

## 3. Immediate Quick Wins (Weeks 1–3)

### Week 1: Security Patches (Ship in 2–3 PRs)

**QW-1: Gate admin data endpoints with role permissions**
- **Files:** `memores/views/admin/data.py`
- **Classes:** `get_unstructured_interactions`, `get_user_responses`, `get_course_durations`
- **Change:** Add `"IsStaffOrSuperUser"` to `permission_classes` on all three function views. These are admin analytics endpoints; no app user should access them.
- **Effort:** 3 lines. **Risk:** None. These endpoints are not referenced by any app-facing URL pattern.

**QW-2: Gate content management list views**
- **Files:** `memores/views/management/content.py`
- **Classes:** `CourseKeysListView`, `CoursePathsListView`, `handle_s3_uploads`
- **Change:** Add `"IsCreator"` or `"IsStaffOrSuperUser"` to `permission_classes`. `CourseKeysListView` and `CoursePathsListView` expose `Course.course_key` and `Course.course_path` — content identifiers that should not be visible to all authenticated users.
- **Effort:** 3 lines. **Risk:** Low. Verify that the content-creation frontend does not call these endpoints as a non-creator.

**QW-3: Scope `SharingCodeRetrieveUpdateDestroyView`**
- **File:** `memores/views/app/sharing_code.py`
- **Class:** `SharingCodeRetrieveUpdateDestroyView`
- **Change:** Add a `get_queryset` method that filters `SharingCode.objects.filter(user=request.user)` (or by sharing code ownership chain). Currently `queryset: "SharingCode.objects.all"` with `lookup_field: "code"` and no override.
- **Effort:** 5 lines. **Risk:** Low. The `SharingCode` model has a `user` FK; scoping by `request.user` is the natural boundary.

**QW-4: Verify Stripe webhook signature**
- **File:** `memores/views/payment/stripe.py`
- **Class:** `create_registration_code_from_checkout_session`
- **Action:** Read the file. Confirm that Stripe signature verification (`stripe.Webhook.construct_event` or equivalent) is present. If it is in a decorator or middleware not visible to the parser, add a comment documenting the verification path. If it is absent, add it before the next deploy.
- **Effort:** 1 hour investigation + 0–30 minutes fix. **Risk:** Critical if unverified.

**QW-5: Confirm `DEBUG` setting**
- **Action:** Check `settings.py` (or equivalent) for `DEBUG = True`. The live app info reports `debug_mode: true`. If this is the production or staging environment, set `DEBUG = False` and ensure `ALLOWED_HOSTS` is configured.
- **Effort:** 10 minutes. **Risk:** Critical if production.

### Week 2: Serializer Hygiene (1 PR)

**QW-6: Remove `is_deleted` from `CoachEntryCreateSerializer`**
- **File:** `memores/serializers/coach_serializers.py`
- **Class:** `CoachEntryCreateSerializer`
- **Change:** Remove `"is_deleted"` from `Meta.fields` or add it to `read_only_fields`. The model default is `False` (via `SoftDeleteModel`); a user should not set this at creation.
- **Effort:** 1 line. **Risk:** None.

**QW-7: Remove `status` from `EmailReportRequestCreateSerializer`**
- **File:** `memores/serializers/email_report_request_serializers.py`
- **Class:** `EmailReportRequestCreateSerializer`
- **Change:** Remove `"status"` from `Meta.fields`. The model default is `JobStatuses.PENDING`; the status should be set by the Celery task pipeline, not by the client.
- **Effort:** 1 line. **Risk:** None. Verify that `AdminStartEmailReportView` and `AdminResetEmailReportView` in `memores/views/admin/email_report_request.py` set the status server-side.

**QW-8: Protect `CreateUpdateUserSerializer` writable fields**
- **File:** `memores/serializers/user_serializers.py`
- **Class:** `CreateUpdateUserSerializer`
- **Change:** Add `"is_active"`, `"is_deleted"`, `"stripe_customer_id"` to `read_only_fields` in `Meta`. If admin views need to write these fields, create a separate `AdminUserUpdateSerializer` with those fields writable.
- **Effort:** 10 minutes. **Risk:** Low. Audit all views that use `CreateUpdateUserSerializer` to confirm none rely on writing these fields.

**QW-9: Remove stub HTTP methods**
- **Files:**
  - `memores/views/app/analysis.py` → `AnalyseCourseResults`: remove `patch` and `delete` stubs
  - `memores/views/management/content.py` → `ManageCreatorContent`: remove `post` and `delete` stubs
  - `memores/views/public/registration.py` → `RegistrationCompleteView`: remove `put` and `delete` stubs
- **Change:** Delete the stub methods that return `HttpResponseNotAllowed`. DRF's `APIView` already returns 405 for unimplemented methods.
- **Effort:** 15 minutes. **Risk:** None.

### Week 3: Query Performance & Schema (1–2 PRs)

**QW-10: Add `select_related`/`prefetch_related` to course list views**
- **Files:** `memores/views/app/course.py`
- **Classes:** `CourseListView`, `CourseRetrieveView`
- **Change:** The `CourseFullListSerializer` (used by both views) computes `is_completed`, `completed_at`, `progress_percentage`, `blocked`, `blocked_by_reason`, `course_status` — each likely triggering a query against `UserCourseCompletion` or `CourseProgress`. Add `select_related('content_creator')` and `prefetch_related('course_map_set', 'courseprovidermap_set')` to `get_queryset`.
- **Effort:** 30 minutes. **Risk:** Low. Verify with `django-debug-toolbar` or `django-silk` that query count drops.

**QW-11: Add `created_at`/`updated_at` to content models**
- **Models:** `Course`, `Question`, `Audio`, `CourseGroup`, `BenefactorCohort` (in `memores/models.py`)
- **Change:** Add `created_at = models.DateTimeField(auto_now_add=True)` and `updated_at = models.DateTimeField(auto_now=True)` via a migration. These models have no audit timestamps.
- **Effort:** 1 migration + 5 model edits. **Risk:** Low. Backfill with `timezone.now()` for existing rows.

**QW-12: Add unique constraint on `UserResponse(user, question, course)`**
- **Model:** `UserResponse` in `memores/models.py`
- **Change:** Add `unique_together = [('user', 'question', 'course')]` or a `UniqueConstraint`. Prevents duplicate response rows.
- **Effort:** 1 migration. **Risk:** Low. Run a dedup query first to check for existing duplicates.

---

## 4. Strategic Architecture & Database Investments (Months 2–3)

### 4.1 — Extract an LLM Service Layer (Month 2, Weeks 5–7)

**Rationale:** `PromptTemplate` (with `model`, `temperature`, `stop_sequences`, `output_schema`, `system_prompt`, `output_limit`), `AnalysisOutput` (with `status` using `JobStatuses`: PENDING, IN_PROGRESS, FINISHED, ERROR; `task_id`; `error_message`), `LlmUseSummary`, and `PromptSummary` form a cohesive domain that is currently scattered across views, Celery tasks (invisible in topography), and the `services/results_analysis/` sub-package.

**Concrete actions:**
- Create `memores/services/llm/` with:
  - `LLMService` — encapsulates prompt resolution from `PromptTemplate`, model selection, token counting, output parsing (the `json-repair` dependency added in commit `ef6d33c` suggests JSON parsing is fragile and should be centralized), and usage aggregation into `LlmUseSummary`/`PromptSummary`.
  - `AnalysisOrchestrator` — manages the `AnalysisOutput` lifecycle: create with `status=JobStatuses.PENDING`, dispatch Celery task, update to `IN_PROGRESS`, handle completion to `FINISHED` or failure to `ERROR` with `error_message`.
- Migrate `AnalyseCourseResults` (`memores/views/app/analysis.py`), `AnalysisExplanationView`, `AnalysePersonalityResults`, and `start_journal_analysis` (`memores/views/app/journal.py`) to call `LLMService` instead of inline logic.
- Add a `PromptTemplate` validation step: when `output_schema` is set, validate it against a JSON Schema before the template can be activated (`is_active=True`).

### 4.2 — Extract a Course Progression Service (Month 2, Weeks 6–8)

**Rationale:** Five function views in `memores/views/app/course.py` (`start_or_end_course`, `post_play_pause`, `post_heart`, `post_user_response`, `check_profile_completion`) all mutate `CourseProgress`, `UserCourseCompletion`, `UserAudioCompletion`, and `UnstructuredUserInteraction`. The state transitions are implicit.

**Concrete actions:**
- Create `memores/services/course_progression/` with:
  - `ProgressionService` — explicit state machine: `STARTED → IN_PROGRESS → COMPLETED`, with `progress_percentage` calculation.
  - `AudioTrackingService` — handles `UserAudioCompletion` and `UnstructuredUserInteraction` creation.
  - `ResponseService` — handles `UserResponse` creation with `answer_group` and `order` logic.
- Add a `CourseProgress` unique constraint on `(user, course, session)` to prevent duplicate progress rows.
- Add a `pre_delete` signal on `Course` that soft-deletes child `UserCourseCompletion` and `CourseProgress` rows before the hard delete, preserving the `is_deleted` flag for audit purposes.

### 4.3 — Database Index & Schema Hardening (Month 2, Weeks 5–6)

**Concrete actions:**
- **Add indexes:**
  - `UserResponse(timestamp)` — time-range queries for analytics
  - `CourseProgress(timestamp)` — time-range queries for progress tracking
  - `AnalysisOutput(timestamp, status)` — filtering by status for job polling
  - `EmailReportRequest(created_at, status)` — filtering for admin report views
  - `UserResponse(answer_group)` — already has `db_index=True` on the model, but verify the index exists in the live schema
  - GIN index on `Benefactor.grant_catalog_classes` (ArrayField) if queried with `@>` containment
  - GIN index on `UserResponse.metadata` and `Course.course_meta_data` (JSONField) if queried with `->` operators
- **Standardize PK types:** `RegistrationCode`, `SharingCode`, and `RegistrationWaitlist` use `BigAutoField` while all other models use `UUIDField`. Plan a migration to `UUIDField` for API contract consistency. This is a multi-step migration (add UUID column, backfill, switch, drop old column).
- **Add `created_at`/`updated_at`** to `Course`, `Question`, `Audio`, `CourseGroup`, `BenefactorCohort`, `CohortMembership`, `BenefactorCohortGate` (see QW-11 for the first batch).

### 4.4 — `PromptSummary` Archival Strategy (Month 3, Week 10)

**Rationale:** `PromptSummary` is 1:1 with `AnalysisOutput` and grows unbounded. `LlmUseSummary` (1:1 with `PromptTemplate`) already aggregates the same metrics.

**Concrete actions:**
- Add a Celery periodic task (via `django_celery_beat`) that:
  - Aggregates `PromptSummary` rows older than 30 days into `LlmUseSummary`
  - Archives or deletes `PromptSummary` rows older than 90 days
- Add a `retention_days` setting to `PromptTemplate` or a global setting.

### 4.5 — i18n / modeltranslation Hardening (Month 3, Weeks 9–10)

**Rationale:** 6 models carry `_en`, `_es`, `_fr`, `_pt` translation fields. Every serializer must resolve the active language. No caching of translated content is visible.

**Concrete actions:**
- Add a `LocaleMiddleware`-aware cache layer for translated model fields. Cache key: `(model_name, pk, language)`. TTL: 5 minutes.
- Audit all 75 serializers to confirm they use `modeltranslation`'s `TranslationField` resolution rather than raw field access.
- Add a migration safety check: any new field added to a translated model must be added to all 4 language variants.

### 4.6 — Multi-App Decomposition (Month 3, Weeks 11–12)

**Rationale:** The single `memores` app contains 36 models, 75 serializers, 106 views. The directory structure (`views/app/`, `views/admin/`, `views/management/`, `views/public/`, `views/payment/`) already implies domain boundaries.

**Concrete actions:**
- Plan (do not execute in 90 days) a split into:
  - `memores_core` — `User`, `Benefactor`, `CourseProvider`, `CourseProviderGrant`, `CohortMembership`, `BenefactorCohort`, `BenefactorCohortGate`
  - `memores_content` — `Course`, `CourseGroup`, `CourseMap`, `CourseSession`, `Question`, `QuestionGroup`, `QuestionMap`, `ResponseOption`, `ResponseGroup`, `ResponseGroupMap`, `Audio`
  - `memores_engagement` — `UserResponse`, `UserAudioCompletion`, `UserCourseCompletion`, `CourseProgress`, `UnstructuredUserInteraction`
  - `memores_analysis` — `AnalysisResult`, `AnalysisOutput`, `PromptTemplate`, `LlmUseSummary`, `PromptSummary`, `AnalysableProfileQuestion`
  - `memores_coaching` — `CoachEntry`, `JournalEntry`, `SharingCode`, `EmailReportRequest`
  - `memores_billing` — `RegistrationCode`, `RegistrationWaitlist`, Stripe integration
- This is a 6-month initiative. The 90-day deliverable is the decomposition plan and a proof-of-concept split of `memores_analysis` into its own app.

---

## 5. Observability, Telemetry (OpenTelemetry), & Testing Enhancements

### 5.1 — Structured Logging & Request Correlation (Weeks 2–3)

**Current state:** `RequestIDMiddleware` is in the middleware stack, suggesting request IDs are generated. However, no view method in the topography shows any logging call. The `AnalysisOutput.error_message` and `EmailReportRequest.error_message` fields capture errors as free-text strings with no structured taxonomy.

**Actions:**
- **Week 2:** Add structured logging (JSON format) to all Celery task entry/exit points. Log `task_id`, `AnalysisOutput.id` or `EmailReportRequest.id`, `status` transitions (PENDING → IN_PROGRESS → FINISHED/ERROR), and token counts. Propagate the `RequestIDMiddleware` request ID into Celery task context via `task_prerun` signal.
- **Week 3:** Add a `status` transition log to `AnalysisOutput` and `EmailReportRequest` updates. When `status` changes from `PENDING` to `IN_PROGRESS`, log the transition. When it changes to `ERROR`, log the `error_message` at `ERROR` level with the `task_id` as a structured field.

### 5.2 — OpenTelemetry Tracing (Month 2, Weeks 5–7)

**Current state:** No tracing visible in the topography. The LLM call path (`PromptTemplate` → Celery task → LLM API → `AnalysisOutput`) is a multi-hop async flow with no visible correlation.

**Actions:**
- Instrument the LLM service layer (from §4.1) with OpenTelemetry spans:
  - `llm.prompt_resolve` — resolving the `PromptTemplate` and its `required_courses`/`optional_courses`
  - `llm.call` — the actual LLM API call, with `model`, `temperature`, `input_tokens`, `output_tokens`, `duration_seconds` as span attributes
  - `llm.output_parse` — the JSON parsing step (the `json-repair` dependency suggests this is a failure point)
- Add a `celery.task` span that wraps each Celery task execution, linking to the parent `AnalysisOutput` or `EmailReportRequest` via `task_id`.
- Export to a collector (Jaeger, Tempo, or Datadog). The `LlmUseSummary` and `PromptSummary` models already track the metrics; the tracing adds the causal chain.

### 5.3 — Alerting & Thresholds (Month 2, Weeks 7–8)

**Actions:**
- Add a Celery periodic task that checks for `AnalysisOutput` rows with `status=JobStatuses.IN_PROGRESS` older than a configurable threshold (e.g., 5 minutes). Alert via the team's existing channel (Slack, PagerDuty).
- Add a `LlmUseSummary` threshold alert: if `average_duration_seconds` exceeds a threshold for a given `PromptTemplate`, alert.
- Add a `PromptSummary` growth alert: if the table exceeds N rows, trigger the archival job from §4.4.

### 5.4 — Testing Strategy (Weeks 2–4, ongoing)

**Current state:** No test files appear in the topography. The `.github/workflows/ci.yml` is modified but its contents are not parsed.

**Actions:**
- **Week 2:** Establish a test baseline. Write integration tests for:
  - `SharingCodeRetrieveUpdateDestroyView` — verify that a user cannot access another user's sharing code (the IDOR fix from QW-3)
  - `CoachEntryCreateSerializer` — verify that `is_deleted` cannot be set at creation (QW-6)
  - `EmailReportRequestCreateSerializer` — verify that `status` cannot be set at creation (QW-7)
  - `CreateUpdateUserSerializer` — verify that `is_active`, `is_deleted`, `stripe_customer_id` are read-only for non-admin views (QW-8)
  - `get_unstructured_interactions`, `get_user_responses`, `get_course_durations` — verify that non-staff users receive 403 (QW-1)
- **Week 3:** Add a `conftest.py` with fixtures for:
  - A `Benefactor` with a `RegistrationCode` and a `User`
  - A `Course` with `CourseSession`, `QuestionGroup`, `Question`, `ResponseOption`
  - An `AnalysisOutput` in each `JobStatuses` state (PENDING, IN_PROGRESS, FINISHED, ERROR)
  - A `PromptTemplate` with `output_schema` set
- **Week 4:** Add a contract test for the Stripe webhook path (`create_registration_code_from_checkout_session`) that verifies signature validation.
- **Ongoing:** Add a CI gate that fails if any view in `memores/views/admin/` or `memores/views/management/` lacks a permission class beyond `IsAuthenticated`.

### 5.5 — Health Check Enhancement (Week 3)

**Current state:** `HealthCheckView` in `memores/views/health_check.py` extends `BaseHealthCheckView` (from the `health_check` app). Its checks are not visible.

**Actions:**
- Add a Celery worker health check (verify the worker is responsive).
- Add a database connection check.
- Add a `PromptTemplate` count check (alert if zero active templates).
- Expose the health check at `/health` (confirmed in URL patterns) and `/health/` (both map to `HealthCheckView`).

---

## 6. Organizational & Workflow Improvement Recommendations

### 6.1 — Permission Class Consolidation (Week 3)

**Current state:** 10+ custom permission classes in `memores/permissions.py` with inconsistent naming:
- `IsStaffOrSuperUser` vs `IsSuperUser` vs `IsStaffOrBenefactorScopeOwner` vs `IsStaffOrSuperUserInSimDataEnv`
- `IsAppUser` vs `IsCreator` vs `IsContentCreatorUser` vs `IsBenefactorScopeOwnerOrCreator`

**Recommendation:**
- Consolidate into 4 permission classes:
  - `IsStaff` — `request.user.is_staff`
  - `IsSuperUser` — `request.user.is_superuser`
  - `IsBenefactorScoped` — checks `request.user.benefactor` against the target object's benefactor
  - `IsContentCreator` — checks `request.user.user_type` against a creator role
- Replace `IsStaffOrSuperUserInSimDataEnv` with `IsStaff` + an environment check in the view (or a middleware). The environment gate should not be in the permission class.
- Add `has_object_permission` to `IsBenefactorScoped` to enforce per-object scoping.

### 6.2 — Code Review Triggers (Week 2)

**Recommendation:** Add a CI check (in the modified `.github/workflows/ci.yml`) that:
- Fails if any new view in `memores/views/admin/` or `memores/views/management/` has `permission_classes` containing only `["IsAuthenticated"]` without a role gate.
- Fails if any serializer's `Meta.fields` includes `"is_deleted"` or `"status"` without a corresponding `read_only_fields` entry.
- Fails if any model adds a new field without a corresponding `created_at`/`updated_at` audit field (for content models).
- Fails if any `TextField` has a `max_length` constraint (no-op in PostgreSQL; misleading).

### 6.3 — Ownership Boundaries (Month 2)

**Current state:** The single `memores` app means every developer touches the same `models.py`, the same `permissions.py`, and the same `serializers/` directory. The 75 serializers and 106 views create merge conflicts on every feature branch.

**Recommendation:**
- Assign domain ownership:
  - **Content team:** `memores/views/management/content.py`, `memores/serializers/course_serializers.py`, `memores/serializers/response_serializers.py`, `Course`, `Question`, `Audio`, `CourseGroup`, `CourseSession`, `QuestionGroup`, `ResponseOption`, `ResponseGroup`
  - **Analysis team:** `memores/views/app/analysis.py`, `memores/services/results_analysis/`, `memores/serializers/analysis_output_serializers.py`, `memores/serializers/prompt_template_serializers.py`, `AnalysisOutput`, `PromptTemplate`, `LlmUseSummary`, `PromptSummary`
  - **Engagement team:** `memores/views/app/course.py`, `memores/views/app/coach.py`, `memores/views/app/journal.py`, `memores/serializers/coach_serializers.py`, `memores/serializers/journal_serializers.py`, `memores/serializers/course_progress_serializers.py`, `CourseProgress`, `UserCourseCompletion`, `CoachEntry`, `JournalEntry`
  - **Platform team:** `memores/views/admin/`, `memores/views/payment/`, `memores/views/public/`, `memores/permissions.py`, `memores/views/health_check.py`, `User`, `Benefactor`, `RegistrationCode`, `SharingCode`
- Enforce via CODEOWNERS file.

### 6.4 — CI/CD Pipeline Stabilization (Week 1)

**Current state:** 5 workflow files are modified in the working tree. The pipeline is in flux.

**Recommendation:**
- Freeze the CI/CD pipeline changes for 1 sprint. Ship the 5 modified workflow files as a single PR with a clear changelog.
- Add a `load-test-after-deploy.yml` gate that runs a smoke test against the deployed environment before marking the release as successful.
- Add a `dependabot.yml` review gate: security updates auto-merge, but dependency updates require a human review.

### 6.5 — Documentation & Onboarding (Week 2)

**Recommendation:**
- Create a `docs/ARCHITECTURE.md` that maps the 36 models to their domain clusters (as outlined in §4.6).
- Create a `docs/PERMISSIONS.md` that documents each of the 10+ permission classes, what they check, and which views use them.
- Create a `docs/LLM_PIPELINE.md` that documents the `PromptTemplate` → `AnalysisOutput` → `LlmUseSummary` flow, including the Celery task lifecycle and the `JobStatuses` state machine (PENDING → IN_PROGRESS → FINISHED/ERROR).
- Add a `docs/MIGRATIONS.md` that documents the soft-delete pattern, the `SoftDeleteModel` abstract base, and the 7 concrete models that use it.

### 6.6 — `channels`/`daphne` Decision (Week 2)

**Current state:** `channels` and `daphne` are in `INSTALLED_APPS`. No WebSocket views appear in the topography. No WebSocket URL patterns appear in the live URL list.

**Recommendation:**
- If WebSocket support is planned, document the intended use case and add a `docs/WEBSOCKETS.md` stub.
- If WebSocket support is not planned, remove `channels` and `daphne` from `INSTALLED_APPS` and the ASGI configuration. The `daphne` dependency adds startup overhead and a security surface (ASGI middleware must be audited separately from WSGI).

### 6.7 — `django_extensions` in Production (Week 1)

**Current state:** `django_extensions` is in `INSTALLED_APPS`. It provides `showmigrations`, `sqlmigrate`, `runserver_plus`, and other development utilities.

**Recommendation:**
- Remove `django_extensions` from production `INSTALLED_APPS`. It is a development convenience that adds no production value and a small attack surface.
- If the team relies on `showmigrations` or `sqlmigrate` in production, use `django-extensions` only in the development settings file.

---

**Summary of 90-day deliverables:**

| Week | Deliverable |
|---|---|
| 1 | Security patches (QW-1 through QW-5), CI/CD freeze, `django_extensions` removal from prod |
| 2 | Serializer hygiene (QW-6 through QW-9), test baseline, permission class consolidation plan, CODEOWNERS |
| 3 | Query performance (QW-10), schema hardening (QW-11, QW-12), structured logging, health check enhancement |
| 4 | Test coverage for security fixes, Stripe webhook contract test, `channels`/`daphne` decision |
| 5–7 | LLM service layer extraction, OpenTelemetry tracing, alerting thresholds |
| 6–8 | Course progression service, database index hardening, i18n caching |
| 9–10 | `PromptSummary` archival, i18n hardening, documentation |
| 11–12 | Multi-app decomposition plan, proof-of-concept split of `memores_analysis` |

---
--------------------------------------------------
## Fidelity Check

**Score:** 8/10 — Good

**Parser Ground Truth:** 36 concrete models, 1 abstract, 75 serializers, 106 views (82 class-based, 24 function-based)

**Issues Found:**

  ✗ Line 244: "COMPLETED" (should be FINISHED for JobStatuses)

  ✗ Line 137: view name (should be AdminBenefactorRetrieveView)

  ✓ Correctly distinguishes concrete vs abstract models

