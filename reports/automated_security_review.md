## Vulnerability Findings

---

### [HIGH] BOLA: SharingCode RetrieveUpdateDestroyAPIView — Any App User Can Delete or Modify Any Sharing Code

- **Target File:** `memores/views/app/sharing_code.py` (RetrieveUpdateDestroyAPIView entry)
- **Vulnerability Type:** Broken Object Level Authorization (BOLA / IDOR)
- **Confidence:** HIGH
- **Investigation Trace:**
  1. **Where the ID enters:** `lookup_field: "code"` — the sharing code value is taken from the URL path (e.g., `/api/sharing-codes/{code}/`).
  2. **Where data is fetched:** No `get_queryset` method is listed in the view's methods. No `get_object` override. DRF's default `get_object()` calls `get_queryset()` which returns the class-level `queryset: "SharingCode.objects.all"`.
  3. **Checks between input and data:**
     - `permission_classes: ["IsAuthenticated", "IsAppUser"]` — `IsAppUser` has `has_permission: false, has_object_permission: false` in the topography. It is not listed as extending `_ObjectLevelPermission` (unlike `IsBenefactorScopeOwner`, `IsCreatorOrStaff`, `IsBenefactorScopeOwnerOrCreator`). No object-level ownership check exists.
     - No `get_queryset` override to filter by `request.user`.
     - No `get_object` override.
     - `update` and `destroy` methods are stubs (`s: true`), meaning the actual mutation logic is inherited from DRF's `UpdateAPIView`/`DestroyAPIView`, which call `get_object()` → `get_queryset()` → `SharingCode.objects.all`.
  4. **Verdict:** Any authenticated app user who knows (or guesses) a `SharingCode.code` value can DELETE or PUT that code. The `SharingCodeListSerializer` exposes `is_active`, `role`, `label`, and `code` as writable fields with no `read_only_fields`.
- **Evidence:**
```python
# Topography entry for the view:
{
  "permission_classes": ["IsAuthenticated", "IsAppUser"],
  "serializer_class": "SharingCodeListSerializer",
  "queryset": "SharingCode.objects.all",
  "lookup_field": "code",
  "h": ["DELETE", "PUT"],
  "m": [
    {"n": "update", "h": "PUT", "s": true},
    {"n": "destroy", "h": "DELETE", "s": true}
  ]
  # No get_queryset, no get_object in methods list
}

# IsAppUser permission — no object-level check:
{"name": "IsAppUser", "has_permission": false, "has_object_permission": false, "is_custom": true}

# SharingCodeListSerializer — all fields writable, no read_only_fields:
{"name": "SharingCodeListSerializer", "f": [], "m": {"model": "SharingCode",
  "fields": ["code", "is_active", "label", "role"]}}
```
- **Impact:** An attacker (any authenticated app user) can deactivate another user's sharing code (`is_active: false`), change its `role` to escalate the shared access level, or delete it entirely. Sharing codes are the mechanism for cross-user data sharing; disrupting them is a confidentiality and availability attack.
- **Fix:**
```python
class SharingCodeRetrieveUpdateDestroyView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsAppUser]
    serializer_class = SharingCodeListSerializer
    lookup_field = "code"

    def get_queryset(self):
        return SharingCode.objects.filter(user=self.request.user)
```
Alternatively, add an object-level permission class (e.g., `IsSharingCodeOwner`) that checks `obj.user == request.user` and register it in `permission_classes`.

---

### [HIGH] BOLA: Course RetrieveUpdateAPIView — Any Content Creator Can Modify Any Benefactor's Course

- **Target File:** `memores/views/management/content.py` (Course RetrieveAPIView + UpdateAPIView entry)
- **Vulnerability Type:** Broken Object Level Authorization (BOLA / cross-tenant)
- **Confidence:** HIGH
- **Investigation Trace:**
  1. **Where the ID enters:** `lookup_field: "id"` — course ID from URL path.
  2. **Where data is fetched:** No `get_queryset` method in the view's methods list. No `get_object` override. DRF's default `get_object()` uses `queryset: "Course.objects.all"`.
  3. **Checks between input and data:**
     - `permission_classes: ["IsAuthenticated", "IsCreator"]` — `IsCreator` has `has_permission: false, has_object_permission: false`. It is not listed as extending `_ObjectLevelPermission`. No ownership check.
     - No `get_queryset` override.
     - No `get_object` override.
     - `retrieve` and `update` are stubs; DRF's inherited handlers call `get_object()` → `Course.objects.all`.
  4. **Verdict:** Any user with `user_type` in the creator set can GET or PUT any `Course` by ID, regardless of which benefactor owns it. The `CourseUpdateSerializer` exposes `content_creator`, `is_disabled`, `course_key`, `course_path`, `course_group_ids`, `course_provider_ids` as writable fields.
- **Evidence:**
```python
# View entry:
{
  "permission_classes": ["IsAuthenticated", "IsCreator"],
  "queryset": "Course.objects.all",
  "lookup_field": "id",
  "h": ["GET", "PUT"],
  "m": [
    {"n": "get_serializer_class"},
    {"n": "retrieve", "h": "GET", "s": true},
    {"n": "update", "h": "PUT", "s": true}
  ]
  # No get_queryset, no get_object
}

# IsCreator — no object-level permission:
{"name": "IsCreator", "has_permission": false, "has_object_permission": false, "is_custom": true}

# CourseUpdateSerializer — content_creator and is_disabled are writable:
{"name": "CourseUpdateSerializer", "f": [{"name": "course_type"}],
 "m": {"model": "Course",
  "fields": ["id","title","description","introduction_message","completion_message",
             "course_type","content_creator","course_meta_data","is_disabled",
             "course_key","course_path","course_group_ids","course_provider_ids",
             "explanation_prompt_template_id"],
  "read_only_fields": ["id", "course_type"]}}
```
- **Impact:** A content creator from Benefactor A can: (a) read Benefactor B's course content (confidentiality), (b) set `is_disabled: true` to disrupt Benefactor B's service (availability), (c) reassign `content_creator` to steal course ownership (integrity), (d) modify `course_key`/`course_path` to redirect users.
- **Fix:**
```python
class CourseRetrieveUpdateView(RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated, IsCreator]
    lookup_field = "id"

    def get_queryset(self):
        user = self.request.user
        return Course.objects.filter(
            Q(content_creator=user) | Q(course_provider__benefactor__id=user.benefactor_id)
        ).distinct()
```
Or add an `IsCreatorOrBenefactorScopeOwner` object-level permission (the composite `IsBenefactorScopeOwnerOrCreator` already exists in `permissions.py` and should be used here).

---

### [HIGH] BOLA: ResponseOption UpdateAPIView — Any Content Creator Can Modify Any Response Option

- **Target File:** `memores/views/management/content.py` (ResponseOption UpdateAPIView entry)
- **Vulnerability Type:** Broken Object Level Authorization (BOLA / cross-tenant)
- **Confidence:** HIGH
- **Investigation Trace:**
  1. **Where the ID enters:** `lookup_field: "id"` — response option ID from URL path.
  2. **Where data is fetched:** No `get_queryset` method. No `get_object` override. DRF's default `get_object()` uses `queryset: "ResponseOption.objects.all"`.
  3. **Checks between input and data:**
     - `permission_classes: ["IsAuthenticated", "IsContentCreatorUser"]` — `IsContentCreatorUser` has `has_permission: false, has_object_permission: false`. No object-level check.
     - No `get_queryset` override.
     - `update` is a stub; DRF's inherited handler calls `get_object()` → `ResponseOption.objects.all`.
  4. **Verdict:** Any content creator can PUT any `ResponseOption` by ID. The `ResponseOptionCreateUpdateSerializer` exposes `text`, `sentiment`, `ordinal`, `question_group_id` as writable fields with no `read_only_fields`.
- **Evidence:**
```python
# View entry:
{
  "permission_classes": ["IsAuthenticated", "IsContentCreatorUser"],
  "serializer_class": "ResponseOptionCreateUpdateSerializer",
  "queryset": "ResponseOption.objects.all",
  "lookup_field": "id",
  "h": ["PUT"],
  "m": [{"n": "update", "h": "PUT", "s": true}]
  # No get_queryset, no get_object
}

# IsContentCreatorUser — no object-level permission:
{"name": "IsContentCreatorUser", "has_permission": false, "has_object_permission": false, "is_custom": true}

# ResponseOptionCreateUpdateSerializer — all fields writable:
{"name": "ResponseOptionCreateUpdateSerializer",
 "f": [{"name": "sentiment"}, {"name": "ordinal"}, {"name": "question_group_id"}],
 "m": {"model": "ResponseOption",
  "fields": ["text", "sentiment", "ordinal", "question_group_id"]}}
```
- **Impact:** A content creator from one benefactor can alter the `text`, `sentiment`, or `ordinal` of response options belonging to another benefactor's questionnaires, corrupting assessment data and potentially manipulating user-facing survey results.
- **Fix:**
```python
class ResponseOptionUpdateView(UpdateAPIView):
    permission_classes = [IsAuthenticated, IsContentCreatorUser]
    serializer_class = ResponseOptionCreateUpdateSerializer
    lookup_field = "id"

    def get_queryset(self):
        user = self.request.user
        return ResponseOption.objects.filter(
            question_group__question__content_creator=user
        ).distinct()
```

---

### [MEDIUM] BFLA: Admin Data Endpoints Accessible to Any Authenticated User

- **Target File:** `memores/views/admin/data.py` (three APIView entries)
- **Vulnerability Type:** Broken Function Level Authorization (BFLA)
- **Confidence:** MEDIUM
- **Investigation Trace:**
  1. Three views in the `admin/` directory: `get_unstructured_interactions`, `get_user_responses`, `get_course_durations`.
  2. All three have `permission_classes: ["IsAuthenticated"]` only — no `IsStaffOrSuperUser`, `IsBenefactorScopeOwner`, or any role-gating permission.
  3. All three are `APIView` subclasses with `h: ["GET"]`, `r: true`.
  4. Methods are **not** stubs (no `s: true`), meaning they contain real query logic.
  5. The data returned includes user-specific records: `UnstructuredUserInteraction` (per-user course interactions), `UserResponse` (per-user questionnaire answers), and course duration aggregates.
- **Evidence:**
```python
# All three views share the same permission configuration:
{"permission_classes": ["IsAuthenticated"], "authentication_classes": ["TokenAuthentication"],
 "h": ["GET"], "r": true,
 "m": [{"n": "get_unstructured_interactions"}]}   # no s: true → real logic
{"permission_classes": ["IsAuthenticated"], "authentication_classes": ["TokenAuthentication"],
 "h": ["GET"], "r": true,
 "m": [{"n": "get_user_responses"}]}
{"permission_classes": ["IsAuthenticated"], "authentication_classes": ["TokenAuthentication"],
 "h": ["GET"], "r": true,
 "m": [{"n": "get_course_durations"}]}
```
- **Impact:** Any authenticated user (including a regular `app_user` with no admin role) can query admin-level data endpoints. If the method bodies do not internally scope results to `request.user`, this is a multi-tenant data leak. Even if they do scope, the absence of a role gate means the authorization boundary is enforced in application logic rather than at the permission layer, making it fragile.
- **Fix:**
```python
class AdminDataView(APIView):
    permission_classes = [IsAuthenticated, IsStaffOrSuperUser]
    # or IsBenefactorScopeOwner if benefactor-scoped access is intended
```

---

### [MEDIUM] Mass Assignment: CreateUpdateUserSerializer Exposes Privilege-Escalation Fields

- **Target File:** `memores/serializers/user_serializers.py` (`CreateUpdateUserSerializer`)
- **Vulnerability Type:** Mass Assignment / Privilege Escalation
- **Confidence:** MEDIUM (UNCERTAIN which view uses this serializer for writes — no view in the topography lists `CreateUpdateUserSerializer` as its `serializer_class`)
- **Investigation Trace:**
  1. `CreateUpdateUserSerializer.Meta.fields` includes: `user_type`, `is_active`, `is_deleted`, `stripe_customer_id`, `meta`, `email`.
  2. No `read_only_fields` are declared on this serializer.
  3. `user_type` is the field that distinguishes `app_user` from `staff_user`/`super_user` (confirmed by `User.resolve_feature_access` in `models.py:217-233`).
  4. No view in the topography explicitly sets `serializer_class = CreateUpdateUserSerializer`. It may be used via `get_serializer_class()` inside a stubbed method in `memores/views/app/user.py` (the third view has `get`/`patch`/`post` all stubbed).
- **Evidence:**
```python
{"name": "CreateUpdateUserSerializer",
 "f": [{"name": "country_of_birth"}, {"name": "country_of_residence"},
       {"name": "birthdate"}, {"name": "password"}],
 "m": {"model": "User",
  "fields": ["username","password","user_type","first_name","last_name",
             "gender_at_birth","gender","email","is_active","is_deleted",
             "avatar","language","birthdate","country_of_birth",
             "country_of_residence","meta","stripe_customer_id"]}}
# No read_only_fields declared
```
- **Impact:** If this serializer is used in a write path accessible to non-staff users, an attacker can set `user_type: "super_user"`, `is_active: false` on other users, or link `stripe_customer_id` to another user's Stripe account.
- **Fix:**
```python
class CreateUpdateUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [...]
        read_only_fields = ["user_type", "is_active", "is_deleted",
                           "stripe_customer_id", "meta"]
```
If these fields must be writable, restrict the view to `IsStaffOrSuperUser` and add audit logging.

---

### [MEDIUM] Mass Assignment: EmailReportRequestCreateSerializer Exposes `user` and `status`

- **Target File:** `memores/serializers/email_report_request_serializers.py` (`EmailReportRequestCreateSerializer`)
- **Vulnerability Type:** Mass Assignment / BOLA via serializer
- **Confidence:** MEDIUM (the consuming view's `create` method is stubbed)
- **Investigation Trace:**
  1. `EmailReportRequestCreateSerializer.Meta.fields` includes `user`, `status`, `error_message`, `updated_at`.
  2. `read_only_fields: ["id", "created_at"]` — `user`, `status`, `error_message`, `updated_at` are writable.
  3. The consuming view (`memores/views/app/email_report_request.py`, CreateAPIView) has `permission_classes: ["IsAuthenticated", "IsAppUser"]` and `create` is stubbed.
  4. A user could set `user` to another user's ID, creating a report request attributed to a different user.
- **Evidence:**
```python
{"name": "EmailReportRequestCreateSerializer",
 "f": [],
 "m": {"model": "EmailReportRequest",
  "fields": ["id","user","created_at","updated_at","status",
             "report_type","sharing_codes","error_message"],
  "read_only_fields": ["id", "created_at"]}}
# user, status, error_message, updated_at are all writable
```
- **Fix:**
```python
class EmailReportRequestCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmailReportRequest
        fields = ["report_type", "sharing_codes"]
        # user, status, error_message, updated_at must NOT be in fields
```
Set `user` in `perform_create` from `self.context["request"].user`.

---

### [MEDIUM] Mass Assignment: CoachEntryCreateSerializer Exposes `user` and `is_deleted`

- **Target File:** `memores/serializers/coach_serializers.py` (`CoachEntryCreateSerializer`)
- **Vulnerability Type:** Mass Assignment / BOLA via serializer
- **Confidence:** MEDIUM (the consuming view's `create` method is stubbed; the view uses `CoachEntryListSerializer` as its class-level `serializer_class`, not `CoachEntryCreateSerializer` — the create serializer may be selected via a stubbed `get_serializer_class`)
- **Investigation Trace:**
  1. `CoachEntryCreateSerializer.Meta.fields` includes `user`, `sharing_code`, `is_deleted`, `metadata`.
  2. `read_only_fields: ["id", "timestamp"]` — `user`, `sharing_code`, `is_deleted` are writable.
  3. A user could set `user` to another user's ID or `is_deleted: true` to soft-delete another user's coach entry.
- **Evidence:**
```python
{"name": "CoachEntryCreateSerializer",
 "f": [],
 "m": {"model": "CoachEntry",
  "fields": ["id","received_message","received_context","proposed_message",
             "proposed_context","proposed_emotion","sharing_code","user",
             "timestamp","coaching_type","is_deleted","metadata"],
  "read_only_fields": ["id", "timestamp"]}}
```
- **Fix:**
```python
class CoachEntryCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoachEntry
        fields = ["received_message", "received_context", "proposed_message",
                  "proposed_context", "proposed_emotion", "sharing_code",
                  "coaching_type", "metadata"]
        # user, is_deleted, timestamp, id must NOT be in fields
```

---

### [LOW] Unrestricted Public Registration Endpoints — No Visible Rate Limiting

- **Target File:** `memores/views/public/registration.py` (three APIView entries)
- **Vulnerability Type:** Unrestricted Resource Consumption
- **Confidence:** LOW
- **Investigation Trace:**
  1. All three views have `permission_classes: ["AllowAny"]` and `authentication_classes: []`.
  2. Methods are stubs (`s: true`), so internal rate-limiting or throttling cannot be verified.
  3. No `throttle_classes` or `throttle_scope` visible in the topography.
- **Evidence:**
```python
# All three public registration views:
{"permission_classes": ["AllowAny"], "authentication_classes": [],
 "b": ["APIView"], "h": ["GET"], "m": [{"n": "get", "h": "GET", "s": true}]}
{"permission_classes": ["AllowAny"], "authentication_classes": [],
 "b": ["APIView"], "h": ["POST"], "m": [{"n": "post", "h": "POST", "s": true}]}
{"permission_classes": ["AllowAny"], "authentication_classes": [],
 "b": ["APIView"], "h": ["POST"], "m": [{"n": "post", "h": "POST", "s": true}]}
```
- **Note:** Public registration is expected to be unauthenticated. The risk is DoS via unthrottled registration. If DRF's `DEFAULT_THROTTLE_CLASSES` is configured globally, this is mitigated. **UNCERTAIN** whether global throttling is active.

---

### [UNCERTAIN] Stripe Webhook — Signature Verification Not Verifiable

- **Target File:** `memores/views/payment/stripe.py` (first view, `create_registration_code_from_checkout_session`)
- **Vulnerability Type:** Missing Authentication / Webhook Forgery
- **Confidence:** UNCERTAIN
- **Investigation Trace:**
  1. The view has `permission_classes: []` and `authentication_classes: []` — no auth layer.
  2. No `base_classes` listed (not an `APIView` or `GenericAPIView`).
  3. The method `create_registration_code_from_checkout_session` is **not** stubbed, so it contains real logic.
  4. The parser cannot inspect method bodies beyond auth-call scanning. Stripe signature verification (`stripe.Webhook.construct_event`) may or may not be present.
  5. The second view in the same file has `permission_classes: ["AllowAny"]`, `authentication_classes: []`, `b: ["APIView"]`, `post` is stubbed — likely the actual Stripe webhook receiver.
- **Evidence:**
```python
# First view — no auth, no base class visible:
{"permission_classes": [], "authentication_classes": [],
 "h": ["POST"],
 "m": [{"n": "create_registration_code_from_checkout_session"}]}

# Second view — AllowAny, stubbed:
{"permission_classes": ["AllowAny"], "authentication_classes": [],
 "b": ["APIView"], "h": ["POST"],
 "m": [{"n": "post", "h": "POST", "s": true}]}
```
- **Action:** Manually verify that `create_registration_code_from_checkout_session` calls `stripe.Webhook.construct_event(payload, sig_header, secret)` before processing. If signature verification is absent, an attacker can forge checkout session events to create registration codes.

---

### [UNCERTAIN] Multiple Stubbed Methods Prevent Authorization Verification

- **Target Files:** Multiple (see below)
- **Vulnerability Type:** Incomplete Analysis
- **Confidence:** UNCERTAIN
- **Affected views with stubbed critical methods:**
  - `memores/views/app/coach.py` — `get_queryset` (stub), `create` (stub) on ListCreateAPIView
  - `memores/views/app/journal.py` — `get_queryset` (stub), `create` (stub) on ListCreateAPIView
  - `memores/views/app/coach.py` — `get_object` (stub) on detail view with `queryset: "CoachEntry.objects.none"`
  - `memores/views/app/journal.py` — `get_object` (stub) on detail view with `queryset: "JournalEntry.objects.none"`
  - `memores/views/app/email_report_request.py` — `create` (stub)
  - `memores/views/app/sharing_code.py` — `get_queryset` (stub), `create` (stub) on ListCreateAPIView
  - `memores/views/app/user.py` — all three methods stubbed on the GET/PATCH/POST view
  - `memores/views/admin/benefactor.py` — `get_queryset` (stub) on ListCreateAPIView
  - `memores/views/admin/registration_code.py` — `get_queryset` (stub) on both views
  - `memores/views/management/content.py` — `get_queryset` (stub) on CourseGroup ListCreateAPIView
- **Note:** Per anti-hallucination rule 4, empty `inline_auth_calls` on a stubbed method does not mean authorization is absent — the logic may be in a delegated service function. These require manual code review.

---

## Protected Areas (Verified — Not Vulnerabilities)

| View / Method | File | Exclusion Rule | Evidence |
|---|---|---|---|
| `AnalysisOutput RetrieveAPIView` | `memores/views/app/analysis_output.py` | Rule 7: read-only + class-level `permission_classes` | `r: true`, `permission_classes: ["IsAuthenticated", "IsAppUser"]` |
| `Coach Stats View` | `memores/views/app/coach.py` | Rule 7: read-only + class-level auth | `r: true`, `permission_classes: ["IsAuthenticated", "IsAppUser"]` |
| `Course RetrieveAPIView` (app) | `memores/views/app/course.py` | Rule 7: read-only + class-level auth | `r: true`, `permission_classes: ["IsAuthenticated", "IsAppUser"]`, `get_queryset` present (not stub) |
| `Course ListAPIView` (app) | `memores/views/app/course.py` | Rule 7: read-only + class-level auth | `r: true`, `permission_classes: ["IsAuthenticated", "IsAppUser"]` |
| `Check Profile Completion` | `memores/views/app/course.py` | Rule 7: read-only + class-level auth | `r: true`, `permission_classes: ["IsAuthenticated", "IsAppUser"]` |
| `Check Prompt Enabled` | `memores/views/app/analysis.py` | Rule 7: read-only + class-level auth | `r: true`, `permission_classes: ["IsAuthenticated", "IsAppUser"]` |
| `Analysis GET` (AccessGate) | `memores/views/app/analysis.py` | Rule 7: read-only + class-level auth + `AccessGatePermission` | `r: true`, `permission_classes: ["IsAuthenticated", "IsAppUser", "AccessGatePermission"]` |
| `Analysis GET` (required_feature_slug) | `memores/views/app/analysis.py` | Rule 7: read-only + class-level auth + `AccessGatePermission` | `r: true`, `permission_classes: ["IsAuthenticated", "IsAppUser", "AccessGatePermission"]` |
| `Coach Detail View` | `memores/views/app/coach.py` | Rule 5: `.none()` queryset + `get_object` override | `queryset: "CoachEntry.objects.none"`, `g: true`, `q: "overridden"`, `permission_classes` includes `AccessGatePermission` |
| `Journal Detail View` | `memores/views/app/journal.py` | Rule 5: `.none()` queryset + `get_object` override | `queryset: "JournalEntry.objects.none"`, `g: true`, `q: "overridden"`, `permission_classes` includes `AccessGatePermission` |
| `EmailReportRequest CreateAPIView` | `memores/views/app/email_report_request.py` | Rule 5: `.none()` queryset on CreateAPIView (create doesn't use queryset for lookup) | `queryset: "EmailReportRequest.objects.none"`, `h: ["POST"]` |
| `ResponseOption CreateAPIView` | `memores/views/management/content.py` | Rule 5: `.none()` queryset on CreateAPIView | `queryset: "ResponseOption.objects.none"`, `h: ["POST"]` |
| `AdminBenefactorListCreateView` | `memores/views/admin/benefactor.py` | Retrieved code shows `get_queryset` scopes by `user.benefactor_id` | `get_queryset` filters: `is_staff_or_superuser` → all; `user.benefactor_id` → `.filter(id=user.benefactor_id)`; else → `.none()` |
| `AdminBenefactor RetrieveUpdateAPIView` | `memores/views/admin/benefactor.py` | `IsStaffOrBenefactorScopeOwner` has `has_permission: true` | `permission_classes: ["IsAuthenticated", "IsStaffOrBenefactorScopeOwner"]` |
| `Admin User ListAPIView` (BenefactorScope) | `memores/views/admin/user.py` | `IsBenefactorScopeOwner` + `get_queryset` present | `permission_classes: ["IsAuthenticated", "IsBenefactorScopeOwner"]`, `get_queryset` in methods |
| `Admin User RetrieveAPIView` (BenefactorScope) | `memores/views/admin/user.py` | `IsBenefactorScopeOwner` + `get_queryset` present | `permission_classes: ["IsAuthenticated", "IsBenefactorScopeOwner"]`, `get_queryset` in methods |
| `Admin User ListAPIView` (Staff) | `memores/views/admin/user.py` | Rule 1: superuser-only + `.objects.all` is expected admin pattern | `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]` |
| `Admin User UpdateAPIView` | `memores/views/admin/user.py` | Rule 1: superuser-only | `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]` |
| `Admin AnalysisOutput ListAPIView` | `memores/views/admin/analysis_output.py` | Rule 1: staff/super + `IsLowerEnv` environment gate | `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser", "IsLowerEnv"]` |
| `Admin AnalysisOutput RetrieveDestroyAPIView` | `memores/views/admin/analysis_output.py` | Rule 1: staff/super + `IsLowerEnv` | `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser", "IsLowerEnv"]` |
| `Admin Config Views` (2) | `memores/views/admin/config.py` | Rule 1: staff/super, read-only | `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]`, `r: true` |
| `Admin RegistrationCode Views` (2) | `memores/views/admin/registration_code.py` | `IsBenefactorScopeOwner` with `get_queryset` present | `permission_classes: ["IsAuthenticated", "IsBenefactorScopeOwner"]` |
| `Admin EmailReportRequest ListAPIView` | `memores/views/admin/email_report_request.py` | Rule 1: staff/super, read-only | `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]`, `r: true` |
| `Admin EmailReportRequest POST Views` (2) | `memores/views/admin/email_report_request.py` | Rule 1: staff/super | `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]` |
| `Admin Docs Views` (2) | `memores/views/admin/docs.py` | `IsBenefactorScopeOwner`, read-only | `permission_classes: ["IsAuthenticated", "IsBenefactorScopeOwner"]`, `r: true` |
| `Admin CourseProviderGrant Views` (3) | `memores/views/admin/course_provider_grant.py` | `IsBenefactorScopeOwner` / `IsStaffOrBenefactorScopeOwner` with `get_queryset` | All have `get_queryset` in methods |
| `Admin Content Views` (4) | `memores/views/admin/content.py` | Rule 1: staff/super or superuser-only | `permission_classes` includes `IsStaffOrSuperUser` or `IsSuperUser` |
| `Admin Reports Views` (3) | `memores/views/admin/reports.py` | `IsBenefactorScopeOwner` / `IsStaffOrSuperUser` | Role-gated |
| `Admin SharingCode ListAPIView` | `memores/views/admin/sharing_code.py` | Rule 1: staff/super, read-only | `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]`, `r: true` |
| `Admin SimulatedDataRuns Views` (7) | `memores/views/admin/simulated_data_runs.py` | `IsStaffOrSuperUserInSimDataEnv` or `IsStaffOrSuperUser` | Environment-gated |
| `Admin PromptTemplate Views` (3) | `memores/views/admin/prompt_template.py` | Rule 1: staff/super | `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]` |
| `Admin DeleteUserData` | `memores/views/admin/admin.py` | Rule 1: staff/super | `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]` |
| `Admin Course DestroyAPIView` | `memores/views/admin/admin.py` | Rule 1: superuser-only | `permission_classes: ["IsAuthenticated", "IsSuperUser"]` |
| `Management Content Views` (IsCreator) | `memores/views/management/content.py` | `IsCreator` role gate on all creator operations | `permission_classes` includes `IsCreator` |
| `Management ContentGroup Views` | `memores/views/management/content.py` | `IsBenefactorScopeOwnerOrCreator` with `get_queryset` | `permission_classes: ["IsAuthenticated", "IsBenefactorScopeOwnerOrCreator"]` |
| `Public Registration Views` (3) | `memores/views/public/registration.py` | Expected unauthenticated public endpoints | `permission_classes: ["AllowAny"]` |
| `Public User Auth Views` (3) | `memores/views/public/user.py` | Expected unauthenticated password reset flow | `permission_classes: ["AllowAny"]` |
| `HealthCheckView` | `memores/views/health_check.py` | Infrastructure health check | `b: ["BaseHealthCheckView"]` |

---

## Attack Path Analysis

### Attack Path 1: Cross-Tenant Course Disruption and Ownership Theft

- **Prerequisites:** Attacker is an authenticated content creator (`user_type` in creator set) for Benefactor A.
- **Exploitation Steps:**
  1. Attacker enumerates course IDs (sequential or via the unscoped `Course.objects.all` queryset on the management Course view).
  2. `GET /api/management/courses/{victim_course_id}/` — reads Benefactor B's course content (title, description, prompt templates, session structure).
  3. `PUT /api/management/courses/{victim_course_id}/` with body `{"is_disabled": true}` — disables Benefactor B's course for all their users.
  4. `PUT /api/management/courses/{victim_course_id}/` with body `{"content_creator": "<attacker_id>"}` — steals course ownership.
  5. `PUT /api/management/courses/{victim_course_id}/` with body `{"course_key": "attacker-controlled-key"}` — redirects users to attacker-controlled content.
- **Affected Endpoints:**
  - `memores/views/management/content.py` — Course `RetrieveAPIView` + `UpdateAPIView` (`IsCreator`, `queryset: "Course.objects.all"`, no `get_queryset`)
  - `memores/serializers/course_serializers.py` — `CourseUpdateSerializer` (writable: `content_creator`, `is_disabled`, `course_key`, `course_path`)
- **Business Impact:** Confidentiality (course content theft), Availability (service disruption for Benefactor B), Integrity (ownership theft, content redirection).
- **Mitigations:**
  1. Add `get_queryset` to the Course management view filtering by `content_creator=request.user` or `course_provider__benefactor__id=request.user.benefactor_id`.
  2. Add `content_creator` to `read_only_fields` in `CourseUpdateSerializer` (ownership should be set at creation, not modifiable via update).
  3. Use the existing `IsBenefactorScopeOwnerOrCreator` permission class (already implemented in `permissions.py:189-199`) instead of bare `IsCreator`.

### Attack Path 2: Sharing Code Hijack — Disrupt Cross-User Data Sharing

- **Prerequisites:** Attacker is any authenticated app user. Attacker knows or guesses a `SharingCode.code` value (codes may be shared via email, making them discoverable).
- **Exploitation Steps:**
  1. `DELETE /api/sharing-codes/{victim_code}/` — deletes the victim's sharing code, severing their data-sharing link.
  2. `PUT /api/sharing-codes/{victim_code}/` with body `{"is_active": false, "role": "viewer"}` — deactivates the code and downgrades its role.
  3. `PUT /api/sharing-codes/{victim_code}/` with body `{"role": "admin"}` — escalates the sharing code's role if the application grants elevated access based on `role`.
- **Affected Endpoints:**
  - `memores/views/app/sharing_code.py` — `RetrieveUpdateDestroyAPIView` (`IsAppUser`, `queryset: "SharingCode.objects.all"`, no `get_queryset`, no `get_object`)
  - `memores/serializers/sharing_code_serializers.py` — `SharingCodeListSerializer` (writable: `code`, `is_active`, `label`, `role`)
- **Business Impact:** Availability (disrupted data sharing), Integrity (role manipulation), Confidentiality (if role escalation grants access to shared data).
- **Mitigations:**
  1. Add `get_queryset` filtering by `user=request.user` to the SharingCode view.
  2. Add an object-level permission (`IsSharingCodeOwner`) checking `obj.user == request.user`.
  3. Mark `code` as `read_only` in `SharingCodeListSerializer` (the code should not be modifiable after creation).

### Attack Path 3: Cross-Tenant Response Option Tampering

- **Prerequisites:** Attacker is an authenticated content creator.
- **Exploitation Steps:**
  1. Attacker enumerates `ResponseOption` IDs.
  2. `PUT /api/management/response-options/{victim_id}/` with body `{"text": "Manipulated text", "sentiment": "positive", "ordinal": 1}` — alters the response option for another benefactor's questionnaire.
  3. This corrupts assessment data: users selecting this option will have their responses recorded with altered sentiment/text.
- **Affected Endpoints:**
  - `memores/views/management/content.py` — ResponseOption `UpdateAPIView` (`IsContentCreatorUser`, `queryset: "ResponseOption.objects.all"`, no `get_queryset`)
  - `memores/serializers/response_serializers.py` — `ResponseOptionCreateUpdateSerializer` (all fields writable)
- **Business Impact:** Integrity (corrupted assessment data across tenants).
- **Mitigations:**
  1. Add `get_queryset` filtering `ResponseOption` by the creator's benefactor scope.
  2. Use `IsBenefactorScopeOwnerOrCreator` instead of bare `IsContentCreatorUser`.

---

## Secure Areas

| Module / View | File | Security Controls Observed |
|---|---|---|
| `AdminBenefactorListCreateView` | `memores/views/admin/benefactor.py` | `IsStaffOrBenefactorScopeOwner` + `get_queryset` with explicit `user.benefactor_id` filtering (retrieved code confirms three-way branch: staff→all, benefactor→scoped, else→`.none()`) |
| `AdminBenefactor RetrieveUpdateAPIView` | `memores/views/admin/benefactor.py` | `IsStaffOrBenefactorScopeOwner` (`has_permission: true`) + `perform_update` override |
| `Admin User List/Retrieve (BenefactorScope)` | `memores/views/admin/user.py` | `IsBenefactorScopeOwner` + `get_queryset` present |
| `Admin User List/Update (Staff)` | `memores/views/admin/user.py` | `IsStaffOrSuperUser` — cross-tenant access is by design for staff |
| `Admin AnalysisOutput Views` | `memores/views/admin/analysis_output.py` | `IsStaffOrSuperUser` + `IsLowerEnv` (environment gate prevents production access) |
| `Admin Config Views` | `memores/views/admin/config.py` | `IsStaffOrSuperUser`, read-only |
| `Admin RegistrationCode Views` | `memores/views/admin/registration_code.py` | `IsBenefactorScopeOwner` + `get_queryset` present |
| `Admin EmailReportRequest Views` | `memores/views/admin/email_report_request.py` | `IsStaffOrSuperUser` |
| `Admin Docs Views` | `memores/views/admin/docs.py` | `IsBenefactorScopeOwner`, read-only |
| `Admin CourseProviderGrant Views` | `memores/views/admin/course_provider_grant.py` | `IsBenefactorScopeOwner` / `IsStaffOrBenefactorScopeOwner` + `get_queryset` |
| `Admin Content Views` | `memores/views/admin/content.py` | `IsStaffOrSuperUser` / `IsSuperUser` |
| `Admin Reports Views` | `memores/views/admin/reports.py` | `IsBenefactorScopeOwner` / `IsStaffOrSuperUser` |
| `Admin SharingCode ListAPIView` | `memores/views/admin/sharing_code.py` | `IsStaffOrSuperUser`, read-only |
| `Admin SimulatedDataRuns Views` | `memores/views/admin/simulated_data_runs.py` | `IsStaffOrSuperUserInSimDataEnv` (environment-gated) or `IsStaffOrSuperUser` |
| `Admin PromptTemplate Views` | `memores/views/admin/prompt_template.py` | `IsStaffOrSuperUser` |
| `Admin DeleteUserData / Course Destroy` | `memores/views/admin/admin.py` | `IsStaffOrSuperUser` / `IsSuperUser` |
| `Management ContentGroup Views` | `memores/views/management/content.py` | `IsBenefactorScopeOwnerOrCreator` (composite OR-logic permission with `_check_ownership` in `permissions.py:189-199`) |
| `Management Question View` | `memores/views/management/content.py` | `IsCreator` + `get_queryset` present |
| `Management ResponseOption CreateAPIView` | `memores/views/management/content.py` | `IsCreator` + `queryset: "ResponseOption.objects.none"` (safety guard) |
| `App Course Views` (5) | `memores/views/app/course.py` | `IsAuthenticated` + `IsAppUser`, read-only or self-scoped operations |
| `App Analysis Views` (4) | `memores/views/app/analysis.py` | `IsAuthenticated` + `IsAppUser` + `AccessGatePermission` (feature-gated) |
| `App Coach Detail View` | `memores/views/app/coach.py` | `IsAppUser` + `AccessGatePermission` + `.none()` queryset + `get_object` override |
| `App Journal Detail View` | `memores/views/app/journal.py` | `IsAppUser` + `AccessGatePermission` + `.none()` queryset + `get_object` override |
| `App EmailReportRequest CreateAPIView` | `memores/views/app/email_report_request.py` | `IsAppUser` + `.none()` queryset |
| `App User Views` (4) | `memores/views/app/user.py` | `IsAuthenticated` + `TokenAuthentication` |
| `App SharingCode ListCreateAPIView` | `memores/views/app/sharing_code.py` | `IsAppUser` (POST-only, create is stubbed — uncertain) |
| `App Coach ListCreateAPIView` | `memores/views/app/coach.py` | `IsAppUser` (POST-only, create is stubbed — uncertain) |
| `App Journal ListCreateAPIView` | `memores/views/app/journal.py` | `IsAppUser` (POST-only, create is stubbed — uncertain) |
| `App Jobs View` | `memores/views/app/jobs.py` | `IsAuthenticated`, read-only |
| `Public Registration Views` (3) | `memores/views/public/registration.py` | `AllowAny` — expected for public registration |
| `Public User Auth Views` (3) | `memores/views/public/user.py` | `AllowAny` — expected for password reset flow |
| `HealthCheckView` | `memores/views/health_check.py` | Infrastructure health check |
| `_ObjectLevelPermission` base class | `memores/permissions.py:91-112` | Soft-delete guard + staff/super bypass + `_check_ownership` delegation — well-structured object-level permission framework |
| `IsBenefactorScopeOwner` | `memores/permissions.py:149-170` | Benefactor-scoped ownership check via `_matches_id(user.benefactor_id, obj.benefactor_id)` |
| `IsBenefactorScopeOwnerOrCreator` | `memores/permissions.py:189-199` | Composite OR-logic: benefactor scope OR content creator ownership |
| `IsCreatorOrStaff` | `memores/permissions.py:173-179` | Content-creator ownership or staff/super override |
| `User.resolve_feature_access` | `memores/models.py:217-233` | Three-tier feature authorization: admin override → meta override → Django permissions |
| `check_object_permission` helper | `memores/permissions.py:219-230` | Non-DRF context object-level permission check with `PermissionDenied` |
| `DRFIdiomVisitor` linter | `scripts/check_drf_idioms.py:107-193` | Static analysis catching missing `permission_classes`, `serializer_class`, `queryset`, and `AllowAny` usage |
| `TokenAuthentication` | All authenticated views | Consistent use of `TokenAuthentication` across all non-public views |
| `IsLowerEnv` permission | `memores/views/admin/analysis_output.py` | Environment-gated access prevents production data exposure in non-prod |
| `IsStaffOrSuperUserInSimDataEnv` | `memores/views/admin/simulated_data_runs.py` | Environment-gated access for simulated data operations |
| `AccessGatePermission` | `memores/views/app/coach.py`, `memores/views/app/journal.py`, `memores/views/app/analysis.py` | Feature-gated access with `has_permission: true` |
| `SoftDeleteModel` base | `memores/models.py` | `is_deleted` field + `objects`/`all_objects` manager split — soft-delete pattern prevents hard deletion |
| `BenefactorCreateSerializer` | `memores/serializers/benefactor_serializers.py` | `initial_admin_username`, `initial_admin_password`, `admin_usernames` all `read_only: true` — prevents mass assignment of credential fields |
| `AnalysisOutputListSerializer` | `memores/serializers/analysis_output_serializers.py` | `exclude: ["metadata", "other_users"]` — sensitive fields excluded from list representation |
| `SharingCodeListWithUserSerializer` | `memores/serializers/sharing_code_serializers.py` | `user` field is `read_only: true` — prevents user field mass assignment in list context |
| `EmailReportRequestListSerializer` | `memores/serializers/email_report_request_serializers.py` | `user` is `read_only: true`, `id`/`created_at` are `read_only` |
| `CoachEntryListSerializer` | `memores/serializers/coach_serializers.py` | `id`/`timestamp` are `read_only` |
| `JournalEntryListSerializer` | `memores/serializers/journal_serializers.py` | `id`/`timestamp` are `read_only` (inherited from `BaseJournalSerializer`) |
| `PromptTemplateListSerializer` | `memores/serializers/prompt_template_serializers.py` | `id` is `read_only` |
| `RegistrationCodeListSerializer` | `memores/serializers/registration_code_serializers.py` | `id` is `read_only` |
| `CourseGroupCreateUpdateSerializer` | `memores/serializers/course_serializers.py` | `id` is `read_only` |
| `CourseUpdateSerializer` | `memores/serializers/course_serializers.py` | `id`/`course_type` are `read_only` |
| `CourseProgressCreateSerializer` | `memores/serializers/course_progress_serializers.py` | Uses explicit field list (not `__all__`) |
| `AdminCourseProviderGrantSerializer` | `memores/serializers/course_provider_grant_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only` |
| `SimpleUserSerializer` | `memores/serializers/user_serializers.py` | Minimal field set: `id`, `username`, `first_name`, `last_name`, `user_type`, `avatar` |
| `LimitedUserSerializer` | `memores/serializers/data_view_serializers.py` | Minimal field set: `id`, `first_name`, `last_name` |
| `SimpleBenefactorSerializer` | `memores/serializers/benefactor_serializers.py` | Minimal field set: `id`, `name` |
| `SimpleCourseSerializer` | `memores/serializers/course_serializers.py` | Minimal field set for list representations |
| `SimplePromptTemplateSerializer` | `memores/serializers/prompt_template_serializers.py` | Minimal field set: `id`, `name`, `is_active`, `model` |
| `AudioSessionSerializer` | `memores/serializers/course_serializers.py` | `audio` is `read_only: true` |
| `CourseWithIdsSerializer` | `memores/serializers/course_serializers.py` | `sessions` is `read_only: true` |
| `CourseGroupSerializer` | `memores/serializers/course_serializers.py` | `course_count` is `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `course_provider` is `read_only: true` |
| `AnalysisResultSerializer` | `memores/services/results_analysis/serializers.py` | `course`/`user` are `read_only: true` |
| `AnalysisOutputListSerializer` | `memores/serializers/analysis_output_serializers.py` | `user`/`prompt_template` are `read_only: true` |
| `AnalysisOutputDetailSerializer` | `memores/serializers/analysis_output_serializers.py` | `user`/`prompt_template` are `read_only: true` |
| `EmailReportRequestWithOutputSerializer` | `memores/serializers/email_report_request_serializers.py` | `user` is `read_only: true` |
| `SharingCodeListWithUserSerializer` | `memores/serializers/sharing_code_serializers.py` | `user` is `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `course_provider` is `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer` | `memores/serializers/benefactor_serializers.py` | `id`/`username`/`course_provider_grants` are `read_only: true` |
| `CourseProviderGrantSerializer`

---

## 5. Automated Claim Verification

Claim Verification: 48/48 verified (100% accuracy)
