"""Deterministic diff-audit layer for code review.

Reviews are LLM-generated and occasionally hallucinate schema facts — e.g.
claiming a schema-altering DB migration is required for a column that the
diff merely RELOCATED to an abstract base class (either "must ADD the column"
or "must RemoveField it", the latter of which would drop the existing column
and destroy data). This module parses the raw diff mechanically, derives
invariants the model must ground to, and provides a post-hoc audit that flags
review claims contradicting those invariants.

Pure stdlib — no network, no third-party dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_FIELD_LINE_RE = re.compile(
    r"^[ \t]*([+-])[ \t]*([A-Za-z_]\w*)\s*=\s*models\.(\w+Field)\s*\("
)
_CLASS_LINE_RE = re.compile(r"^[ \t]*([+-]?)class\s+(\w+)\s*\(([^)]*)\)\s*:")
_CLASS_BLOCK_RE = re.compile(r"^class\s+(\w+)\s*\(([^)]*)\)\s*:", re.M)
_BODY_FIELD_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*models\.(\w+Field)\s*\(", re.M)
_MANAGER_RE = re.compile(r"^[ \t]*[+-][ \t]*objects\s*=\s*\w+Manager\s*\(")


@dataclass
class FieldChange:
    file: str = ""
    field: str = ""
    kind: str = ""  # "added" | "removed"
    field_type: str = ""
    class_name: str | None = None


@dataclass
class DiffInvariants:
    field_changes: list[FieldChange] = field(default_factory=list)
    inheritance_changes: list[str] = field(default_factory=list)
    manager_changes: list[str] = field(default_factory=list)
    migration_files: list[str] = field(default_factory=list)

    @property
    def added_fields(self) -> set[str]:
        return {c.field for c in self.field_changes if c.kind == "added"}

    @property
    def removed_fields(self) -> set[str]:
        return {c.field for c in self.field_changes if c.kind == "removed"}

    @property
    def moved_fields(self) -> set[str]:
        return self.added_fields & self.removed_fields


def parse_diff_invariants(changed_files: list[str], raw_diff: str) -> DiffInvariants:
    """Mechanically parse model-field facts out of a unified git diff."""
    inv = DiffInvariants()
    inv.migration_files = [
        f.replace("\\", "/")
        for f in changed_files
        if "migrations/" in f.replace("\\", "/")
    ]

    current_file = ""
    current_class: str | None = None
    for raw in raw_diff.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            current_class = None
            continue
        if raw.startswith(("--- ", "+++ ")) or raw[:1] not in ("+", "-", " "):
            continue

        cm = _CLASS_LINE_RE.match(raw)
        if cm:
            sign, name, bases = cm.group(1), cm.group(2), cm.group(3)
            current_class = name
            if sign:
                verb = "added" if sign == "+" else "removed"
                inv.inheritance_changes.append(f"{name} inherits ({bases}) ({verb})")
            continue

        fm = _FIELD_LINE_RE.match(raw)
        if fm:
            sign, field_name, field_type = fm.group(1), fm.group(2), fm.group(3)
            inv.field_changes.append(
                FieldChange(
                    file=current_file,
                    field=field_name,
                    kind="added" if sign == "+" else "removed",
                    field_type=field_type,
                    class_name=current_class,
                )
            )
            continue

        if _MANAGER_RE.match(raw):
            sign = "+" if raw.lstrip().startswith("+") else "-"
            verb = "added" if sign == "+" else "removed"
            inv.manager_changes.append(f"manager assignment ({verb})")

    return inv


def abstract_base_fields(text: str) -> dict[str, str]:
    """Map field_name -> base class name for classes marked ``abstract``.

    Runs over collected key-file contents (models.py blocks), not the diff,
    so removed concrete fields can be matched against their inherited source.
    """
    found: dict[str, str] = {}
    matches = list(_CLASS_BLOCK_RE.finditer(text))
    for idx, m in enumerate(matches):
        class_name = m.group(1)
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[m.end() : end]
        if "abstract = True" in body:
            for fm in _BODY_FIELD_RE.finditer(body):
                found.setdefault(fm.group(1), class_name)
    return found


def build_invariant_block(inv: DiffInvariants, key_file_context: str) -> str:
    """Build the machine-derived invariants block injected into every prompt."""
    if (
        not inv.field_changes
        and not inv.inheritance_changes
        and not inv.manager_changes
        and not inv.migration_files
    ):
        return ""

    abs_fields = abstract_base_fields(key_file_context)
    moved = inv.moved_fields
    removed = {c.field for c in inv.field_changes if c.kind == "removed"}
    inherited = removed & set(abs_fields)
    orphan_removed = removed - set(abs_fields)
    new_columns = inv.added_fields - moved

    lines = [
        "## Machine-Derived Diff Invariants (deterministic — parsed from the raw diff, not inferred)",
        "Trust these facts over any inference. Findings that contradict them are fabrications.\n",
    ]

    if inv.field_changes:
        lines.append("### Model field changes")
        for c in inv.field_changes:
            loc = c.file + (f" [{c.class_name}]" if c.class_name else "")
            lines.append(f"- `{c.field}` ({c.field_type}) {c.kind} in {loc}")
        if moved:
            lines.append("")
            lines.append(
                "Moved fields (added AND removed in the same diff — column already "
                "exists; this is a relocation, not a new column): "
                + ", ".join(sorted(moved))
            )
        if inherited:
            lines.append("")
            lines.append(
                "Removed fields still defined in an abstract base — the column "
                "already exists in the DB; a migration is NOT required to ADD it:"
            )
            for f in sorted(inherited):
                lines.append(f"- `{f}` inherited from `{abs_fields[f]}`")
        if orphan_removed:
            lines.append("")
            lines.append(
                "Removed fields NOT found in any abstract base in the key files "
                "(verify whether a column-drop migration is intended):"
            )
            for f in sorted(orphan_removed):
                lines.append(f"- `{f}`")

    if new_columns:
        lines.append("")
        lines.append(
            "New columns introduced (a migration may be required if not already "
            "present): " + ", ".join(sorted(new_columns))
        )
    if inv.migration_files:
        lines.append("")
        lines.append("Migration files in this diff: " + ", ".join(inv.migration_files))
    elif new_columns:
        lines.append("")
        lines.append(
            "No migration files in this diff — the new columns above need a migration."
        )

    if inv.inheritance_changes:
        lines.append("")
        lines.append("### Inheritance / manager changes")
        lines.extend(f"- {c}" for c in inv.inheritance_changes)
        lines.extend(f"- {c}" for c in inv.manager_changes)

    return "\n".join(lines)


_NEGATED_CONTEXT = re.compile(
    r"\b(?:no migration|not required|not needed|no schema change|no new column|"
    r"already exists|unnecessary|does not require|doesn't require|not necessary|"
    r"no addfield|no removefield|may be needed|might)\b"
)
_MIGRATION_SIGNAL = re.compile(
    r"\b(?:migrations?|makemigrations|schema[- ]altering|schema change|addfields?|removefields?)\b"
)
_ACTION_DEMAND = re.compile(
    r"\b(?:require|requires|required|need|needs|needed|must|cannot|can't|without|"
    r"missing|generate|addfield|removefield)\b"
)


def _split_sentences(text: str) -> list[str]:
    """Split review text into sentences, honoring wrapped prose.

    Hard-wrapped lines (a newline mid-sentence) are joined with a space;
    only paragraph breaks (blank lines) and sentence punctuation split.
    """
    flattened = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    parts = re.split(r"(?<=[.!?])\s+|\n\s*\n", flattened)
    return [p for p in parts if p.strip()]


def _demands_schema_migration(sentence: str, f_low: str) -> bool:
    """True when a sentence asserts a schema-altering migration is REQUIRED.

    Covers both directions of the false claim: "a migration must ADD column X"
    and "a migration must REMOVE field X" — for a field the diff only
    relocated to an abstract base (column already exists, no schema change).
    Sentences that already deny necessity or hedge are not flagged.
    """
    if _NEGATED_CONTEXT.search(sentence):
        return False
    if f_low not in sentence or not _MIGRATION_SIGNAL.search(sentence):
        return False
    return bool(_ACTION_DEMAND.search(sentence))


def audit_review_against_invariants(
    review_text: str, inv: DiffInvariants, key_file_context: str
) -> str:
    """Flag review claims that contradict the machine-derived invariants.

    Catches the classic false finding in both directions: the diff REMOVES a
    model field (relocating it to an abstract base), but the review demands a
    schema-altering migration — either to ADD the column (which already
    exists) or to RemoveField it (which would DROP the existing column and
    destroy data). Sentences that already acknowledge the column exists / no
    migration is needed are not flagged.
    """
    if not inv.field_changes:
        return ""
    abs_fields = abstract_base_fields(key_file_context)
    existing_columns = inv.moved_fields | (
        {c.field for c in inv.field_changes if c.kind == "removed"} & set(abs_fields)
    )
    if not existing_columns:
        return ""

    sentences = _split_sentences(review_text)
    notes: list[str] = []
    for f in sorted(existing_columns):
        f_low = f.lower()
        for sent in sentences:
            s = sent.lower()
            if f_low not in s:
                continue
            if not _demands_schema_migration(s, f_low):
                continue
            source = abs_fields.get(f, "an abstract base")
            notes.append(
                f"- Review claims a schema-altering migration is REQUIRED for column "
                f"`{f}` (e.g. AddField/RemoveField), but the diff relocates it to "
                f"abstract base `{source}` — the column already exists and Django's "
                f"flattened model state is unchanged, so no schema-altering "
                f"migration is needed. Applying a RemoveField here would DROP the "
                f"existing column (data loss). This claim contradicts the "
                f"machine-derived diff invariants."
            )
            break

    if not notes:
        return ""
    block = "\n\n## ⚠️ Machine Audit (diff-invariant check)\n"
    block += "The following review claims contradict the machine-derived diff invariants:\n\n"
    block += "\n".join(notes)
    block += "\n\nRe-verify these findings against the raw diff before acting on them."
    return block
