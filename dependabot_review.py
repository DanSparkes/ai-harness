import argparse
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from core.git_provider import GitDiffProvider
from core.judge import AutomatedEvaluator
from core.mcp_orchestrator import init_orchestrator
from core.parser import minify_markdown
from core.runner import StatefulHarnessRunner
from core.warehouse import HarnessWarehouse

# ==============================================================================
# MODEL & API CONFIGURATION
# ==============================================================================
USE_GEMINI = os.getenv("USE_GEMINI", "").lower() in ("1", "true", "yes")

CLOUD_MODEL = "gemini-2.5-flash"
LOCAL_MODEL = "ornith:35b"

REASONING_ARCHITECT = CLOUD_MODEL if USE_GEMINI else LOCAL_MODEL
ARCHITECT_API_BASE = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
    if USE_GEMINI
    else "http://localhost:11434"
)
ARCHITECT_API_KEY = os.getenv("GEMINI_API_KEY") if USE_GEMINI else None

FALLBACK_REVIEWER = "gemini-2.5-flash"
HEAVY_REVIEWER = "deepseek-r1:14b"
LOCAL_JUDGE = "qwen3-coder:latest"
# ==============================================================================

TARGET_REPO = os.environ.get("TARGET_REPO")
MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.python.json")

_mcp_orch = None


def init_mcp(repo_path: str | None = None, config_path: str | None = None):
    global _mcp_orch
    if _mcp_orch is not None:
        return _mcp_orch
    cfg_path = config_path or MCP_CONFIG_PATH
    path = repo_path or TARGET_REPO or os.getcwd()
    orch = init_orchestrator(cfg_path, path)
    if orch:
        _mcp_orch = orch
    return orch


def build_mcp_context() -> str:
    orch = _mcp_orch
    if not orch:
        return ""
    return orch.build_mcp_context_block(
        tags=["dependabot_review", "architectural_rule"]
    )


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Local-Cloud Hybrid Dependabot Review Engine"
    )
    parser.add_argument(
        "--target",
        "-t",
        default="dev",
        help="Target branch to merge into (default: dev)",
    )
    parser.add_argument(
        "--source",
        "-s",
        default="develop",
        help="Source branch containing Dependabot changes (default: develop)",
    )
    parser.add_argument(
        "--project-context",
        "-c",
        default=None,
        help="Path to a project-specific context file (markdown)",
    )
    parser.add_argument(
        "--repo",
        "-r",
        default=None,
        help="Path to the target repository (overrides TARGET_REPO env var)",
    )
    parser.add_argument(
        "--mcp-config",
        "-m",
        default=None,
        help="Path to MCP server config file (overrides MCP_CONFIG env var)",
    )
    return parser.parse_args()


# ── Git Helpers ────────────────────────────────────────────────────────────────


def get_file_at_ref(repo_path: str, ref: str, filepath: str) -> str | None:
    """Read a file from a specific git ref (branch, tag, commit)."""
    try:
        r = subprocess.run(
            ["git", "show", f"{ref}:{filepath}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# ── Dependency Helpers ─────────────────────────────────────────────────────────


def parse_package_version_changes(raw_diff: str) -> list[dict]:
    """Parse package.json diff lines to extract package version changes."""
    changes = []
    in_pkg_section = False
    for line in raw_diff.splitlines():
        if re.match(r"^diff --git a/.*package\.json", line):
            in_pkg_section = True
            continue
        if in_pkg_section and re.match(r"^diff --git ", line):
            in_pkg_section = False
            continue
        if not in_pkg_section:
            continue
        if (
            line.startswith("--- a/")
            or line.startswith("+++ b/")
            or line.startswith("@@")
        ):
            continue
        m = re.match(r'^([+-])\s{4}"((?:@[^/]+/)?[^"]+)"\s*:\s*"([^"]+)"', line)
        if m:
            changes.append(
                {
                    "package": m.group(2),
                    "version": m.group(3),
                    "change": "added" if m.group(1) == "+" else "removed",
                    "line": line,
                }
            )
        # Also catch non-indented like "+    \"i18next\": \"^23.11.5\","
        m2 = re.match(r'^([+-])\s+"((?:@[^/]+/)?[^"]+)"\s*:\s*"([^"]+)"', line)
        if m2 and not m:
            changes.append(
                {
                    "package": m2.group(2),
                    "version": m2.group(3),
                    "change": "added" if m2.group(1) == "+" else "removed",
                    "line": line,
                }
            )
    return changes


def parse_lockfile_version_changes(raw_diff: str) -> list[dict]:
    """Parse package-lock.json diff to extract transitive dependency resolution changes."""
    changes: list[dict] = []
    in_lock_section = False
    in_package = False
    for line in raw_diff.splitlines():
        if re.match(r"^diff --git a/.*package-lock\.json", line):
            in_lock_section = True
            continue
        if in_lock_section and re.match(r"^diff --git ", line):
            in_lock_section = False
            continue
        if not in_lock_section:
            continue
        if (
            line.startswith("--- a/")
            or line.startswith("+++ b/")
            or line.startswith("@@")
        ):
            continue
        if in_package:
            name_m = re.search(r'"(name|version)"\s*:\s*"([^"]+)"', line)
            if name_m:
                key, val = name_m.group(1), name_m.group(2)
                if key == "version":
                    if "old_version" not in changes[-1]:
                        changes[-1]["old_version"] = val
                    else:
                        changes[-1]["new_version"] = val
            if re.search(r"^\s{8}\}", line) or re.search(r'"[^"]+":\s*\{', line):
                in_package = False
        if '"resolved"' in line:
            in_package = False
        pkg_m = re.search(r'^[+-]\s{4}"(node_modules/[^"]+)":\s*\{', line)
        if pkg_m:
            pkg_name = pkg_m.group(1)
            change_type = (
                "added"
                if line.startswith("+")
                else "removed" if line.startswith("-") else "modified"
            )
            changes.append(
                {
                    "package": pkg_name.replace("node_modules/", ""),
                    "change": change_type,
                    "old_version": "",
                    "new_version": "",
                    "line": line.strip(),
                }
            )
            in_package = True
    return [
        c
        for c in changes
        if c["old_version"] or c["new_version"] or c["change"] != "modified"
    ]


def group_package_changes(changes: list[dict]) -> list[dict]:
    """Group +/- lines to identify version bumps."""
    by_name: dict[str, dict] = {}
    for c in changes:
        name = c["package"]
        if name not in by_name:
            by_name[name] = {
                "package": name,
                "old_version": None,
                "new_version": None,
                "diff_lines": [],
            }
        if c["change"] == "removed":
            by_name[name]["old_version"] = c["version"]
        elif c["change"] == "added":
            by_name[name]["new_version"] = c["version"]
        by_name[name]["diff_lines"].append(c["line"])
    result = []
    for _name, info in by_name.items():
        if info["old_version"] and info["new_version"]:
            info["change_type"] = classify_semver(
                info["old_version"], info["new_version"]
            )
            result.append(info)
    return result


def classify_semver(old: str, new: str) -> str:
    try:
        o = tuple(int(x) for x in old.lstrip("^~>=<").split(".")[:3])
        n = tuple(int(x) for x in new.lstrip("^~>=<").split(".")[:3])
    except (ValueError, IndexError):
        return "unknown"
    if n[0] > o[0]:
        return "major"
    if n[1] > o[1]:
        return "minor"
    if n[2] > o[2]:
        return "patch"
    return "unknown"


def get_npm_package_info(pkg_name: str) -> dict:
    """Fetch package info from npm registry."""
    info = {
        "name": pkg_name,
        "description": "",
        "homepage": "",
        "repository": "",
        "error": None,
    }
    try:
        url = f"https://registry.npmjs.org/{urllib.parse.quote(pkg_name, safe='@/%')}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        info["description"] = data.get("description", "")
        info["homepage"] = data.get("homepage", "")
        repo = data.get("repository", {}) or {}
        if isinstance(repo, dict):
            info["repository"] = repo.get("url", "")
        elif isinstance(repo, str):
            info["repository"] = repo
        info["latest_version"] = data.get("dist-tags", {}).get("latest", "")
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        OSError,
    ) as e:
        info["error"] = str(e)
    return info


def extract_owner_repo(repo_url: str) -> tuple[str | None, str | None]:
    """Extract owner and repo name from a git URL."""
    if not repo_url:
        return None, None
    cleaned = re.sub(r"^git\+", "", repo_url)
    cleaned = re.sub(r"^git://", "https://", cleaned)
    cleaned = re.sub(r"^ssh://git@", "https://", cleaned)
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:/.*)?$", cleaned)
    if m:
        return m.group(1), m.group(2).removesuffix(".git")
    return None, None


def fetch_changelog_raw(owner: str, repo: str) -> str:
    """Fetch CHANGELOG.md from raw.githubusercontent.com."""
    branches = ["main", "master"]
    paths = [
        "CHANGELOG.md",
        "CHANGELOG",
        "CHANGELOG.txt",
        "CHANGES.md",
        "RELEASE_NOTES.md",
        "HISTORY.md",
    ]
    for branch in branches:
        for path in paths:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
            try:
                req = urllib.request.Request(url, headers={"Accept": "text/plain"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    if resp.status == 200:
                        content = resp.read().decode("utf-8", errors="replace")
                        return content[:8000] + (
                            "\n... [truncated]" if len(content) > 8000 else ""
                        )
            except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                continue
    return ""


def fetch_npm_readme(pkg_name: str) -> str:
    """Fetch the README from the npm registry as changelog fallback."""
    try:
        url = f"https://registry.npmjs.org/{urllib.parse.quote(pkg_name, safe='@/%')}/latest"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        readme = data.get("readme", "")
        if readme:
            return readme[:6000] + ("\n... [truncated]" if len(readme) > 6000 else "")
    except Exception:
        pass
    return ""


def get_package_changelog(pkg_name: str, repo_url: str) -> dict:
    """Try multiple strategies to fetch changelog content."""
    result = {"source": "", "url": "", "content": ""}
    owner, repo = extract_owner_repo(repo_url)
    if owner and repo:
        content = fetch_changelog_raw(owner, repo)
        if content:
            result["source"] = f"raw.githubusercontent.com/{owner}/{repo}"
            result["url"] = f"https://github.com/{owner}/{repo}/blob/main/CHANGELOG.md"
            result["content"] = content
            return result
        releases_url = f"https://github.com/{owner}/{repo}/releases"
        result["url"] = releases_url
        result["source"] = "releases_page"
    readme = fetch_npm_readme(pkg_name)
    if readme and len(readme) > 200:
        changelog_section = extract_changelog_from_readme(readme)
        if changelog_section:
            result["source"] = "npm_registry_readme"
            result["content"] = changelog_section
            return result
    result["content"] = ""
    result["url"] = result.get("url", "") or (
        f"https://www.npmjs.com/package/{pkg_name}"
    )
    return result


def extract_changelog_from_readme(readme: str) -> str:
    """Try to extract a changelog/release section from a README."""
    for header in (
        r"#+ ?Changelog",
        r"#+ ?Change ?Log",
        r"#+ ?Release.*Notes",
        r"#+ ?Releases",
        r"#+ ?History",
    ):
        m = re.search(
            header + r"\s*\n(.*?)(?=\n#+ |\Z)", readme, re.IGNORECASE | re.DOTALL
        )
        if m:
            return m.group(1).strip()[:6000]
    return ""


# ── Usage Scanning ─────────────────────────────────────────────────────────────


def scan_package_usage(repo_path: str, pkg_name: str) -> str:
    """Search the repo for concrete imports/usages of the given package."""
    pkg_escaped = re.escape(pkg_name)
    results = []

    # Search 1: import/require statements matching the package name
    import_pattern = f"""['"]{pkg_escaped}['"]"""
    try:
        r = subprocess.run(
            [
                "rg",
                "-n",
                "--no-heading",
                import_pattern,
                "--type",
                "ts",
                "--type",
                "js",
                "-g",
                "!node_modules",
                "-g",
                "!dist",
                "-g",
                "!build",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.stdout.strip():
            results.append("### Import Statements")
            lines = r.stdout.strip().splitlines()
            for line in lines:
                results.append(f"  {line.strip()}")
        if r.stderr.strip():
            results.append(f"  STDERR: {r.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        results.append("  (timeout scanning imports)")
    except FileNotFoundError:
        results.append("  (rg not found)")

    # Search 2: project-specific import variations (camelCase, kebab-case)
    name_variants = set()
    name_variants.add(pkg_name)
    parts = pkg_name.replace("@", "").split("/")
    last_part = parts[-1]
    name_variants.add(last_part.replace("-", ""))
    name_variants.add(last_part.replace("-", "_"))
    name_variants.add("".join(w.capitalize() for w in last_part.split("-")))
    name_variants.add(last_part.split("-")[0])
    for variant in name_variants - {pkg_name}:
        if len(variant) < 3:
            continue
        try:
            r = subprocess.run(
                [
                    "rg",
                    "-n",
                    "--no-heading",
                    f"""from ['"]{re.escape(variant)}['"]""",
                    "--type",
                    "ts",
                    "--type",
                    "js",
                    "-g",
                    "!node_modules",
                    "-g",
                    "!dist",
                    "-g",
                    "!build",
                ],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if r.stdout.strip():
                results.append(f"### Import Variations (matched '{variant}')")
                lines = r.stdout.strip().splitlines()
                for line in lines[:10]:
                    results.append(f"  {line.strip()}")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # Search 3: package.json / tsconfig references
    try:
        r = subprocess.run(
            [
                "rg",
                "-n",
                "--no-heading",
                pkg_escaped,
                "--type",
                "json",
                "-g",
                "!node_modules",
                "-g",
                "!package-lock.json",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.stdout.strip():
            results.append("### Config File References")
            lines = r.stdout.strip().splitlines()
            for line in lines:
                results.append(f"  {line.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if not results:
        return "(no usages found in tracked source files)"
    return "\n".join(results)


def get_import_file_paths(usage_text: str) -> list[str]:
    """Extract unique source file paths from usage scan output."""
    paths = []
    for line in usage_text.splitlines():
        m = re.match(r"^\s{2}(src/\S+):(\d+)", line)
        if m:
            path = m.group(1)
            if path not in paths:
                paths.append(path)
    return paths


def read_source_files(repo_path: str, ref: str, file_paths: list[str]) -> str:
    """Read key affected source files from a git ref and return their content."""
    blocks = []
    for fp in file_paths:
        content = get_file_at_ref(repo_path, ref, fp)
        if content:
            blocks.append(f"--- {fp} ---\n{content}")
    return "\n\n".join(blocks)


def collect_downstream_consumers(repo_path: str, pkg_name: str) -> str:
    """Find all files that transitively depend on a package via common bindings.

    For i18next, this means finding all files that import from react-i18next or i18next.
    Returns a concise file list.
    """
    related_packages = {"i18next-http-backend": ["react-i18next", "i18next"]}
    targets = related_packages.get(pkg_name, [])
    if not targets:
        return ""

    results = []
    for target in targets:
        try:
            r = subprocess.run(
                [
                    "rg",
                    "-l",
                    "--no-heading",
                    f"from '{re.escape(target)}'",
                    "--type",
                    "ts",
                    "--type",
                    "js",
                    "-g",
                    "!node_modules",
                    "-g",
                    "!dist",
                    "-g",
                    "!build",
                ],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if r.stdout.strip():
                files = [f.strip() for f in r.stdout.strip().splitlines() if f.strip()]
                results.append(f"### Consumers of '{target}' ({len(files)} files)")
                for f in files:
                    results.append(f"  {f}")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return "\n".join(results)


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    args = parse_arguments()
    target_branch = args.target
    source_branch = args.source

    target_repo = args.repo or os.environ.get("TARGET_REPO", TARGET_REPO)
    mcp_config_path = args.mcp_config or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)

    is_local_mode = not ARCHITECT_API_KEY
    if not is_local_mode and not ARCHITECT_API_KEY:
        print("Error: USE_GEMINI=true requires GEMINI_API_KEY to be set.")
        return

    if not target_repo:
        print("Error: No target repository specified.")
        print("Set TARGET_REPO env var or pass --repo /path/to/project")
        return

    print(f"{'=' * 60}")
    print("Launching Dependabot Review Engine (Hybrid Mode)")
    print(f"Target Project   : {target_repo}")
    print(f"Cloud Architect  : {REASONING_ARCHITECT}")
    print(f"Review Delta     : {target_branch} <--- {source_branch}")
    print(f"{'=' * 60}\n")

    start_time = time.time()

    # 1. Gather Git Diff
    print("Extracting Git modifications...")
    git_layer = GitDiffProvider(target_repo)
    try:
        raw_diff = git_layer.get_diff(target_branch, source_branch)
        changed_files = git_layer.get_changed_files(target_branch, source_branch)
    except RuntimeError as e:
        print(f"Error: Could not diff {target_branch}...{source_branch}: {e}")
        print("  Run 'git branch -a | grep <branch>' to verify branch names.")
        return

    if not raw_diff or not raw_diff.strip():
        print("Error: No Git differences found between these branches. Exiting.")
        return

    print(f"   [Git] Detected changes across {len(changed_files)} file(s).")

    # 2. Try to guess the primary package from the Dependabot branch name
    guessed_package = ""
    dep_branch_match = re.match(r"dependabot/[^/]+/([@a-z0-9_/-]+)", source_branch)
    if dep_branch_match:
        guessed_package = dep_branch_match.group(1)
        # Dependabot encodes / as - in branch names for scoped packages
        # but also uses the raw package name. Show as hint.
        print(f"   [Branch] Dependabot branch suggests package: {guessed_package}")
        print(f"   [Branch] Full branch: {source_branch}")

    # 3. Read the actual current package.json from the source branch
    source_pkg_json = get_file_at_ref(target_repo, source_branch, "package.json")

    # 4. Parse dependency changes
    print("\nParsing dependency version changes...")
    raw_changes = parse_package_version_changes(raw_diff)
    grouped = group_package_changes(raw_changes)
    updated_packages = [g for g in grouped if g["old_version"] and g["new_version"]]

    # Also parse lockfile changes for transitive deps
    lockfile_changes = parse_lockfile_version_changes(raw_diff)

    if updated_packages:
        print(f"   Found {len(updated_packages)} updated direct package(s):")
        for p in updated_packages:
            print(
                f"     - {p['package']}: {p['old_version']} -> {p['new_version']} ({p['change_type']})"
            )
    else:
        print("   No direct package.json version bumps detected.")
        if guessed_package:
            print(
                f"   But branch name suggests '{guessed_package}' was updated (maybe in lockfile only)."
            )

    if lockfile_changes:
        print(f"   Also {len(lockfile_changes)} lockfile resolution change(s).")
        for lc in lockfile_changes[:5]:
            print(f"     - {lc['package']} ({lc['change']})")

    # 4b. If no packages found from diff, try reading package.json from source
    if not updated_packages and source_pkg_json:
        print(
            "\n   Attempting to infer changes by comparing package.json with target branch..."
        )
        target_pkg_json = get_file_at_ref(target_repo, target_branch, "package.json")
        if target_pkg_json and source_pkg_json:
            try:
                target_deps = json.loads(target_pkg_json).get("dependencies", {})
                source_deps = json.loads(source_pkg_json).get("dependencies", {})
                for pkg, version in source_deps.items():
                    if pkg in target_deps and target_deps[pkg] != version:
                        updated_packages.append(
                            {
                                "package": pkg,
                                "old_version": target_deps[pkg],
                                "new_version": version,
                                "change_type": classify_semver(
                                    target_deps[pkg], version
                                ),
                                "diff_lines": [],
                            }
                        )
                        print(
                            f"     - {pkg}: {target_deps[pkg]} -> {version} ({classify_semver(target_deps[pkg], version)})"
                        )
            except json.JSONDecodeError:
                pass

    # 5. Fetch npm info + changelogs + usage scans
    print("\nFetching package intelligence...")
    dep_contexts = []
    for pkg in updated_packages:
        name = pkg["package"]
        print(f"   [{name}] Fetching npm info...")
        npm_info = get_npm_package_info(name)

        print(f"   [{name}] Resolving changelog...")
        changelog = get_package_changelog(name, npm_info.get("repository", ""))
        if changelog["content"]:
            print(f"      Found via {changelog['source']}: {changelog['url']}")
        else:
            print(f"      No changelog found. URL: {changelog['url']}")

        print(f"   [{name}] Scanning codebase for usage...")
        usage = scan_package_usage(target_repo, name)
        import_paths = get_import_file_paths(usage)

        print(f"   [{name}] Reading {len(import_paths)} affected source file(s)...")
        source_contents = read_source_files(target_repo, source_branch, import_paths)

        print(f"   [{name}] Mapping downstream consumers...")
        downstream = collect_downstream_consumers(target_repo, name)

        dep_contexts.append(
            {
                "package": name,
                "old_version": pkg["old_version"],
                "new_version": pkg["new_version"],
                "change_type": pkg["change_type"],
                "npm_info": npm_info,
                "changelog": changelog,
                "usage": usage,
                "source_contents": source_contents,
                "downstream": downstream,
            }
        )

    # 6. Load Agent Persona
    persona_path = "agents/dependabot_reviewer.md"
    if not os.path.exists(persona_path):
        print(f"Error: System prompt missing at {persona_path}")
        return

    with open(persona_path, encoding="utf-8") as f:
        system_agent_prompt = f.read()

    # 7. Initialize MCP workbench
    print("Initializing MCP workbench...")
    orch = init_mcp(repo_path=target_repo, config_path=mcp_config_path)
    mcp_block = build_mcp_context() if orch else ""
    if orch:
        print("   [Done] MCP workbench active\n")
    else:
        print("   [Skipped] No MCP config found\n")

    # 8. Load project context
    project_context_block = ""
    if args.project_context:
        ctx_path = args.project_context
        if os.path.exists(ctx_path):
            with open(ctx_path, encoding="utf-8") as f:
                project_context_block = minify_markdown(f.read())
            print(f"   [Loaded] Project context from {ctx_path}\n")
        else:
            print(f"   [Warning] Project context file not found: {ctx_path}\n")
    else:
        print("   [Skipped] No project context file specified (-c to add)\n")

    # 9. Build dependency context block
    dep_blocks = []
    for dc in dep_contexts:
        changelog_section = dc["changelog"]["content"] or "(not available)"
        changelog_source = dc["changelog"]["source"] or "none"
        changelog_url = dc["changelog"]["url"] or "N/A"

        block = f"""
### Package: {dc['package']}
**Version Delta:** `{dc['old_version']}` → `{dc['new_version']}` ({dc['change_type']})
**NPM Description:** {dc['npm_info'].get('description', '')[:200]}
**Latest Version on NPM:** {dc['npm_info'].get('latest_version', 'N/A')}
**Repository:** {dc['npm_info'].get('repository', 'N/A')}

**Changelog Source:** {changelog_source}
**Changelog URL:** {changelog_url}
**Changelog Content:**
```
{changelog_section}
```

**Codebase Usage Scan:**
{dc['usage']}

**Affected Source Files (read from source branch):**
{dc['source_contents'] or '(no source files read)'}

**Downstream Consumers (transitive dependents in the repo):**
{dc['downstream'] or '(none detected)'}
"""
        dep_blocks.append(block)

    dep_context_str = (
        "\n---\n".join(dep_blocks)
        if dep_blocks
        else "(No package version changes detected)"
    )

    # 10. Build prompt context sections
    mcp_prompt_section = (
        f"\n\n### MCP-Augmented Context (Live Project State)\n{mcp_block}"
        if mcp_block
        else ""
    )
    project_context_section = (
        f"\n\n### Project-Specific Context ({os.path.basename(args.project_context)})\n{project_context_block}"
        if project_context_block
        else ""
    )

    changed_files_json = json.dumps(changed_files, separators=(",", ":"))
    source_pkg_section = ""
    if source_pkg_json:
        try:
            parsed = json.loads(source_pkg_json)
            deps = {
                k: v
                for k, v in parsed.get("dependencies", {}).items()
                if k in [dc["package"] for dc in dep_contexts]
            }
            if deps:
                source_pkg_section = f"\n\n### Current package.json (from source branch) — relevant entries\n```json\n{json.dumps(deps, indent=2)}\n```"
        except json.JSONDecodeError:
            pass

    review_prompt = f"""Below is the git diff, changed files, and detailed package intelligence for the Dependabot PR.{mcp_prompt_section}{project_context_section}{source_pkg_section}

## Changed Files
```json
{changed_files_json}
```

## Git Diff
```diff
{raw_diff}
```

## Package Intelligence
{dep_context_str}

## Review Instructions
Analyze the Dependabot dependency changes above. For each updated package, evaluate the impact.

### Mandatory Rules:
1. **CITE DIFF LINES** — For every version change, quote the actual `+`/`-` lines from the diff.
2. **CHANGELOG ACCURACY** — Base your changelog analysis *only* on the content provided above in the Changelog Content section. If no changelog content was found, say so explicitly — do not fabricate or infer changes.
3. **EXACT FILE PATHS** — Every usage reference must come from the Codebase Usage Scan output. Use exact paths like `src/language/i18n.ts:3`. Do not say "or equivalent" or use generic descriptions when the scan shows exact files.
4. **GROUND CONFIG ANALYSIS IN SOURCE** — The "Affected Source Files" section contains the actual file contents read from the source branch. Use this to CONFIRM whether the existing config is compatible with the new version. For example, check if `loadPath` uses `{{lng}}`/`{{ns}}` syntax, check requestOptions, check for deprecated patterns. If compatible, say so explicitly rather than asking the user to verify.
5. **DOWNSTREAM LIST** — The "Downstream Consumers" section lists every file in the repo that transitively depends on the updated package. Use this to scope blast radius and testing recommendations. When making a recommendation, reference specific files from this list.
6. **TRANSITIVE DEPS** — Note any lockfile resolution changes shown in the diff but focus analysis on direct dependency updates.
7. **PACKAGE-BY-PACKAGE** — Cover each updated package in order.

Follow the markdown schema and headers defined in your system prompt."""

    # 11. Execute Review
    print(f"Processing Review via [{REASONING_ARCHITECT}]...")
    pass_start = time.time()

    runner = StatefulHarnessRunner(
        model_name=REASONING_ARCHITECT,
        base_url=ARCHITECT_API_BASE,
        api_key=ARCHITECT_API_KEY,
        fallback_model_name=FALLBACK_REVIEWER,
        num_ctx=65536,
    )
    history = runner.execute_sequence(
        system_prompt=system_agent_prompt,
        passes=[review_prompt],
        fallback_prompt=review_prompt,
    )
    final_review = history[-1]["output"]
    model_used = runner.model_name

    print(
        f"   [Done] Review generated via {model_used} in {time.time() - pass_start:.2f}s"
    )

    # 12. Evaluate review quality
    print(f"Checking review quality via Local Judge [{LOCAL_JUDGE}]...")
    judge_start = time.time()

    judge_context = (
        f"Diff:\n```diff\n{raw_diff[:10000]}\n```\n\n"
        f"Changed Packages:\n{json.dumps(updated_packages, indent=2)}"
    )
    evaluator = AutomatedEvaluator(judge_model=LOCAL_JUDGE)
    scores = evaluator.grade_run(
        final_review, "rubrics/code_review_rubric.json", context=judge_context
    )

    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"Review Reliability Scores: {scores}")

    # 13. Log and Export Artifacts
    print("Archiving run data...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=model_used,
        agent_role=f"Dependabot Review ({source_branch})",
        raw_output=final_review,
        scores=scores,
    )

    report_filename = "reports/dependabot_review_report.md"
    os.makedirs("reports", exist_ok=True)
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(final_review)

    if _mcp_orch:
        _mcp_orch.remember(
            f"dependabot_review:{source_branch}:complete",
            f"Dependabot review completed for {source_branch} -> {target_branch}. Report: {report_filename}",
            tags=["dependabot_review", source_branch, "complete"],
        )
        _mcp_orch.stop()

    total_duration = time.time() - start_time
    print(f"\nReport saved to: {report_filename}")
    print(f"Total Time: {total_duration:.2f}s  Model: {model_used}")


if __name__ == "__main__":
    main()
