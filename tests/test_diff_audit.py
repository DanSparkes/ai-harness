"""Tests for core.diff_audit: deterministic diff invariants and the post-hoc
audit that catches hallucinated migration claims. Network-free."""

from core.diff_audit import (
    abstract_base_fields,
    audit_review_against_invariants,
    build_invariant_block,
    parse_diff_invariants,
)

SOFT_DELETE_DIFF = """diff --git a/profiles/models.py b/profiles/models.py
--- a/profiles/models.py
+++ b/profiles/models.py
@@ -160,7 +160,7 @@ class User(AbstractUser):
-class User(AbstractUser):
+class User(SoftDeleteModel, AbstractUser):
     \"\"\"Custom user model replacing the split Profile/User architecture.\"\"\"

-    objects = UserManager()  # type: ignore[misc]
+    objects = MemoresUserManager()  # type: ignore[misc, assignment]

     class Meta:
         db_table = "memores_user"
         permissions = profile_permissions()  # type: ignore[assignment]
+        base_manager_name = "all_objects"

     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
@@ -166,10 +196,6 @@ class User(AbstractUser):
     password_reset_code = models.CharField(max_length=16, null=True, blank=True)
     stripe_customer_id = models.CharField(max_length=256, blank=True, null=True)

-    is_deleted = models.BooleanField(default=False)
"""

KEY_FILES = """### profiles/models.py
```python
class SoftDeleteQuerySet(models.QuerySet):
    def all_objects(self):
        return self.filter(is_deleted=False)

class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).filter(is_deleted=False)

class SoftDeleteModel(models.Model):
    is_deleted = models.BooleanField(default=False)

    class Meta:
        abstract = True

class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    is_deleted = models.BooleanField(default=False)
```"""


# ── parsing ───────────────────────────────────────────────────────────────────


def test_parse_removed_field() -> None:
    inv = parse_diff_invariants(["profiles/models.py"], SOFT_DELETE_DIFF)
    assert "is_deleted" in inv.removed_fields
    removed = [c for c in inv.field_changes if c.field == "is_deleted"]
    assert removed and removed[0].kind == "removed"
    assert removed[0].class_name == "User"


def test_parse_inheritance_and_manager_changes() -> None:
    inv = parse_diff_invariants(["profiles/models.py"], SOFT_DELETE_DIFF)
    assert any("SoftDeleteModel" in c for c in inv.inheritance_changes)
    assert inv.manager_changes == [
        "manager assignment (removed)",
        "manager assignment (added)",
    ]


def test_parse_no_migrations_in_diff() -> None:
    inv = parse_diff_invariants(["profiles/models.py"], SOFT_DELETE_DIFF)
    assert inv.migration_files == []


def test_parse_detects_migration_files() -> None:
    inv = parse_diff_invariants(
        ["profiles/models.py", "profiles/migrations/0190_auto.py"], SOFT_DELETE_DIFF
    )
    assert inv.migration_files == ["profiles/migrations/0190_auto.py"]


def test_parse_added_field_is_new_column() -> None:
    diff = "diff --git a/x/models.py b/x/models.py\n--- a/x/models.py\n+++ b/x/models.py\n@@ -1 +1 @@\n+    is_active = models.BooleanField(default=True)\n"
    inv = parse_diff_invariants(["x/models.py"], diff)
    assert "is_active" in inv.added_fields


# ── abstract-base detection ───────────────────────────────────────────────────


def test_abstract_base_fields_finds_inherited_column() -> None:
    fields = abstract_base_fields(KEY_FILES)
    assert fields["is_deleted"] == "SoftDeleteModel"


def test_abstract_base_fields_ignores_concrete_classes() -> None:
    fields = abstract_base_fields(KEY_FILES)
    assert (
        "User" not in fields.values()
    )  # User is concrete; is_deleted maps to the base


# ── invariant block ───────────────────────────────────────────────────────────


def test_invariant_block_marks_column_already_exists() -> None:
    inv = parse_diff_invariants(["profiles/models.py"], SOFT_DELETE_DIFF)
    block = build_invariant_block(inv, KEY_FILES)
    assert "Machine-Derived Diff Invariants" in block
    assert "column already exists" in block
    assert "SoftDeleteModel" in block
    assert "is NOT required to ADD it" in block
    # The moved column must NOT be listed as a brand-new column needing a migration
    assert "New columns introduced" not in block


def test_invariant_block_empty_for_non_model_diff() -> None:
    inv = parse_diff_invariants(
        ["views.py"], "diff --git a/views.py b/views.py\n+print('hi')\n"
    )
    assert build_invariant_block(inv, "") == ""


def test_invariant_block_new_column_warns_missing_migration() -> None:
    diff = "diff --git a/x/models.py b/x/models.py\n--- a/x/models.py\n+++ b/x/models.py\n@@ -1 +1 @@\n+    is_active = models.BooleanField(default=True)\n"
    inv = parse_diff_invariants(["x/models.py"], diff)
    block = build_invariant_block(inv, "")
    assert "New columns introduced" in block
    assert "need a migration" in block


def test_invariant_block_moved_field_detected() -> None:
    diff = (
        "diff --git a/x/models.py b/x/models.py\n"
        "--- a/x/models.py\n+++ b/x/models.py\n"
        "@@ -1 +1 @@\n-class SoftDeleteModel(models.Model):\n+class SoftDeleteModel(models.Model):\n"
        "-    is_deleted = models.BooleanField(default=False)\n"
        "+    is_deleted = models.BooleanField(default=False)\n"
    )
    inv = parse_diff_invariants(["x/models.py"], diff)
    assert "is_deleted" in inv.moved_fields
    assert "relocation" in build_invariant_block(inv, "")


# ── post-hoc audit ────────────────────────────────────────────────────────────


def test_audit_flags_false_critical_migration_claim() -> None:
    inv = parse_diff_invariants(["profiles/models.py"], SOFT_DELETE_DIFF)
    review = """# Overall Architectural Verdict
REQUEST CHANGES - Critical missing artifact: no migrations included for schema
changes required by adding SoftDeleteModel to User's MRO. The PR cannot deploy
without database migrations to add the is_deleted column to the user table."""
    note = audit_review_against_invariants(review, inv, KEY_FILES)
    assert note
    assert "contradict the machine-derived diff invariants" in note
    assert "is_deleted" in note


def test_audit_flags_false_removefield_migration_claim() -> None:
    """The review demands a RemoveField migration for a field the diff merely
    relocated to an abstract base. Flag it — and warn it would drop data."""
    inv = parse_diff_invariants(["profiles/models.py"], SOFT_DELETE_DIFF)
    review = """The is_deleted field is removed from User's class body because it is now
inherited from SoftDeleteModel. However, no migration file is included in the
diff. This is a schema-altering change that requires Django to remove the
explicit is_deleted column definition from User. Generate and include the
migration file: python manage.py makemigrations memores. The generated
migration should show: migrations.RemoveField(model_name='user',
name='is_deleted')."""
    note = audit_review_against_invariants(review, inv, KEY_FILES)
    assert note
    assert "schema-altering migration is REQUIRED" in note
    assert "DROP the existing column (data loss)" in note


def test_audit_does_not_flag_correct_migration_advice() -> None:
    """Advising to VERIFY whether a migration is needed (with the right
    conclusion) must not be flagged as a contradiction."""
    inv = parse_diff_invariants(["profiles/models.py"], SOFT_DELETE_DIFF)
    review = (
        "No migration is required: is_deleted is now inherited from "
        "SoftDeleteModel, the column already exists, and Django reports "
        "'No changes detected'."
    )
    assert audit_review_against_invariants(review, inv, KEY_FILES) == ""


def test_audit_silent_when_no_contradiction() -> None:
    inv = parse_diff_invariants(["profiles/models.py"], SOFT_DELETE_DIFF)
    review = (
        "# Looks good\n\n## profiles/models.py\n"
        "Looks correct: User now inherits SoftDeleteModel; the is_deleted field "
        "is removed from the concrete class since the abstract base already "
        "defines it. No new column is introduced."
    )
    assert audit_review_against_invariants(review, inv, KEY_FILES) == ""


def test_audit_silent_when_review_never_mentions_field() -> None:
    inv = parse_diff_invariants(["profiles/models.py"], SOFT_DELETE_DIFF)
    assert audit_review_against_invariants("# fine", inv, KEY_FILES) == ""


def test_audit_empty_for_no_field_changes() -> None:
    inv = parse_diff_invariants(
        ["views.py"], "diff --git a/views.py b/views.py\n+print('x')\n"
    )
    assert audit_review_against_invariants("needs a migration", inv, "") == ""
