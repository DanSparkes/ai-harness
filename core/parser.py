# core/parser.py
import ast
import hashlib
import os
import re
from pathlib import Path
from typing import Optional

from core.cache import get as cache_get, set as cache_set, make_key, get_git_head

EXCLUDE_DIRS = {".venv", "__pycache__", ".git", "node_modules", ".tox", "migrations", "fixtures", ".egg-info", ".mypy_cache", ".pytest_cache"}

HTTP_METHOD_MAP = {
    "get": "GET", "list": "GET", "retrieve": "GET",
    "post": "POST", "create": "POST",
    "put": "PUT", "update": "PUT",
    "patch": "PATCH", "partial_update": "PATCH",
    "delete": "DELETE", "destroy": "DELETE",
}

AUTH_KEYWORDS = {"authorize", "check_perm", "has_perm", "is_staff", "is_superuser", "is_authenticated"}

DRF_BUILTIN_PERMISSIONS = {
    "IsAuthenticated", "IsAdminUser", "AllowAny",
    "IsAuthenticatedOrReadOnly", "DjangoModelPermissions",
    "DjangoObjectPermissions", "BasePermission",
}

READ_ONLY_VIEW_BASES = {"ListAPIView", "RetrieveAPIView", "ReadOnlyModelViewSet",
                         "ListModelMixin", "RetrieveModelMixin"}


class DjangoTopographer:
    """Parses a Django project to create a structural map, safely ignoring fixture directories."""
    def __init__(self, target_dir: str):
        self.target_dir = Path(target_dir).resolve()
        self._model_base_names = self._discover_model_base_names()

    def _find_app_dirs(self) -> list[Path]:
        """Find Django app directories (containing models.py)."""
        app_dirs = []
        for root, dirs, files in os.walk(self.target_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            if "models.py" in files:
                app_dirs.append(Path(root))
        return app_dirs

    def _discover_model_base_names(self) -> set:
        """Discover model base class names by finding direct models.Model subclasses."""
        model_bases = set()
        settings_path = self.target_dir / "memores" / "settings.py"
        if settings_path.exists():
            try:
                with open(settings_path, encoding="utf-8") as f:
                    content = f.read()
                for m in re.finditer(r"^\s*AUTH_USER_MODEL\s*=\s*['\"](.+?)['\"]", content, re.M):
                    model_bases.add(m.group(1).split(".")[-1])
            except Exception:
                pass
        for app_dir in self._find_app_dirs():
            for root, _dirs, files in os.walk(app_dir):
                for f in files:
                    if f.endswith(".py") and not f.startswith("test_"):
                        try:
                            with open(Path(root) / f, encoding="utf-8") as fh:
                                node = ast.parse(fh.read())
                            for child in node.body:
                                if not isinstance(child, ast.ClassDef):
                                    continue
                                is_models_model = any(
                                    isinstance(b, ast.Attribute)
                                    and isinstance(b.value, ast.Name)
                                    and b.value.id == "models"
                                    and b.attr == "Model"
                                    for b in child.bases
                                )
                                if is_models_model:
                                    model_bases.add(child.name)
                        except Exception:
                            pass
        return model_bases

    def scan_project(self) -> dict:
        head = get_git_head(str(self.target_dir))
        if head:
            key = make_key("parser:scan_project", str(self.target_dir), head)
            cached = cache_get(key, max_age=86400)
            if cached is not None:
                return cached

        topology = {"serializers": [], "views": [], "models": []}
        for app_dir in self._find_app_dirs():
            for root, dirs, files in os.walk(app_dir):
                if "fixtures" in dirs:
                    dirs.remove("fixtures")
                for file in files:
                    if file.endswith(".py") and not file.startswith("test_"):
                        full_path = Path(root) / file
                        self._parse_file(full_path, topology)

        if head:
            cache_set(key, topology)
        return topology

    def _resolve_ast_value(self, node):
        """Recursively resolve an AST node to a simplified Python value."""
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.List):
            return [self._resolve_ast_value(e) for e in node.elts]
        elif isinstance(node, ast.Tuple):
            return [self._resolve_ast_value(e) for e in node.elts]
        elif isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            base = self._resolve_ast_value(node.value)
            if isinstance(base, str):
                return f"{base}.{node.attr}"
            return node.attr
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            return self._resolve_ast_value(node.func)
        else:
            return f"<unresolved: {type(node).__name__}>"

    def _resolve_meta_fields(self, value_node, class_name: str, file_classes: dict):
        """Resolve a Meta.fields value, including inheritance patterns like Parent.Meta.fields + [...].

        Recursively resolves:
          - Direct lists: ['id', 'name']
          - String literals: '__all__'
          - Name references: some_variable
          - Binary ops: Parent.Meta.fields + ['extra']  (chained inheritance)
        """
        if isinstance(value_node, ast.List):
            return [e.value for e in value_node.elts if isinstance(e, ast.Constant)]

        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            return value_node.value

        if isinstance(value_node, ast.Name):
            return value_node.id

        # Pattern: ParentClass.Meta.fields (direct reference)
        if (isinstance(value_node, ast.Attribute) and value_node.attr == "fields"
                and isinstance(value_node.value, ast.Attribute) and value_node.value.attr == "Meta"):
            parent_name = self._resolve_ast_value(value_node.value.value)
            if isinstance(parent_name, str) and parent_name in file_classes:
                parent_class = file_classes[parent_name]
                return self._find_meta_fields(parent_class, parent_name, file_classes)

        # Pattern: ParentClass.Meta.fields + ["extra"]  (or similar BinOp)
        if isinstance(value_node, ast.BinOp) and isinstance(value_node.op, ast.Add):
            left = value_node.left
            right = value_node.right

            left_fields = self._resolve_meta_fields(left, class_name, file_classes)
            right_fields = self._resolve_meta_fields(right, class_name, file_classes)
            if isinstance(left_fields, list) and isinstance(right_fields, list):
                return left_fields + right_fields

            return f"<inherited: {ast.dump(value_node)}>"

        return f"<unresolved: {ast.dump(value_node)}>"

    def _find_meta_fields(self, class_node: ast.ClassDef, class_name: str, file_classes: dict):
        """Find the 'fields' attribute inside a class's Meta inner class."""
        for item in class_node.body:
            if isinstance(item, ast.ClassDef) and item.name == "Meta":
                for meta_item in item.body:
                    if isinstance(meta_item, ast.Assign):
                        for target in meta_item.targets:
                            if isinstance(target, ast.Name) and target.id == "fields":
                                return self._resolve_meta_fields(meta_item.value, class_name, file_classes)
        return None

    def _is_method_stub(self, func_def: ast.FunctionDef) -> tuple[bool, str | None]:
        """Check if a method body is a stub with no real logic.

        Returns (is_stub, reason) where reason describes why.
        """
        body = func_def.body
        if not body:
            return True, "empty body"

        if len(body) == 1:
            stmt = body[0]
            if isinstance(stmt, ast.Pass):
                return True, "pass"
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
                return True, "ellipsis"
            if isinstance(stmt, ast.Raise):
                exc_name = None
                if isinstance(stmt.exc, ast.Call) and isinstance(stmt.exc.func, ast.Name):
                    exc_name = stmt.exc.func.id
                elif isinstance(stmt.exc, ast.Name):
                    exc_name = stmt.exc.id
                if exc_name in ("NotImplementedError", "Http404"):
                    return True, f"raises {exc_name}"

        if len(body) <= 2:
            for stmt in body:
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Call):
                    func = stmt.value.func
                    if isinstance(func, ast.Name) and func.id == "HttpResponseNotAllowed":
                        return True, "returns HttpResponseNotAllowed"
                    if isinstance(func, ast.Attribute) and func.attr == "HttpResponseNotAllowed":
                        return True, "returns HttpResponseNotAllowed"

        return False, None

    def _find_inline_auth_calls(self, func_def: ast.FunctionDef) -> list[str]:
        """Scan a method body for authorization-related function calls.

        Returns list of function names found (e.g., ['authorize_superuser']).
        """
        found = set()
        for node in ast.walk(func_def):
            if isinstance(node, ast.Call):
                candidates = []
                if isinstance(node.func, ast.Name):
                    candidates.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    candidates.append(node.func.attr)
                for name in candidates:
                    base = name.lower().replace("_", "")
                    if any(kw in base for kw in AUTH_KEYWORDS):
                        found.add(name)
        return sorted(found)

    def _parse_imports(self, tree: ast.Module, current_file: Path) -> dict[str, dict]:
        """Parse import statements from a module's AST.

        Returns dict mapping imported name -> {module, name, alias}
        For `from X import Y as Z`, key is Z (or Y if no alias).
        For `import X`, key is X with no 'name' field.
        """
        imports = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    key = alias.asname or alias.name
                    imports[key] = {"module": alias.name, "alias": alias.asname}
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    key = alias.asname or alias.name
                    imports[key] = {"module": module, "name": alias.name, "alias": alias.asname}
        return imports

    def _resolve_module_to_path(self, module: str) -> Path | None:
        """Resolve a Python dotted module path to a .py file path."""
        parts = module.split(".")
        # Try as module.py
        candidate = self.target_dir.joinpath(*parts).with_suffix(".py")
        if candidate.exists():
            return candidate
        # Try as package/__init__.py
        candidate = self.target_dir.joinpath(*parts) / "__init__.py"
        if candidate.exists():
            return candidate
        return None

    def _analyze_permission_class(self, class_name: str, imports: dict, current_file: Path) -> dict:
        """Resolve a custom permission class definition and check for object-level auth methods.

        Returns dict with:
          - name: the class name
          - resolved_file: str | None — where the class is defined
          - has_permission: bool — has_permission() defined
          - has_object_permission: bool — has_object_permission() defined
          - is_custom: True if resolved (not a DRF built-in)
        """
        result = {
            "name": class_name,
            "resolved_file": None,
            "has_permission": False,
            "has_object_permission": False,
            "is_custom": False,
        }

        if class_name in DRF_BUILTIN_PERMISSIONS:
            return result

        imp = imports.get(class_name)
        if not imp:
            return result

        module = imp.get("module", "")
        if not module:
            return result

        resolved_path = self._resolve_module_to_path(module)
        if not resolved_path:
            return result

        result["resolved_file"] = str(resolved_path)
        result["is_custom"] = True

        try:
            with open(resolved_path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == class_name:
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            if item.name == "has_permission":
                                result["has_permission"] = True
                            if item.name == "has_object_permission":
                                result["has_object_permission"] = True
        except Exception:
            pass

        return result

    def _is_self_scoped(self, func_def: ast.FunctionDef) -> bool:
        """Check if a method exclusively operates on the authenticated user (request.user)
        and never accepts a user-controllable resource ID from external input.

        This catches the pattern where an APIView handler (patch/post) has no visible
        auth calls but inherently scopes to request.user — no external ID is accepted,
        so an attacker cannot target another user.

        Returns True if the method:
          1. References request.user or self.request.user, and never extracts a user
             identity ID from request data, OR
          2. Accepts request as a parameter, has no ID-like function params or catch-all
             args, and never extracts a user identity ID — this covers inherently
             self-scoped operations like logout(request) where Django's session
             middleware scopes to the current request.
        """
        USER_ID_KEYS = {"pk", "id", "user_id", "profile_id", "resource_id", "target_id", "account_id"}
        ALLOWED_PARAMS = {"self", "request", "format"}

        # Check function signature for explicit ID-like parameters
        func_arg_names = {arg.arg for arg in func_def.args.args}
        has_request_param = "request" in func_arg_names
        has_explicit_id_param = bool((func_arg_names - ALLOWED_PARAMS) & USER_ID_KEYS)
        has_catchall = func_def.args.vararg is not None or func_def.args.kwarg is not None

        uses_request_user = False
        extracts_user_id = False

        for node in ast.walk(func_def):
            if isinstance(node, ast.Attribute) and node.attr == "user":
                if (isinstance(node.value, ast.Name) and node.value.id == "request"):
                    uses_request_user = True
                elif (isinstance(node.value, ast.Attribute)
                      and isinstance(node.value.value, ast.Name)
                      and node.value.value.id == "self"
                      and node.value.attr == "request"):
                    uses_request_user = True

            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                src = node.func.value
                src_name = None
                if isinstance(src, ast.Name):
                    src_name = src.id
                elif isinstance(src, ast.Attribute):
                    src_name = src.attr if isinstance(src, ast.Attribute) else None
                if src_name in ("kwargs", "data", "query_params", "args"):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            if arg.value.lower() in USER_ID_KEYS:
                                extracts_user_id = True

            if isinstance(node, ast.Subscript):
                src = node.value
                src_name = None
                if isinstance(src, ast.Name):
                    src_name = src.id
                elif isinstance(src, ast.Attribute):
                    src_name = src.attr
                if src_name in ("kwargs", "data", "query_params", "args"):
                    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                        if node.slice.value.lower() in USER_ID_KEYS:
                            extracts_user_id = True

        # Self-scoped via request.user reference
        if uses_request_user and not extracts_user_id:
            return True

        # Self-scoped via inherent request scoping (e.g., logout):
        # method accepts request, has no way to target a different user
        if has_request_param and not extracts_user_id and not has_explicit_id_param and not has_catchall:
            return True

        return False

    def _classify_view(self, class_node: ast.ClassDef) -> dict:
        """Classify a view class by its base classes, HTTP methods, and auth patterns.

        Returns dict with:
          - base_classes: list of base class name strings
          - http_methods: list of HTTP methods supported (from non-stub method names)
          - is_read_only: True if only GET is supported
          - method_details: ALL methods including non-HTTP hooks (perform_create,
            perform_destroy, get_queryset, etc.) with inline auth call detection
        """
        base_names = set()
        for b in class_node.bases:
            name = getattr(b, "id", getattr(b, "attr", ""))
            if name:
                base_names.add(name)

        http_methods = set()
        method_details = []
        for item in class_node.body:
            if isinstance(item, ast.FunctionDef):
                http = HTTP_METHOD_MAP.get(item.name)
                is_stub, stub_reason = self._is_method_stub(item)
                auth_calls = self._find_inline_auth_calls(item)
                self_scoped = self._is_self_scoped(item)
                entry = {
                    "name": item.name,
                    "is_stub": is_stub,
                    "stub_reason": stub_reason,
                    "inline_auth_calls": auth_calls,
                    "self_scoped": self_scoped,
                }
                if http:
                    entry["http_method"] = http
                    if not is_stub:
                        http_methods.add(http)
                method_details.append(entry)

        has_read_only_base = bool(base_names & READ_ONLY_VIEW_BASES)
        only_get = http_methods == {"GET"}
        no_write_methods = not (http_methods & {"POST", "PUT", "PATCH", "DELETE"})
        is_read_only = has_read_only_base or (only_get and no_write_methods)

        return {
            "base_classes": sorted(base_names),
            "http_methods": sorted(http_methods),
            "is_read_only": is_read_only,
            "method_details": method_details,
        }

    def _parse_class_body_assignments(self, class_node: ast.ClassDef,
                                        target_names: set[str] | None = None) -> dict:
        """Extract named class-level assignments, optionally filtering by name set.

        Returns dict of {name: resolved_value}
        """
        attrs = {}
        for item in class_node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        if target_names is None or target.id in target_names:
                            attrs[target.id] = self._resolve_ast_value(item.value)
        return attrs

    def _parse_file(self, file_path: Path, topology: dict):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=file_path.name)
            relative_path = file_path.relative_to(self.target_dir)

            # Build class index for cross-reference resolution
            file_classes: dict[str, ast.ClassDef] = {}
            for child in tree.body:
                if isinstance(child, ast.ClassDef):
                    file_classes[child.name] = child

            # Parse imports for permission class resolution
            imports = self._parse_imports(tree, file_path)

            for child in tree.body:
                if not isinstance(child, ast.ClassDef):
                    continue

                base_names = {
                    getattr(b, "id", getattr(b, "attr", ""))
                    for b in child.bases
                }

                is_model = (
                    bool(base_names & self._model_base_names) or
                    any(
                        isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name)
                        and b.value.id == "models" and b.attr == "Model"
                        for b in child.bases
                    )
                )

                if is_model:
                    topology["models"].append({
                        "absolute_path": str(file_path),
                        "relative_path": str(relative_path),
                        "class": child.name,
                        "fields": self._extract_model_fields(child),
                        "methods": [m.name for m in child.body if isinstance(m, ast.FunctionDef)]
                    })

                elif any("Serializer" in name for name in base_names):
                    fields, meta = self._parse_serializer_class(child, file_classes)
                    entry = {
                        "absolute_path": str(file_path),
                        "relative_path": str(relative_path),
                        "class": child.name,
                        "fields": fields,
                    }
                    if meta:
                        entry["meta"] = meta
                    topology["serializers"].append(entry)

                elif any(any(k in name for k in ["View", "ViewSet", "APIView"]) for name in base_names):
                    view_attrs = self._parse_class_body_assignments(
                        child,
                        target_names={"permission_classes", "authentication_classes",
                                      "serializer_class", "queryset", "lookup_field",
                                      "pagination_class", "filter_backends"}
                    )
                    view_classification = self._classify_view(child)
                    methods = view_classification["method_details"]

                    # Analyze custom permission classes
                    permission_analysis = []
                    raw_perms = view_attrs.get("permission_classes", [])
                    if isinstance(raw_perms, list):
                        for pcls_name in raw_perms:
                            if isinstance(pcls_name, str) and pcls_name not in DRF_BUILTIN_PERMISSIONS:
                                analysis = self._analyze_permission_class(pcls_name, imports, file_path)
                                if analysis.get("is_custom"):
                                    permission_analysis.append(analysis)

                    entry = {
                        "absolute_path": str(file_path),
                        "relative_path": str(relative_path),
                        "class": child.name,
                        "class_attributes": view_attrs,
                        "base_classes": view_classification["base_classes"],
                        "http_methods": view_classification["http_methods"],
                        "is_read_only": view_classification["is_read_only"],
                        "methods": methods,
                    }
                    if permission_analysis:
                        entry["permission_class_analysis"] = permission_analysis
                    topology["views"].append(entry)

        except Exception:
            pass

    def _extract_model_fields(self, class_node: ast.ClassDef) -> list[dict]:
        """Extract model field declarations with type and key attributes."""
        fields = []
        for item in class_node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        field_info = {"name": target.id}
                        if isinstance(item.value, ast.Call) and hasattr(item.value.func, "attr"):
                            field_info["type"] = item.value.func.attr
                            for kw in item.value.keywords:
                                if kw.arg in ("null", "default", "unique", "db_index", "blank", "primary_key", "editable"):
                                    try:
                                        field_info[kw.arg] = ast.literal_eval(kw.value)
                                    except (ValueError, TypeError):
                                        field_info[kw.arg] = ast.dump(kw.value)
                        fields.append(field_info)
        return fields

    def _parse_serializer_class(self, class_node: ast.ClassDef, file_classes: dict) -> tuple[list[dict], dict | None]:
        fields = []
        meta = None
        for item in class_node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        field_info = {"name": target.id}
                        if isinstance(item.value, ast.Call):
                            # Capture type from both Attribute (serializers.UUIDField)
                            # and Name (SimpleProfileSerializer) function calls
                            if hasattr(item.value.func, "attr"):
                                field_info["type"] = item.value.func.attr
                            elif isinstance(item.value.func, ast.Name):
                                field_info["type"] = item.value.func.id
                            for kw in item.value.keywords:
                                if kw.arg in ("required", "read_only", "allow_null", "allow_blank"):
                                    try:
                                        field_info[kw.arg] = ast.literal_eval(kw.value)
                                    except (ValueError, TypeError):
                                        field_info[kw.arg] = ast.dump(kw.value)
                        fields.append(field_info)
            elif isinstance(item, ast.ClassDef) and item.name == "Meta":
                meta = {"class": "Meta"}
                for meta_item in item.body:
                    if isinstance(meta_item, ast.Assign):
                        for target in meta_item.targets:
                            if isinstance(target, ast.Name):
                                if target.id in ("fields", "exclude", "read_only_fields"):
                                    meta[target.id] = self._resolve_meta_fields(
                                        meta_item.value, class_node.name, file_classes
                                    )
                                elif target.id == "model":
                                    if isinstance(meta_item.value, ast.Name):
                                        meta["model"] = meta_item.value.id
        return fields, meta


def _walk_py_files(target_dir: str):
    """Generator yielding (root, rel_path) for every .py file in the project."""
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py") and not f.startswith("test_"):
                yield root, os.path.relpath(os.path.join(root, f), target_dir)


def scan_file_tree(target_dir: str) -> list[str]:
    head = get_git_head(target_dir)
    if head:
        key = make_key("parser:scan_file_tree", target_dir, head)
        cached = cache_get(key, max_age=86400)
        if cached is not None:
            return cached
    paths = [rel for _, rel in _walk_py_files(target_dir)]
    paths.sort()
    if head:
        cache_set(key, paths)
    return paths


def scan_celery_tasks(target_dir: str) -> list[dict]:
    head = get_git_head(target_dir)
    if head:
        key = make_key("parser:scan_celery_tasks", target_dir, head)
        cached = cache_get(key, max_age=86400)
        if cached is not None:
            return cached
    results = []
    for root, rel in _walk_py_files(target_dir):
        path = os.path.join(root, os.path.basename(rel) if os.path.dirname(rel) == "." else rel)
        try:
            with open(os.path.join(target_dir, rel), "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue
        for match in re.finditer(
            r"^(@(?:shared_)?task\(.*?\))\s*$.*?^def (\w+)\s*\(",
            content, re.MULTILINE | re.DOTALL
        ):
            results.append({
                "file": rel,
                "decorator": match.group(1).strip(),
                "function": match.group(2).strip(),
            })
    if head:
        cache_set(key, results)
    return results


def scan_files_by_keyword(target_dir: str, keyword: str, max_lines: int = 80) -> list[dict]:
    head = get_git_head(target_dir)
    if head:
        key = make_key("parser:scan_files_by_keyword", target_dir, head, keyword, str(max_lines))
        cached = cache_get(key, max_age=86400)
        if cached is not None:
            return cached
    results = []
    kw_lower = keyword.lower()
    for root, rel in _walk_py_files(target_dir):
        if kw_lower not in rel.lower():
            continue
        try:
            with open(os.path.join(target_dir, rel), "r", encoding="utf-8") as f:
                lines = "".join(f.readlines()[:max_lines])
        except Exception:
            continue
        results.append({"file": rel, "content": lines})
    if head:
        cache_set(key, results)
    return results


def scan_files_by_pattern(target_dir: str, patterns: list[str], max_lines: int = 80) -> list[dict]:
    head = get_git_head(target_dir)
    if head:
        patterns_key = ",".join(sorted(patterns))
        key = make_key("parser:scan_files_by_pattern", target_dir, head, patterns_key, str(max_lines))
        cached = cache_get(key, max_age=86400)
        if cached is not None:
            return cached
    results = []
    for root, rel in _walk_py_files(target_dir):
        if not any(p in rel for p in patterns):
            continue
        try:
            with open(os.path.join(target_dir, rel), "r", encoding="utf-8") as f:
                lines = "".join(f.readlines()[:max_lines])
        except Exception:
            continue
        results.append({"file": rel, "content": lines})
    if head:
        cache_set(key, results)
    return results


def format_scan_results(file_tree: list[str],
                         celery_tasks: list[dict],
                         keyword_matches: list[dict],
                         pattern_matches: list[dict]) -> str:
    """Format all scan results into a single text block for the LLM prompt."""
    parts = []

    parts.append("=== PROJECT FILE TREE (all .py files, relative paths) ===")
    parts.append("\n".join(file_tree) if file_tree else "(empty)")

    if celery_tasks:
        parts.append("\n=== CELERY TASK DECORATORS ===")
        for t in celery_tasks:
            parts.append(f"  {t['file']}:")
            parts.append(f"    {t['decorator']}")
            parts.append(f"    def {t['function']}(")

    for label, matches in [("KEYWORD", keyword_matches), ("PATTERN", pattern_matches)]:
        if matches:
            parts.append(f"\n=== {label} FILE MATCHES (first 80 lines) ===")
            for m in matches:
                parts.append(f"--- {m['file']} ---")
                parts.append(m['content'].rstrip())

    return "\n".join(parts)
