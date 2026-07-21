# ADR 0005: Multi-Language Model Field Translation

**Date:** 2026-07-16
**Status:** Proposed
**Deciders:** Engineering Team

---

## Context

Several core domain models contain user-facing text fields that must be available in multiple languages to support localization across the application's course content, questions, and responses. The models requiring translation are:

| Model | Translatable Fields | Field Types |
|-------|-------------------|-------------|
| `Question` | `question_text` | CharField(4096) |
| `QuestionGroup` | `name`, `description` | CharField(1024), CharField(2048) |
| `ResponseOption` | `text` | CharField(2048) |
| `ResponseGroup` | `name` | CharField(1024) |
| `Course` | `title`, `description`, `introduction_message`, `completion_message` | CharField(2048), CharField(8128), CharField(4096), CharField(4096) |
| `Audio` | `title` | CharField(256) |
| `CourseGroup` | `name`, `description` | CharField(256), CharField(2048) |
| `BenefactorCohort` | `name`, `description` | CharField(256), CharField(2048) |

**Total: ~15 translatable fields across 8 models.**

### Current State

- The `Course` and `User` models already have a `language` field (`CharField(max_length=8)`) indicating the language of the content or user preference.
- All text fields are currently single-language — a `Question` written in English has no mechanism to store a Spanish translation alongside it.
- The application uses Django REST Framework for all API endpoints and PostgreSQL as the database.

### Requirements

| Requirement | Priority | Details |
|-------------|----------|---------|
| Minimal schema disruption | P0 | Translation mechanism must not require rewriting all existing queries or serializers |
| DRF integration | P0 | Must work cleanly with existing DRF `ModelSerializer` patterns and the `django-filter` backend |
| Querying/filtering on translations | P1 | Must support filtering querysets by translated text (e.g., find questions by `question_text` in a specific language) |
| Performance | P1 | No N+1 query explosion when fetching objects with translations; prefer single-table or pre-fetchable patterns |
| Fallback to default language | P1 | When a translation is missing, fall back to the default language (English) automatically |
| Migration path | P1 | Must be able to migrate existing single-language data into the translation system without data loss |
| Admin support | P2 | Translated fields should be manageable in Django admin |
| Maintenance activity | P2 | Library must have active maintenance and Django 5.x compatibility |

---

## Options Considered

### Option 1: `django-modeltranslation` (Separate Columns)

**Approach:** Adds one column per language per translatable field directly to the same table. For example, `Course.title` becomes `Course.title_en`, `Course.title_es`, `Course.title_fr`.

- **Storage:** Extra columns on the existing table (no joins, no separate tables)
- **Querying:** Native ORM — filter on `title_es` directly; the active translation is resolved via a custom manager based on `request.LANGUAGE_CODE`
- **DRF:** Works with standard `ModelSerializer` (fields are regular Django fields); no custom serializer needed
- **django-filter:** Compatible — translatable fields are real columns, so filter backends work normally
- **Maintenance:** Actively maintained — releases every 1-3 months (v0.20.3 as of April 2026); Django 5.x support; ~500 GitHub stars
- **Migration:** `makemigrations` detects new columns; existing data copied to `*_en` columns via `update_translation_fields` management command
- **Admin:** Built-in `TranslationAdmin` with tabbed language interface

**Pros:**
- Zero join overhead — translations live in the same row
- Standard ORM filtering — no custom manager queries
- DRF `ModelSerializer` works out of the box
- Clean fallback mechanism built in
- Well-documented, widely used in production

**Cons:**
- Table width increases with each language (15 fields × N languages)
- Adding a new language requires a migration (new columns)
- No way to query "does this object have a translation in language X?" without checking each column
- Column proliferation can make admin and raw SQL harder to read

**Table width impact (at 5 languages):**

| Model | Current columns | Translatable | Extra columns | Total |
|-------|----------------|-------------|---------------|-------|
| `Course` | ~16 | 4 | 20 | ~36 |
| `Question` | ~8 | 1 | 5 | ~13 |
| `QuestionGroup` | ~4 | 2 | 10 | ~14 |
| `ResponseOption` | ~5 | 1 | 5 | ~10 |
| `ResponseGroup` | ~3 | 1 | 5 | ~8 |
| `Audio` | ~5 | 1 | 5 | ~10 |
| `CourseGroup` | ~5 | 2 | 10 | ~15 |
| `BenefactorCohort` | ~6 | 2 | 10 | ~16 |

### Option 2: `django-parler` (Separate Translation Table)

**Approach:** Creates a `CourseTranslation` model with a FK back to `Course`, storing translated fields in a separate row per language.

- **Storage:** Separate table per translatable model (one row per language per object)
- **Querying:** Requires `.translated('es')` manager calls or `.active_translations()` — not standard ORM filtering
- **DRF:** Requires `parler.contrib.rest_framework.TranslatableModelSerializer` — not standard `ModelSerializer`
- **django-filter:** Known incompatibility — `django-filter` inspects `queryset.model` and fails on translated querysets
- **Maintenance:** Last release 2024; 106 open issues; tests failing on Django 5.x main branch; slow activity
- **Migration:** Auto-creates translation tables via `TranslatedFields` wrapper; existing data requires manual copy

**Pros:**
- Clean separation — translation data doesn't bloat the main table
- Can query "does this object have a translation in language X?"
- Compatible with django-hvad patterns (historical ecosystem)

**Cons:**
- **Maintenance risk** — failing tests on Django 5.x, 106 open issues, slow release cadence
- Requires join to access translated fields (performance concern for list endpoints)
- Custom serializer required — breaks existing DRF patterns
- `django-filter` incompatibility requires workarounds
- `.translated()` calls produce multiple queries per object without careful prefetching

### Option 3: `django-i18nfield` (JSON in TextField)

**Approach:** Stores translations as JSON in a single `TextField` column (e.g., `{"en": "Hello", "es": "Hola"}`).

- **Storage:** Single column per translatable field (JSON blob)
- **Querying:** No native ORM filtering on individual languages — requires JSON lookups or custom Q objects
- **DRF:** Has built-in DRF integration via `I18nCharFieldSerializer` / `I18nTextFieldSerializer`
- **Maintenance:** Maintained (v1.11.0, Sep 2025); supports Django 5.x; used in production by pretix
- **Migration:** Simple — converts existing field to JSON storage

**Pros:**
- Zero schema changes for new languages
- Single column, no table bloat
- No joins needed
- Production-tested in pretix

**Cons:**
- **Violates 1NF** — JSON in a text field
- No useful ORM filtering on individual languages (cannot do `WHERE title->>'es' LIKE '%foo%'` without raw SQL)
- No database-level indexing on translated content
- Harder for non-Django tools to read
- Cannot add database constraints on translations

### Option 4: `django-i18n-fields` (JSON in JSONField — Database-Agnostic)

**Approach:** Similar to Option 3 but uses Django's native `JSONField` and provides `LocalizedModelSerializer` for DRF.

- **Storage:** Single JSONField per translatable field
- **Querying:** Uses `L()` query expressions for filtering
- **DRF:** Built-in `LocalizedModelSerializer`
- **Maintenance:** Active (v1.1.1); Django 5.x support; newer project
- **Migration:** Requires field type change (CharField → JSONField)

**Pros:**
- Database-agnostic (works with PostgreSQL, MySQL, SQLite)
- Built-in DRF support
- Query expressions for filtering
- Active maintenance

**Cons:**
- Newer project, less battle-tested
- JSON approach has same indexing limitations as Option 3
- Requires changing field types in existing models

### Option 5: `django-modeltrans` (JSONB Field — PostgreSQL Only)

**Approach:** Uses a single PostgreSQL `JSONB` column per model (not per field) to hold all translations. Uses a registration approach similar to `django-modeltranslation`.

- **Storage:** One JSONB column per model (not per field)
- **Querying:** JSONB operators for filtering; registration-based field resolution
- **DRF:** No built-in DRF integration
- **Maintenance:** Active (v0.9.0, Oct 2025); Django 5.x support
- **Migration:** Requires adding a JSONB column and running a data migration

**Pros:**
- Single column per model, not per field
- PostgreSQL JSONB indexing available
- Registration-based (non-invasive)

**Cons:**
- PostgreSQL only
- No built-in DRF support
- Less mature than `django-modeltranslation`
- JSONB querying is less intuitive than column-based filtering

---

## Decision

**Adopt `django-modeltranslation` (Option 1).**

### Rationale

| Criterion | modeltranslation | parler | i18nfield | i18n-fields | modeltrans |
|-----------|-----------------|--------|-----------|-------------|------------|
| DRF compatibility | ✅ Standard ModelSerializer | ⚠️ Custom serializer | ✅ Custom fields | ✅ Custom serializer | ⚠️ No built-in |
| django-filter compatibility | ✅ Native columns | ❌ Known issues | ⚠️ JSON lookups | ⚠️ JSON lookups | ⚠️ JSON lookups |
| Query performance | ✅ No joins | ⚠️ Requires join/prefetch | ✅ No joins | ✅ No joins | ✅ No joins |
| ORM filtering on translations | ✅ Direct column access | ⚠️ Custom manager | ❌ No native filtering | ⚠️ L() expressions | ⚠️ JSONB operators |
| Maintenance activity | ✅ Monthly releases | ⚠️ Slow, failing tests | ✅ Active | ✅ Active | ✅ Active |
| Django 5.x support | ✅ | ❌ Failing tests | ✅ | ✅ | ✅ |
| Migration simplicity | ✅ Add columns | ⚠️ New tables | ✅ Change field type | ⚠️ Change field type | ⚠️ Add JSONB column |
| Schema simplicity | ⚠️ More columns | ✅ Separate tables | ✅ Single column | ✅ Single column | ✅ Single JSONB column |

**`django-modeltranslation` wins on the two highest-priority criteria: DRF compatibility and query/filtering performance.** The column-per-language approach means our existing `ModelSerializer` patterns, `django-filter` backends, and ORM queries continue to work with minimal changes. The table width increase is acceptable at 15 fields across 8 models — even at 5 languages, the largest table (`Course`) grows from ~16 to ~36 columns, which is well within PostgreSQL's efficient column count range.

The primary trade-off (table width) is offset by the fact that `django-modeltranslation` is the most widely used, best-maintained, and most DRF-compatible option in the Django ecosystem.

---

## Detailed Design

### 1. Settings Configuration

```python
# settings.py

LANGUAGES = [
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    # Add languages as needed
]

LANGUAGE_CODE = "en"

MODELTRANSLATION_DEFAULT_LANGUAGE = "en"
MODELTRANSLATION_LANGUAGES = ("en", "es", "fr")
MODELTRANSLATION_FALLBACK_LANGUAGES = {"default": ("en",)}
MODELTRANSLATION_PREPOPULATE_LANGUAGE = "en"
```

### 2. Translation Registration

Create a new module `memores/translation.py`:

```python
from modeltranslation.options import TranslationOptions
from modeltranslation.decorators import register

from memores import models


@register(models.Course)
class CourseTranslationOptions(TranslationOptions):
    fields = ("title", "description", "introduction_message", "completion_message")
    fallback_values = ""


@register(models.Question)
class QuestionTranslationOptions(TranslationOptions):
    fields = ("question_text",)
    fallback_values = ""


@register(models.QuestionGroup)
class QuestionGroupTranslationOptions(TranslationOptions):
    fields = ("name", "description")
    fallback_values = ""


@register(models.ResponseOption)
class ResponseOptionTranslationOptions(TranslationOptions):
    fields = ("text",)
    fallback_values = ""


@register(models.ResponseGroup)
class ResponseGroupTranslationOptions(TranslationOptions):
    fields = ("name",)
    fallback_values = ""


@register(models.Audio)
class AudioTranslationOptions(TranslationOptions):
    fields = ("title",)
    fallback_values = ""


@register(models.CourseGroup)
class CourseGroupTranslationOptions(TranslationOptions):
    fields = ("name", "description")
    fallback_values = ""


@register(models.BenefactorCohort)
class BenefactorCohortTranslationOptions(TranslationOptions):
    fields = ("name", "description")
    fallback_values = ""
```

### 3. Model Changes

After running `makemigrations`, each registered field gains `{field}_en`, `{field}_es`, `{field}_fr` columns. The original field becomes a descriptor that resolves to the active language's column.

**Before:**
```python
class Course(models.Model):
    title = models.CharField(max_length=2048)
    description = models.CharField(max_length=8128, null=True, blank=True)
```

**After (automatic, no model change needed):**
```python
class Course(models.Model):
    title = models.CharField(max_length=2048)       # becomes descriptor
    title_en = models.CharField(max_length=2048)    # new column
    title_es = models.CharField(max_length=2048)    # new column
    title_fr = models.CharField(max_length=2048)    # new column
    description = models.CharField(max_length=8128, null=True, blank=True)
    description_en = models.CharField(max_length=8128, null=True, blank=True)
    description_es = models.CharField(max_length=8128, null=True, blank=True)
    description_fr = models.CharField(max_length=8128, null=True, blank=True)
```

### 4. Querying Translations

```python
from django.utils import translation

# Automatic — resolves based on active language (from request or middleware)
course = Course.objects.get(pk=course_id)
print(course.title)  # prints title in current active language

# Explicit — access a specific language
print(course.title_en)
print(course.title_es)

# Filtering — standard ORM works
 Course.objects.filter(title_es__icontains="curso")

# Translation-aware manager
from modeltranslation.utils import get_translation
course_es = get_translation(course, "es")
```

### 5. DRF Serializer Changes

Existing `ModelSerializer` subclasses continue to work because `modeltranslation` replaces the original field descriptors. The serialized output includes only the active language's fields.

**Option A — Active language only (recommended for client-facing APIs):**

```python
class CourseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ["id", "title", "description", "language"]
        # "title" resolves to the active language automatically
```

**Option B — All translations (for admin or multi-language clients):**

```python
class CourseDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ["id", "title_en", "title_es", "title_fr", "description_en", ...]
```

### 6. Middleware Configuration

```python
# settings.py
MIDDLEWARE = [
    # ...
    "django.middleware.locale.LocaleMiddleware",  # already present in most Django setups
    # ...
]
```

The `LocaleMiddleware` sets `request.LANGUAGE_CODE` based on the `Accept-Language` header, which `modeltranslation` uses to resolve the active language.

### 7. Data Migration

After adding `modeltranslation` to `INSTALLED_APPS` and running `makemigrations`, existing data must be copied into the new language-specific columns:

```bash
# Copies existing single-language data into the default language columns
python manage.py update_translation_fields
```

This management command populates `title_en` from `title`, `description_en` from `description`, etc., preserving all existing data.

### 8. Admin Integration

```python
from modeltranslation.admin import TranslationAdmin

@admin.register(Course)
class CourseAdmin(TranslationAdmin):
    list_display = ["title", "language"]
    # Provides tabbed interface for editing translations
```

---

## Migration Steps

1. **Add `modeltranslation` to `INSTALLED_APPS`** — must be placed before the app containing translated models
2. **Create `memores/translation.py`** with registration classes
3. **Run `makemigrations`** — generates `AddField` migrations for all `{field}_{lang}` columns
4. **Run `migrate`** — applies column additions
5. **Run `update_translation_fields`** — copies existing data into default language columns
6. **Update serializers** — ensure translated fields are referenced correctly
7. **Update admin classes** — add `TranslationAdmin` mixin
8. **Update tests** — factories and assertions may need language-aware adjustments

---

## Consequences

### Positive

1. **Minimal code changes** — `modeltranslation`'s registration approach means model classes stay unchanged; translation is configured externally
2. **Standard ORM queries** — no custom managers, no `.translated()` calls, no joins; filter on `title_es` like any other column
3. **DRF compatibility** — existing `ModelSerializer` patterns work without modification
4. **`django-filter` compatibility** — filter backends work because translations are real columns
5. **Performance** — zero join overhead; translations resolved via column access, not queries
6. **Fallback built in** — missing translations automatically fall back to the default language
7. **Well-maintained** — monthly releases, Django 5.x support, large community

### Negative

1. **Table width** — 15 fields × (N-1) extra columns per language; at 5 languages, ~60 extra columns across 8 tables
2. **Migration for new languages** — adding a language requires `makemigrations` + `migrate` for new columns
3. **No atomic language addition** — cannot add a language at runtime without a migration; requires deployment
4. **Column proliferation in admin** — raw SQL and admin list views show language-specific columns

### Neutral

- The `language` field already on `Course` and `User` models can serve as the content language indicator, independent of `modeltranslation`'s active language resolution
- Existing `PromptTemplate` fields (`prompt`, `system_prompt`) are intentionally excluded from translation — they are LLM instructions, not user-facing content

---

## Risks & Mitigations

### Risk 1: Table Width Exceeds PostgreSQL Efficiency Threshold

PostgreSQL performs best with fewer columns; wide tables can slow sequential scans and increase `TOAST` overhead.

**Mitigation:** At 5 languages, the largest table (`Course`) reaches ~36 columns — well within PostgreSQL's efficient range. Monitor with `pg_stat_user_tables` after deployment. If width becomes a concern, consider partitioning translation columns into a separate table via `modeltranslation`'s `TranslationAdmin` configuration.

### Risk 2: Migration Adds Many Columns in Single Deploy

Adding 60+ columns across 8 tables in one migration could lock tables for an extended period.

**Mitigation:** `ALTER TABLE ADD COLUMN` in PostgreSQL is fast (metadata-only for nullable columns). All new translation columns are nullable, so no table rewrite occurs. Run `EXPLAIN ANALYZE` on a production-sized copy before deploying.

### Risk 3: Existing Queries Hardcode Original Field Names

Any raw SQL or ORM queries that reference the original field name (e.g., `Course.objects.filter(title__icontains="foo")`) may resolve to the default language column automatically via `modeltranslation`'s descriptor — but raw SQL will break if it references `title` directly (column no longer exists after migration).

**Mitigation:** Audit all raw SQL for references to translatable fields. The `update_translation_fields` command renames the original column; raw SQL must reference `title_en` (or whichever is the default language column).
