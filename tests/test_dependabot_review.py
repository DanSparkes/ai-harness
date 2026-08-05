"""Tests for JS/npm + Python/PyPI support in dependabot_review.py.

Covers:
- package.json (npm) version parsing
- pyproject.toml / requirements.txt (PyPI) version parsing
- registry tagging carried through grouping (per-package routing)
- polyglot diffs routing packages to the correct registry
- pyproject pin extraction fallback
- changelog version-coverage heuristic + registry-gated fallbacks
- GitHub blob -> raw URL conversion
- import-path extraction from usage scans

These tests are network-free: they exercise pure logic only.
"""

from dependabot_review import (
    changelog_covers,
    classify_semver,
    extract_owner_repo,
    extract_pyproject_pins,
    get_import_file_paths,
    get_package_changelog,
    github_blob_to_raw,
    group_package_changes,
    parse_package_version_changes,
    parse_python_version_changes,
)

NPM_DIFF = """diff --git a/package.json b/package.json
index 1111111..2222222 100644
--- a/package.json
+++ b/package.json
@@ -15,8 +15,8 @@
   },
   "dependencies": {
-    "i18next": "^23.11.5",
+    "i18next": "^24.0.0",
-    "axios": "^1.6.0",
+    "axios": "^1.7.2",
     "react": "^18.2.0",
   }
 }
"""

PYPROJECT_DIFF = """diff --git a/pyproject.toml b/pyproject.toml
@@ -19,11 +19,11 @@ dependencies = [
     "daphne==4.2.3",
     "celery==5.6.3",
-    "django[argon2]==5.2.15",
-    "dj-database-url==2.2.0",
+    "django[argon2]==6.0.7",
+    "dj-database-url==3.1.2",
     "django-celery-beat==2.9.0",
-    "faker==38.2.0",
-    "gunicorn==25.3.0",
+    "faker==40.36.0",
+    "gunicorn==26.0.0",
@@ -47,13 +47,13 @@ dependencies = [
-    "redis==7.1.0",
+    "redis==8.1.0",
-    "stripe==14.0.1",
+    "stripe==15.4.0",
-    "reportlab==4.4.9",
+    "reportlab==5.0.0",
     "uvicorn==0.52.0",
]
"""


def test_parse_python_version_changes_pyproject():
    changes = parse_python_version_changes(PYPROJECT_DIFF)
    names = {c["package"] for c in changes}
    assert names == {
        "django",
        "dj-database-url",
        "faker",
        "gunicorn",
        "redis",
        "stripe",
        "reportlab",
    }
    # Extras brackets must be stripped so the package maps to the PyPI name,
    # and both the removed (old) and added (new) sides are captured.
    django_entries = [c for c in changes if c["package"] == "django"]
    assert {c["version"] for c in django_entries} == {"5.2.15", "6.0.7"}
    assert {c["change"] for c in django_entries} == {"removed", "added"}
    faker_entries = [c for c in changes if c["package"] == "faker"]
    assert faker_entries == [
        {
            "package": "faker",
            "version": "38.2.0",
            "change": "removed",
            "line": '-    "faker==38.2.0",',
            "registry": "pypi",
        },
        {
            "package": "faker",
            "version": "40.36.0",
            "change": "added",
            "line": '+    "faker==40.36.0",',
            "registry": "pypi",
        },
    ]


def test_parse_python_version_changes_ignores_unchanged_lines():
    changes = parse_python_version_changes(PYPROJECT_DIFF)
    names = [c["package"] for c in changes]
    assert "daphne" not in names
    assert "celery" not in names


def test_parse_python_version_changes_requirements_txt():
    diff = """diff --git a/requirements.txt b/requirements.txt
@@ -1,3 +1,3 @@
-django==5.2.15
+django==6.0.7
 stripe==14.0.1
"""
    changes = parse_python_version_changes(diff)
    by_name = {c["package"]: c for c in changes}
    assert by_name["django"]["version"] == "6.0.7"
    assert by_name["django"]["change"] == "added"
    # Unchanged line must not appear.
    assert "stripe" not in by_name


def test_python_changes_group_and_classify():
    raw = parse_python_version_changes(PYPROJECT_DIFF)
    grouped = group_package_changes(raw)
    redis = next(g for g in grouped if g["package"] == "redis")
    assert redis["old_version"] == "7.1.0"
    assert redis["new_version"] == "8.1.0"
    assert redis["change_type"] == "major"
    django = next(g for g in grouped if g["package"] == "django")
    assert django["change_type"] == "major"


def test_extract_pyproject_pins():
    content = """dependencies = [
    "django[argon2]==6.0.7",
    "dj-database-url==3.1.2",
    "msgpack>=1.2.1",
]
"""
    pins = extract_pyproject_pins(content)
    assert pins["django"] == "6.0.7"
    assert pins["dj-database-url"] == "3.1.2"
    # Non-pinned entries are ignored.
    assert "msgpack" not in pins


def test_changelog_covers():
    stale = "## 3.5.3\nFixes for the 3.x line.\n"
    current = "## 40.36.0\n# Changelog\n## 40.35.0\n"
    assert changelog_covers(stale, "8.1.0") is False
    assert changelog_covers(current, "40.36.0") is True
    assert changelog_covers("", "8.1.0") is False
    assert changelog_covers(current, None) is False


def test_github_blob_to_raw():
    assert (
        github_blob_to_raw(
            "https://github.com/jazzband/dj-database-url/blob/master/CHANGELOG.md"
        )
        == "https://raw.githubusercontent.com/jazzband/dj-database-url/master/CHANGELOG.md"
    )
    assert (
        github_blob_to_raw("https://docs.djangoproject.com/en/stable/releases/") is None
    )


def test_extract_owner_repo():
    assert extract_owner_repo("https://github.com/django/django") == (
        "django",
        "django",
    )
    assert extract_owner_repo("git+https://github.com/redis/redis-py.git") == (
        "redis",
        "redis-py",
    )
    assert extract_owner_repo("https://www.reportlab.com/") == (None, None)


def test_get_import_file_paths_python_paths():
    usage = """### Import Statements
  memores/models.py:10:from django_countries.fields import CountryField
  memores/serializers/user_serializers.py:6:from django_countries.serializer_fields import CountryField
### Config File References
  pyproject.toml:26:    "django-countries==9.0.0",
  STDERR: something went wrong
"""
    paths = get_import_file_paths(usage)
    assert "memores/models.py" in paths
    assert "memores/serializers/user_serializers.py" in paths
    assert "pyproject.toml" in paths
    # STDERR noise must not be treated as a file path.
    assert not any("STDERR" in p for p in paths)


def test_classify_semver_handles_python_versions():
    assert classify_semver("5.2.15", "6.0.7") == "major"
    assert classify_semver("2.2.0", "3.1.2") == "major"
    assert classify_semver("25.3.0", "26.0.0") == "major"


# ── JS / npm path ────────────────────────────────────────────────────────────


def test_parse_package_version_changes_npm():
    changes = parse_package_version_changes(NPM_DIFF)
    grouped = group_package_changes(changes)
    by_name = {g["package"]: g for g in grouped}
    assert set(by_name) == {"i18next", "axios"}
    assert by_name["i18next"]["old_version"] == "^23.11.5"
    assert by_name["i18next"]["new_version"] == "^24.0.0"
    assert by_name["i18next"]["change_type"] == "major"
    # npm packages must be tagged for the npm registry.
    assert by_name["i18next"]["registry"] == "npm"
    assert by_name["axios"]["registry"] == "npm"


def test_parse_package_version_changes_ignores_non_package_json():
    # The python diff contains quoted "==X" lines but is in pyproject.toml,
    # so the npm parser must not pick them up.
    changes = parse_package_version_changes(PYPROJECT_DIFF)
    assert changes == []


# ── Polyglot / per-package routing ───────────────────────────────────────────


def test_polyglot_diff_routes_each_package_to_correct_registry():
    polyglot = NPM_DIFF + "\n" + PYPROJECT_DIFF
    raw = parse_package_version_changes(polyglot) + parse_python_version_changes(
        polyglot
    )
    grouped = {g["package"]: g for g in group_package_changes(raw)}
    assert grouped["i18next"]["registry"] == "npm"
    assert grouped["axios"]["registry"] == "npm"
    assert grouped["django"]["registry"] == "pypi"
    assert grouped["redis"]["registry"] == "pypi"


# ── Changelog fallback registry gating ───────────────────────────────────────


def test_get_package_changelog_js_does_not_use_pypi(monkeypatch):
    """A JS package must never resolve to a PyPI page or npm-README fallback
    for a python package's name."""
    import dependabot_review as d

    seen_urls = []

    def fake_fetch_url(url):
        seen_urls.append(url)
        return ""

    monkeypatch.setattr(d, "fetch_changelog_raw", lambda *a, **k: "")
    monkeypatch.setattr(d, "fetch_github_releases", lambda *a, **k: "")
    monkeypatch.setattr(d, "fetch_url_text", fake_fetch_url)
    monkeypatch.setattr(d, "fetch_npm_readme", lambda *a, **k: "")

    result = get_package_changelog(
        "axios", "https://github.com/axios/axios", new_version="1.7.2", is_python=False
    )
    assert result["content"] == ""
    # No PyPI URL must ever be probed for an npm package.
    assert not any("pypi.org" in u for u in seen_urls)


def test_get_package_changelog_python_fallback_is_pypi(monkeypatch):
    """A Python package with no resolvable changelog must fall back to PyPI,
    not to the npm registry README."""
    import dependabot_review as d

    seen_urls = []

    def fake_fetch_url(url):
        seen_urls.append(url)
        return ""

    monkeypatch.setattr(d, "fetch_changelog_raw", lambda *a, **k: "")
    monkeypatch.setattr(d, "fetch_github_releases", lambda *a, **k: "")
    monkeypatch.setattr(d, "fetch_url_text", fake_fetch_url)
    monkeypatch.setattr(d, "fetch_npm_readme", lambda *a, **k: "")

    result = get_package_changelog(
        "some-py-pkg", "", new_version="1.0.0", is_python=True
    )
    # The PyPI release-history URL must have been probed.
    assert any("pypi.org/project/some-py-pkg" in u for u in seen_urls)
    assert result["url"] == "https://pypi.org/project/some-py-pkg"
