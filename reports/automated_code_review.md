# Staff Code Review Report

## 1. Overall Architectural Verdict

**REQUEST CHANGES**

The prompt-preview subsystem is well-structured: a dedicated constants module, a persona-seeding service, a preview service, a serializer with validation, and two admin API views with proper `IsStaffOrSuperUser` gating. The `results_analysis` edits (adding typed substitution keys with legacy aliases, wrapping `gettext` proxies in `str()`) are a clean backward-compatibility improvement. However, the PR **removes an existing setup step** (`ensure_report_baseline_schedules`) from `scripts/setup.sh`, introduces a **race condition** on a shared `PromptTemplate` row in the E2E path, and **silently swallows seeding failures** in the persona service. These must be fixed before merge.

## 2. Blast Radius & Coupling Assessment

| Changed Module | Downstream Impact | Risk |
|---|---|---|
| `memores/constants/prompt_preview.py` | Transitively imported by nearly every module (per Pass 1 import-graph). A syntax error or circular import here crashes the entire app at boot. The file imports only from `memores.constants.constants` and `memores.utils.prompt_helpers`, both of which are leaf-level — **no circular risk**. | LOW |
| `memores/services/prompt_preview_personas.py` | Consumed by the new `PromptPreviewView` and the seeding command. Creates `User`, `UserResponse`, `AnalysisResult`, `Course`, `Question`, `ResponseGroup`, `ResponseOption`, `AnalysableProfileQuestion` rows. A bug here pollutes the DB with synthetic data that can leak into production analytics. | MEDIUM |
| `memores/services/prompt_preview_service.py` | Imports `_resolve_model_name` (private) from `memores.external.claude_api` and `llm_job` from `memores.jobs.llm_job`. Couples the preview service to the production LLM pipeline. | MEDIUM |
| `memores/services/results_analysis/*.py` (7 files) | Invoked by `claude_ai_job` / `detailed_report_job` post-analysis. The `str(_("…"))` wrapping and new substitution keys are additive and backward-compatible. | LOW |
| `memores/serializers/prompt_preview_serializers.py` | Consumed only by `PromptPreviewView`. No upstream impact. | LOW |
| `memores/urls/admin.py` | Two new routes under the admin namespace. No existing routes modified. | LOW |
| `memores/utils/prompt_helpers/substitute.py` | Adds `SOCIAL_STYLES` to the substitution registry. Any template using `{{social_styles}}` will now resolve. The f-string change to the log line is cosmetic. | LOW |
| `scripts/setup.sh` | **Removes** `ensure_report_baseline_schedules` from the dev setup pipeline. All new dev environments will lack report baseline schedules. | **HIGH** |

## 3. Line-by-Line Code Critiques

### File: `scripts/setup.sh`

- **Issue Category:** Regression / Infrastructure
- **The Defect:** The diff **removes** an existing setup step and replaces it with the new seeder:
  ```diff
  -python3 manage.py ensure_report_baseline_schedules
  +python3 manage.py seed_prompt_preview_personas
  ```
  This means every new developer or CI environment that runs `scripts/setup.sh` will no longer have report baseline schedules created. This is a silent regression that will surface as missing cron-like schedules in dev/staging.
- **Remediation:** Add the new command *without* removing the existing one:
  ```diff
   python3 manage.py migrate
  +python3 manage.py ensure_report_baseline_schedules
   python3 manage.py loaddata memores/fixtures/dev_data.yaml
  +python3 manage.py seed_prompt_preview_personas
  ```

---

### File: `memores/services/prompt_preview_personas.py`

- **Line:** `+    except (ValueError, TypeError, KeyError) as e:` (in `_create_responses_for_course`)
- **Issue Category:** Defensive Engineering / Silent Failure
- **The Defect:**
  ```python
  except (ValueError, TypeError, KeyError) as e:
      logging.warning(
          f"[prompt_preview] analyze_answers failed course={course.course_key} user={user.id}: {e}"
      )
  ```
  The `transaction.atomic()` block correctly rolls back `UserResponse` rows on failure, but the exception is **swallowed** — the caller `ensure_preview_profile` has no way to know that a course failed to seed. The persona will be **partially seeded** (some courses have answers, others don't), and the admin UI will show a preview that silently omits data. Additionally, the `except` tuple omits `django.db.IntegrityError` and `django.db.DatabaseError`, which are the most likely exceptions from a `create()` call.
- **Remediation:** Either re-raise after logging, or propagate a structured failure signal to the caller:
  ```python
  except (ValueError, TypeError, KeyError, IntegrityError, DatabaseError) as e:
      logging.warning(
          f"[prompt_preview] analyze_answers failed course={course.course_key} user={user.id}: {e}",
          exc_info=True,
      )
      raise  # let the outer transaction.atomic() in seed_prompt_preview_persona roll back
  ```
  If partial seeding is intentional, add a `failed_courses: list[str]` return value so the caller can surface the gap.

---

- **Line:** `+    option = ResponseOption.objects.create(text=str(text), sentiment="0")` (in `_ensure_dummy_apq`)
- **Issue Category:** Data Integrity
- **The Defect:** `sentiment="0"` is a string literal. The retrieved `conflict_styles.py` source shows `sentiment = int(response.get("sentiment", 1))`, suggesting the field may be an `IntegerField` or a `CharField` that is later cast. If `sentiment` is an `IntegerField`, passing `"0"` will work in SQLite (dev) but may raise `DataError` in PostgreSQL (production) depending on the column type. **UNCERTAIN:** the `ResponseOption` model definition is not in the provided topography or key source files, so I cannot confirm the field type.
- **Remediation:** Verify the `ResponseOption.sentiment` field type. If it is an `IntegerField`, use `sentiment=0`. If it is a `CharField`, the string is correct but add a comment explaining why.

---

- **Line:** `+    user, _ = User.objects.get_or_create(` (in `ensure_preview_profile`)
- **Issue Category:** Concurrency / Data Integrity
- **The Defect:**
  ```python
  user, _ = User.objects.get_or_create(
      username=uname,
      defaults={...},
  )
  user.email = defaults["email"]
  user.is_active = True
  ...
  user.save()
  ```
  The `get_or_create` returns an existing user on the second call, but the code then **unconditionally overwrites** `email`, `is_active`, `user_type`, `first_name`, `last_name`, `gender`, `birthdate`, and `meta`. If a real user somehow has the same username (the `prompt_preview__` prefix makes this unlikely but not impossible), their profile is silently clobbered. The `meta` merge (`meta = dict(user.meta or {})`) is correct, but the top-level fields are not.
- **Remediation:** Guard the overwrite with a check that the user is a preview persona:
  ```python
  user, created = User.objects.get_or_create(
      username=uname,
      defaults={**defaults, "is_active": True, "user_type": UserTypes.APP_USER.value},
  )
  if not created:
      # Only overwrite if this is a known preview persona
      if (user.meta or {}).get(PREVIEW_META_KEY) != persona.value:
          raise ValueError(f"Username {uname} is not a preview persona; refusing to overwrite")
  ```

---

### File: `memores/services/prompt_preview_service.py`

- **Line:** `+from memores.external.claude_api import _resolve_model_name`
- **Issue Category:** Maintainability / Coupling
- **The Defect:** `_resolve_model_name` is a **private** function (underscore prefix) in `memores.external.claude_api`. Importing it into a separate service module creates a hidden coupling: a rename or signature change in `claude_api.py` will silently break the preview service with no static-analysis warning (mypy/`ruff` typically skip underscore-prefixed cross-module imports).
- **Remediation:** Either (a) promote `_resolve_model_name` to a public `resolve_model_name` in `claude_api.py`, or (b) duplicate the small resolution logic here with a comment referencing the source. Option (a) is preferred.

---

- **Line:** `+    template, _ = PromptTemplate.objects.get_or_create(` (in `_template_for_e2e`)
- **Issue Category:** Concurrency / Data Integrity
- **The Defect:**
  ```python
  template, _ = PromptTemplate.objects.get_or_create(
      name=PREVIEW_E2E_TEMPLATE_NAME,
      defaults={"prompt": prompt or "preview", "is_active": False},
  )
  template.prompt = prompt
  template.system_prompt = system_prompt or ""
  template.model = model or template.model
  template.output_limit = output_limit or template.output_limit or 1000
  template.output_schema = output_schema
  template.is_active = False
  template.save()
  ```
  This is a **read-modify-write on a single shared row** (`name="__admin_prompt_preview_e2e__"`). Two concurrent admin preview requests will both `get_or_create` the same row, then both `save()`, with the second overwriting the first's `prompt`/`system_prompt`/`model`. The `llm_job.delay()` call that follows will then execute with whichever `template` object was saved last, not the one the caller intended.
- **Remediation:** Use a per-request unique name (e.g., include a UUID) or use `select_for_update()` inside a transaction:
  ```python
  with transaction.atomic():
      template, _ = PromptTemplate.objects.select_for_update().get_or_create(
          name=PREVIEW_E2E_TEMPLATE_NAME,
          defaults={"prompt": prompt or "preview", "is_active": False},
      )
      template.prompt = prompt
      ...
      template.save()
  ```
  Or, more cleanly, generate a unique template name per request: `name=f"{PREVIEW_E2E_TEMPLATE_NAME}_{uuid.uuid4().hex[:8]}"`.

---

### File: `memores/views/admin/prompt_template.py`

- **Line:** `+        logging.info(` (in `PromptPreviewView.post`)
- **Issue Category:** Potential ImportError
- **The Defect:** The diff adds `logging.info(...)` calls in `PromptPreviewView.post` but does **not** add `import logging` to the file's import block. The existing context lines in the diff show imports from `rest_framework`, `memores.models`, `memores.permissions`, and the new service/serializer imports — but no `import logging`. **UNCERTAIN:** the full file may already import `logging` above the diff window. If it does not, this is a `NameError` at runtime.
- **Remediation:** Verify that `import logging` exists at the top of `memores/views/admin/prompt_template.py`. If not, add it.

---

- **Line:** `+        job_status = get_job_status(job.id)` (in `PromptPreviewView.post`)
- **Issue Category:** Performance / Latency
- **The Defect:** After `enqueue_preview_e2e` returns a Celery `AsyncResult`, the view **synchronously** calls `get_job_status(job.id)`. If `get_job_status` polls the broker or waits for a result, this blocks the HTTP request thread. The test mocks this call, so the blocking behavior is untested.
- **Remediation:** If `get_job_status` is a non-blocking snapshot (reads a cached status), this is acceptable. If it blocks, return the `job.id` immediately and let the client poll via the WebSocket URL (`build_ws_url`). Add a comment documenting the expected latency contract.

---

### File: `memores/serializers/prompt_preview_serializers.py`

- **Line:** `+    preview_persona_key = serializers.CharField()`
- **Issue Category:** API Contract
- **The Defect:** The field is a bare `CharField` with a `validate_preview_persona_key` method that checks membership in `{p.value for p in PromptPreviewPersona}`. This is functionally correct but constructs the allowed-set on **every request**. A `ChoiceField(choices=PromptPreviewPersona)` would be more idiomatic and would produce a DRF-standard error message.
- **Remediation:** This is a style preference, not a bug. The current approach is acceptable. If changing:
  ```python
  preview_persona_key = serializers.ChoiceField(choices=PromptPreviewPersona)
  ```
  and remove `validate_preview_persona_key`.

---

- **Line:** `+    report_question_type = serializers.CharField(` / `+        default=AnalysableProfileQuestionTypes.BASIC.value,`
- **Issue Category:** Redundancy
- **The Defect:** The field has `default=AnalysableProfileQuestionTypes.BASIC.value`, and `validate_report_question_type` also defaults to `AnalysableProfileQuestionTypes.BASIC.value` when the value is blank:
  ```python
  text = (value or "").strip() or AnalysableProfileQuestionTypes.BASIC.value
  ```
  The default is applied twice. Not a bug, but the `validate` method's defaulting is dead code when the field's `default` is in effect.
- **Remediation:** Remove the `default=` from the field declaration and let `validate_report_question_type` be the single source of truth, or remove the `or` fallback in the validator. Minor.

---

### File: `memores/services/results_analysis/communication_styles.py`

- **Line:** `+                 "title": str(_("Passive")),` (and 3 similar lines)
- **Issue Category:** Correctness
- **The Defect:** The diff wraps `title` values in `str()` but the `text` fields in this module are all commented out (e.g., `# "tendencies": ...`). This is consistent — there are no `text` fields to wrap. **Looks correct.**

---

### File: `memores/services/results_analysis/conflict_styles.py`

- **Line:** `+                 "title": str(_("Competing (Forcing/Power-Oriented)")),` and corresponding `text` wraps
- **Issue Category:** Correctness
- **The Defect:** All five `title` and `text` fields are wrapped in `str()`. This is the correct fix for `gettext` lazy-proxy serialization issues. **Looks correct.**

---

### File: `memores/services/results_analysis/big_five.py`, `enneagram.py`, `mbti.py`, `attachment_styles.py`

- **Line:** New substitution keys (`big_five`, `enneagram`, `mbti`, `attachment_styles`) with legacy aliases (`scores`, `results`, `type`, `percentages`)
- **Issue Category:** Correctness / Maintainability
- **The Defect:** The pattern is consistent across all four files: add a typed key, keep the legacy key as an alias. The `mbti.py` change also hoists `percentages_text` and `mbti_text` into local variables to avoid recomputing the join in two lambdas. **Looks correct.** The legacy aliases are well-commented.

---

### File: `memores/services/results_analysis/love_languages.py`

- **Line:** `+        return str(love_language_names.get(key, key))`
- **Issue Category:** Correctness
- **The Defect:** Wraps the return in `str()` to handle the `gettext` lazy proxy. **Looks correct.**

---

### File: `memores/utils/prompt_helpers/substitute.py`

- **Line:** `+            f"[prompt_helpers] unresolved placeholders remain: {unresolved}"`
- **Issue Category:** Minor / Logging
- **The Defect:** Changes from lazy `%s` formatting to an f-string. The f-string is always constructed even when the log level is below `WARNING`, a negligible cost. Not a bug, but a minor regression in logging hygiene.
- **Remediation:** Revert to the lazy form:
  ```python
  logging.warning("[prompt_helpers] unresolved placeholders remain: %s", unresolved)
  ```

---

### File: `memores/utils/prompt_helpers/constants.py`

- **Line:** `+    SOCIAL_STYLES = CourseKeys.SOCIAL_STYLES.value`
- **Issue Category:** Correctness
- **The Defect:** Adds a new `SubKeys` enum member. The corresponding registry entry in `substitute.py` is also added. **Looks correct.**

---

### File: `memores/urls/admin.py`

- **Line:** New routes for `PromptPreviewPersonasListView` and `PromptPreviewView`
- **Issue Category:** Correctness
- **The Defect:** Routes are added under the admin URL namespace. The views declare `permission_classes = (IsAuthenticated, IsStaffOrSuperUser)` and `authentication_classes = (TokenAuthentication,)`. **Looks correct.** No existing routes are modified.

---

### File: `memores/management/commands/seed_prompt_preview_personas.py`

- **Line:** `+        seed_all_prompt_preview_personas()`
- **Issue Category:** Correctness
- **The Defect:** Thin wrapper around the service function. The command is idempotent because `ensure_preview_profile` uses `get_or_create` and clears prior state. **Looks correct.**

---

### File: `memores/constants/prompt_preview.py`

- **Line:** Entire new file
- **Issue Category:** Correctness
- **The Defect:** The `PromptPreviewPersona` enum, label maps, variant-shift map, and dummy APQ specs are all self-contained. Imports are limited to `memores.constants.constants` and `memores.utils.prompt_helpers` — no circular-import risk. **Looks correct.**

---

### File: `memores/tests/test_prompt_preview.py`

- **Line:** Entire new file (193 lines)
- **Issue Category:** Test Coverage
- **The Defect:** The test suite is reasonably comprehensive (auth, happy path, e2e enqueue, model override, unseeded persona, invalid input, personas list, current_user). However:
  1. **No test for the `execute=True` error path** — what happens if `llm_job.delay` raises? The view has no `try/except` around `enqueue_preview_e2e`, so a Celery broker failure will 500.
  2. **No test for `PromptPreviewPersonasListView` auth** — `test_preview_requires_staff_profile` tests the POST endpoint, but the GET `/preview-personas/` endpoint's auth is untested.
  3. **`test_preview_personas_list_includes_labels`** asserts `len(rows) == 10` — a magic number that will break when a persona is added. Acceptable but fragile.
  4. **No test for the `results_analysis` changes** — the seven `str(_("…"))` wraps and new substitution keys have no corresponding test. A regression in any of the seven modules would go undetected.
- **Remediation:** Add at minimum:
  - A test that `execute=True` with a mocked `llm_job.delay` that raises returns a 500 (or a structured error).
  - A test that an unauthenticated GET to `/preview-personas/` returns 401.
  - A parametrized test that instantiates each `BaseTestResults` subclass and asserts the new substitution key is present in the output.

## 4. Test Coverage Assessment

| Changed Source File | Test File | Coverage |
|---|---|---|
| `memores/constants/prompt_preview.py` | `test_prompt_preview.py` | Indirectly tested via persona seeding. No direct unit test of the enum/label maps. Acceptable. |
| `memores/serializers/prompt_preview_serializers.py` | `test_prompt_preview.py` | Tested via the view (integration). No direct serializer unit test. Acceptable for a thin serializer. |
| `memores/services/prompt_preview_personas.py` | `test_prompt_preview.py` | `setUp` calls `seed_prompt_preview_persona(EMPTY_PROFILE)`. `test_preview_supporting_questions_include_dummy_basic_answers` seeds `PROFILE_ONLY`. `test_preview_supporting_questions_include_dummy_advanced_on_full` seeds `FULL_1`. **Missing:** `PARTIAL_1/2/3`, `FULL_2/3/4`, and the `CURRENT_USER` raise path. |
| `memores/services/prompt_preview_service.py` | `test_prompt_preview.py` | `test_preview_e2e_enqueues_llm_job` mocks `llm_job.delay` and `get_job_status`. **Missing:** error path when `llm_job.delay` raises. |
| `memores/services/results_analysis/*.py` (7 files) | **None** | **No test file for `results_analysis` is in the changed files.** The `str(_("…"))` wraps and new substitution keys are untested. |
| `memores/views/admin/prompt_template.py` | `test_prompt_preview.py` | Well-covered for the happy path. **Missing:** `execute=True` error path, `PromptPreviewPersonasListView` auth. |
| `memores/urls/admin.py` | `test_prompt_preview.py` | Routes are exercised by the view tests. Acceptable. |
| `memores/utils/prompt_helpers/substitute.py` | **None** | The `SOCIAL_STYLES` registry entry and the f-string log change are untested. |
| `scripts/setup.sh` | N/A | Shell script; not unit-testable. |

**Assertion quality:** The existing assertions are generally strong — they check response body content (`assertIn("Can we move the review to Friday", data["populatedPrompt"])`), not just status codes. The `test_preview_unseeded_persona_returns_seed_hint` test asserts on the detail message content, which is good. No tautological assertions found.

**Summary:** The test coverage for the new preview subsystem is solid for the happy path. The gaps are: (1) no tests for the seven `results_analysis` modules, (2) no error-path test for `execute=True`, (3) no auth test for the GET personas endpoint, (4) partial coverage of the nine non-`CURRENT_USER` personas.

---

## 5. Automated Claim Verification

Claim Verification: 26/26 verified (100% accuracy)


---

## 📊 Review Reliability Scores

- architectural_soundness: 3
- claim_grounding: 2
- concision: 2
- confidence_calibration: 3
- diff_adherence: 2
- factual_accuracy: 2
- remediation_utility: 3
- test_scrutiny: 4
- verdict_clarity: 5