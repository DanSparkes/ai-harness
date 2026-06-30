"""Terragrunt/OpenTofu project parser for infrastructure-as-code review."""

import os
import re
from pathlib import Path
from typing import Any

from core.cache import get as cache_get
from core.cache import get_git_head, make_key
from core.cache import set as cache_set

EXCLUDE_DIRS = {
    ".venv",
    "__pycache__",
    ".git",
    "node_modules",
    ".terragrunt-cache",
    ".external_modules",
    ".terraform",
    "vendor",
}

HCL_BLOCK_PATTERN = re.compile(
    r'^(\w+)\s+"([^"]*)"\s+"([^"]*)"\s*\{|^(\w+)\s*\{|^(\w+)\s+"([^"]*)"\s*\{',
    re.MULTILINE,
)


class TerragruntTopographer:
    """Parses a Terragrunt/OpenTofu project to create a structural map."""

    def __init__(self, target_dir: str):
        # Expand ~ and resolve relative paths before any file operations.
        resolved = os.path.expanduser(target_dir)
        if not os.path.isabs(resolved):
            resolved = os.path.abspath(resolved)
        self.target_dir = Path(resolved)

    def scan_project(self) -> dict:
        head = get_git_head(str(self.target_dir))
        if head:
            key = make_key("terragrunt_parser:scan_project", str(self.target_dir), head)
            cached = cache_get(key, max_age=86400)
            if cached is not None and isinstance(cached, dict):
                return cached

        topology: dict[str, Any] = {
            "accounts": {},
            "modules": {},
            "dependencies": [],
            "providers": [],
            "remote_state": [],
            "network_topology": {},
            "security": {},
            "ci_cd": {},
            "files": [],
        }

        self._scan_root_config(topology)
        self._scan_accounts(topology)
        self._scan_modules(topology)
        self._scan_dependencies(topology)
        self._scan_network_topology(topology)
        self._scan_security(topology)
        self._scan_ci_cd(topology)

        # Diagnostic when nothing was found.
        if (
            not topology["accounts"]
            and not topology["modules"]
            and not topology["dependencies"]
        ):
            import warnings

            hints = []
            if not self.target_dir.exists():
                hints.append(f"Directory does not exist: {self.target_dir}")
            else:
                subdirs = [
                    d.name
                    for d in self.target_dir.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ]
                if subdirs:
                    potential_account_dirs = [
                        d for d in subdirs if d in topology.get("_auto_detected", [])
                    ]
                    if potential_account_dirs:
                        hints.append(
                            "Directories resembling account roots found: "
                            + ", ".join(potential_account_dirs[:8])
                            + ("..." if len(potential_account_dirs) > 8 else "")
                        )
                    hints.append(
                        f"Non-standard subdirectories present: {', '.join(subdirs[:8])}"
                        + ("..." if len(subdirs) > 8 else "")
                    )
                else:
                    hints.append("No subdirectories at all — the repo may be flat.")

            msg = "[TerragruntTopographer] No accounts/modules/dependencies found. " + (
                "; ".join(hints)
            )
            warnings.warn(msg, stacklevel=2)

        if head:
            cache_set(key, topology)
        return topology

    def _read_file(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _scan_root_config(self, topology: dict):
        root_hcl = self.target_dir / "root.hcl"
        if not root_hcl.exists():
            return
        content = self._read_file(root_hcl)
        topology["root_config"] = {
            "path": str(root_hcl),
            "has_remote_state": "remote_state" in content,
            "has_generate_block": "generate" in content,
            "has_inputs_block": "inputs" in content,
        }

        # Extract remote state backend
        backend_match = re.search(r'backend\s*=\s*"(\w+)"', content)
        if backend_match:
            topology["remote_state"].append(
                {"backend": backend_match.group(1), "file": "root.hcl"}
            )

        # Extract provider version constraints
        provider_matches = re.finditer(r'version\s*=\s*"([^"]+)"', content)
        for m in provider_matches:
            topology["providers"].append(
                {"version_constraint": m.group(1), "file": "root.hcl"}
            )

    def _scan_accounts(self, topology: dict):
        # If target_dir doesn't exist at all, scan_project already returned
        # an empty map; no need to add noise here.
        if not self.target_dir.exists():
            return
        account_dirs = ["dev", "prod", "shared-services", "shared-services-ca"]

        # Auto-detect any subdirectory that looks like an account root
        # (contains a terragrunt.hcl or _envcommon).
        auto_found = []
        if self.target_dir.is_dir():
            for entry in sorted(self.target_dir.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if (entry / "terragrunt.hcl").exists() or (
                    entry / "_envcommon"
                ).exists():
                    account_dirs.append(entry.name)
                    auto_found.append(entry.name)
            # Expose auto-detected dirs so scan_project diagnostics can reference them.
            topology.setdefault("_auto_detected", []).extend(auto_found)
        for acct in account_dirs:
            acct_path = self.target_dir / acct
            if not acct_path.exists():
                continue

            acct_info: dict[str, Any] = {
                "path": str(acct_path),
                "units": [],
                "envcommon": [],
            }

            # Scan envcommon files
            envcommon_dir = acct_path / "_envcommon"
            if envcommon_dir.exists():
                for f in envcommon_dir.glob("*.hcl"):
                    content = self._read_file(f)
                    acct_info["envcommon"].append(
                        {
                            "file": f.name,
                            "has_dependency": "dependency" in content,
                            "has_mock_outputs": "mock_outputs" in content,
                            "blocks": self._extract_blocks(content),
                        }
                    )

            # Scan terragrunt.hcl
            tg_hcl = acct_path / "terragrunt.hcl"
            if tg_hcl.exists():
                content = self._read_file(tg_hcl)
                acct_info["root_terragrunt"] = {
                    "has_include": "include" in content,
                    "has_remote_state": "remote_state" in content,
                    "has_generate": "generate" in content,
                }

            # Scan unit directories
            for unit_dir in acct_path.iterdir():
                if not unit_dir.is_dir() or unit_dir.name.startswith("."):
                    continue
                if unit_dir.name == "_envcommon":
                    continue

                unit_info = self._scan_unit(unit_dir, acct)
                if unit_info:
                    acct_info["units"].append(unit_info)

            topology["accounts"][acct] = acct_info

    def _scan_unit(self, unit_path: Path, account: str) -> dict | None:
        tg_hcl = unit_path / "terragrunt.hcl"
        if not tg_hcl.exists():
            return None

        content = self._read_file(tg_hcl)

        info: dict[str, Any] = {
            "name": unit_path.name,
            "path": str(unit_path),
            "account": account,
            "has_include": "include" in content,
            "has_dependency": "dependency" in content,
            "has_mock_outputs": "mock_outputs" in content,
            "blocks": self._extract_blocks(content),
            "dependencies": self._extract_dependencies(content),
            "provider_aliases": self._extract_provider_aliases(content),
        }

        # Check for .tf files
        tf_files = list(unit_path.glob("*.tf"))
        if tf_files:
            info["tf_files"] = [f.name for f in tf_files]

        return info

    def _extract_blocks(self, content: str) -> list[dict]:
        blocks = []
        for match in HCL_BLOCK_PATTERN.finditer(content):
            groups = match.groups()
            if groups[0]:  # block type "name" "name" { pattern
                blocks.append(
                    {"type": groups[0], "name": groups[1], "secondary": groups[2]}
                )
            elif groups[3]:  # block type { pattern
                blocks.append({"type": groups[3]})
            elif groups[4]:  # block type "name" { pattern
                blocks.append({"type": groups[4], "name": groups[5]})
        return blocks

    def _extract_dependencies(self, content: str) -> list[dict]:
        deps = []
        dep_blocks = re.finditer(
            r'dependency\s+"([^"]+)"\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}',
            content,
            re.DOTALL,
        )
        for m in dep_blocks:
            dep_name = m.group(1)
            dep_body = m.group(2)
            config_path = re.search(r'config_path\s*=\s*"([^"]+)"', dep_body)
            mock_outputs = "mock_outputs" in dep_body
            skip = re.search(r"skip\s*=\s*(true|false)", dep_body)
            deps.append(
                {
                    "name": dep_name,
                    "config_path": config_path.group(1) if config_path else None,
                    "has_mock_outputs": mock_outputs,
                    "skip": skip.group(1) == "true" if skip else False,
                }
            )
        return deps

    def _extract_provider_aliases(self, content: str) -> list[dict]:
        aliases = []
        alias_pattern = re.findall(
            r'provider\s+"(\w+)"\s*\{[^}]*alias\s*=\s*(\w+)', content, re.DOTALL
        )
        for provider, alias in alias_pattern:
            aliases.append({"provider": provider, "alias": alias})
        return aliases

    def _scan_modules(self, topology: dict):
        modules_dir = self.target_dir / "modules"
        if not modules_dir.exists():
            return

        for module_dir in modules_dir.iterdir():
            if not module_dir.is_dir() or module_dir.name.startswith("."):
                continue

            module_info: dict[str, Any] = {
                "name": module_dir.name,
                "path": str(module_dir),
                "variables": [],
                "outputs": [],
                "resources": [],
                "has_main_tf": (module_dir / "main.tf").exists(),
                "has_variables_tf": (module_dir / "variables.tf").exists(),
                "has_outputs_tf": (module_dir / "outputs.tf").exists(),
                "has_versions_tf": (module_dir / "versions.tf").exists(),
            }

            # Parse variables.tf
            variables_tf = module_dir / "variables.tf"
            if variables_tf.exists():
                content = self._read_file(variables_tf)
                module_info["variables"] = self._extract_hcl_blocks(content)

            # Parse outputs.tf
            outputs_tf = module_dir / "outputs.tf"
            if outputs_tf.exists():
                content = self._read_file(outputs_tf)
                module_info["outputs"] = self._extract_hcl_blocks(content)

            # Parse main.tf for resources
            main_tf = module_dir / "main.tf"
            if main_tf.exists():
                content = self._read_file(main_tf)
                module_info["resources"] = self._extract_resources(content)
                module_info["has_provider_aliases"] = "alias" in content

            topology["modules"][module_dir.name] = module_info

    def _extract_hcl_blocks(self, content: str) -> list[dict]:
        blocks = []
        for match in re.finditer(r'^(\w+)\s+"([^"]+)"\s*\{', content, re.MULTILINE):
            blocks.append({"type": match.group(1), "name": match.group(2)})
        return blocks

    def _extract_resources(self, content: str) -> list[dict]:
        resources = []
        for match in re.finditer(
            r'resource\s+"(\w+)"\s+"([^"]+)"\s*\{', content, re.MULTILINE
        ):
            resources.append({"type": match.group(1), "name": match.group(2)})
        return resources

    def _scan_dependencies(self, topology: dict):
        for acct_name, acct_info in topology["accounts"].items():
            for unit in acct_info.get("units", []):
                unit_deps = []
                for dep in unit.get("dependencies", []):
                    if dep.get("config_path"):
                        unit_deps.append(
                            {
                                "from": f"{acct_name}/{unit['name']}",
                                "to": dep["config_path"],
                                "has_mock_outputs": dep.get("has_mock_outputs", False),
                                "skip": dep.get("skip", False),
                            }
                        )
                topology["dependencies"].extend(unit_deps)

    def _scan_network_topology(self, topology: dict):
        for module_name, module_info in topology["modules"].items():
            is_network = any(
                kw in module_name.lower()
                for kw in ["vpc", "network", "transit", "tgw", "vpn", "peering"]
            )
            if is_network:
                resources = [r["type"] for r in module_info.get("resources", [])]
                topology["network_topology"][module_name] = {
                    "path": module_info["path"],
                    "resource_types": resources,
                }

    def _scan_security(self, topology: dict):
        security_keywords = ["iam", "security", "waf", "kms", "secrets", "acm", "ssl"]
        for module_name, module_info in topology["modules"].items():
            is_security = any(kw in module_name.lower() for kw in security_keywords)
            if is_security:
                if "security_modules" not in topology["security"]:
                    topology["security"]["security_modules"] = []
                topology["security"]["security_modules"].append(
                    {
                        "name": module_name,
                        "resources": [
                            r["type"] for r in module_info.get("resources", [])
                        ],
                    }
                )

        # Check for pre-commit and security scanning
        pre_commit = self.target_dir / ".pre-commit-config.yaml"
        if pre_commit.exists():
            topology["ci_cd"]["pre_commit"] = True

        trivy_ignore = self.target_dir / ".trivyignore"
        if trivy_ignore.exists():
            content = self._read_file(trivy_ignore)
            topology["security"]["trivy_suppressions"] = len(
                re.findall(r"^[A-Z]+-", content, re.MULTILINE)
            )

    def _scan_ci_cd(self, topology: dict):
        github_dir = self.target_dir / ".github"
        if github_dir.exists():
            workflows_dir = github_dir / "workflows"
            if workflows_dir.exists():
                workflows = []
                for f in workflows_dir.glob("*.y*ml"):
                    content = self._read_file(f)
                    workflows.append(
                        {
                            "name": f.stem,
                            "has_terraform": "terraform" in content.lower()
                            or "tofu" in content.lower(),
                            "has_terragrunt": "terragrunt" in content.lower(),
                            "has_checkov": "checkov" in content.lower(),
                            "has_trivy": "trivy" in content.lower(),
                            "has_tflint": "tflint" in content.lower(),
                        }
                    )
                topology["ci_cd"]["workflows"] = workflows

        tflint = self.target_dir / ".tflint.hcl"
        if tflint.exists():
            topology["ci_cd"]["tflint"] = True

        checkov_baseline = self.target_dir / ".checkov.baseline"
        if checkov_baseline.exists():
            topology["ci_cd"]["checkov_baseline"] = True


def format_topology_for_prompt(topology: dict) -> str:
    """Format topology into a text block for LLM prompts."""
    parts = []

    # Accounts summary
    parts.append("=== ACCOUNTS ===")
    for acct_name, acct_info in topology.get("accounts", {}).items():
        unit_count = len(acct_info.get("units", []))
        envcommon_count = len(acct_info.get("envcommon", []))
        parts.append(
            f"  {acct_name}: {unit_count} units, {envcommon_count} envcommon configs"
        )
        for unit in acct_info.get("units", []):
            dep_count = len(unit.get("dependencies", []))
            parts.append(
                f"    - {unit['name']}: {dep_count} deps, "
                f"mock_outputs={any(d.get('has_mock_outputs') for d in unit.get('dependencies', []))}"
            )

    # Modules
    parts.append("\n=== MODULES ===")
    for mod_name, mod_info in topology.get("modules", {}).items():
        res_count = len(mod_info.get("resources", []))
        var_count = len(mod_info.get("variables", []))
        out_count = len(mod_info.get("outputs", []))
        parts.append(
            f"  {mod_name}: {res_count} resources, {var_count} vars, {out_count} outputs"
        )
        if mod_info.get("has_provider_aliases"):
            parts.append("    [has provider aliases]")

    # Dependencies
    parts.append("\n=== DEPENDENCY GRAPH ===")
    deps = topology.get("dependencies", [])
    if deps:
        for dep in deps:
            mock_tag = " [mock]" if dep.get("has_mock_outputs") else ""
            skip_tag = " [SKIP]" if dep.get("skip") else ""
            parts.append(f"  {dep['from']} -> {dep['to']}{mock_tag}{skip_tag}")
    else:
        parts.append("  (no cross-unit dependencies)")

    # Network
    parts.append("\n=== NETWORK TOPOLOGY ===")
    for mod_name, net_info in topology.get("network_topology", {}).items():
        parts.append(f"  {mod_name}: {', '.join(net_info.get('resource_types', []))}")

    # Security
    parts.append("\n=== SECURITY ===")
    for sec_mod in topology.get("security", {}).get("security_modules", []):
        parts.append(f"  {sec_mod['name']}: {', '.join(sec_mod.get('resources', []))}")
    if topology.get("security", {}).get("trivy_suppressions"):
        parts.append(
            f"  Trivy suppressions: {topology['security']['trivy_suppressions']}"
        )

    # CI/CD
    parts.append("\n=== CI/CD ===")
    ci = topology.get("ci_cd", {})
    if ci.get("pre_commit"):
        parts.append("  pre-commit: enabled")
    if ci.get("tflint"):
        parts.append("  tflint: configured")
    if ci.get("checkov_baseline"):
        parts.append("  checkov baseline: present")
    for wf in ci.get("workflows", []):
        tools = []
        if wf.get("has_terragrunt"):
            tools.append("terragrunt")
        if wf.get("has_checkov"):
            tools.append("checkov")
        if wf.get("has_trivy"):
            tools.append("trivy")
        if wf.get("has_tflint"):
            tools.append("tflint")
        parts.append(f"  workflow/{wf['name']}: {', '.join(tools) or 'basic'}")

    return "\n".join(parts)
