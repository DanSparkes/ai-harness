# core/parser.py
import ast
import os
import re
from pathlib import Path
from typing import Optional

EXCLUDE_DIRS = {".venv", "__pycache__", ".git", "node_modules", ".tox", "migrations", "fixtures", ".egg-info", ".mypy_cache", ".pytest_cache"}


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
        topology = {"serializers": [], "views": [], "models": []}
        for app_dir in self._find_app_dirs():
            for root, dirs, files in os.walk(app_dir):
                if "fixtures" in dirs:
                    dirs.remove("fixtures")
                for file in files:
                    if file.endswith(".py") and not file.startswith("test_"):
                        full_path = Path(root) / file
                        self._parse_file(full_path, topology)
        return topology

    def _parse_file(self, file_path: Path, topology: dict):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                node = ast.parse(f.read(), filename=file_path.name)
            relative_path = file_path.relative_to(self.target_dir)
            for child in node.body:
                if isinstance(child, ast.ClassDef):
                    base_names = {
                        getattr(b, "id", getattr(b, "attr", ""))
                        for b in child.bases
                    }
                    is_model = (
                        bool(base_names & self._model_base_names) or
                        any(
                            isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name) and b.value.id == "models" and b.attr == "Model"
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
                    elif any("Serializer" in getattr(b, "id", "") for b in child.bases):
                        topology["serializers"].append({
                            "absolute_path": str(file_path),
                            "relative_path": str(relative_path),
                            "class": child.name,
                            "fields": self._get_class_assignments(child)
                        })
                    elif any(any(k in getattr(b, "id", "") for k in ["View", "ViewSet", "APIView"]) for b in child.bases):
                        topology["views"].append({
                            "absolute_path": str(file_path),
                            "relative_path": str(relative_path),
                            "class": child.name,
                            "methods": [m.name for m in child.body if isinstance(m, ast.FunctionDef)]
                        })
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
                                if kw.arg in ("null", "default", "unique", "db_index", "blank"):
                                    try:
                                        field_info[kw.arg] = ast.literal_eval(kw.value)
                                    except (ValueError, TypeError):
                                        field_info[kw.arg] = ast.dump(kw.value)
                        fields.append(field_info)
        return fields

    def _get_class_assignments(self, class_node: ast.ClassDef) -> list:
        fields = []
        for item in class_node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        fields.append(target.id)
        return fields


def _walk_py_files(target_dir: str):
    """Generator yielding (root, rel_path) for every .py file in the project."""
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py") and not f.startswith("test_"):
                yield root, os.path.relpath(os.path.join(root, f), target_dir)


def scan_file_tree(target_dir: str) -> list[str]:
    """Return a sorted list of relative paths for all .py files in the project."""
    paths = [rel for _, rel in _walk_py_files(target_dir)]
    paths.sort()
    return paths


def scan_celery_tasks(target_dir: str) -> list[dict]:
    """Find Celery task definitions with their decorator and function name."""
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
    return results


def scan_files_by_keyword(target_dir: str, keyword: str, max_lines: int = 80) -> list[dict]:
    """Find files matching a keyword in their path and return their first N lines."""
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
    return results


def scan_files_by_pattern(target_dir: str, patterns: list[str], max_lines: int = 80) -> list[dict]:
    """Show first N lines of files matching any of the given substrings in their relative path."""
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
