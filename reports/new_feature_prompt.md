# ADR 0001 Implementation Plan: Replace User/Profile Split with Custom User Model

## Overview

This plan merges the `Profile` model fields into a custom `User(AbstractUser)` model, eliminating the current User/Profile split architecture. **No data is deleted until explicit verification is complete.**

---

## 1. Codebase Target Map

### Core Files to Modify

| File | Change Type | Description |
|------|-------------|-------------|
| `memores/settings.py` | MODIFY | Add `AUTH_USER_MODEL = "memores.User"` |
| `memores/models.py` | MODIFY | Define new `User(AbstractUser)` model with Profile fields, remove Profile model |
| `memores/admin.py` | MODIFY | Register User model in admin |
| `memores/apps.py` | MODIFY | Update AppConfig if needed |
| `memores/signals.py` | MODIFY | Update signal handlers for user model |

### Model Files with ForeignKey Dependencies (ALL 17 must be updated)

| File | Fields to Update |
|------|------------------|
| `memores/models.py` | CourseProviderGrant.user, Question.content_creator, Audio.content_creator, Course.content_creator, CourseGroup.content_creator, BenefactorCohortMember.user, UserResponse.user, UserAudioCompletion.user, UserCourseCompletion.user, CourseProgress.user, SharingCode.user, CoachEntry.user, JournalEntry.user, EmailReportRequest.user, AnalysisOutput.user, PromptSummary.user, UnstructuredUserInteraction.user |

### Serializer Files (ALL must be updated)

| File | Serializers to Update |
|------|----------------------|
| `memores/serializers/user_serializers.py` | All serializers (UserSerializer, ProfileSerializer, etc.) |
| `memores/serializers/course_provider_grant_serializers.py` | AdminCourseProviderGrantSerializer |
| `memores/serializers/journal_serializers.py` | JournalEntryCreateSerializer |
| `memores/serializers/email_report_request_serializers.py` | EmailReportRequestListSerializer, EmailReportRequestWithOutputSerializer, EmailReportRequestCreateSerializer |
| `memores/serializers/sharing_code_serializers.py` | SharingCodeSerializer, SharingCodeListWithUserSerializer |
| `memores/serializers/coach_serializers.py` | CoachEntryCreateSerializer |
| `memores/serializers/user_course_completion_serializers.py` | UserCourseCompletionSerializer, UserCourseCompletionCreateUpdateSerializer |
| `memores/serializers/analysis_result_serializers.py` | UserAnalysisResultSerializer, AnalysisResultSerializer |
| `memores/serializers/data_view_serializers.py` | LimitedUserSerializer |
| `memores/serializers/response_serializers.py` | UserResponseSerializer |
| `memores/serializers/admin_serializers.py` | ContentCreatorUserSerializer |
| `memores/services/results_analysis/serializers.py` | ProfileSerializer, AnalysisResultSerializer |

### View Files (ALL must be updated)

| File | Views to Update |
|------|-----------------|
| `memores/views/app/user.py` | UserView, LogoutView |
| `memores/views/admin/user.py` | AdminProfileListView, AdminProfileRetrieveView |
| `memores/views/admin/benefactor.py` | AdminBenefactorUsersListView |
| `memores/views/admin/content.py` | AdminUserCompletedCoursesListView, AdminUserCourseProgressListView |
| `memores/views/admin/analysis_results.py` | AdminUserAnalysisResultListView, AdminUserAnalysisResultRetrieveView |
| `memores/views/public/user.py` | LoginView, update_password, forgot_password |
| `memores/views/app/course.py` | CourseRetrieveView, CourseListView |

### Service Files (ALL must be updated)

| File | Services to Update |
|------|-------------------|
| `memores/services/user_helper.py` | All functions |
| `memores/services/common.py` | Functions using user queries |
| `memores/services/course_access_service.py` | Access validation logic |
| `memores/services/profile_permission_service.py` | Permission checks |

### Celery Task Files (ALL must be updated)

| File | Tasks to Update |
|------|-----------------|
| `memores/jobs/claude_ai_job.py` | claude_ai_job, perform_claude_ai_job |
| `memores/jobs/coaching_job.py` | coaching_job |
| `memores/jobs/detailed_report_job.py` | detailed_report_job |
| `memores/jobs/journal_jobs.py` | journal_basic_job, journal_advanced_job |
| `memores/jobs/personality_report_job.py` | personality_report_job |
| `memores/jobs/result_explanation_job.py` | result_explanation_job |

### Test Files (ALL must be updated)

| File | Description |
|------|-------------|
| `memores/tests/factories.py` | Update UserFactory, remove ProfileFactory |
| `memores/tests/views/base.py` | AuthAPITestCase base classes |
| `memores/tests/conftest.py` | Any shared fixtures |

### URL Files (Verify/Update)

| File | Description |
|------|-------------|
| `memores/urls/admin.py` | Verify user-related URLs |
| `memores/urls/app.py` | Verify user-related URLs |
| `memores/urls/public.py` | Verify auth URLs |

---

## 2. Architecture & Design

### Current Database State

- **auth_user table**: Integer primary key (id), contains: password, last_login, is_superuser, username, first_name, last_name, email, is_staff, is_active, date_joined
- **profile table**: UUID primary key (id), contains: user (OneToOne to auth_user), benefactor, first_name, last_name, gender_at_birth, gender, user_type, email, is_active, is_deleted, avatar, language, birthdate, country_of_birth, country_of_residence, meta, password_reset_code, stripe_customer_id
- **All 17 ForeignKey references**: Reference Profile (UUID), not auth_user (integer)

### Approach Overview

The implementation follows a **safe migration strategy** that preserves data and allows verification before any destructive operations:

1. **Phase 1**: Define new User model with UUID primary key (additive only)
2. **Phase 2**: Migrate data from both auth_user and Profile to new User table
3. **Phase 3**: Update all foreign keys to reference new User model
4. **Phase 4**: Update all application code (serializers, views, services, tasks)
5. **Phase 5**: Data verification and validation
6. **Phase 6**: Remove Profile and auth_user tables (ONLY after verification passes)

### Key Design Decisions

#### 1. Custom User Model Structure

```python
# memores/models.py
import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django_countries.fields import CountryField

from memores.constants.model_choice_fields import (
    GENDERS_AT_BIRTH,
    USER_TYPES,
)
from memores.constants.profile_permissions import profile_permissions


class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class User(AbstractUser):
    """Custom user model replacing the split Profile/User architecture."""

    class Meta:
        db_table = "memores_user"  # New table with UUID primary key
        permissions = profile_permissions()  # Migrate from Profile

    # UUID primary key (replacing auth_user's integer PK)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Override first_name/last_name with Profile's max_length
    first_name = models.CharField(max_length=128, blank=True)
    last_name = models.CharField(max_length=128, blank=True)

    # Fields migrated from Profile
    benefactor = models.ForeignKey(
        "Benefactor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users"
    )
    gender_at_birth = models.CharField(
        max_length=32, choices=GENDERS_AT_BIRTH, null=True, blank=True
    )
    gender = models.CharField(max_length=32, null=True, blank=True)
    user_type = models.CharField(
        max_length=32, choices=USER_TYPES, null=True, blank=True
    )
    avatar = models.CharField(max_length=1024, null=True, blank=True)
    language = models.CharField(max_length=8, default="en-us")
    birthdate = models.DateTimeField(null=True, blank=True)  # Keep DateTimeField to match Profile
    country_of_birth = CountryField(null=True, blank=True)
    country_of_residence = CountryField(null=True, blank=True)
    meta = models.JSONField(default=dict)
    password_reset_code = models.CharField(max_length=16, null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=256, blank=True, null=True)

    # Soft delete pattern (migrated from Profile)
    is_deleted = models.BooleanField(default=False)

    # Custom managers for soft delete
    objects = SoftDeleteManager()
    all_objects = models.Manager()

    def __str__(self):
        return self.username

    def resolve_feature_access(self, codename: str) -> bool:
        """
        Unified evaluation engine for feature authorization.
        Checks criteria in order of precedence:
        1. Administrative Role Overrides (Staff/Superusers)
        2. Top-level JSON 'meta' Field Permission Overrides
        3. Database-backed Django Permissions
        """
        if self.user_type in ("staff_user", "super_user"):
            return True

        # 2. Look up the exact permission slug directly inside the meta JSON
        permission_slug = codename.split(".")[-1]

        if isinstance(self.meta, dict) and self.meta.get(permission_slug) is True:
            return True

        return self.has_perm(codename)
```

#### 2. Settings Configuration

```python
# memores/settings.py
AUTH_USER_MODEL = "memores.User"
```

#### 3. Data Migration Strategy

**Critical**: The new User model uses UUID primary keys (from Profile), not integer primary keys (from auth_user). The data migration must:

1. For each Profile record:
   - Create a new User row with Profile.id as the UUID primary key
   - Copy authentication data from the linked auth_user row (password, last_login, is_superuser, username, is_staff, is_active, date_joined)
   - Copy all profile fields from the Profile row
2. Handle orphan cases:
   - auth_user rows with no Profile: Create User with default profile values
   - Profile rows with no auth_user: Create User with unusable password
   - Profile rows with is_deleted=True: Preserve is_deleted flag

#### 4. ForeignKey Migration Pattern

**Before:**
```python
class CourseProviderGrant(models.Model):
    user = models.ForeignKey(Profile, on_delete=models.CASCADE)
```

**After:**
```python
from django.conf import settings

class CourseProviderGrant(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="course_provider_grants"
    )
```

#### 5. Import Pattern Standardization

**Before:**
```python
from django.contrib.auth.models import User
from memores.models import Profile

user = User.objects.get(id=user_id)
profile = Profile.objects.get(user=request.user)
```

**After:**
```python
from django.contrib.auth import get_user_model

User = get_user_model()
user = User.objects.get(id=user_id)
# Direct access - no more profile lookup
user = request.user  # Already the User model instance
```

#### 6. Serializer Updates

**Before:**
```python
class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ['id', 'user', 'username', ...]
```

**After:**
```python
class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User  # Now references the custom User model
        fields = ['id', 'username', 'first_name', ...]
```

---

## 3. Risk Assessment & Mitigations

### Critical Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Primary Key Migration** | CRITICAL | New User table uses UUID PKs from Profile. Data migration must correctly map auth_user integer IDs to Profile UUIDs. Test extensively with sample data. |
| **Database Migration Complexity** | HIGH | Use additive migrations only until verification. Never delete tables until all code is updated and tests pass. |
| **Foreign Key Constraint Failures** | HIGH | Update all 17 models with user FKs atomically. Use `related_name` consistently. Run migrations in dependency order. |
| **Authentication System Breakage** | CRITICAL | Django's auth system tightly couples to AUTH_USER_MODEL. Test login/logout, password reset, and permission checks extensively. |
| **Signal Handler Failures** | MEDIUM | Review all signals in `memores/signals.py` that reference Profile. Update receivers to work with User model. |
| **Test Suite Failures** | HIGH | Update all 35+ test files. Fix factories, assertions, and query patterns. Run full suite after each phase. |

### Backwards Compatibility Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Existing User Data Loss** | CRITICAL | Write data migration to copy both auth_user and Profile data into new User model. Verify field mappings carefully. Keep original tables intact until verification. |
| **API Response Format Changes** | MEDIUM | Maintain serializer field names where possible. Use `source` parameter to map old field names to new structure. |
| **Third-party Integration Breakage** | LOW | If any external systems reference user IDs, ensure ID continuity (use Profile UUIDs as new User PKs). |

### Performance Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **N+1 Query Issues** | MEDIUM | Add `select_related()` to all queries that previously joined with User and Profile separately. Now they're the same table. |
| **Migration Downtime** | HIGH | Use online migration techniques for large tables. Monitor lock times during FK changes. |

### Security Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Permission System Breakage** | CRITICAL | Django's permission system uses AUTH_USER_MODEL. Migrate `profile_permissions()` to User.Meta.permissions. Test all permission checks. |
| **Password Hash Migration** | MEDIUM | Password hashes are copied from auth_user to new User table. Verify password validation still works. |
| **Feature Access Gating Breakage** | HIGH | Move `resolve_feature_access()` method to User model. Test feature gating extensively. |

---

## 4. Implementation Pipeline

### Phase 1: Define New User Model (Additive Only)

```json
{
  "feature_name": "adr_0001_user_model_migration",
  "target_workspace": "/Users/dansparkes/memores/memores-api",
  "pipeline": [
    {
      "step": 1,
      "name": "Update Django Settings",
      "target_file": "memores/settings.py",
      "task": "Add AUTH_USER_MODEL = 'memores.User' to the settings file. Place it near other authentication-related settings like AUTH_PASSWORD_VALIDATORS. This setting must be present before any model imports occur.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 2
    },
    {
      "step": 2,
      "name": "Define New User Model in models.py",
      "target_file": "memores/models.py",
      "task": "Import AbstractUser from django.contrib.auth.models. Define a new User class that extends AbstractUser with db_table = 'memores_user' in Meta. CRITICAL: Add id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False) as the primary key. Add all fields migrated from Profile: benefactor (ForeignKey to Benefactor with SET_NULL), gender_at_birth, gender, user_type, avatar, language, birthdate (DateTimeField to match Profile), country_of_birth (CountryField), country_of_residence (CountryField), meta (JSONField), password_reset_code, stripe_customer_id, is_deleted (BooleanField default=False). Override first_name and last_name with max_length=128. Add custom permissions from profile_permissions(). Add resolve_feature_access() method. Ensure the default 'objects' manager uses Django's default UserManager (or a custom subclass inheriting from UserManager) to preserve create_user/create_superuser capability, and assign your custom SoftDeleteManager to a secondary manager property like 'active_objects'. Keep the existing Profile model definition but mark it as deprecated with a comment. Ensure both models are exported from the module.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax", "run_mypy"],
      "max_attempts": 3
    },
    {
      "step": 3,
      "name": "Configure Custom UserAdmin",
      "target_file": "memores/admin.py",
      "task": "Import the new User model and custom UserAdmin from django.contrib.auth.admin. Register the custom User model. Override the 'fieldsets' and 'add_fieldsets' properties on UserAdmin to cleanly expose the newly migrated custom fields (benefactor, user_type, stripe_customer_id, etc.) within the Django Admin dashboard UI without exposing raw password hashes.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 2
    },
    {
      "step": 4,
      "name": "Generate Initial Migration for User Model",
      "target_file": null,
      "task": "Run the Django migration command to create initial migrations for the memores app. This will generate migration files in memores/migrations/ directory that define the new User table (memores_user) and preserve the existing Profile and auth_user tables temporarily.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["run_command"],
      "max_attempts": 2
    }
  ]
}
```

### Phase 2: Data Migration (Copy auth_user + Profile → User)

```json
{
  "pipeline": [
    {
      "step": 5,
      "name": "Create Data Migration to Copy auth_user and Profile to User",
      "target_file": "memores/migrations/0002_copy_profile_to_user.py",
      "task": "Create a data migration using migrations.RunPython. The forward function must: (1) Fetch all Profile rows, (2) For each Profile, look up the linked auth_user row via Profile.user_id, (3) Create a new User row in memores_user with Profile.id as the UUID primary key, (4) Copy authentication fields from auth_user (password, last_login, is_superuser, username, is_staff, is_active, date_joined), (5) Copy all profile fields from Profile (benefactor, first_name, last_name, gender_at_birth, gender, user_type, email, avatar, language, birthdate, country_of_birth, country_of_residence, meta, password_reset_code, stripe_customer_id, is_deleted). Handle orphan cases: (A) auth_user rows with no Profile get default profile values, (B) Profile rows with no auth_user get an unusable password and placeholder username. The migration must be fully idempotent (safe to run multiple times).",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "write_file", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 6,
      "name": "Verify Data Migration Integrity",
      "target_file": null,
      "task": "Run the data migration and verify that all data has been correctly copied to User fields. Run verification queries: count Profile objects, count auth_user objects, count User objects, compare specific field values between Profile/auth_user and User for a sample of records. Log any discrepancies. Do NOT proceed to next step until verification passes.",
      "assigned_agent": "QA_Tester",
      "auditor_agent": "Engineer",
      "allowed_skills": ["run_command"],
      "max_attempts": 3
    }
  ]
}
```

### Phase 3: Update Foreign Keys (Profile → User)

```json
{
  "pipeline": [
    {
      "step": 7,
      "name": "Update All ForeignKey References to Use AUTH_USER_MODEL",
      "target_file": "memores/models.py",
      "task": "In memores/models.py, import settings from django.conf. Update all ForeignKey and OneToOneField definitions that reference Profile to use settings.AUTH_USER_MODEL instead. Specifically update these 17 fields: CourseProviderGrant.user, Question.content_creator, Audio.content_creator, Course.content_creator, CourseGroup.content_creator, BenefactorCohortMember.user, UserResponse.user, UserAudioCompletion.user, UserCourseCompletion.user, CourseProgress.user, SharingCode.user, CoachEntry.user, JournalEntry.user, EmailReportRequest.user, AnalysisOutput.user, PromptSummary.user, UnstructuredUserInteraction.user. Add appropriate related_name attributes to each.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 8,
      "name": "Generate Migration for FK Changes",
      "target_file": null,
      "task": "Run the Django migration command to create a new migration that updates all foreign key references from Profile to settings.AUTH_USER_MODEL across all 17 affected tables. CRITICAL: If running this migration on databases with existing history, include an operation to truncate the 'django_admin_log' table first, as PostgreSQL cannot implicitly cast historic integer user IDs to UUIDs within system log tables.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["run_command"],
      "max_attempts": 2
    }
  ]
}
```

### Phase 4: Update Application Code

```json
{
  "pipeline": [
    {
      "step": 9,
      "name": "Update All Model Imports to Use get_user_model()",
      "target_file": "memores/models.py",
      "task": "In memores/models.py and all other model files, replace 'from django.contrib.auth.models import User' with 'from django.contrib.auth import get_user_model'. Update all runtime calls to use get_user_model() instead of the explicit User class. This ensures lazy evaluation and prevents circular imports.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 3
    },
    {
      "step": 10,
      "name": "Update All Serializer Files to Reference New User Model",
      "target_file": "memores/serializers/user_serializers.py",
      "task": "In memores/serializers/user_serializers.py, update all serializers to reference the new User model instead of Profile. Update imports to use get_user_model(). Modify ProfileSerializer to serialize User fields directly. Update CreateUpdateProfileSerializer to accept user data in the request. Ensure all field references match the new User model structure.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 11,
      "name": "Update All Other Serializer Files",
      "target_file": "memores/serializers/course_provider_grant_serializers.py",
      "task": "Systematically update all remaining serializer files to use the new User model. Update these files: memores/serializers/course_provider_grant_serializers.py, memores/serializers/journal_serializers.py, memores/serializers/email_report_request_serializers.py, memores/serializers/sharing_code_serializers.py, memores/serializers/coach_serializers.py, memores/serializers/user_course_completion_serializers.py, memores/serializers/analysis_result_serializers.py, memores/serializers/data_view_serializers.py, memores/serializers/response_serializers.py, memores/serializers/admin_serializers.py, and memores/services/results_analysis/serializers.py. Replace all model references from Profile to User, update field names where necessary.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 5
    },
    {
      "step": 12,
      "name": "Update All View Files to Use New User Model",
      "target_file": "memores/views/app/user.py",
      "task": "Update all view files to work with the new User model. Start with memores/views/app/user.py and update UserView.get(), .patch(), .post() methods, LogoutView.post(). Then systematically update: memores/views/admin/user.py (AdminProfileListView, AdminProfileRetrieveView), memores/views/admin/benefactor.py (AdminBenefactorUsersListView), memores/views/admin/content.py (AdminUserCompletedCoursesListView, AdminUserCourseProgressListView), memores/views/admin/analysis_results.py (AdminUserAnalysisResultListView, AdminUserAnalysisResultRetrieveView), memores/views/public/user.py (LoginView.post(), update_password(), forgot_password()), and memores/views/app/course.py (CourseRetrieveView.get_queryset(), CourseListView.get_queryset()). Replace all Profile.objects queries with User.objects queries.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 5
    },
    {
      "step": 13,
      "name": "Update All Service Files to Use New User Model",
      "target_file": "memores/services/user_helper.py",
      "task": "Update all service files to use the new User model. Start with memores/services/user_helper.py and update all functions that query or manipulate user data. Then systematically update: memores/services/common.py, memores/services/course_access_service.py, memores/services/profile_permission_service.py. Replace all Profile.objects queries with User.objects queries. Update any signal receivers or helper functions that reference the old model structure.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 14,
      "name": "Update All Celery Task Files to Use New User Model",
      "target_file": "memores/jobs/claude_ai_job.py",
      "task": "Update all Celery task files to use the new User model. Start with memores/jobs/claude_ai_job.py and update both perform_claude_ai_job() function and claude_ai_job @shared_task decorated function. Then systematically update: memores/jobs/coaching_job.py (coaching_job), memores/jobs/detailed_report_job.py (detailed_report_job), memores/jobs/journal_jobs.py (journal_basic_job, journal_advanced_job), memores/jobs/personality_report_job.py (personality_report_job), and memores/jobs/result_explanation_job.py (result_explanation_job). Replace all Profile.objects queries with User.objects queries.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 4
    },
    {
      "step": 15,
      "name": "Update Test Factories",
      "target_file": "memores/tests/factories.py",
      "task": "Update memores/tests/factories.py to work with the new User model. Remove ProfileFactory entirely. Update UserFactory to include all fields that were previously on Profile (gender_at_birth, gender, user_type, benefactor, avatar, language, birthdate, country_of_birth, country_of_residence, meta, password_reset_code, stripe_customer_id, is_deleted). Update CourseProviderGrantFactory and other factories that used ProfileFactory as SubFactory to use UserFactory instead.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 3
    },
    {
      "step": 16,
      "name": "Update Test Base Classes and Fixtures",
      "target_file": "memores/tests/views/base.py",
      "task": "Update test infrastructure files to use the new User model. Update memores/tests/views/base.py (AuthAPITestCase, StaffAPITestCase, SuperUserAPITestCase, BenefactorAPITestCase) to work with the new authentication system. Check memores/tests/conftest.py for any shared fixtures that reference Profile and update them accordingly.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 3
    },
    {
      "step": 17,
      "name": "Update Signal Handlers",
      "target_file": "memores/signals.py",
      "task": "Review and update memores/signals.py to work with the new User model. Update any signal receivers that listen for Profile signals to instead listen for User signals (pre_save, post_save, etc.). Ensure signal handlers properly access user data from the new model structure.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 3
    },

  ]
}
```

### Phase 5: Testing and Verification

```json
{
  "pipeline": [
    {
      "step": 18,
      "name": "Run Full Test Suite Verification with Migrations",
      "target_file": null,
      "task": "Execute the test suite forcing full migration execution to verify that the migration chain from scratch is structurally sound. Run: docker compose exec api sh -c 'DJANGO_SETTINGS_MODULE=memores.test_settings pytest --migrations'. Fix any relational integrity, type mismatches, or constraint errors caused by the custom migrations.",
      "assigned_agent": "QA_Tester",
      "auditor_agent": "Engineer",
      "allowed_skills": ["run_command"],
      "max_attempts": 5
    },
    {
      "step": 19,
      "name": "Run Type Checking and Linting",
      "target_file": null,
      "task": "Run mypy type checking and pre-commit hooks to ensure code quality. Run: docker compose exec api sh -c 'mypy memores/' followed by running the full pre-commit hook suite. Fix any type errors or linting violations introduced during the migration.",
      "assigned_agent": "QA_Tester",
      "auditor_agent": "Engineer",
      "allowed_skills": ["run_command"],
      "max_attempts": 4
    },
    {
      "step": 20,
      "name": "Verify Authentication Flow",
      "target_file": null,
      "task": "Manually verify the complete authentication flow works correctly: user registration, login, logout, password reset, and permission checks. Test both staff/superuser and regular user access patterns. Verify that request.user now returns the User model instance directly (not Profile).",
      "assigned_agent": "QA_Tester",
      "auditor_agent": "Engineer",
      "allowed_skills": ["run_command"],
      "max_attempts": 3
    },
    {
      "step": 21,
      "name": "Verify Feature Access Gating",
      "target_file": null,
      "task": "Verify that the resolve_feature_access() method works correctly on the new User model. Test all three access paths: staff/superuser override, meta JSON permissions, and database permissions. Ensure feature gating is not broken.",
      "assigned_agent": "QA_Tester",
      "auditor_agent": "Engineer",
      "allowed_skills": ["run_command"],
      "max_attempts": 3
    },
    {
      "step": 22,
      "name": "Verify Data Integrity",
      "target_file": null,
      "task": "Run comprehensive data verification queries to ensure all Profile data has been correctly migrated to User fields. Check: record counts match, all foreign keys are valid, no null values in required fields, meta JSON fields are intact, country fields are valid. Generate a verification report.",
      "assigned_agent": "QA_Tester",
      "auditor_agent": "Engineer",
      "allowed_skills": ["run_command"],
      "max_attempts": 3
    }
  ]
}
```

### Phase 6: Cleanup (ONLY after verification passes)

```json
{
  "pipeline": [
    {
      "step": 23,
      "name": "Remove Profile Model from Codebase",
      "target_file": "memores/models.py",
      "task": "After ALL verification steps pass (18-22), remove the Profile model definition from memores/models.py. Remove the import of profile_permissions from Profile's Meta class (it's now on User). Remove any Profile-related helper functions or methods. Update any remaining references to Profile in the codebase.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["read_file", "edit_file", "validate_syntax"],
      "max_attempts": 3
    },
    {
      "step": 24,
      "name": "Generate Migration to Remove Legacy Tables",
      "target_file": null,
      "task": "Run the Django migration command or write a manual schema migration that drops both the legacy 'profile' table AND the legacy 'auth_user' table. This step must verify that constraints have been completely severed from them in Phase 3.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["run_command"],
      "max_attempts": 2
    },
    {
      "step": 25,
      "name": "Final Test Suite Run",
      "target_file": null,
      "task": "Run the full test suite one final time after table removal to ensure no regressions. Run: docker compose exec api sh -c 'DJANGO_SETTINGS_MODULE=memores.test_settings pytest --numprocesses=auto'",
      "assigned_agent": "QA_Tester",
      "auditor_agent": "Engineer",
      "allowed_skills": ["run_command"],
      "max_attempts": 3
    }
  ]
}
```

---

## 5. Pipeline Execution Notes

### Dependency Ordering
Steps are ordered to ensure earlier changes (settings, models) are in place before dependent code (serializers, views, services) is updated.

### Migration Strategy
The pipeline uses a safe, incremental approach:
- **Phase 1 (Steps 1-4)**: Define new model (additive only, no destructive changes)
- **Phase 2 (Steps 5-6)**: Copy data from auth_user and Profile to new User table (original tables untouched)
- **Phase 3 (Steps 7-8)**: Update foreign keys to reference User (original tables still exist)
- **Phase 4 (Steps 9-17)**: Update application code (all three models coexist)
- **Phase 5 (Steps 18-22)**: Comprehensive testing and verification
- **Phase 6 (Steps 23-25)**: Remove Profile and auth_user tables ONLY after verification passes

### No Deletes Until Verification
The Profile and auth_user tables are preserved until Step 24, which only executes after Steps 18-22 pass completely. This ensures:
- Data can be verified at any point
- Rollback is possible until the final phase
- No data loss occurs accidentally

### Verification Gates
Steps 18-22 serve as verification gates:
- Step 18: Full test suite passes with migrations
- Step 19: Type checking and linting pass
- Step 20: Authentication flow works
- Step 21: Feature access gating works
- Step 22: Data integrity verified

### Retry Safety
No Celery tasks have `autoretry_for` added because several tasks modify model state in error handlers. Adding autoretry would cause permanent failures on retry.

---

## 6. Rollback Plan

If any verification step fails:

1. **Phase 1-3 Failure**: Revert the migration files and model changes. Original tables are still intact.
2. **Phase 4 Failure**: Fix the application code issues. Original tables are still intact.
3. **Phase 5 Failure**: Fix the failing verification. Original tables are still intact.
4. **Phase 6 Failure**: This should not happen if Phase 5 passes. If it does, restore from backup.

The key safety mechanism is that the Profile and auth_user tables are never deleted until all verification passes, allowing for easy rollback at any point before Step 24.
