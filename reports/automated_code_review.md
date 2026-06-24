# Staff Code Review Report

## 1. Overall Architectural Verdict
**REQUEST CHANGES**
This PR introduces a global pagination default change that drastically reduces payload sizes (50 → 10) and alters query parameter names (`page_size` → `pageSize`) across all ~80+ endpoints without corresponding frontend updates or explicit opt-outs. While adding server-side search to the admin user list is beneficial, the broad blast radius of these pagination defaults poses a high risk of breaking the admin dashboard's data fetching patterns and increasing network latency.

## 2. Blast Radius & Coupling Assessment
- **Global Pagination Shift:** Changing `DEFAULT_PAGINATION_CLASS` and `PAGE_SIZE` in `settings.py` impacts every `ListAPIView` across `app`, `admin`, and `management` domains. The reduction from 50 to 10 items per page will multiply API calls for list-heavy admin pages (e.g., Benefactors, Courses, Prompts), increasing client-side rendering load and network overhead.
- **Query Param Naming:** Introducing `pageSize` as the query parameter deviates from DRF's standard `page_size`. If the React Admin dashboard uses TanStack Query with default DRF adapters, it will likely send `page_size`, causing pagination to silently fail or return empty pages.
- **Admin User View:** Removing `AdminProfilePagination` and relying on the new global default is acceptable, but the search filter addition (`meta__registration_code`) relies on PostgreSQL JSONB lookups which are untested in this diff and lack database indexing.

## 3. Line-by-Line Code Critiques

- **File:** `memores/settings.py` — Lines 113-115
- **Issue Category:** Blast Radius / Performance
- **The Defect:**
  ```python
      "DEFAULT_PAGINATION_CLASS": "memores.views.common.DefaultPagination",
      "PAGE_SIZE": 10,
  }
  ```
  Reducing the global `PAGE_SIZE` from 50 to 10 is a severe behavioral change. It forces all downstream list endpoints (courses, benefactors, analysis outputs, etc.) to paginate at a much smaller granularity without explicit justification or frontend coordination. This will likely degrade admin dashboard performance due to increased pagination requests.
- **Remediation:** Keep the global default at `50` for backward compatibility. Apply the new pagination class and size explicitly only where needed (e.g., on `AdminProfileListView`), or document this as a coordinated breaking change requiring frontend updates. If keeping it global, ensure the admin dashboard's React Query client is updated to handle the increased page count efficiently.

- **File:** `memores/views/common.py` — Lines 98-101
- **Issue Category:** Integration Risk / API Contract
- **The Defect:**
  ```python
  class DefaultPagination(PageNumberPagination):
      page_size = 10
      page_size_query_param = "pageSize"
      max_page_size = 1000
  ```
  Renaming the query parameter to `pageSize` breaks standard DRF contract expectations. The admin dashboard (React + TanStack Query) typically expects `page_size`. If the client sends `page_size`, this custom class will ignore it, defaulting to page 1 and returning only 10 results regardless of client intent.
- **Remediation:** Align with DRF standards or explicitly document the contract change. Support both safely by overriding `get_page_size`:
  ```python
  class DefaultPagination(PageNumberPagination):
      page_size = 10
      max_page_size = 1000

      def get_page_size(self, request):
          if request.query_params.get("pageSize"):
              return int(request.query_params["pageSize"])
          return super().get_page_size(request)
  ```

- **File:** `memores/views/admin/user.py` — Lines 28-36
- **Issue Category:** Maintainability / Database Performance
- **The Defect:**
  ```python
      filter_backends = [filters.SearchFilter]
      search_fields = [
          "=id",
          "first_name",
          "user__username",
          "benefactor__name",
          "meta__registration_code",
      ]
  ```
  Searching across `meta__registration_code` (a JSONField on `Profile`) will trigger PostgreSQL `jsonb` lookups. Without a GIN index on `Profile.meta`, this search will perform sequentially on every request, causing significant latency spikes as the user base grows. Combining exact ID search (`=id`) with broad text/JSON searches in a single `SearchFilter` backend can also lead to unpredictable query planning.
- **Remediation:** Add a database index for the JSON field lookup via migration (e.g., `models.Index(fields=['meta'])` or a functional GIN index). Alternatively, split the search logic into `filters.DjangoFilterBackend` with explicit lookups to give Django's ORM better control over query optimization.

## 4. Test Coverage Assessment
- **Missing Test Files:** The diff introduces a new global pagination class and modifies search behavior on `AdminProfileListView`. There are no accompanying tests verifying the new pagination defaults or search filter functionality.
- **Weak Assertions Risk:** If tests are added, avoid asserting only `response.status_code == 200`. Tests must verify that `len(response.data['results'])` respects the new page size (10), and that search queries actually filter the queryset correctly.
- **Untested Edge Cases:**
  - JSON field lookup (`meta__registration_code`) should be tested against known `Profile` fixtures to ensure the ORM translates it correctly and doesn't raise `FieldError`.
  - Pagination boundary conditions when `pageSize` is provided vs. when standard `page_size` is used.
- **Recommendation:** Add a test case using `StaffAPITestCase` that verifies pagination boundaries, explicitly tests the JSON field search against known `Profile` fixtures, and confirms that both `pageSize` and `page_size` query params are handled gracefully if the remediation above is applied.
