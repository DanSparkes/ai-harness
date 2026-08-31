# Staff Architecture Review Report

## 1. Executive Summary

The Memores API is a multi-tenant Django/DRF application (37 models, 75 serializers, 106 views) built around a Benefactor → User → Course → Analysis domain. It has evolved over multiple years, as evidenced by layered permission classes, a soft-delete abstraction, LLM usage tracking, and an async job pattern for analysis workloads. The codebase is a single Django app (`memores/`) with a clear separation of concerns: models in `models.py`, serializers in `serializers/`, views partitioned into `app/`, `admin/`, `management/`, `public/`, and `payment/` subdirectories.

**Major strengths:**
- A consistent authentication baseline (`TokenAuthentication` + `IsAuthenticated`) across all 106 authenticated endpoints.
- A deliberate multi-tenant permission architecture (11 custom classes in `memores/permissions.py`) that distinguishes app users, benefactor-scope owners, content creators, and staff.
- LLM usage is tracked at two granularities (`LlmUseSummary` per template, `PromptSummary` per user), providing cost observability.
- Soft-delete via `SoftDeleteModel` with separate `objects`/`all_objects` managers demonstrates data-lifecycle awareness.
- Environment-gated admin endpoints (`IsLowerEnv`, `IsStaffOrSuperUserInSimDataEnv`) show intent to restrict destructive operations to non-production.

**Major risks (all confirmed by topography evidence):**
- `stripe_customer_id` is writable through `UserSerializer` and `CreateUpdateUserSerializer`, creating a direct billing-integrity vulnerability.
- `AdminSimulatedDataDeleteBenefactorView` uses `IsStaffOrSuperUser` instead of `IsStaffOrSuperUserInSimDataEnv`, exposing a cascading delete in all environments.
- Stripe webhook endpoints (`create_registration_code_from_checkout_session`, `StripeCheckoutSession`) have no view-level authentication, and signature verification is not visible in the topography.
- The async job pattern (`AnalysisOutput`, `EmailReportRequest`) has no visible idempotency key or stuck-job recovery.
- `SharingCode` is a permanent bearer token with no expiry or use-count fields.

**Confidence in this review:** Medium-High. All five prioritized findings are supported by explicit field definitions, class attributes, or permission configurations visible in the topography. The primary limitation is that method bodies, Celery task definitions, and middleware are outside the static parser's reach. Where a risk depends on unparseable code (e.g., whether Stripe signature verification exists in a method body), the finding is qualified as "Plausible" rather than "Confirmed." No finding in this report asserts a vulnerability that is not directly supported by a field, class attribute, or permission configuration in the topography.

**Key assumptions (explicitly stated):**
- The `memores/` directory is the sole Django app. No additional apps are visible in the topography.
- Celery task definitions exist but are not captured by the parser. The `task_id` fields on `AnalysisOutput` and `EmailReportRequest` and the `check_job_status` polling endpoint confirm an async workflow, but the task layer is opaque.
- The 11 custom permission classes in `memores/permissions.py` contain their authorization logic in method bodies that the parser cannot resolve. The `has_permission: false` / `has_object_permission: false` results for 10 of 11 classes indicate a parser limitation, not necessarily absent logic.
- `JOURNAL_BASE_FIELDS` and `BENEFACTOR_BASE_FIELDS` are module-level constants whose values are not resolved in the topography.
- The `get_queryset` method bodies on 106 views are not parseable. The `self_scoped: true` flag on many of these methods suggests user-scoping logic exists but cannot be verified.
- No test files, management commands, or middleware classes appear in the topography. Their absence in the parsed output does not confirm their absence in the repository.

---

## 2. Top 5 Prioritized Improvements

---

### Rank 1: Lock Down `stripe_customer_id` in User-Facing Serializers

| Field | Value |
|---|---|
| **Confidence** | Confirmed |
| **Focus Category** | Operational Reliability / Security |
| **Target Location** | `memores/serializers/user_serializers.py` — `UserSerializer`, `CreateUpdateUserSerializer` |
| **Estimated Effort** | S (≤ 4 hours including tests) |
| **Expected Impact** | Eliminates a confirmed path for a user to overwrite their own Stripe customer ID. |

**Evidence (from topography):**
- `UserSerializer` (`memores/serializers/user_serializers.py`): `Meta.fields` includes `"stripe_customer_id"`. `Meta.read_only_fields` is `["id"]`. `stripe_customer_id` is not in `read_only_fields`.
- `CreateUpdateUserSerializer` (same file): `Meta.fields` includes `"stripe_customer_id"`. No `read_only_fields` is defined.
- `User` model (`memores/models.py`): `stripe_customer_id` is `CharField(max_length=256, blank=True, null=True)`.
- `UserView` (`memores/views/app/user.py`): `base_classes: ["APIView"]`, `http_methods: ["GET", "PATCH", "POST"]`, `permission_classes: ["IsAuthenticated"]`, `authentication_classes: ["TokenAuthentication"]`. All three methods report `self_scoped: true`.
- `Benefactor` model also carries `stripe_customer_id` (`CharField, max_length=256, blank=True, null=True`). `BenefactorSerializer` inherits it via `BaseBenefactorSerializer.Meta.fields + [...]`. The benefactor write path is gated by `IsStaffOrBenefactorScopeOwner`.

**Interpretation:**
Because `stripe_customer_id` is present in `Meta.fields` without a corresponding `read_only_fields` entry, DRF will accept it as a writable field on both serializers. The `UserView` PATCH path, scoped to the authenticated user, provides a direct write path. A user can set their own `stripe_customer_id` to an arbitrary value, potentially redirecting future charges to a different Stripe account or breaking the billing relationship.

**Risk Statement:**
Direct financial-integrity vulnerability. A single authenticated user can overwrite their billing identifier. The `Benefactor.stripe_customer_id` is lower risk because the write path is gated by `IsStaffOrBenefactorScopeOwner`, but it should be verified.

**Why selected over alternatives:**
This is the highest ROI item in the review. The fix is a one-line change per serializer. The risk is direct, financial, and confirmed. No architectural change is required. No new dependencies. No deployment risk beyond a serializer field-visibility change. Findings 12 and 5 were considered for this rank but require either a broader audit (Finding 12) or verification of unparseable code (Finding 5).

---

### Rank 2: Correct Environment-Gated Permission on Destructive Admin Endpoints

| Field | Value |
|---|---|
| **Confidence** | Confirmed |
| **Focus Category** | Operational Reliability / Security |
| **Target Location** | `memores/views/admin/simulated_data_runs.py` — `AdminSimulatedDataDeleteBenefactorView`, `AdminSimulatedDataCodeCleanupView`, `AdminSimulatedDataCleanupRunView` |
| **Estimated Effort** | S–M (4–8 hours including audit and tests) |
| **Expected Impact** | Prevents a staff user in any environment from deleting an entire Benefactor and its cascading data. |

**Evidence (from topography):**
- `AdminSimulatedDataDeleteBenefactorView` (`memores/views/admin/simulated_data_runs.py`): `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]`, `http_methods: ["DELETE"]`.
- `AdminSimulatedDataCodeCleanupView` (same file): `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]`, `http_methods: ["POST"]`.
- `AdminSimulatedDataCleanupRunView` (same file): `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]`, `http_methods: ["POST"]`.
- All other `AdminSimulatedData*` views in the same file (`AdminSimulatedDataDepartmentArchetypesView`, `AdminSimulatedDataStatusView`, `AdminSimulatedDataCreateBenefactorView`, `AdminSimulatedDataAddCodeView`, `AdminSimulatedDataUserRunView`, `AdminSimulatedDataQuizRunView`) use `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUserInSimDataEnv"]`.
- `AdminAnalysisOutputRetrieveDestroyView` (`memores/views/admin/analysis_output.py`): `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser", "IsLowerEnv"]`, `queryset: "AnalysisOutput.all_objects.all.select_related"`, `http_methods: ["DELETE"]`.
- `IsStaffOrSuperUserInSimDataEnv` (`memores/permissions.py`): `has_permission: false`, `has_object_permission: false` (parser cannot resolve the method body).
- `IsLowerEnv` (`memores/permissions.py`): `has_permission: true`, `has_object_permission: false`.

**Interpretation:**
The established pattern in `simulated_data_runs.py` is to gate destructive operations with `IsStaffOrSuperUserInSimDataEnv`. Three views deviate from this pattern by using `IsStaffOrSuperUser` instead. `AdminSimulatedDataDeleteBenefactorView` performs a DELETE on a `Benefactor`, which cascades to all associated `User`, `Course`, `CourseProgress`, `UserResponse`, and `AnalysisOutput` records. Because the permission class does not check the deployment environment, any staff or superuser token valid in production can trigger this deletion.

**Risk Statement:**
Irreversible multi-table cascade delete accessible in production. The `AdminSimulatedDataCodeCleanupView` and `AdminSimulatedDataCleanupRunView` share the same misconfiguration. The `AdminAnalysisOutputRetrieveDestroyView` is correctly gated by `IsLowerEnv`, confirming that the team is aware of the pattern but has not applied it consistently.

**Uncertainty:** The internal logic of `IsStaffOrSuperUserInSimDataEnv` is not resolvable by the parser. It is assumed to check a deployment-environment setting. If the class does not actually gate on environment (e.g., it is a no-op or a misconfigured stub), the fix in Rank 2 would not be sufficient and a deeper audit of `permissions.py` would be required. This should be verified as the first step of implementation.

**Why selected over alternatives:**
The fix is a permission-class substitution on three views. The blast radius of a misfire is catastrophic. The effort is small relative to the risk. Finding 5 (Stripe webhook auth) was considered for this rank but requires verification of unparseable method bodies before a fix can be scoped.

---

### Rank 3: Verify and Harden Stripe Webhook Authentication

| Field | Value |
|---|---|
| **Confidence** | Confirmed (view configuration) / Plausible (absence of signature verification) |
| **Focus Category** | Operational Reliability / Security |
| **Target Location** | `memores/views/payment/stripe.py` — `create_registration_code_from_checkout_session`, `StripeCheckoutSession` |
| **Estimated Effort** | M (1–2 days including verification, implementation, and tests) |
| **Expected Impact** | Ensures that only Stripe-originated requests can create registration codes or checkout sessions. |

**Evidence (from topography):**
- `create_registration_code_from_checkout_session` (`memores/views/payment/stripe.py`): function view, `permission_classes: []`, `authentication_classes: []`, `inline_auth_calls: []`, `http_methods: ["POST"]`.
- `StripeCheckoutSession` (same file): `base_classes: ["APIView"]`, `permission_classes: ["AllowAny"]`, `authentication_classes: []`, `inline_auth_calls: []`, `http_methods: ["POST"]`.
- `RegistrationCode` model (`memores/models.py`): `available_uses` is `PositiveIntegerField` with `default: 99999`. `expiration_timestamp` is `DateTimeField` with `default: one_year_from_now`. `is_active` is `BooleanField` with `default: true`.
- `RegistrationCode` model has a `clean()` method.
- No middleware or signature-verification decorator is visible in the topography.

**Interpretation:**
Both Stripe-related endpoints have no authentication or permission classes. The `inline_auth_calls` array is empty, meaning no authorization calls are detected in the method bodies. Stripe webhooks conventionally rely on `stripe-signature` header verification. This verification may exist in the unparseable method body or in middleware not captured by the parser. The `RegistrationCode.available_uses` default of 99999 means a single successful forgery creates a high-capacity, one-year access grant.

**Risk Statement:**
If signature verification is absent or bypassable, any unauthenticated caller can create registration codes. The 99999 default amplifies the impact. Even if verification is present, the absence of a visible, testable auth boundary means a regression (middleware removal, settings change) would silently disable it.

**Uncertainty:** The topography cannot confirm or deny the presence of signature verification. The `inline_auth_calls: []` result reflects a parser limitation, not a confirmed absence. The `clean()` method on `RegistrationCode` may enforce constraints on `available_uses` that are not visible. This finding is a verification task, not a confirmed vulnerability.

**Why selected over alternatives:**
The verification step (reading two method bodies) is cheap. If verification is absent, the fix is a well-understood pattern. If verification is present, the initiative becomes a test-coverage addition. The risk is high because the endpoints create access grants. Finding 13 (SharingCode expiry) was considered for this rank but has lower immediate blast radius.

---

### Rank 4: Add Idempotency and Stuck-Job Recovery to the Async Analysis Pipeline

| Field | Value |
|---|---|
| **Confidence** | Confirmed (structural pattern) / Plausible (operational risk) |
| **Focus Category** | Operational Reliability / Observability |
| **Target Location** | `memores/models.py` — `AnalysisOutput`, `EmailReportRequest`; `memores/views/app/jobs.py` — `check_job_status`; `memores/views/app/analysis.py` — `AnalyseCourseResults`; `memores/views/app/journal.py` — `start_journal_analysis` |
| **Estimated Effort** | M–L (1–2 weeks, deployable in three sub-tasks) |
| **Expected Impact** | Prevents duplicate LLM invocations on client retry, enables recovery of stuck `PENDING` jobs, restricts job-status polling to the owning user. |

**Evidence (from topography):**
- `AnalysisOutput` (`memores/models.py`): `status` (CharField, default `JobStatuses.PENDING`), `task_id` (CharField, max_length 255, null), `error_message` (CharField, max_length 1024, null, blank). No `idempotency_key` field.
- `EmailReportRequest` (same file): `status` (CharField, default `JobStatuses.PENDING`), `task_id` (CharField, max_length 255, null), `error_message` (CharField, max_length 255, null, blank). No `idempotency_key` field.
- `CoachEntry.analysis_output` (FK, null, blank), `JournalEntry.analysis_output` (FK, null, blank), `JournalEntry.advanced_analysis_output` (FK, null, blank), `EmailReportRequest.analysis_output` (FK, null, blank) — four models reference `AnalysisOutput`.
- `check_job_status` (`memores/views/app/jobs.py`): function view, GET, `permission_classes: ["IsAuthenticated"]`, `authentication_classes: ["TokenAuthentication"]`. No `IsAppUser` or user-scoping permission.
- `AnalyseCourseResults` (`memores/views/app/analysis.py`): `base_classes: ["APIView"]`, `http_methods: ["GET", "POST"]`, `permission_classes: ["IsAuthenticated", "IsAppUser"]`.
- `start_journal_analysis` (`memores/views/app/journal.py`): function view, POST, `permission_classes: ["IsAuthenticated", "IsAppUser"]`.
- No Celery task definitions appear in the topography. The parser capabilities state that Celery task definitions are detectable. Their absence is a confirmed gap in the parsed output.
- `LlmUseSummary` and `PromptSummary` track usage but no visible mechanism enforces a per-user or per-template token budget.

**Interpretation:**
Two models share an identical async-job shape (status + task_id + error_message). Four other models reference `AnalysisOutput` via nullable FKs, forming a dependency chain. The sole polling endpoint (`check_job_status`) is gated only by `IsAuthenticated` — any authenticated user can poll any job. The `task_id` is a free-form string, not a typed reference. No idempotency key field exists on either model. A client retry on POST to `AnalyseCourseResults` or `start_journal_analysis` can enqueue a duplicate LLM call. A worker crash mid-processing leaves the row in `PENDING` indefinitely.

**Risk Statement:**
Duplicate LLM invocations on client retry incur cost and produce duplicate `AnalysisOutput` rows. Stuck `PENDING` rows have no visible recovery mechanism. The `check_job_status` endpoint leaks job-status information across users.

**Uncertainty:** The Celery task layer is entirely opaque to the parser. It is possible that idempotency or stuck-job recovery is implemented at the task level (e.g., a Celery `bind=True` task with a deduplication check, or a `task_acks_late` + `reject_on_worker_lost` configuration). The structural gap in the model layer is confirmed; the operational risk is plausible but not confirmed. The `check_job_status` cross-user access is confirmed at the permission-class level, but the method body may contain an additional user-scoping check that the parser cannot resolve.

**Why selected over alternatives:**
This is the highest-effort item in the top 5, but it addresses a recurring operational pattern that will generate incidents as LLM usage grows. The fix is decomposable into three independently deployable sub-tasks. Finding 13 (SharingCode) was considered for this rank but is a smaller, more contained fix.

---

### Rank 5: Add Expiry and Use-Count Enforcement to `SharingCode`

| Field | Value |
|---|---|
| **Confidence** | Confirmed (structural gap) / Plausible (operational risk) |
| **Focus Category** | Operational Reliability / Security |
| **Target Location** | `memores/models.py` — `SharingCode`; `memores/permissions.py` — `AccessGatePermission`; `memores/views/app/sharing_code.py` — `sharing_code_validation` |
| **Estimated Effort** | M (3–5 days including migration, enforcement logic, and tests) |
| **Expected Impact** | Converts `SharingCode` from a permanent bearer token to a time-bounded, use-limited access grant. |

**Evidence (from topography):**
- `SharingCode` (`memores/models.py`): fields are `code` (CharField, unique), `is_active` (BooleanField, default true), `role` (CharField, max_length 64, choices, null), `label` (CharField, max_length 255, null, blank), `user` (FK), `created_at` (DateTimeField). No `expires_at`, `max_uses`, or `use_count` field.
- `CoachEntry.sharing_code` (FK, null, blank) references `SharingCode`.
- `EmailReportRequest.sharing_codes` references `SharingCode`.
- `AccessGatePermission` (`memores/permissions.py`): `has_permission: true`, `has_object_permission: false`. Used on `CoachEntryRetrieveView` and `JournalEntryDetailView`, both of which accept a `sharing_code` lookup.
- `sharing_code_validation` (`memores/views/app/sharing_code.py`): function view, POST, `permission_classes: ["IsAuthenticated"]`, `inline_auth_calls: []`.
- `SharingCodeListCreateView` and `SharingCodeRetrieveUpdateDestroyView` (same file): `permission_classes: ["IsAuthenticated", "IsAppUser"]`.

**Interpretation:**
`SharingCode` is a multi-purpose access mechanism referenced by `CoachEntry`, `EmailReportRequest`, and the `AccessGatePermission` class. It has no expiry or use-count fields. The only revocation mechanism is setting `is_active: false` via `SharingCodeRetrieveUpdateDestroyView`, which requires the code owner to act. The `sharing_code_validation` endpoint is `IsAuthenticated`-only, meaning any authenticated user can validate any code, enabling enumeration. The `role` field suggests differentiated access levels, but enforcement is in the unparseable `AccessGatePermission.has_permission` method.

**Risk Statement:**
A leaked `SharingCode` grants indefinite access to another user's coach entries, journal entries, or email reports. The product handles personal journaling and coaching data, making this a privacy concern.

**Uncertainty:** The `AccessGatePermission.has_permission` method body is not resolvable. It may already contain partial expiry or use-count logic that the parser cannot see. The `sharing_code_validation` endpoint's `inline_auth_calls: []` reflects a parser limitation. The structural gap (no fields on the model) is confirmed; the operational risk depends on whether the unparseable permission logic compensates.

**Why selected over alternatives:**
This is a confirmed structural gap on a multi-purpose access mechanism. The fix is a model migration plus enforcement logic. Finding 4 (`CourseListView` pagination) is trivial but lower impact. Finding 7 (`SoftDeleteModel` writable `is_deleted`) is a lower-severity data-integrity concern.

---

## 3. Deferred Opportunities

| # | Finding | Category | Reason Deferred |
|---|---|---|---|
| D1 | `CourseListView` has no pagination (`pagination_class: null`, `CourseFullListSerializer` with nested `CourseMapGroupSerializer` many=True) | Performance | Trivial fix (add a `pagination_class`). No immediate incident risk at current catalog size. Bundle into a routine performance pass. |
| D2 | 10 of 11 custom permission classes in `memores/permissions.py` report `has_permission: false` / `has_object_permission: false` | Maintainability / Developer Effectiveness | This is a parser limitation, not a confirmed code defect. The classes likely contain logic in method bodies the parser cannot resolve. The actionable item is a testing strategy (unit tests per permission class, a CI check that validates `has_permission`/`has_object_permission` behavior), not a code fix. Best addressed as a longer-term initiative. |
| D3 | `queryset_auth_chain: "unknown"` on all 106 views | Maintainability / Developer Effectiveness | Same root cause as D2. The parser cannot resolve `get_queryset` bodies. A CI-level test that asserts every `get_queryset` filters by user or benefactor would address this, but it is a large testing effort. |
| D4 | `SoftDeleteModel.is_deleted` is writable through `UserSerializer` and `CreateUpdateUserSerializer` (not in `read_only_fields`) | Data Integrity | A user can soft-delete their own account via `UserView` PATCH. Lower severity than billing or access-control risks. Address by adding `is_deleted` to `read_only_fields` in a future serializer hardening pass alongside Rank 1. |
| D5 | Inherited `Meta.fields` expressions (`JOURNAL_BASE_FIELDS + [...]`, `BaseBenefactorSerializer.Meta.fields + [...]`) prevent static field verification | Maintainability | A change to a base constant propagates silently to all child serializers. Resolving requires either inlining the constants or adding a CI check. Low urgency. |
| D6 | `LlmUseSummary` and `PromptSummary` track usage but no visible mechanism enforces a per-user or per-template token budget | Operational Reliability | Plausible risk, not confirmed. The check may exist in unparseable view bodies or Celery task code. Revisit after Rank 4 (async job idempotency) is complete, as the two are related. |
| D7 | `AdminUserRetrieveView` uses `UserSerializer` (full profile including `stripe_customer_id`, `email`, `birthdate`) with `IsBenefactorScopeOwner` | Security | `get_queryset` reports `self_scoped: true`, suggesting filtering exists. The scoping gap is plausible but not confirmed. Verify as part of the permission-class testing initiative (D2). |

**Items removed from the original deferred list:**

- *Original D8* (`AnalysisOutput.other_users` unresolved type): Removed. The field's type and cardinality are not visible in the topography, and the field is excluded from the user-facing serializer. Without evidence of operational impact, this is not a finding.
- *Original D9* (missing `select_related`/`prefetch_related` in view class attributes): Removed. The `get_queryset` method bodies are unparseable. Absence of `select_related` in class attributes does not confirm its absence in method bodies. This is a parser limitation, not a confirmed performance gap.
- *Original D10* (`CoachEntryRetrieveView` MRO with `CreateAPIView` + `RetrieveAPIView`): Removed. The topography shows the inheritance; the code likely resolves correctly via MRO. Without evidence of a runtime failure, this is a style note, not a finding.
- *Original D11* (`CourseProgress` missing `unique_together`): Removed. The parser may not capture `Meta.unique_together` or `UniqueConstraint`. The `CourseProgressCreateSerializer` making `session_id`, `question_id`, `audio_id` optional suggests multiple rows per user-course pair are expected by design. No evidence of a data-integrity defect.
- *Original D12* (`RegistrationCode.available_uses` default of 99999): Consolidated into Rank 3, which already addresses the registration-code creation path and the `available_uses` default.

---

## 4. Concrete Implementation Suggestions

### Rank 1: Lock Down `stripe_customer_id`

**Changes:**
1. In `memores/serializers/user_serializers.py`, add `"stripe_customer_id"` to `read_only_fields` in `UserSerializer.Meta`.
2. In the same file, add `read_only_fields = ["id", "stripe_customer_id"]` to `CreateUpdateUserSerializer.Meta` (currently no `read_only_fields` is defined).
3. In `memores/serializers/benefactor_serializers.py`, verify that `stripe_customer_id` is in `read_only_fields` for `BenefactorUpdateSerializer`. The `BaseBenefactorSerializer.Meta.read_only_fields` is `["id"]`. If `stripe_customer_id` is not in the inherited `read_only_fields`, add it.

**Preserve existing behavior:**
`stripe_customer_id` remains readable in all serializers. Only the write path is restricted. DRF silently ignores read-only fields in write requests. No API contract change for consumers that read the field.

**Deployment risk:**
Minimal. A `read_only_fields` change is a no-op for read requests. No migration required. No data change.

**Rollback:**
Revert the serializer change. No data migration needed.

**Testing requirements:**
- Unit test: `CreateUpdateUserSerializer(data={"stripe_customer_id": "acct_123", ...})` → `is_valid()` returns `True`, but `stripe_customer_id` is not in `validated_data`.
- Integration test: `PATCH /api/user/` with `stripe_customer_id` in body → 200, field unchanged (assert via a second GET).
- Integration test: `PUT /api/admin/benefactor/{id}/` with `stripe_customer_id` in body → 200, field unchanged.
- Regression test: `GET /api/user/` still returns `stripe_customer_id` in the response.

---

### Rank 2: Correct Environment-Gated Permissions

**Changes:**
1. In `memores/views/admin/simulated_data_runs.py`, change `permission_classes` on three views:
   - `AdminSimulatedDataDeleteBenefactorView`: `["IsAuthenticated", "IsStaffOrSuperUser"]` → `["IsAuthenticated", "IsStaffOrSuperUserInSimDataEnv"]`
   - `AdminSimulatedDataCodeCleanupView`: same change
   - `AdminSimulatedDataCleanupRunView`: same change
2. Audit all views in `memores/views/admin/` that use `IsStaffOrSuperUser`. For each, determine whether the operation is destructive or environment-sensitive. If so, add `IsLowerEnv` or `IsStaffOrSuperUserInSimDataEnv`.
3. Read `memores/permissions.py` and confirm the implementation of `IsStaffOrSuperUserInSimDataEnv` and `IsLowerEnv`. Verify that they check a Django setting or environment variable (e.g., `settings.DEPLOYMENT_ENV`). **This step is a prerequisite for the permission-class substitution; if the classes do not actually gate on environment, the fix is insufficient.**

**Preserve existing behavior:**
In simulation environments, the endpoints remain accessible to staff/superusers. In production, they become inaccessible. This is the intended behavior. No change to non-destructive `AdminSimulatedData*` views (which already use `IsStaffOrSuperUserInSimDataEnv`).

**Deployment risk:**
Low. The permission-class change is a no-op in simulation environments. In production, the endpoints become inaccessible, which is the desired outcome. No data migration needed.

**Rollback:**
Revert the permission-class change. If a production incident requires emergency access, the rollback restores the previous behavior.

**Testing requirements:**
- Unit test: `IsStaffOrSuperUserInSimDataEnv().has_permission(request, view)` returns `False` when `settings.DEPLOYMENT_ENV == "production"`, `True` when `settings.DEPLOYMENT_ENV == "simulation"`.
- Unit test: `IsLowerEnv().has_permission(request, view)` returns `False` in production, `True` in staging/dev.
- Integration test: staff user + production settings → 403 on `AdminSimulatedDataDeleteBenefactorView`, `AdminSimulatedDataCodeCleanupView`, `AdminSimulatedDataCleanupRunView`.
- Integration test: staff user + simulation settings → 200 on the same endpoints.
- Integration test: non-staff user → 403 in all environments.

---

### Rank 3: Verify and Harden Stripe Webhook Authentication

**Changes (conditional on verification step):**

**Step 1 — Verification (Day 1, read-only):**
1. Read the method body of `create_registration_code_from_checkout_session` in `memores/views/payment/stripe.py`. Check for `stripe.Webhook.construct_event()`, `stripe_signature` header parsing, or a call to a shared verification utility.
2. Read the method body of `StripeCheckoutSession.post` in the same file. Same check.
3. Read `settings.py` (or equivalent) for `STRIPE_WEBHOOK_SECRET` and any middleware in `MIDDLEWARE` that references Stripe.
4. Read `memores/permissions.py` for any Stripe-specific permission class.

**Step 2a — If verification is absent (Days 1–2):**
1. Implement signature verification. The preferred Django-native approach is a middleware or a DRF authentication class that validates the `Stripe-Signature` header using `STRIPE_WEBHOOK_SECRET` from settings.
2. Apply the verification to both endpoints. For `create_registration_code_from_checkout_session` (a function view), add a decorator. For `StripeCheckoutSession` (an `APIView`), add the authentication class to `authentication_classes`.
3. Change `permission_classes` on `StripeCheckoutSession` from `["AllowAny"]` to `[]` (the authentication class handles the check).
4. Reduce `RegistrationCode.available_uses` default from 99999 to a conservative value (e.g., 100). Generate a migration. Coordinate with the product team to confirm the intended default.

**Step 2b — If verification is present (Day 1):**
1. Add a test that asserts the signature check is invoked.
2. Add a test that a request with an invalid signature returns 400 or 403.
3. Add a test that a request with no signature returns 400 or 403.

**Preserve existing behavior:**
Valid Stripe webhooks continue to work. The only behavioral change is that invalid or missing signatures are rejected. The `available_uses` default change affects only new codes; existing codes retain their current value.

**Deployment risk:**
Low if verification is already present (test-only change). Medium if verification must be added (requires coordination with the Stripe dashboard to ensure the webhook endpoint URL and signing secret are configured). The `available_uses` default change requires a migration.

**Rollback:**
Revert the middleware/decorator or authentication class. Revert the `available_uses` default via a reverse migration.

**Testing requirements:**
- Unit test: `create_registration_code_from_checkout_session` with a valid Stripe signature → 200, `RegistrationCode` created.
- Unit test: `create_registration_code_from_checkout_session` with an invalid signature → 400/403, no `RegistrationCode` created.
- Unit test: `create_registration_code_from_checkout_session` with no signature → 400/403.
- Unit test: `StripeCheckoutSession.post` with a valid signature → 200.
- Unit test: `StripeCheckoutSession.post` with no signature → 400/403.

---

### Rank 4: Add Idempotency and Stuck-Job Recovery

**Changes (three independently deployable sub-tasks):**

**Sub-task 4a — Idempotency key on LLM-triggering POST endpoints (Week 1):**
1. Add an `idempotency_key` field to `AnalysisOutput` and `EmailReportRequest` in `memores/models.py`:
   ```python
   idempotency_key = models.CharField(max_length=64, null=True, blank=True, db_index=True)
   ```
2. Add a `UniqueConstraint` on `(user, idempotency_key)` for `AnalysisOutput` and on `(user, idempotency_key)` for `EmailReportRequest`. Generate a migration.
3. In `AnalyseCourseResults.post` (`memores/views/app/analysis.py`) and `start_journal_analysis` (`memores/views/app/journal.py`):
   - Read an `Idempotency-Key` header (or a body field).
   - If a row with the same `(user, idempotency_key)` exists, return the existing row's status instead of enqueuing a new task.
   - If the key is absent, generate one (e.g., `uuid4().hex`) and store it.
4. The `task_id` field remains a `CharField`. No change to the Celery task layer is required for this sub-task.

**Sub-task 4b — Stuck-job detection (Week 2):**
1. Add a Celery beat task (or a management command invoked via cron) that queries:
   ```python
   AnalysisOutput.objects.filter(
       status=JobStatuses.PENDING,
       timestamp__lt=timezone.now() - timedelta(minutes=15)
   )
   ```
   Mark matching rows as `FAILED` with `error_message="Stuck job: no progress in 15 minutes"`.
2. Optionally re-enqueue the task with a retry count limit (e.g., 3 retries). Track retry count in the `metadata` JSONField on `AnalysisOutput`.
3. Apply the same pattern to `EmailReportRequest`.

**Sub-task 4c — Scope `check_job_status` to the owning user (Week 2):**
1. In `check_job_status` (`memores/views/app/jobs.py`), filter the query by `request.user`:
   ```python
   job = AnalysisOutput.objects.filter(id=job_id, user=request.user).first()
   ```
   Return 404 if the job is not found or does not belong to the requesting user.
2. Apply the same scoping to any `EmailReportRequest` lookup in the same function.

**Preserve existing behavior:**
- Sub-task 4a: Existing clients that do not send an `Idempotency-Key` header continue to work. The key is optional.
- Sub-task 4b: The stuck-job detection is a background task. It does not affect the request path.
- Sub-task 4c: The scoping change may break clients that poll other users' jobs. Deploy behind a feature flag if client compatibility is a concern.

**Deployment risk:**
Low for 4a and 4b (additive changes). Medium for 4c (behavioral change to `check_job_status`). Each sub-task is independently deployable and independently rollback-able.

**Rollback:**
- 4a: Revert the idempotency check. Existing rows with `idempotency_key` set are harmless.
- 4b: Disable the Celery beat task or cron job.
- 4c: Revert the user-scoping filter.

**Testing requirements:**
- Unit test: two POSTs to `AnalyseCourseResults` with the same `Idempotency-Key` header → one `AnalysisOutput` row, one Celery task enqueued.
- Unit test: two POSTs with different `Idempotency-Key` headers → two rows, two tasks.
- Unit test: a POST with no `Idempotency-Key` header → one row, one task (key auto-generated).
- Unit test: stuck-job detection marks a `PENDING` row older than 15 minutes as `FAILED`.
- Unit test: a `PENDING` row younger than 15 minutes is untouched.
- Integration test: user A cannot poll user B's job via `check_job_status` → 404.
- Integration test: user A can poll their own job → 200.

---

### Rank 5: Add Expiry and Use-Count Enforcement to `SharingCode`

**Changes:**
1. **Migration (Day 1):** Add three fields to `SharingCode` in `memores/models.py`:
   ```python
   expires_at = models.DateTimeField(null=True, blank=True)
   max_uses = models.PositiveIntegerField(null=True, blank=True)
   use_count = models.PositiveIntegerField(default=0)
   ```
   Generate a migration. Set `expires_at` to `null` and `max_uses` to `null` for existing rows (no expiry, unlimited uses).

2. **Enforcement in `AccessGatePermission` (Days 2–3):**
   In `has_permission` (in `memores/permissions.py`), add checks:
   - If `sharing_code.expires_at` is not null and `timezone.now() > sharing_code.expires_at`, return `False`.
   - If `sharing_code.max_uses` is not null and `sharing_code.use_count >= sharing_code.max_uses`, return `False`.
   - On successful permission check, increment `sharing_code.use_count` and save.
   - Use `select_for_update()` on the `SharingCode` row to prevent race conditions on `use_count`.

3. **Enforcement in `sharing_code_validation` (Day 3):**
   In `memores/views/app/sharing_code.py`, add the same expiry and use-count checks. Return a distinct error message for each case: `"code expired"`, `"code exhausted"`, `"code inactive"`.

4. **Enforcement in `SharingCodeListCreateView` and `SharingCodeRetrieveUpdateDestroyView` (Day 3):**
   Allow setting `expires_at` and `max_uses` on creation and update. Add these fields to `SharingCodeSerializer` and `SharingCodeListSerializer` in `memores/serializers/sharing_code_serializers.py`.

5. **Tests (Days 4–5):**
   - A code past `expires_at` is rejected by `AccessGatePermission.has_permission`.
   - A code with `use_count >= max_uses` is rejected.
   - A code with `is_active: false` is rejected (existing behavior, regression test).
   - `sharing_code_validation` returns a distinct error for expired vs. exhausted vs. inactive.
   - A code with `expires_at: null` and `max_uses: null` continues to work indefinitely (regression test for existing codes).
   - Concurrent access: two simultaneous requests with the same code and `max_uses=1` → one succeeds, one is rejected.

**Preserve existing behavior:**
Existing codes with `expires_at: null` and `max_uses: null` continue to work indefinitely. The new fields are optional. No existing client breaks. The `is_active` check is unchanged.

**Deployment risk:**
Low. The migration is additive (three new nullable columns). The enforcement logic is a no-op for codes with null `expires_at` and null `max_uses`. No data migration needed for existing codes.

**Rollback:**
Revert the enforcement logic in `AccessGatePermission` and `sharing_code_validation`. The new columns remain but are unused. No data loss.

**Testing requirements:**
- Unit test: `AccessGatePermission.has_permission` with an expired code → `False`.
- Unit test: `AccessGatePermission.has_permission` with an exhausted code → `False`.
- Unit test: `AccessGatePermission.has_permission` with a valid code → `True`, `use_count` incremented by 1.
- Unit test: `AccessGatePermission.has_permission` with `expires_at: null`, `max_uses: null` → `True` (no enforcement).
- Integration test: `sharing_code_validation` with an expired code → 400, error message contains "expired".
- Integration test: `sharing_code_validation` with an exhausted code → 400, error message contains "exhausted".
- Integration test: `sharing_code_validation` with an inactive code → 400, error message contains "inactive".
- Migration test: existing codes with null `expires_at` and null `max_uses` continue to work.
- Concurrency test: two simultaneous requests with `max_uses=1` → one succeeds, one is rejected.

---

### Suggested Two-Engineer Allocation (One Quarter)

| | Weeks 1–2 | Weeks 3–4 | Weeks 5–8 |
|---|---|---|---|
| **Engineer A** (Security / Access Control) | Rank 1 (`stripe_customer_id` read-only) + Rank 2 (environment-gated permissions) | Rank 3 (Stripe webhook auth verification and hardening) | Rank 5 (`SharingCode` expiry and use-count) |
| **Engineer B** (Reliability / Observability) | Rank 4a (idempotency key on `AnalysisOutput` and `EmailReportRequest`) | Rank 4b (stuck-job detection) + Rank 4c (`check_job_status` user scoping) | Buffer: D1 (`CourseListView` pagination), D4 (`is_deleted` read-only) |

**Rationale for allocation:**
- Engineer A's work is a sequence of small, independently deployable changes with no interdependencies. Each rank can be shipped and verified before the next begins.
- Engineer B's work is a single feature (async job hardening) with three sub-tasks that are sequentially dependent (4a before 4b before 4c) but independently deployable.
- The two engineers work on different files with no merge conflicts: Engineer A touches `serializers/user_serializers.py`, `serializers/benefactor_serializers.py`, `views/admin/simulated_data_runs.py`, `views/payment/stripe.py`, `models.py` (SharingCode migration), `permissions.py`. Engineer B touches `models.py` (AnalysisOutput/EmailReportRequest migration), `views/app/analysis.py`, `views/app/journal.py`, `views/app/jobs.py`, and a new Celery beat task or management command.
- The only shared file is `memores/models.py`, where the two migrations are on different models and can be generated independently.

---

### What This Report Does Not Recommend

- **Service layers, DTO layers, command buses, CQRS, or event sourcing.** No evidence in the topography demonstrates that the current view → serializer → model flow is failing. The 106 views are partitioned into `app/`, `admin/`, `management/`, `public/`, and `payment/` subdirectories, which is a reasonable organizational pattern for a single-app Django project. Introducing additional layers would add complexity without a demonstrated benefit.
- **Splitting `memores/` into multiple Django apps.** The topography shows a single app with 37 models, 75 serializers, and 106 views. This is large but not pathologically so. The models share a common domain (Benefactor → User → Course → Analysis), and splitting would introduce inter-app dependencies without a clear boundary. No evidence of migration conflicts, circular imports, or deployment coupling is visible.
- **Extensive ORM abstraction.** The views use standard DRF patterns (`get_queryset`, `queryset` class attribute, `serializer_class`). The `self_scoped: true` flag on `get_queryset` methods suggests user-scoping is implemented inline, which is the Django-native approach. No evidence of duplicated query logic or fragile abstractions is visible.
- **Introducing a new task queue or workflow engine.** The existing `task_id` + `status` + `error_message` pattern on `AnalysisOutput` and `EmailReportRequest` is a standard Celery pattern. The fix is to add idempotency and stuck-job recovery to the existing pattern, not to replace it.