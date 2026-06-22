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
- Base classes: `AuthAPITestCase`, `StaffAPITestCase`, `SuperUserAPITestCase`, `BenefactorAPITestCase`
- Coverage: Views (admin: 11 files, app: 9, management: 1, public: 3), Services (12 files)
- Config: `test_settings.py` — eager Celery, in-memory channel layer, locmem email, MD5 password hasher

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

## Notable Observations for Reviewers

| Issue | Detail |
|-------|--------|
| MCP server not implemented | `AGENTS.md` describes `memores/mcp.py` with SSE — file doesn't exist |
| Constants duplication | `UserTypes` enum in both `constants/constants.py` and `views/constants.py` |
| Thick registration view | `RegistrationCompleteView.post()` is ~130 lines — violates thin-views pattern |
| Mixed auth patterns | Views mix procedural `authorize_*()` calls with DRF `permission_classes` |
| Unmanaged model | `CourseProviderMap` has `managed = False` — table must exist independently |
| conftest.py empty | No shared fixtures, all setup in base classes or per-file setUp methods |
| Migration gaps | Numbering jumps (0001 → 0002 → 0006 → 0008 → 0011) suggest squashes |
| Active refactoring | Multiple `# TODO` and legacy-route comments in coach/journal views |
| Django forms + DRF hybrid | Registration uses both `RegistrationForm` (Django) and DRF serializers |
| No mypy config in pyproject.toml | Type checking runs via pre-commit hook only |
