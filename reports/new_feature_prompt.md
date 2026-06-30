## 1. Codebase Target Map
- `memores/serializers/base_serializers.py`: Add `JSONApiSerializer` base class.
- `memores/serializers/journal_serializers.py`: Refactor `JournalEntryListSerializer`, `JournalEntryCreateSerializer`, and `JournalEntryDetailSerializer` to inherit from `JSONApiSerializer`.

## 2. Architecture & Design

We introduce `JSONApiSerializer` in `base_serializers.py` to encapsulate the shared JSON:API field conventions. Children use DRF's native `Meta` class inheritance (`class Meta(JSONApiSerializer.Meta)`) and extend `fields` via list concatenation. No `__init_subclass__`, no mutation of class state -- standard, predictable DRF patterns only.

```python
# memores/serializers/base_serializers.py
from rest_framework import serializers


class JSONApiSerializer(serializers.ModelSerializer):
    class Meta:
        fields = ["id", "description", "context", "emotion", "date", "timestamp"]
        read_only_fields = ["id", "timestamp"]
```

In `journal_serializers.py`, each child declares its unique fields while inheriting the base's `Meta.fields`, `Meta.read_only_fields`, and everything else through standard Python class inheritance.

```python
# memores/serializers/journal_serializers.py
from memores.serializers.base_serializers import JSONApiSerializer


class JournalEntryListSerializer(JSONApiSerializer):
    has_analysis = serializers.SerializerMethodField()
    has_advanced_analysis = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta(JSONApiSerializer.Meta):
        model = JournalEntry
        fields = JSONApiSerializer.Meta.fields + [
            "has_analysis", "has_advanced_analysis", "status",
        ]

    # get_has_analysis, get_has_advanced_analysis, get_status unchanged
```

## 3. Risk Assessment & Mitigations
- **Backwards Compatibility (Field Order)**: Base fields appear first (explicitly concatenated before unique fields), matching the current order exactly.
- **Read-Only Constraints**: `Meta(JSONApiSerializer.Meta)` inherits `read_only_fields = ["id", "timestamp"]` automatically. Children don't need to redeclare it.
- **Custom SerializerMethodFields**: Unchanged -- they're declared on the class body, not in Meta.
- **Migration/DB Impact**: Zero. Purely a serializer-level refactoring.
- **Testing Alignment**: Existing tests pass as long as they validate field presence, not strict positional indices.

## 4. Implementation Pipeline
```json
{
  "feature_name": "Refactor JournalEntry Serializers with JSONApiSerializer Base Class",
  "target_workspace": "/Users/dansparkes/memores/memores-api",
  "pipeline": [
    {
      "step": 1,
      "name": "Add JSONApiSerializer to base_serializers.py",
      "target_file": "memores/serializers/base_serializers.py",
      "task": "Append the JSONApiSerializer class definition to memores/serializers/base_serializers.py. The class must inherit from serializers.ModelSerializer and define a Meta class with fields = [\"id\", \"description\", \"context\", \"emotion\", \"date\", \"timestamp\"] and read_only_fields = [\"id\", \"timestamp\"]. No __init_subclass__ or metaclass logic. Do NOT set model in Meta.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax", "run_mypy"],
      "max_attempts": 4
    },
    {
      "step": 2,
      "name": "Refactor JournalEntryListSerializer",
      "target_file": "memores/serializers/journal_serializers.py",
      "task": "Import JSONApiSerializer from memores.serializers.base_serializers. Change Meta to inherit from JSONApiSerializer.Meta (class Meta(JSONApiSerializer.Meta)). Set fields = JSONApiSerializer.Meta.fields + [\"has_analysis\", \"has_advanced_analysis\", \"status\"]. Set model = JournalEntry in Meta. Remove read_only_fields from Meta (inherited from base). Preserve all SerializerMethodField definitions and get_* methods exactly as they are.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax", "run_mypy"],
      "max_attempts": 4
    },
    {
      "step": 3,
      "name": "Refactor JournalEntryCreateSerializer",
      "target_file": "memores/serializers/journal_serializers.py",
      "task": "Change Meta to inherit from JSONApiSerializer.Meta (class Meta(JSONApiSerializer.Meta)). Set fields = JSONApiSerializer.Meta.fields + [\"user\"]. Set model = JournalEntry in Meta. Remove read_only_fields from Meta (inherited from base). Preserve the validate_description method exactly as is.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax", "run_mypy"],
      "max_attempts": 4
    },
    {
      "step": 4,
      "name": "Refactor JournalEntryDetailSerializer",
      "target_file": "memores/serializers/journal_serializers.py",
      "task": "Change Meta to inherit from JSONApiSerializer.Meta (class Meta(JSONApiSerializer.Meta)). Set fields = JSONApiSerializer.Meta.fields + [\"analysis\", \"advanced_analysis\", \"status\"]. Set model = JournalEntry in Meta. Remove read_only_fields from Meta (inherited from base). Preserve all SerializerMethodField definitions and get_* methods exactly as they are.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax", "run_mypy"],
      "max_attempts": 4
    }
  ]
}
```
