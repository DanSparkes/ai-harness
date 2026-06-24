## Vulnerability Findings

### [HIGH] - Broken Object-Level Authorization on Sharing Code Update
- **Target File:** `/Users/dansparkes/memores/memores-api/memores/views/app/sharing_code.py`
- **Vulnerability Type:** BOLA / IDOR
- **Confidence:** HIGH
- **Investigation Trace:**
  1. Where the ID enters the system: URL path via `lookup_field: "code"` on a `RetrieveUpdateDestroyAPIView`.
  2. Where data is fetched: Class-level `queryset: "SharingCode.objects.all"`. `get_queryset` is NOT overridden in the methods list.
  3. Checks between input and data: The `update` (PUT) method entry shows empty `inline_auth_calls` and no `delegated_auth_calls`. `queryset_auth_chain` is `"unknown"`. DRF's default `get_object()` will query the unscoped class-level queryset, returning any `SharingCode` matching the provided `code`. No ownership or tenant filter exists.
  4. Verdict: Any authenticated user can PUT to `/sharing_codes/{code}/` and modify another user's sharing code (e.g., change `role`, `is_active`, or `label`).
- **Evidence:**
```python
# View class_attributes
"queryset": "SharingCode.objects.all",
"lookup_field": "code",

# Method entry for update
{"n":"update","h":"PUT"}  # No inline_auth_calls, no delegated_auth_calls
"q": "unknown"
```

### [MEDIUM/UNCERTAIN] - Unscoped Admin List Querysets (Analysis Output & Journal)
- **Target File:** `/Users/dansparkes/memores/memores-api/memores/views/admin/analysis_output.py` & `/Users/dansparkes/memores/memores-api/memores/views/admin/journal.py`
- **Vulnerability Type:** Unscoped Data Retrieval / Potential Information Disclosure
- **Confidence:** MEDIUM (UNCERTAIN per calibration rule 17)
- **Investigation Trace:**
  1. Where the ID enters the system: N/A (List endpoints).
  2. Where data is fetched: `get_queryset` method exists but has empty `inline_auth_calls`. Class-level `queryset` is not explicitly set, defaulting to DRF's model manager or inherited state.
  3. Checks between input and data: The `list` handler has `authorize_staff_or_superuser`, but the underlying queryset scoping is invisible to static analysis. If unscoped, these endpoints return all admin records regardless of staff role boundaries.
  4. Verdict: Cannot confirm scoping. Flagged as UNCERTAIN per rule 17. Requires manual verification of delegated service functions or middleware context.
- **Evidence:**
```python
# admin/analysis_output.py
{"n":"get_queryset"}  # Empty inline_auth_calls
"q": "unknown" (implied)

# admin/journal.py
{"n":"get_queryset"}  # Empty inline_auth_calls
"q": "unknown" (implied)
```

## Protected Areas (Verified — Not Vulnerabilities)
- **`/Users/dansparkes/memores/memores-api/memores/views/app/analysis_output.py` (`RetrieveAPIView`):** `queryset_auth_chain: "scoped"`. `get_queryset` contains `authorize_app_user`. Authorization cascades to all single-object operations via DRF's inherited `get_object()`.
- **`/Users/dansparkes/memores/memores-api/memores/views/app/coach.py` (`ListCreateAPIView`):** `queryset_auth_chain: "scoped"`. `get_queryset` and `create` both contain `authorize_app_user`. Strict app-user scoping enforced.
- **`/Users/dansparkes/memores/memores-api/memores/views/app/journal.py` (`ListCreateAPIView`):** `queryset_auth_chain: "scoped"`. `get_queryset` and `create` both contain `authorize_app_user`. Strict app-user scoping enforced.
- **`/Users/dansparkes/memores/memores-api/memores/views/app/sharing_code.py` (`RetrieveUpdateDestroyAPIView` DELETE path):** `destroy` method contains `inline_auth_calls: ["authorize_app_user"]`. Object-level ownership verified before mutation.
- **`/Users/dansparkes/memores/memores-api/memores/views/admin/benefactor.py` (`RetrieveUpdateAPIView`):** `get` and `update` methods contain `authorize_benefactor_scope` and `authorize_benefactor` respectively. Benefactor-scoped queries enforced.
- **`/Users/dansparkes/memores/memores-api/memores/views/management/content.py` (`RetrieveUpdateDestroyAPIView` for Question):** `queryset_auth_chain: "scoped"`. `get_queryset` contains `authorize_creator`. Ownership filtering cascades to `update` and `destroy`.
- **`/Users/dansparkes/memores/memores-api/memores/views/app/email_report_request.py` (`CreateAPIView`):** Class-level `queryset: "EmailReportRequest.objects.none"` intentionally breaks DRF's default lookup chain. `create` method contains `authorize_app_user`. Safe pattern.
- **`/Users/dansparkes/memores/memores-api/memores/views/admin/admin.py` (`delete_user_data`):** Explicit `permission_classes: ["IsAuthenticated", "IsStaffOrSuperUser"]`. Staff-only destructive operation.

## Attack Path Analysis
**Path 1: Privilege Escalation via Sharing Code Manipulation**
- **Prerequisites:** Valid authentication token (any registered user). No special privileges required.
- **Exploitation Steps:**
  1. Attacker enumerates or guesses a valid `code` value for another user's sharing code (or obtains it via logs/headers).
  2. Attacker sends `PUT /sharing_codes/{victim_code}/` with payload `{"role": "admin", "is_active": true}`.
  3. Due to missing object-level authorization on the `update` handler, the request succeeds without verifying the authenticated user owns the code.
  4. Attacker now controls a sharing code linked to another user's account, potentially bypassing access gates or triggering unauthorized data exports/reports.
- **Affected Endpoints:** `/Users/dansparkes/memores/memores-api/memores/views/app/sharing_code.py` (`RetrieveUpdateDestroyAPIView` PUT method)
- **Business Impact:** Integrity & Confidentiality compromised. Enables cross-tenant/user privilege escalation and unauthorized data sharing without audit trails or ownership validation.
- **Mitigations:** Override `get_object()` to filter by `request.user`, or add `has_object_permission` enforcement. Example:
```python
def get_object(self):
    obj = super().get_object()
    if obj.user != self.request.user:
        raise PermissionDenied("Cannot update another user's sharing code.")
    return obj
```

**No Evidence-Based Attack Paths Found:**
- Public endpoints (`payment/stripe.py`, `public/registration.py`, `public/user.py`) correctly use `AllowAny` for their intended public-facing flows. No evidence of sensitive data leakage or unvalidated webhook signature requirements in the provided topography.
- All admin destructive operations (`destroy` on `UserCourseCompletion`, `CourseProgress`, `Question`, `AnalysisOutput`) explicitly require `authorize_superuser` or `authorize_staff_or_superuser`.
- Serializer mass assignment is mitigated by explicit `fields` lists and `read_only_fields` on all write-capable serializers. `__all__` usage is confined to read-only or staff-gated detail views.

## Secure Areas
- **`/Users/dansparkes/memores/memores-api/memores/views/app/analysis_output.py`:** Scoped queryset + app user auth.
- **`/Users/dansparkes/memores/memores-api/memores/views/app/coach.py`:** Scoped queryset + app user auth on list/create.
- **`/Users/dansparkes/memores/memores-api/memores/views/app/journal.py`:** Scoped queryset + app user auth on list/create.
- **`/Users/dansparkes/memores/memores-api/memores/views/admin/benefactor.py`:** Benefactor-scoped queries + staff/creator authorization helpers on retrieve/update/list.
- **`/Users/dansparkes/memores/memores-api/memores/views/management/content.py`:** Creator/staff authorization helpers (`authorize_creator`, `authorize_content_owner_or_staff`, `authorize_staff_or_superuser`) explicitly applied to create, update, and destroy handlers. Queryset scoping enforced where applicable.
- **`/Users/dansparkes/memores/memores-api/memores/views/admin/prompt_template.py`:** Staff-only authorization (`authorize_staff_or_superuser`) on create/update handlers. Explicit field lists prevent mass assignment.
- **`/Users/dansparkes/memores/memores-api/memores/views/admin/user.py`:** Staff/superuser authorization (`authorize_staff_or_superuser`) on profile update and retrieval. Explicit `read_only_fields` applied to serializers.
- **`/Users/dansparkes/memores/memores-api/memores/serializers/`:** All write-capable serializers use explicit `fields` lists rather than `__all__`. `read_only_fields` correctly applied to prevent mass assignment of sensitive identifiers (`id`, `user_id`, `timestamp`). No writable nested serializers detected that could bypass field-level controls.
- **`/Users/dansparkes/memores/memores-api/memores/views/app/email_report_request.py`:** Intentional `.none()` queryset pattern combined with explicit `authorize_app_user` on create prevents unauthorized data access or injection via default DRF lookup chains.
