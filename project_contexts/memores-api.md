# Project Context: memores-api

## Overview
Django 5.2 + DRF 3.16 backend for a health/wellness platform called "Sparks" — audio-based personality assessments, AI-powered analysis (via Claude API), journaling, text-based coaching, result sharing, and scheduled email reports. Uses Stripe for checkout, Celery/Redis for async jobs, Channels for WebSocket push, PostgreSQL, Docker, and OpenTelemetry for observability.

Single monolithic Django app (`memores/`) — all 27 models in one `models.py`, views organized by domain in subdirectories, business logic in a `services/` layer.

## Database: 27 Models
Key models: `Profile` (extended user), `Course`/`Session`/`Audio`/`Question` (content tree), `UserResponse`/`UserCourseCompletion`/`CourseProgress` (user progress), `AnalysisOutput`/`AnalysisResult` (LLM results), `JournalEntry`/`CoachEntry` (user generated content), `CourseProviderGrant` (access control), `RegistrationCode` (invitation system), `Benefactor`/`BenefactorCohort` (sponsor orgs), `SharingCode`, `EmailReportRequest`, `PromptTemplate`, `LlmUseSummary`.

All models use `SoftDeleteModel` abstract base (soft-delete via `is_deleted` flag).

## API Surface: ~80+ Endpoints
- **Public** (no auth): Login, password reset, registration (code-based), Stripe checkout/webhook, health check
- **App** (token auth): Courses, user responses, audio tracking, analysis, journal, coach, sharing codes, job status (REST + WebSocket)
- **Admin** (staff/superuser): User management, benefactor management, analysis output inspection, course provider grants, prompt templates, data views, email reports
- **Management** (content creator/staff): CRUD for courses, sessions, questions, response options, S3 uploads

## Celery Jobs (7 tasks)
| Task | Purpose |
|------|---------|
| `claude_ai_job` | Generic Claude AI analysis |
| `coaching_job` | AI coaching response |
| `detailed_report_job` | Detailed report + email |
| `email_report_cron_job` | Cron: kick pending email reports |
| `journal_basic_job` / `journal_advanced_job` | Journal analysis (2 tiers) |
| `personality_report_job` | Personality report |
| `result_explanation_job` | Result explanation |

## Services Layer (15 modules)
`course_service.py`, `course_access_service.py`, `course_provider_service.py`, `job_service.py`, `journal_service.py`, `coach_helper.py`, `profile_permission_service.py`, `user_helper.py`, `common.py`, `ws_push.py`, `content_manage_service.py`, `admin_docs_service.py`, `email_service.py`, `reports/base_report.py`, `reports/report_service.py`

## Serializers (18 modules)
Cover all models plus: `access_gates.py`, `base_serializers.py` (None-field exclusion), `exceptions.py` (409 Conflict), `data_view_serializers.py`

## Test Infrastructure (~35 test files)
- Factories: factory_boy factories for all models (UserFactory, ProfileFactory, etc.)
  Use factories in setUpTestData for all test data creation. NEVER use direct model creation (.objects.create, .objects.create_user, etc.).
- Base classes: `AuthAPITestCase`, `StaffAPITestCase`, `SuperUserAPITestCase`, `BenefactorAPITestCase`
- Coverage: Views (admin: 11 files, app: 9, management: 1, public: 3), Services (12 files)
- Config: `test_settings.py` — eager Celery, in-memory channel layer, locmem email, MD5 password hasher
- Run tests: `docker compose exec api sh -c "DJANGO_SETTINGS_MODULE=memores.test_settings pytest --no-migrations --numprocesses=auto"`

## External Integrations
- **Claude API** — tenacity retry (3 attempts, exponential backoff), supports multiple Claude models
- **Stripe** — checkout sessions (30-day trial), webhook handler for `checkout.session.completed`
- **AWS S3** — audio and avatar buckets via boto3 presigned URLs
- **OpenTelemetry** — Django + Celery instrumentation via aws-opentelemetry-distro
- **Email** — SMTP-based (signup, forgot password, reports)

## Infrastructure
- Docker: Multi-stage nginx + Python 3.12, 5 services (db, api, redis, celery, celery-beat)
- Gunicorn: UvicornWorker, 2 workers, Unix socket, 60s timeout
- CI/CD: GitHub Actions — linting, type checking, tests on PR; deploy pipeline
- AWS: ECS task definitions for web, celery workers, migrations

## Code Quality
16 pre-commit hooks including: black, isort, mypy, bandit, flake8 (with bugbear/comprehensions/mutable/simplify/print plugins), pip-audit, autoflake, pyupgrade, django-upgrade. Custom `check_drf_idioms.py` AST linter catches DRF anti-patterns.

## Load Testing (`load_testing/`)
The `load_testing/` directory is a standalone **Locust** project, not a Django app. It has its own `requirements.txt` and `locust.conf`, and runs independently from the Django project. Tasks in `load_testing/tasks/` define Locust `HttpUser` behaviors for simulating real user traffic. Do not apply Django conventions (models, views, serializers, settings, migrations) to files under `load_testing/`.

## Code Reviewer Guidance — Flag If the Diff Touches These

### Known Anti-Patterns
1. **Thick views with business logic**: `RegistrationCompleteView.post()` is ~130 lines. Flag any view method exceeding ~50 lines that mixes request handling with service logic — delegate to services.
2. **Mixed auth patterns**: Codebase mixes procedural `authorize_*()` calls with DRF `permission_classes`. Flag new views that add to this inconsistency. New code should use one pattern consistently.
3. **Django forms + DRF hybrid**: Registration uses both Django `RegistrationForm` and DRF serializers. Flag new code introducing further form/serializer duplication.
4. **Constants duplication**: `UserTypes` exists in both `constants/constants.py` and `views/constants.py`. Flag new duplication of enums/constants across modules.
5. **Active refactoring TODOs**: Coach and journal views contain `# TODO` and legacy-route comments. Flag new endpoints adding more deprecated-pattern routes instead of migrating.
6. **MCP server stub**: `AGENTS.md` references `memores/mcp.py` (SSE) — file doesn't exist. Flag new code depending on it.

### Model & DB Patterns
1. All models use `SoftDeleteModel` (soft-delete via `is_deleted`). Ensure new models/queries respect this.
2. `CourseProviderMap` has `managed = False` — table exists independently outside migrations. Do not suggest migration changes for it.
3. Migration numbering has gaps (0001 → 0002 → 0006, etc.) suggesting past squashes. Flag new migrations re-adding already-existing tables/fields.

### Service Layer Conventions
1. Services are the canonical home for business logic, not views. Views should delegate, not inline.
2. Claude AI calls go through Celery tasks with tenacity retry (3 attempts, exponential backoff). Flag synchronous AI calls blocking request-response cycles.
3. WebSocket push via Channels for job status updates. Flag long-running tasks without status notification.

### Test Infrastructure Notes
1. Base classes: `AuthAPITestCase`, `StaffAPITestCase`, `SuperUserAPITestCase`, `BenefactorAPITestCase`.
2. `conftest.py` is empty — setup in base classes or per-file `setUp`. Flag new shared fixtures added to conftest.py without checking for conflicts.
3. Test settings: eager Celery, in-memory channel layer, locmem email, MD5 password hasher.
4. ~35 test files via factory_boy. Ensure new models have corresponding factories and test files.
5. No mypy config in `pyproject.toml` — type checking runs only via pre-commit hook.
