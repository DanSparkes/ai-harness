## Vulnerability Findings

### [MEDIUM] - Broken Object Level Authorization (BOLA) on Course Update Endpoint
- **Target File:** `/Users/dansparkes/memores/memores-api/memores/views/management/content.py`
- **Vulnerability Type:** BOLA / Unscoped Queryset Retrieval
- **Evidence:** 
```python
  # class_attributes in CourseUpdateView
  "queryset": "Course.objects.all",
  "serializer_class": "CourseUpdateSerializer",
  
  # methods in CourseUpdateView
  {
    "name": "update",
    "is_stub": false,
    "inline_auth_calls": ["authorize_content_owner_or_staff", "authorize_creator"],
    ...
  }
```
- **Analysis:** `CourseUpdateView` relies on a class-level `queryset: Course.objects.all` and does not override `get_queryset()`. DRF's `UpdateAPIView` invokes `self.get_object()` (which queries the unscoped class attribute) *before* executing the `update()` method. Inline authorization in `update()` occurs post-retrieval, allowing an authenticated attacker to PUT any course ID without tenant/user isolation checks during object resolution.
- **Mitigation:** Override `get_queryset()` or `get_object()` to filter by `content_creator` or tenant ID before retrieval. Move object-level authorization to the class level using DRF's `HasObjectPermission`.

### [MEDIUM] - Broken Object Level Authorization (BOLA) on Response Option Update
- **Target File:** `/Users/dansparkes/memores/memores-api/memores/views/management/content.py`
- **Vulnerability Type:** BOLA / Missing Object Permission Check
- **Evidence:** 
```python
  # class_attributes in UpdateResponseOptionView
  "permission_classes": ["IsAuthenticated", "IsContentCreatorUser"],
  "queryset": "ResponseOption.objects.all",
  
  # permission_class_analysis for IsContentCreatorUser
  {
    "name": "IsContentCreatorUser",
    "has_object_permission": false,
    ...
  },
  
  # methods in UpdateResponseOptionView
  {
    "name": "update",
    "inline_auth_calls": [],
    ...
  }
```
- **Analysis:** The `IsContentCreatorUser` permission class explicitly lacks object-level checks (`has_object_permission: false`). Combined with an unscoped class-level queryset and zero inline auth on the `update()` method, this endpoint will accept PUT requests for any `ResponseOption` ID belonging to other tenants/users.
- **Mitigation:** Implement `has_object_permission` in `IsContentCreatorUser` to verify ownership, or scope the queryset via a custom manager/filter before DRF processes the request.

### [LOW] - Mass Assignment Surface on CourseUpdateSerializer
- **Target File:** `/Users/dansparkes/memores/memores-api/memores/serializers/course_serializers.py`
- **Vulnerability Type:** Mass Assignment / Unvalidated Data Mapping
- **Evidence:** 
```python
  # Meta.fields in CourseUpdateSerializer
  "fields": [
    "id", "title", "description", "introduction_message", "completion_message",
    "course_type", "course_meta_data", "is_disabled", "course_key", "course_path",
    "course_group_ids", "course_provider_ids", "explanation_prompt_template_id"
  ],
  "read_only_fields": ["id", "course_type"]
```
- **Analysis:** While DRF enforces explicit field whitelisting, the writable surface includes highly sensitive configuration fields (`is_disabled`, `course_key`, `course_path`). If combined with a BOLA flaw (e.g., Finding 1), these fields could be abused to disable courses or alter routing keys. The serializer relies on implicit validation rather than explicit type/format constraints for path/key fields.
- **Mitigation:** Add explicit `validators` to `course_key` and `course_path` fields. Document writable fields in API specifications. Ensure server-side uniqueness/format checks are enforced at the model level.

### [LOW] - Potential Information Disclosure on Course Retrieve Endpoint
- **Target File:** `/Users/dansparkes/memores/memores-api/memores/views/app/course.py`
- **Vulnerability Type:** BOLA / Unscoped Object Retrieval
- **Evidence:** 
```python
  # class_attributes in CourseRetrieveView
  "serializer_class": "CourseFullListSerializer",
  "lookup_field": "id"
  
  # methods in CourseRetrieveView
  {
    "name": "get_queryset",
    "is_stub": false,
    "inline_auth_calls": [],
    ...
  }
```
- **Analysis:** `CourseRetrieveView` lists a `get_queryset` method with zero inline auth calls. If the underlying implementation defaults to `.all()` or lacks tenant filtering, `retrieve()` will expose course data outside the authenticated user's scope. Class-level `IsAuthenticated` only validates identity, not object ownership.
- **Mitigation:** Verify `get_queryset` implementation explicitly filters by tenant/user. Add DRF's `IsAuthenticated` + custom object permission if cross-tenant retrieval is unintended.

---

## Attack Path Analysis

### Path 1: Unauthorized Course Configuration Modification via BOLA Chain
- **Prerequisites:** Valid authenticated user token with overlapping role privileges (e.g., staff or creator role that passes `authorize_content_owner_or_staff`).
- **Exploitation Steps:** 
  1. Attacker enumerates or obtains a target course UUID outside their tenant.
  2. Sends `PUT /courses/{target_id}/` to `CourseUpdateView`.
  3. DRF's `get_object()` uses the unscoped class-level `queryset: Course.objects.all`, bypassing tenant isolation during retrieval.
  4. The `update()` method executes inline auth (`authorize_content_owner_or_staff`). If the attacker holds a staff role or the authorization function has a logic gap, it passes.
  5. Attacker modifies `is_disabled` to `true` or alters `course_key`/`course_path`, disrupting service availability or routing for legitimate users.
- **Affected Endpoints:** 
  - `/Users/dansparkes/memores/memores-api/memores/views/management/content.py::CourseUpdateView`
  - `/Users/dansparkes/memores/memores-api/memores/serializers/course_serializers.py::CourseUpdateSerializer`
- **Business Impact:** Integrity & Availability compromise. Unauthorized configuration changes break course delivery pipelines and disable content for targeted tenants.
- **Mitigations:** Scope `get_object()` to tenant/user before retrieval. Enforce object-level permissions at the DRF permission class level rather than method-level inline calls.

### Path 2: Cross-Tenant Response Option Tampering
- **Prerequisites:** Valid authenticated user token with `IsContentCreatorUser` class-level pass.
- **Exploitation Steps:** 
  1. Attacker identifies a `ResponseOption` UUID belonging to another tenant.
  2. Sends `PUT /response-options/{target_id}/` to `UpdateResponseOptionView`.
  3. Class-level permission passes (user is a creator), but lacks object checks. Queryset returns the target object unscoped.
  4. `update()` executes with no inline auth, directly serializing and saving attacker-supplied data to the foreign tenant's object.
- **Affected Endpoints:** `/Users/dansparkes/memores/memores-api/memores/views/management/content.py::UpdateResponseOptionView`
- **Business Impact:** Integrity compromise. Cross-tenant data pollution, potential prompt injection via response text fields, or corruption of assessment logic.
- **Mitigations:** Implement `has_object_permission` in `IsContentCreatorUser`. Scope queryset to `creator_id=request.user.id`.

---

## Secure Areas

- **Admin & Privileged Views (`admin/content.py`, `admin/admin.py`):** Views like `AdminUserCompletedCourseDestroyView`, `CourseDestroyView`, and `AdminUserCourseProgressCreateView` use `.objects.all()` querysets paired with `authorize_superuser` or `authorize_staff_or_superuser` inline auth. Per calibration rules, superuser/staff admin patterns with cross-tenant access are expected DRF behavior. Auth gates mutation correctly via `perform_destroy`/`destroy` method calls.
- **Public & Authentication Endpoints (`public/registration.py`, `public/user.py`, `payment/stripe.py`):** `LoginView`, `RegistrationWaitlistView`, and `StripeCheckoutSession` correctly use `AllowAny`. `StripeCheckoutSession` is a checkout session *creation* endpoint (outbound API call), not a webhook receiver, making public access appropriate per Rule 13.
- **Serializer Definitions:** All serializers explicitly define `Meta.fields`, preventing implicit mass assignment. Nested serializers like `SimpleProfileSerializer(read_only=True)` and `AudioSerializer(read_only=True)` are correctly constrained. No `fields = '__all__'` patterns detected.
- **Multi-Tenancy Scoping in Management Views (`management/content.py`):** `CourseGroupListCreateView` correctly scopes queryset via `authorize_benefactor_or_creator` and gates creation with `authorize_staff_or_superuser`. `QuestionRetrieveUpdateDestroyView` scopes retrieval via `get_queryset` with `authorize_creator`, mitigating BOLA on read paths.
- **Audit & Compliance Readiness:** Destructive admin endpoints (`AdminUserCompletedCourseDestroyView`, `AdminUserCourseProgressDestroyView`) explicitly call `authorize_superuser`, satisfying SOC 2 requirements for privileged action gating. No hardcoded secrets or credentials detected in model/serializer/view topography.