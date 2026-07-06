You are an expert Django developer agent. Your task is to implement the core structural code changes for ADR 0001(/Users/dansparkes/memores/memores-api/docs/adr/0001-replace-user-profile-with-abstractuser.md), transitioning from a split User/Profile model to a single custom User model.

Relying on the fact that `AUTH_USER_MODEL = "memores.User"` will be set, execute the following changes:

1. In `memores/models.py`, define the new `User(AbstractUser)` model exactly as structured in Section 1 of the ADR's Detailed Design. Ensure the table name is explicitly set to `db_table = "memores_user"`.
2. Update `settings.py` to include `AUTH_USER_MODEL = "memores.User"`.
3. Systematically refactor all model files: replace any `ForeignKey` or `OneToOneField` pointing to `Profile` or `User` to use `settings.AUTH_USER_MODEL` instead. Keep the field names as `user` to maintain backward compatibility.
4. Replace all occurrences of `from django.contrib.auth.models import User` with `from django.contrib.auth import get_user_model`. Ensure runtime calls use `get_user_model()` instead of the explicit class where necessary.
5. Fix all type hints, docstrings, and `isinstance` checks identified in the Step 1 audit to use the new User model pattern.
