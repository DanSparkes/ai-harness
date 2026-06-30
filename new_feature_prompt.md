Audit the JournalEntry serializers in memores/serializers/journal_serializers.py
(JournalEntryListSerializer, JournalEntryCreateSerializer, JournalEntryDetailSerializer)
and the UserCourseCompletion serializers in memores/serializers/user_course_completion_serializers.py
(UserCourseCompletionSerializer, UserCourseCompletionCreateUpdateSerializer).

Both groups have significant field duplication: all three JournalEntry serializers share
["id", "description", "context", "emotion", "date", "timestamp"] with read_only on
["id", "timestamp"], and both UserCourseCompletion serializers list overlapping id/
timestamp fields.

Refactor as follows:

1. Create a shared mixin/base serializer class called JSONApiSerializer (or similar) in
   memores/serializers/base_serializers.py that provides common DRF-JSON:API conventions:
   - id and timestamp read_only fields exposed automatically
   - A Meta option like `common_fields` that any child can extend via inheritance

2. Use Django ModelSerializer Meta.fields inheritance to reduce duplication:
   - Define a private _JournalEntryCommonFields meta sub-class or use the base serializer's
     __init_subclass__ / prepare_fields pattern so child serializers only declare their own
     unique fields and call `super().Meta.fields + [...custom...]`
   - Extract a common `_base_fields = ["id", "description", "context", "emotion", "date", "timestamp"]`
     that all three JournalEntry serializers reference via inheritance instead of copy-paste.

3. Ensure the refactored code:
   - Preserves every existing field, read_only constraint, and custom SerializerMethodField
   - Passes pre-commit (ruff, black, isort, mypy)
   - Passes existing serializer tests

4. Do NOT refactor the UserCourseCompletion serializers yet — those belong to a separate
   audit pass. Focus only on JournalEntry.
