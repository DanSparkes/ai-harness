# Executive Summary & Core Codebase Impressions

This is a Django 5.2 + DRF application with ~37 models, 80 serializers, 113 views (89 CBV / 24 FBV), and 160+ URL patterns. The codebase exhibits classic "growing pains" architecture: a clear LLM-powered analysis pipeline (`AnalysisOutput` → `EmailReportRequest`) that lives entirely in views with no service layer, serializer proliferation without shared base classes, and mixed view patterns (CBV/FBV) with no architectural rule for when to use which.

**Core impressions:**
- **High coupling between HTTP handling and business logic.** The entire LLM analysis pipeline (`AnalyseCourseResults`, `start_journal_analysis`) lives in views — no domain service layer exists to test state transitions (`PENDING` → `IN_PROGRESS` → `FINISHED`/`ERROR`) in isolation.
- **Serializer inheritance is AST-invisible.** Journal and Benefactor serializers use `BinOp` expressions for Meta fields that the parser cannot resolve, meaning field composition changes silently cascade across 3+ serializers with no compile-time signal.
- **Authorization is inconsistent by design.** Content creator listing requires `IsStaffOrSuperUser` while regular admin user listing only needs `IsAuthenticated` — backwards from a security standpoint. Stripe webhook has no visible permission_classes (public by design, but signature verification logic is opaque).
- **Soft delete is partially implemented.** 7 models inherit from abstract `SoftDeleteModel`, but `AdminAnalysisOutputRetrieveDestroyView` uses `all_objects` to bypass soft deletes for admins. The `Course` model does NOT inherit from `SoftDeleteModel` — hard deletes are expected behavior, not a bug.
- **No observability infrastructure.** Zero logging calls visible in any view/serializer/model. Celery tasks (`django_celery_beat`, `django_celery_results`) have no tracing. Health check endpoint has no database/Celery worker verification.

**Scale context:** The admin app alone contains ~50 views spanning 16+ files — this is a single Django app's admin surface area that rivals many small projects' total view count.

---

# Major Technical & Structural Risks

## 1. Stripe Webhook Signature Verification Gap
- **File:** `memores.views.payment.stripe_webhook.stripe_webhook_view`
- **Risk:** Public endpoint with no visible `permission_classes` or `authentication_classes`. Without signature verification, any attacker can forge webhook events to trigger checkout sessions or update billing status.
- **Status:** Confirmed — the parser shows zero auth configuration on this view. Must verify Stripe secret validation exists in method body (cannot be resolved from AST alone).

## 2. Authorization Inversion: Content Creators vs Regular Users
- **File:** `memores.views.admin.user`
- **Risk:** `AdminContentCreatorListView` uses `IsStaffOrSuperUser` while `AdminProfileListView` only requires `IsAuthenticated`. Content creators are a privileged subset — listing them should require higher authorization, not lower.
- **Status:** Confirmed from parser output.

## 3. Soft Delete Bypass in Admin Analysis Output Destroy
- **File:** `memores.views.admin.analysis_output.AdminAnalysisOutputRetrieveDestroyView`
- **Risk:** Uses `all_objects` manager (includes soft-deleted records) instead of the default queryset that filters by `is_deleted=False`. Admin users can destroy "deleted" analysis outputs — effectively a hard delete bypass.
- **Status:** Confirmed from parser output.

## 4. Mass Assignment in Profile Serializer
- **File:** `memores.views.app.user.UserView` → `ProfileSerializer`
- **Risk:** Exposes `password_reset_code`, `stripe_customer_id`, and `meta` (JSONField) as writable fields without explicit `read_only_fields`. A client could overwrite sensitive fields via PATCH.
- **Status:** Confirmed from parser output — these fields appear in Meta fields list without read-only designation.

## 5. Unscoped Queryset in Registration Code Update Path
- **File:** `memores.views.registration_code_handler.RegistrationCodeRetrieveUpdateView`
- **Risk:** `update()` method has `self_scoped: false` and `auth_fully_trusted: false`, while the create path (`RegistrationCodeCreateListView.get_queryset()`) calls `authorize_benefactor`. The update may not properly scope to the benefactor.
- **Status:** Uncertain — parser flags inconsistency but cannot resolve actual queryset filtering in method body.

## 6. AST-Invisible Serializer Inheritance (Journal & Benefactor)
- **Files:** Journal serializers (`JournalEntryListSerializer`, `JournalEntryCreateSerializer`, `JournalEntryDetailSerializer`), Benefactor serializers (`BaseBenefactorSerializer` → `BenefactorSerializer`, `BenefactorCreateSerializer`, `BenefactorUpdateSerializer`)
- **Risk:** Meta fields use `<inherited: BinOp(...)>` expressions that the parser cannot resolve. Any change to `JOURNAL_BASE_FIELDS` silently cascades across three serializers with no compile-time signal.
- **Status:** Confirmed from parser output — these are AST-invisible patterns.

## 7. Hardcoded Magic Numbers in Registration Code
- **File:** `memores.models.RegistrationCode.available_uses`
- **Risk:** Defaults to `99999` — likely unintentional for production codes. If a new registration code is created without specifying available uses, it gets 99,999 uses.
- **Status:** Confirmed from parser output.

## 8. Sentiment Field Type Mismatch
- **File:** `memores.models.ResponseOption.sentiment`
- **Risk:** `CharField(max_length=128)` with default `"0"` (string). Sentiment values should be validated against an enum rather than accepting arbitrary strings.
- **Status:** Confirmed from parser output.

---

# Immediate Quick Wins (Weeks 1-3)

## Week 1: Database Indexes & URL Naming

### Add Missing Database Indexes
**Impact:** High | **Effort:** Low | **Risk:** Minimal

Add indexes to foreign keys that are almost certainly used in WHERE clauses:

```python
# In memores/models.py or via migration
class UserResponse(models.Model):
    # ... existing fields ...

    class Meta:
        indexes = [
            models.Index(fields=['user', 'course'], name='idx_user_response_user_course'),
            models.Index(fields=['user', 'timestamp'], name='idx_user_response_user_timestamp'),
        ]

class CourseProgress(models.Model):
    # ... existing fields ...

    class Meta:
        indexes = [
            models.Index(fields=['user', 'course'], name='idx_course_progress_user_course'),
        ]
```

**Rationale:** `UserResponse.answer_group` already has `db_index: true`, but the foreign keys (`user_id`, `course_id`) that are almost certainly used in WHERE clauses do not. Adding these indexes will dramatically improve query performance for user history lookups.

### Add URL Names to Management API Routes
**Impact:** Medium | **Effort:** Low | **Risk:** Minimal

The management content URLs are completely unnamed, forcing string-literal URL matching everywhere:

```python
# In memores/urls.py or the relevant url config file
urlpatterns = [
    path('api/v1/management/content/course-groups/<str:id>/',
         CourseGroupRetrieveUpdateView.as_view(), name='management-course-group-detail'),
    path('api/v1/management/content/courses/upload/',
         CourseUploadView.as_view(), name='management-courses-upload'),
    # ... add names to all 20+ unnamed management routes
]
```

**Rationale:** Named URLs enable `reverse()` in tests and admin views, and make the URL structure discoverable. The parser confirms over 160 URL patterns with the vast majority having `"name": null`.

## Week 2: Authorization Fixes & Field Validation

### Fix Stripe Webhook Signature Verification
**Impact:** Critical | **Effort:** Medium | **Risk:** High if missed

**Action:** Locate `stripe_webhook_view` in `memores/views/payment/stripe_webhook.py` and verify:
1. Stripe secret validation is present (check for `stripe.Webhook.construct_event()` or similar)
2. If missing, add signature verification before processing the event
3. Add a comment documenting the security requirement

**Rationale:** Public endpoint with no visible permission_classes. Without signature verification, any attacker can forge webhook events.

### Add Field Validation to RegistrationCode.clean()
**Impact:** Medium | **Effort:** Low | **Risk:** Minimal

```python
# In memores/models.py
class RegistrationCode(models.Model):
    # ... existing fields ...

    def clean(self):
        super().clean()
        if self.available_uses <= 0:
            raise ValidationError({'available_uses': 'Available uses must be positive.'})
        if self.expiration_timestamp and self.expiration_timestamp < timezone.now():
            raise ValidationError({'expiration_timestamp': 'Expiration timestamp must be in the future.'})
```

**Rationale:** The model has a `clean()` method but no visible validation logic. Adding validation for `available_uses > 0` and `expiration_timestamp > now()` would catch data entry errors early.

### Convert High-Volume Function Views to Class-Based
**Impact:** Medium | **Effort:** Medium | **Risk:** Low (with tests)

Convert the following FBVs to CBVs:

1. **Public auth flow:**
   - `forgot_password` → `RetrieveUpdateAPIView` or custom `APIView`
   - `update_password` → `RetrieveUpdateAPIView`

2. **Admin data exports:**
   - `get_unstructured_interactions` → `RetrieveAPIView`
   - `get_user_responses` → `RetrieveAPIView`
   - `get_course_durations` → `RetrieveAPIView`

**Rationale:** The 24 FBVs are disproportionately concentrated in simple GET/POST handlers ideal for CBVs. This standardizes the view layer and reduces duplication.

## Week 3: Serializer Consistency & Health Check Depth

### Standardize ProfileSerializer Read-Only Fields
**Impact:** High | **Effort:** Low | **Risk:** Minimal

```python
# In memores/serializers.py or relevant file
class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'first_name', 'last_name', 'email', ...]
        read_only_fields = [
            'password_reset_code',  # Add these
            'stripe_customer_id',   # Add these
            'meta',                 # Add these
        ]
```

**Rationale:** Prevents mass assignment of sensitive fields. The parser confirms `password_reset_code`, `stripe_customer_id`, and `meta` are writable without explicit `read_only_fields`.

### Enhance Health Check Endpoint
**Impact:** Medium | **Effort:** Low | **Risk:** Minimal

```python
# In memores/views/health_check.py
class HealthCheckView(BaseHealthCheckView):
    def check(self, request):
        results = []

        # Database connectivity check
        try:
            from django.db import connection
            connection.ensure_connection()
            results.append({'name': 'database', 'status': 'ok'})
        except Exception as e:
            results.append({'name': 'database', 'status': 'error', 'message': str(e)})

        # Celery worker check
        try:
            from celery import current_app
            status = current_app.control.inspect().ping()
            if status and any(status.values()):
                results.append({'name': 'celery', 'status': 'ok'})
            else:
                results.append({'name': 'celery', 'status': 'error', 'message': 'No workers responding'})
        except Exception as e:
            results.append({'name': 'celery', 'status': 'error', 'message': str(e)})

        # Stripe API health verification (optional)
        try:
            import stripe
            stripe.api_key = settings.STRIPE_SECRET_KEY
            stripe.Account.retrieve()  # or any lightweight call
            results.append({'name': 'stripe', 'status': 'ok'})
        except Exception as e:
            results.append({'name': 'stripe', 'status': 'error', 'message': str(e)})

        return Response(results)
```

**Rationale:** The parser shows `HealthCheckView` extends `BaseHealthCheckView` but has no methods and empty class_attributes. Adding database/Celery/Stripe checks provides operational visibility into service health.

---

# Strategic Architecture & Database Investments (Months 2-3)

## Month 2: Service Layer Extraction

### Extract LLM Analysis Pipeline into Dedicated App
**Impact:** High | **Effort:** High | **Risk:** Medium (requires careful refactoring)

**Target:** Create `memores/analysis/` or `memores/llm/` app with proper domain models.

**Current state:** The entire async analysis flow lives inside views:
1. User triggers analysis → `AnalyseCourseResults` or `start_journal_analysis`
2. Celery task runs with `JobStatuses.PENDING → IN_PROGRESS → FINISHED/ERROR`
3. Result stored in `AnalysisOutput.output` (TextField) and `AnalysisResult.result` (JSONField)
4. Optional email report via `EmailReportRequestCreateView`

**Proposed structure:**
```python
# memores/analysis/services.py
class AnalysisService:
    def __init__(self, user: User, course_id: str):
        self.user = user
        self.course_id = course_id

    async def run_analysis(self) -> AnalysisOutput:
        # Create AnalysisOutput with status=PENDING
        # Dispatch to Celery task
        # Return AnalysisOutput instance

    def get_status(self, analysis_output_id: UUID) -> JobStatuses:
        # Query AnalysisOutput.status
```

**Benefits:**
- Separate concerns between HTTP handling and LLM orchestration
- Enable unit testing of analysis logic without DRF fixtures
- Make the `PENDING`→`FINISHED` state machine testable in isolation

### Extract Content Management Service
**Impact:** High | **Effort:** High | **Risk:** Medium (requires careful refactoring)

**Target:** Create `memores/content/` service layer.

**Current state:** Management views (`CourseListCreateView`, `QuestionCreateView`, `SessionCreateView`, `CourseUploadView`) all call `authorize_creator` or `authorize_staff_or_superuser`. Authorization logic is duplicated across 20+ views.

**Proposed structure:**
```python
# memores/content/services.py
class ContentAuthorizationService:
    @staticmethod
    def authorize_creator(user: User, course_id: UUID) -> None:
        # Centralized authorization logic

    @staticmethod
    def authorize_staff_or_superuser(user: User) -> None:
        # Centralized authorization logic

class CourseService:
    def __init__(self, user: User):
        self.user = user

    async def create_course(self, data: dict) -> Course:
        ContentAuthorizationService.authorize_creator(user, ...)
        # Business logic for course creation

    async def upload_courses(self, files: list[UploadedFile]) -> list[Course]:
        # Upload flow with centralized auth
```

**Benefits:**
- Centralize authorization logic and make it testable
- Reduce duplication across 20+ management views
- Enable consistent error handling for unauthorized access

## Month 3: Caching & Task Observability

### Implement Per-Benefactor Caching Strategy
**Impact:** Medium | **Effort:** Medium | **Risk:** Low (with cache invalidation)

**Target endpoints with no caching:**
- `CourseListView` returns `CourseFullListSerializer` for all authenticated users — identical data per benefactor but no cache key differentiation visible.
- `AdminBenefactorCoursesListView` and `AdminBenefactorUsersListView` return filtered lists that change infrequently.
- `SharingCodeRetrieveUpdateDestroyView` reads by code (unique, rarely changes).

**Implementation:**
```python
# In memores/cache.py or relevant utility file
from django.core.cache import cache

class CourseCacheService:
    @staticmethod
    def get_courses_for_benefactor(benefactor_id: UUID) -> list[Course]:
        cache_key = f'courses:{benefactor_id}'
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        courses = Course.objects.filter(content_creator__benefactor=benefactor_id)
        cache.set(cache_key, courses, timeout=300)  # 5 minutes
        return courses

    @staticmethod
    def invalidate_benefactor_courses(benefactor_id: UUID) -> None:
        cache.delete(f'courses:{benefactor_id}')
```

**Rationale:** Read-heavy endpoints with no caching would benefit significantly from per-benefactor cache keys with `django.core.cache`.

### Celery Task Observability
**Impact:** Medium | **Effort:** Medium | **Risk:** Low (with proper instrumentation)

**Current state:** Celery tasks have no tracing. No task success/failure metrics, no duration tracking beyond `PromptSummary.duration_seconds` (which only covers LLM calls, not the full pipeline).

**Implementation:**
1. Add `celery.contrib.trace` or OTel integration for task spans
2. Connect `RequestIDMiddleware` to trace context for request ID propagation
3. Add dead letter queue and retry monitoring via Celery Beat + Flower (or OTel)

**Rationale:** The topography shows `task_id` on both `AnalysisOutput` and `EmailReportRequest`, confirming async execution. But without tracing, operational visibility into analysis pipeline reliability is zero.

### Simulated Data Runs — Production Isolation
**Impact:** Medium | **Effort:** Low | **Risk:** High if not isolated

**Current state:** The `AdminSimulatedData*` views (8 endpoints under `/api/v1/admin/simulated-data/`) are clearly development/QA tools for generating test data. They're exposed in production URLs with only `_authorize_simulated_data_non_cleanup` / `_authorize_simulated_data_cleanup` as guards.

**Implementation:**
```python
# In settings.py or relevant config file
SIMULATED_DATA_ENABLED = False  # Disable by default in production

# In memores/views/admin/simulated_data_runs.py
from django.conf import settings

class AdminSimulatedDataStatusView(APIView):
    def get(self, request):
        if not settings.SIMULATED_DATA_ENABLED:
            return Response(
                {'error': 'Simulated data is disabled in production'},
                status=status.HTTP_403_FORBIDDEN
            )
        # ... existing logic
```

**Rationale:** These should be moved behind a Django setting flag (`SIMULATED_DATA_ENABLED = False` in production) or restricted to specific IP ranges via `AllowCIDRMiddleware` (which is already in the middleware stack but not visibly configured for this purpose).

---

# Observability, Telemetry (OpenTelemetry), & Testing Enhancements

## 1. Logging Infrastructure Setup
**Impact:** High | **Effort:** Medium | **Risk:** Minimal

**Current state:** Zero logging calls visible in any view, serializer, or model method. No `logger.info()`, no `logging.warning()` on error paths, no structured logging for API requests.

**Implementation:**
```python
# In memores/logging.py or settings.py LOGGING config
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            '()': 'pythonjsonlogger.jsonlogger.JsonFormatter',
            'format': '%(asctime)s %(name)s %(levelname)s %(message)s'
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
    },
    'loggers': {
        'memores': {
            'handlers': ['console'],
            'level': 'INFO',
        },
    },
}

# In memores/views/app/analysis.py (or relevant view)
import logging

logger = logging.getLogger(__name__)

class AnalyseCourseResults(APIView):
    def post(self, request, course_id):
        logger.info(
            'Analysis triggered',
            extra={
                'user_id': request.user.id,
                'course_id': course_id,
                'timestamp': timezone.now().isoformat(),
            }
        )
        # ... existing logic
```

**Rationale:** When a Celery task moves from `PENDING` to `FINISHED`, nothing is recorded. Structured logging enables debugging and monitoring of the analysis pipeline.

## 2. OpenTelemetry Instrumentation
**Impact:** High | **Effort:** Medium | **Risk:** Low (with proper configuration)

**Current state:** No visible tracing setup. Celery task spans are not instrumented, no request ID propagation from Django (`RequestIDMiddleware` exists but isn't connected to any trace context), no distributed tracing across the Stripe webhook → analysis output → email report pipeline.

**Implementation:**
```python
# In settings.py or relevant config file
INSTALLED_APPS = [
    # ... existing apps ...
    'opentelemetry.instrumentation.django',
    'opentelemetry.instrumentation.celery',
]

MIDDLEWARE = [
    'opentelemetry.instrumentation.django.DjangoInstrumentor',  # Add this
    'log_request_id.middleware.RequestIDMiddleware',
    # ... existing middleware ...
]

# In memores/tracing.py or relevant utility file
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

resource = Resource.create({'service.name': 'memores-api'})
provider = TracerProvider(resource=resource)
provider.add_span_processor(OTLPSpanExporter())
trace.set_tracer_provider(provider)

# In memores/views/app/analysis.py (or relevant view)
tracer = trace.get_tracer(__name__)

class AnalyseCourseResults(APIView):
    def post(self, request, course_id):
        with tracer.start_as_current_span('analyse_course_results'):
            # ... existing logic
```

**Rationale:** Enables distributed tracing across the Stripe webhook → analysis output → email report pipeline. Critical for debugging async task failures and performance bottlenecks.

## 3. Testing Enhancements
**Impact:** High | **Effort:** Medium | **Risk:** Low (with proper test isolation)

### Add Unit Tests for Analysis State Machine
**Target:** `JobStatuses.PENDING` → `IN_PROGRESS` → `FINISHED`/`ERROR` transitions in `AnalysisOutput`.

```python
# In tests/test_analysis.py
from memores.models import AnalysisOutput, JobStatuses

class TestAnalysisStateMachine:
    def test_pending_to_in_progress(self):
        output = AnalysisOutput.objects.create(status=JobStatuses.PENDING)
        # Trigger task start
        output.status = JobStatuses.IN_PROGRESS
        output.save()

        assert output.status == JobStatuses.IN_PROGRESS

    def test_in_progress_to_finished(self):
        output = AnalysisOutput.objects.create(status=JobStatuses.IN_PROGRESS)
        # Simulate successful completion
        output.output = '{"result": "success"}'
        output.status = JobStatuses.FINISHED
        output.save()

        assert output.status == JobStatuses.FINISHED

    def test_in_progress_to_error(self):
        output = AnalysisOutput.objects.create(status=JobStatuses.IN_PROGRESS)
        # Simulate failure
        output.error_message = 'LLM timeout'
        output.status = JobStatuses.ERROR
        output.save()

        assert output.status == JobStatuses.ERROR
```

### Add Integration Tests for Stripe Webhook Signature Verification
**Target:** `memores.views.payment.stripe_webhook.stripe_webhook_view`

```python
# In tests/test_stripe_webhook.py
import stripe
from django.test import TestCase, Client
from unittest.mock import patch

class TestStripeWebhook:
    def test_valid_signature(self):
        client = Client()
        payload = '{"event": "checkout.session.completed"}'
        signature = stripe.Webhook.construct_event(payload, 'sig', 'whsec_test')

        response = client.post(
            '/api/v1/stripe/webhook/',
            data=payload,
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE=signature
        )

        assert response.status_code == 200

    def test_invalid_signature(self):
        client = Client()
        payload = '{"event": "checkout.session.completed"}'

        response = client.post(
            '/api/v1/stripe/webhook/',
            data=payload,
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='invalid_sig'
        )

        assert response.status_code == 400
```

### Add Property-Based Tests for Serializer Field Consistency
**Target:** Journal and Benefactor serializers with AST-invisible Meta inheritance.

```python
# In tests/test_serializer_consistency.py
from django.test import TestCase

class TestSerializerFieldConsistency:
    def test_journal_serializers_share_base_fields(self):
        from memores.serializers import (
            JournalEntryListSerializer,
            JournalEntryCreateSerializer,
            JournalEntryDetailSerializer,
        )

        base_fields = {'id', 'description', 'context', 'emotion'}

        for serializer_class in [
            JournalEntryListSerializer,
            JournalEntryCreateSerializer,
            JournalEntryDetailSerializer,
        ]:
            fields = set(serializer_class.Meta.fields)
            assert base_fields.issubset(fields), \
                f'{serializer_class.__name__} missing base fields'
```

---

# Organizational & Workflow Improvement Recommendations

## 1. Establish View Pattern Rules
**Impact:** High | **Effort:** Low | **Risk:** Minimal

**Current state:** The codebase mixes `ListCreateAPIView` / `RetrieveUpdateDestroyAPIView`, custom `APIView` subclasses, and function-based views decorated with `@api_view(...)` — no clear rule for when to use which style.

**Recommendation:**
- Create a team agreement document (e.g., `CONTRIBUTING.md` or internal wiki) that defines:
  - **Use CBVs for:** Standard CRUD operations, list/detail views, admin endpoints
  - **Use FBVs for:** Webhooks (Stripe), complex multi-step flows with signature verification, one-off handlers
- Add a linting rule (e.g., `pylint` or `flake8`) to flag unauthorized view patterns in new code

**Rationale:** The parser shows 113 views with no consistent architectural pattern. Establishing rules prevents future fragmentation.

## 2. Implement Serializer Review Checklist
**Impact:** Medium | **Effort:** Low | **Risk:** Minimal

**Current state:** 80 serializers across ~25 files, with AST-invisible Meta inheritance in Journal and Benefactor serializers. No shared base class enforces consistency.

**Recommendation:** Add a PR checklist item for serializer changes:
- [ ] Does this serializer inherit from `BaseSerializer` or use `BinOp` expressions? If yes, document the inheritance chain.
- [ ] Are sensitive fields (`password_reset_code`, `stripe_customer_id`, `meta`) marked as `read_only_fields`?
- [ ] Have you verified that changes to base field sets (e.g., `JOURNAL_BASE_FIELDS`) don't silently cascade across dependent serializers?

**Rationale:** The parser confirms AST-invisible inheritance patterns. A review checklist catches issues before they reach production.

## 3. Establish Soft Delete Ownership
**Impact:** Medium | **Effort:** Low | **Risk:** Minimal

**Current state:** 7 models inherit from abstract `SoftDeleteModel`, but `AdminAnalysisOutputRetrieveDestroyView` uses `all_objects` to bypass soft deletes for admins. The `Course` model does NOT inherit from `SoftDeleteModel`.

**Recommendation:**
- Document which models use soft delete and why (e.g., "AnalysisOutput uses soft delete because admin users need to recover deleted records")
- Clarify the exception: `AdminAnalysisOutputRetrieveDestroyView` intentionally bypasses soft delete for admin recovery — document this as a feature, not a bug
- For `Course`, confirm with stakeholders that hard deletes are expected behavior (not a missed opportunity for soft delete)

**Rationale:** The parser confirms mixed soft delete implementation. Documentation prevents confusion and ensures consistent behavior.

## 4. Implement Admin View Ownership Boundaries
**Impact:** High | **Effort:** Medium | **Risk:** Low (with proper handoff)

**Current state:** The admin directory alone contains ~50 views spanning 16+ files — this is a single Django app's admin surface area that rivals many small projects' total view count.

**Recommendation:**
- Assign ownership of admin subdirectories to specific team members:
  - `admin.py`, `user.py`, `benefactor.py` → Backend Lead
  - `analysis_output.py`, `analysis_results.py` → LLM/Analysis Team
  - `simulated_data_runs.py` → QA/Test Infrastructure
  - `reports.py`, `data.py` → Data/Analytics Team
- Add a CODEOWNERS file (or equivalent) to enforce review requirements

**Rationale:** The admin app's size and complexity require clear ownership boundaries to prevent review bottlenecks and ensure domain expertise is applied.

## 5. Establish URL Naming Convention
**Impact:** Medium | **Effort:** Low | **Risk:** Minimal

**Current state:** Over 160 URL patterns with the vast majority having `"name": null`. Named URLs are critical for `reverse()` calls and test assertions — unnamed routes force string-literal URL matching everywhere.

**Recommendation:**
- Adopt a naming convention: `{app}_{model}_{action}` (e.g., `management_courses_upload`, `admin_benefactor_list`)
- Add a CI check that fails if new URL patterns are added without names
- Prioritize naming the 20+ management API routes first (highest impact on testability)

**Rationale:** The parser confirms over 160 URL patterns with the vast majority unnamed. A naming convention improves maintainability and test reliability.

---
--------------------------------------------------
## Fidelity Check

**Score:** 10/10 — Excellent

**Parser Ground Truth:** 36 concrete models, 1 abstract, 80 serializers, 113 views (89 class-based, 24 function-based)

**Issues Found:**

  ⚠ Does not distinguish concrete vs abstract models
