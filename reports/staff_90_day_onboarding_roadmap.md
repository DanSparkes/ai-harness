# 90-Day Onboarding Strategy: Memores Codebase

## Executive Summary & Core Codebase Impressions

This is a Django 5.2 / PostgreSQL application (~36 concrete models, 78 serializers, 98 views) built for an AI-powered coaching and journaling platform. The codebase exhibits classic "startup-to-production" growth patterns: it works, but the architectural seams are bleeding across domain boundaries.

**Core impressions:**

- **Monolithic model layer**: `memores/models.py` contains all 36 models in a single file. `Profile` acts as a god hub with 16+ OneToMany relationships coupling courses, analysis, sharing, coaching, journals, and email reports into one entity. This creates a high-coupling blast radius for any schema change.

- **Inconsistent primary key strategy**: `RegistrationCode.id` uses `BigAutoField` while every other model (including the related `RegistrationWaitlist`) uses `UUIDField`. This breaks serialization consistency and suggests the codebase evolved without a PK convention.

- **Serializer layer is fragile**: 78 serializers with AST-manipulated inheritance (`JOURNAL_BASE_FIELDS` via `<inherited: BinOp>`) that the parser cannot resolve at runtime. Naming conventions are inconsistent across `XSerializer`, `XCreateUpdateSerializer`, `XListSerializer`, `XDetailSerializer`, `XWithIdsSerializer`, `XResponseSerializer` patterns.

- **Mixed view architecture**: 75 class-based + 23 function-based views with no clear separation of concerns. Multi-inheritance (`CoachEntryRetrieveView` extends both `CreateAPIView` and `RetrieveAPIView`) creates MRO risk. Authorization patterns vary between inline auth calls, permission classes, and mixed approaches.

- **Celery is configured but invisible**: `django_celery_beat` and `django_celery_results` are installed, and models track `task_id` fields (`AnalysisOutput.task_id`, `EmailReportRequest.task_id`) with a `JobStatuses` enum (`PENDING`, `IN_PROGRESS`, `FINISHED`, `ERROR`). However, no task definitions or monitoring infrastructure are visible—stuck jobs have no alerting mechanism.

- **No observability surface**: Zero OpenTelemetry instrumentation apparent. No logging patterns visible in the topography. The `error_message` fields on `AnalysisOutput` and `EmailReportRequest` exist but have no propagation strategy.

---

## Major Technical & Structural Risks

### RISK 1: AST-Driven Serializer Inheritance (HIGH IMPACT / MEDIUM EFFORT)
**File**: `memores/serializers.py` (or journal serializer module — confirmed by parser finding `<inherited: BinOp(left=Name(id='JOURNAL_BASE_FIELDS', ctx=Load()), op=Add(), ...)>`)
**Classes**: `JournalEntryListSerializer`, `JournalEntryCreateSerializer`, `JournalEntryDetailSerializer`

The three journal serializers use runtime Python expression evaluation to compose field lists. The parser cannot resolve this, meaning any AI-assisted refactoring or static analysis tool will misread the serializer output. This is a maintenance hazard that compounds with every new field addition.

**Mitigation**: Replace AST manipulation with explicit `fields` lists or DRF's built-in inheritance (`class Meta: fields = JournalBaseSerializer.Meta.fields + [...]`).

### RISK 2: Inconsistent Primary Key Strategy (MEDIUM IMPACT / LOW EFFORT)
**File**: `memores/models.py`
**Model**: `RegistrationCode.id` uses `BigAutoField`; all other models use `UUIDField`.

This breaks the UUID convention and creates serialization inconsistencies. The related `RegistrationWaitlist` model also uses `BigAutoField`, suggesting this was a historical decision that should be corrected for consistency.

**Mitigation**: Migrate `RegistrationCode.id` and `RegistrationWaitlist.id` to `UUIDField(default=uuid.uuid4)`. Requires data migration.

### RISK 3: Soft-Delete Inconsistency (MEDIUM IMPACT / LOW EFFORT)
**Confirmed by ground truth**: `SoftDeleteModel` is abstract. Seven concrete models use it (`UserCourseCompletion`, `CourseProgress`, `AnalysisOutput`, `SharingCode`, `CoachEntry`, `JournalEntry`, `EmailReportRequest`). Others like `Profile`, `BenefactorCohort` do not.

The inconsistency suggests ad-hoc adoption without a clear rule for when soft-delete should be applied. The admin views confirm this: `AdminAnalysisOutputRetrieveDestroyView` uses `all_objects` (including deleted), while `AdminUserCompletedCourseDestroyView` and `AdminUserCourseProgressDestroyView` correctly implement soft deletes via `perform_destroy`.

**Mitigation**: Document the soft-delete decision framework. Add a migration to apply consistent behavior where appropriate.

### RISK 4: Public Payment Endpoint Security (HIGH IMPACT / LOW EFFORT)
**File/URL**: `memores/views/payment.stripe.StripeCheckoutSession` → `/api/v1/create-checkout-session/`
**Permission**: `AllowAny`

The Stripe checkout session endpoint is publicly accessible. The webhook at `/api/v1/stripe/webhook/` must validate signatures robustly. This is a critical security boundary that needs immediate verification.

**Mitigation**: Audit webhook signature validation (`stripe.webhooks` secret verification). Add rate limiting to `AllowAny` endpoints.

### RISK 5: Unscoped Admin Querysets (MEDIUM IMPACT / MEDIUM EFFORT)
**Parser finding**: Several admin views have `queryset_auth_chain: "unknown"` suggesting authorization boundaries are unclear or missing.

Admin views like `AdminEmailReportRequestListView`, `AdminStartEmailReportView` lack explicit `authentication_classes`. The `AdminUserPermissionUpdateView` allows PUT to update permissions—verify this is restricted to staff/superuser only.

**Mitigation**: Audit all admin view authentication/authorization. Add explicit permission classes where missing.

### RISK 6: Hard Delete on Course (LOW RISK — EXPECTED)
**Confirmed by ground truth**: `Course` inherits from `models.Model` directly (NOT `SoftDeleteModel`). `CourseDestroyView` performing hard deletes is EXPECTED behavior, not a risk. However, this should be logged/audited given irreversibility.

---

## Immediate Quick Wins (Weeks 1-3)

### W1: Standardize RegistrationCode Primary Key
**File**: `memores/models.py`, line containing `RegistrationCode` model definition
**Change**: Convert `id = BigAutoField()` to `id = UUIDField(default=uuid.uuid4, editable=False)`

```python
# Before
class RegistrationCode(models.Model):
    id = BigAutoField(primary_key=True)

# After
import uuid
class RegistrationCode(models.Model):
    id = UUIDField(default=uuid.uuid4, primary_key=True, editable=False)
```

**Migration**: Run `makemigrations` → data migration to copy existing IDs → rename column. Low risk because `RegistrationCode` is referenced via ForeignKey from `Course.registration_code` and `RegistrationWaitlist.registration_code`.

### W2: Fix Journal Serializer Inheritance
**File**: Journal serializer module (referenced in parser output)
**Change**: Replace AST manipulation with explicit field composition

```python
# Before (AST-driven — unresolvable by parser)
class JournalEntryListSerializer(JOURNAL_BASE_FIELDS):  # BinOp at runtime
    ...

# After (explicit, parseable)
class JournalBaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = JournalEntry
        fields = ['id', 'description', 'context', 'emotion', 'user', 'date']

class JournalEntryListSerializer(JournalBaseSerializer):
    class Meta(JournalBaseSerializer.Meta):
        pass  # inherits all fields explicitly
```

### W3: Add Authentication to Admin Views
**Files**: All admin views in `memores/views/admin/` that lack explicit `authentication_classes`
**Change**: Add `[TokenAuthentication]` or appropriate auth class to admin views

Target files from URL patterns:
- `memores/views/admin/email_report_request.py` — `AdminEmailReportRequestListView`, `AdminStartEmailReportView`
- `memores/views/admin/user.py` — `AdminUserPermissionUpdateView` (verify staff-only)
- `memores/views/admin/analysis_output.py` — `AdminAnalysisOutputListView`

### W4: Audit Public Endpoints for Rate Limiting
**Files**:
- `memores/views/payment/stripe.py` — `StripeCheckoutSession`
- `memores/views/public/registration.py` — `RegistrationStartView`, `RegistrationWaitlistView`, `RegistrationCompleteView`

**Change**: Add DRF throttling classes to all `AllowAny` endpoints. Verify Stripe webhook signature validation in `memores/views/payment/stripe_webhook.py`.

### W5: Add Database Indexes for High-Frequency Queries
**File**: `memores/models.py` — add indexes to frequently queried fields

```python
class AnalysisOutput(models.Model):
    status = models.CharField(max_length=64)  # Add index
    user = models.ForeignKey(Profile, on_delete=models.CASCADE)  # Already indexed via FK

    class Meta:
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['user', 'status']),
        ]

class EmailReportRequest(models.Model):
    status = models.CharField(max_length=64)  # Add index
    user = models.ForeignKey(Profile, on_delete=models.CASCADE)

    class Meta:
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['user', 'status']),
        ]

class UserResponse(models.Model):
    answer_group = models.UUIDField(null=True, blank=True)  # Add index

    class Meta:
        indexes = [
            models.Index(fields=['answer_group']),
            models.Index(fields=['user', 'course']),
        ]
```

---

## Strategic Architecture & Database Investments (Months 2-3)

### INVESTMENT 1: Extract Service Layer from Views
**Rationale**: Views like `CourseListCreateView`, `ManageCreatorContent`, and `QuestionUploadView` contain significant business logic. Analysis-related views (`AnalyseCourseResults`, `AnalysisExplanationView`) orchestrate complex LLM workflows. Separating these improves testability and reduces view complexity.

**Target files for extraction**:
- `memores/views/app/course.py` — `CourseListCreateView`, `CourseRetrieveView`, `start_or_end_course` (function view)
- `memores/views/management/content.py` — `ManageCreatorContent`, `QuestionUploadView`, `CourseUploadView`
- `memores/views/app/analysis.py` — `AnalyseCourseResults`, `AnalysisExplanationView`, `AnalysePersonalityResults`

**Proposed structure**:
```
memores/services/
├── course_service.py        # Course creation, retrieval, upload logic
├── analysis_service.py      # LLM workflow orchestration
├── coaching_service.py      # CoachEntry generation logic
└── email_report_service.py  # Email report generation
```

**Implementation**: Extract business logic from view `post()`/`get()` methods into service class methods. Views become thin controllers that validate input, call services, and return responses.

### INVESTMENT 2: Celery Task Registry & Dead-Letter Queue
**Rationale**: Celery is configured but task definitions are invisible in the topography. Models track `task_id` with `JobStatuses.PENDING/IN_PROGRESS/FINISHED/ERROR`, but there's no mechanism for alerting on stuck jobs or handling failures.

**Actions**:
1. **Map task registry**: Use `[django] list_management_commands` to identify Celery-related commands, then search for `@app.task` decorators across the codebase.
2. **Implement dead-letter queue**: For failed analysis jobs (`AnalysisOutput.status == ERROR`), route to a separate queue with retry logic and alerting.
3. **Add job monitoring**: Create admin view or endpoint to list stuck jobs (e.g., `IN_PROGRESS` for > 30 minutes).

**Target models**: `AnalysisOutput.task_id`, `EmailReportRequest.task_id`

### INVESTMENT 3: Caching Strategy for Repeated Queries
**Rationale**: The URL patterns reveal repeated queries for course lists, user profiles, and benefactor data. `CourseListView` has `pagination_class: null` suggesting potential performance issues for large datasets.

**Implementation**:
1. **Redis integration**: Add `django-redis` for cache backend.
2. **Cache invalidation**: Use Django's `@cache_page` decorator or manual cache invalidation on model save signals.
3. **Target endpoints**:
   - `GET /api/v1/courses/` (CourseListView)
   - `GET /api/v1/admin/benefactors/<uuid:benefactor_id>/courses/` (AdminBenefactorCoursesListView)
   - `GET /api/v1/user/` (UserView)

**Cache key pattern**: `{app}:{view_name}:{query_params_hash}` with TTL based on data freshness requirements.

### INVESTMENT 4: Document Database Cascade Behaviors
**Rationale**: The live schema shows many ForeignKey relationships but no cascade behavior is visible in the topography. This creates risk for data integrity during deletes/updates.

**Actions**:
1. **Run `[django] database_schema`** to get complete FK constraints and ON DELETE/UPDATE behaviors.
2. **Document cascade rules** in `docs/database-cascades.md`.
3. **Add unique constraints** where missing:
   - `SharingCode.code` (currently no unique constraint visible)
   - `RegistrationCode.code` (currently no unique constraint visible)

```python
class SharingCode(models.Model):
    code = models.CharField(max_length=255, unique=True)  # Add unique

class RegistrationCode(models.Model):
    code = models.CharField(unique=True)  # Add unique
```

### INVESTMENT 5: Standardize Serializer Naming Convention
**Rationale**: 78 serializers with inconsistent naming (`XSerializer`, `XCreateUpdateSerializer`, `XListSerializer`, `XDetailSerializer`, `XWithIdsSerializer`, `XResponseSerializer`).

**Decision framework**:
- **Read-only list**: `XListSerializer` (e.g., `CourseListSerializer`)
- **Full CRUD**: `XSerializer` with nested create/update logic
- **Admin-specific**: Prefix with `Admin` (e.g., `AdminUserProfileListSerializer`)
- **Response-only**: `XResponseSerializer` for API responses

**Migration path**: Phase out `XWithIdsSerializer` and `XCreateUpdateSerializer` in favor of explicit list/detail serializers. This requires updating all 78 serializers but provides long-term clarity.

---

## Observability, Telemetry (OpenTelemetry), & Testing Enhancements

### TELEMETRY GAP 1: OpenTelemetry Instrumentation
**Current state**: Zero instrumentation visible in topography. No tracing of API requests or Celery tasks.

**Implementation plan**:
1. **Install OpenTelemetry packages**: `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-instrumentation-django`, `opentelemetry-instrumentation-celery`
2. **Add Django middleware**: `otel.middleware.OpenTelemetryMiddleware` to trace all API requests.
3. **Instrument Celery tasks**: Add `@celery_app.task(bind=True)` with span creation for `AnalysisOutput` and `EmailReportRequest` task execution.
4. **Export to backend**: Configure `opentelemetry-exporter-otlp-proto-grpc` for Jaeger/Zipkin export.

**Target files for instrumentation**:
- `memores/views/app/analysis.py` — LLM workflow tracing (`AnalyseCourseResults`, `AnalysisExplanationView`)
- `memores/services/analysis_service.py` (post-extraction) — Task-level spans
- `memores/tasks.py` (or equivalent) — Celery task definitions

### TELEMETRY GAP 2: Structured Logging
**Current state**: No logging patterns visible in topography. Cannot assess error handling or request auditing.

**Implementation**:
1. **Add structured logging** using Python's `logging` module with JSON format.
2. **Log key events**:
   - Payment webhook processing (`memores/views/payment/stripe_webhook.py`)
   - Analysis job status changes (`AnalysisOutput.status` transitions)
   - Admin actions (permission updates, course deletes)
3. **Add request ID correlation**: Use `log_request_id.middleware.RequestIDMiddleware` (already in middleware stack) to correlate logs across services.

### TESTING GAP 1: Payment Webhook Integration Tests
**Target**: `memores/views/payment/stripe_webhook.py` at `/api/v1/stripe/webhook/`

```python
# tests/test_payment_webhooks.py
class TestStripeWebhookView(APITestCase):
    def test_valid_signature_processed(self):
        # Verify webhook signature validation works
        pass

    def test_invalid_signature_rejected(self):
        # Ensure tampered payloads are rejected
        pass

    def test_duplicate_event_ignored(self):
        # Verify idempotency for duplicate Stripe events
        pass
```

### TESTING GAP 2: Registration Flow Tests
**Target**: `memores/views/public/registration.py` — `RegistrationStartView`, `RegistrationWaitlistView`, `RegistrationCompleteView`

```python
# tests/test_registration_flow.py
class TestRegistrationFlow(APITestCase):
    def test_valid_code_allows_registration(self):
        # Verify active RegistrationCode allows flow completion
        pass

    def test_expired_code_rejected(self):
        # Ensure expired codes are rejected
        pass

    def test_rate_limiting_applied(self):
        # Verify throttling on public endpoints
        pass
```

### TESTING GAP 3: JSON Field Schema Validation
**Target**: Models with `JSONField` for structured data:
- `Course.course_meta_data`
- `Benefactor.theme_data`
- `CoachEntry.metadata`
- `Question.question_meta_data`
- `AnalysisOutput.metadata`

**Implementation**: Add schema validation in model `clean()` method or use Django's `JSONField` with `validators=[JSONSchemaValidator]`.

```python
# In models.py
from django.core.exceptions import ValidationError
import jsonschema

class Course(models.Model):
    course_meta_data = JSONField(null=True, blank=False)

    def clean(self):
        super().clean()
        if self.course_meta_data:
            try:
                jsonschema.validate(
                    self.course_meta_data,
                    COURSE_META_SCHEMA  # Define schema in constants
                )
            except jsonschema.ValidationError as e:
                raise ValidationError(f"Invalid course_meta_data: {e.message}")
```

### TESTING GAP 4: Soft-Delete Behavior Tests
**Target**: All 7 soft-delete models (`UserCourseCompletion`, `CourseProgress`, `AnalysisOutput`, `SharingCode`, `CoachEntry`, `JournalEntry`, `EmailReportRequest`)

```python
# tests/test_soft_delete.py
class TestSoftDeleteBehavior(APITestCase):
    def test_soft_deleted_record_excluded_from_queryset(self):
        # Verify default manager excludes soft-deleted records
        pass

    def test_admin_all_objects_includes_deleted(self):
        # Verify admin views use all_objects for deleted records
        pass

    def test_perform_destroy_sets_is_deleted(self):
        # Verify destroy endpoints set is_deleted=True instead of hard delete
        pass
```

---

## Organizational & Workflow Improvement Recommendations

### RECOMMENDATION 1: Establish Serializer Naming Convention as Team Standard
**Current state**: 78 serializers with inconsistent naming patterns. Parser output shows `XSerializer`, `XCreateUpdateSerializer`, `XListSerializer`, `XDetailSerializer`, `XWithIdsSerializer`, `XResponseSerializer` used interchangeably.

**Action**: Create `docs/serializer-convention.md` documenting:
- When to use each pattern
- Examples for each model domain
- Migration path for existing serializers

**Review trigger**: All new serializer PRs must follow the convention. Existing serializers can be refactored incrementally during feature work.

### RECOMMENDATION 2: Define Domain Ownership Boundaries
**Current state**: `memores/models.py` contains all 36 models in a single file, creating tight coupling. `Profile` acts as a god hub with 16+ OneToMany relationships.

**Action**: Split `models.py` into domain-specific files:
```
memores/models/
├── __init__.py
├── course_models.py      # Course, Session, Question, Audio, etc.
├── analysis_models.py    # AnalysisOutput, AnalysisResult, PromptTemplate, etc.
├── user_models.py        # Profile, UserResponse, UserCourseCompletion, etc.
├── admin_models.py       # Benefactor, CourseProvider, RegistrationCode, etc.
└── sharing_models.py     # SharingCode, CoachEntry, JournalEntry, etc.
```

**Migration path**: Phase 1: Create new files with model definitions. Phase 2: Update `memores/models/__init__.py` to import from new files. Phase 3: Delete old `models.py`.

### RECOMMENDATION 3: Implement PR Review Checklist for Security-Critical Changes
**Current state**: Public endpoints (`AllowAny`) lack explicit security review. Admin views have unclear authorization boundaries.

**Checklist items for PRs touching**:
- [ ] Payment/webhook endpoints: Verify signature validation, rate limiting
- [ ] Registration flows: Verify input validation, rate limiting, code expiration
- [ ] Admin views: Verify `authentication_classes` and `permission_classes` are explicit
- [ ] JSONField changes: Verify schema validation is added or maintained
- [ ] Soft-delete models: Verify `perform_destroy` uses soft delete, not hard delete

### RECOMMENDATION 4: Establish Dead-Letter Queue Process for Failed Analysis Jobs
**Current state**: `JobStatuses.ERROR` exists but no alerting or retry mechanism. `AnalysisOutput.error_message` and `EmailReportRequest.error_message` fields exist but have no propagation strategy.

**Action**:
1. **Define SLA**: Jobs in `IN_PROGRESS` for > 30 minutes are "stuck."
2. **Create monitoring endpoint**: `GET /api/v1/admin/jobs/stuck/` to list stuck jobs.
3. **Implement retry logic**: Failed analysis jobs retry up to 3 times with exponential backoff.
4. **Alert on persistent failures**: After 3 retries, notify on-call engineer via Slack/email.

### RECOMMENDATION 5: Document Soft-Delete Decision Framework
**Current state**: 7 models use `SoftDeleteModel`, others don't. No clear rule for when to apply soft-delete.

**Decision framework**:
- **User-generated content** (JournalEntry, CoachEntry): SOFT DELETE — users may want to recover entries.
- **Audit-critical data** (AnalysisOutput, EmailReportRequest): SOFT DELETE — compliance requires retention.
- **Transactional data** (CourseProgress, UserCourseCompletion): SOFT DELETE — allows re-enrollment.
- **System entities** (BenefactorCohort, CourseProvider): HARD DELETE — no user recovery needed.

**Documentation**: Add to `docs/architecture/soft-delete-policy.md` with examples and migration guidelines for new models.

### RECOMMENDATION 6: Implement Migration Review Process
**Current state**: No visible migration review process. Inconsistent PK strategy (`RegistrationCode.id` uses `BigAutoField`) suggests migrations were applied without cross-model consistency checks.

**Action**:
1. **Pre-migration checklist**: Verify new migrations don't break FK relationships, add unexpected indexes, or change column types without data migration.
2. **Post-migration verification**: Run `[django] run_check` with `tags=["models"]` to validate schema integrity.
3. **Migration naming convention**: Use descriptive names (e.g., `0012_add_analysis_output_status_index`) instead of auto-generated numbers.

---

## Summary Timeline

| Week | Focus Area | Key Deliverables |
|------|-----------|------------------|
| **W1** | Quick Wins | Standardize `RegistrationCode.id` to UUIDField, fix journal serializer inheritance |
| **W2** | Security Audit | Add auth classes to admin views, audit public endpoints for rate limiting, verify Stripe webhook signature validation |
| **W3** | Performance | Add database indexes on high-frequency query fields (`AnalysisOutput.status`, `EmailReportRequest.status`, `UserResponse.answer_group`) |
| **M2 W1-2** | Architecture | Extract service layer from views (course, analysis, coaching), document soft-delete policy |
| **M2 W3-4** | Observability | Implement OpenTelemetry instrumentation for Django + Celery, add structured logging |
| **M3 W1-2** | Infrastructure | Implement Celery dead-letter queue, add caching strategy with Redis, document database cascade behaviors |
| **M3 W3-4** | Testing & Docs | Write integration tests for payment webhooks, registration flow, soft-delete behavior; finalize serializer naming convention docs |

This roadmap prioritizes immediate security and consistency wins (Weeks 1-3) while building foundational investments in observability, architecture, and testing over Months 2-3. Each phase is designed to be independently valuable and deployable without blocking subsequent work.

---
--------------------------------------------------
## Fidelity Check

**Score:** 9/10 — Good

**Parser Ground Truth:** 36 concrete models, 1 abstract, 78 serializers, 98 views (75 class-based, 23 function-based)

**Issues Found:**

  ⚠ Line 398: Claims 7 model count (parser found 37)

  ✓ Correctly distinguishes concrete vs abstract models
