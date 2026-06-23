import hashlib
import os
import re
import json
import time
import ast
import tempfile
import requests
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict

from core.cache import get as cache_get, set as cache_set, make_key, get_git_head

AGENTS_DIR = Path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents"))

# ── Skill ─────────────────────────────────────────────────────────────────────

@dataclass
class Skill:
    name: str
    description: str
    fn: callable
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}, "required": []})

    def to_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }

# ── Agent ─────────────────────────────────────────────────────────────────────

@dataclass
class Agent:
    name: str
    system_prompt: str
    model_name: str = "qwen3.6:latest"
    base_url: str = "http://localhost:11434"
    api_key: str = None
    num_ctx: int = 65536
    allowed_skills: list[str] = field(default_factory=list)

    @property
    def is_cloud(self) -> bool:
        return "gemini" in self.model_name.lower() or bool(self.api_key)

    @property
    def api_url(self) -> str:
        if self.is_cloud:
            base = self.base_url.rstrip("/")
            if not base.endswith("/chat/completions"):
                base += "/chat/completions"
            return base
        return f"{self.base_url}/api/chat"

    def execute(self, task: str, skills: dict[str, Skill] = None, stream: bool = False) -> str:
        messages = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": task}]
        headers = {"Content-Type": "application/json"}
        if self.is_cloud and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        temperature = 0.0 if "coder" in self.model_name else 0.4
        tools = None
        skill_map = {}
        if skills:
            tools = [s.to_tool() for s in skills.values()]
            skill_map = skills

        _cache_key = None
        if not skills and not stream:
            cache_payload = json.dumps(messages, sort_keys=True, default=str)
            _cache_key = make_key("agent:execute", self.model_name, cache_payload, str(temperature), str(self.num_ctx))
            cached = cache_get(_cache_key, max_age=86400)
            if cached is not None:
                return cached

        # Streaming mode for code generation (local Ollama only, no tool calls)
        if stream and not self.is_cloud and not skills:
            return self._execute_stream(messages, headers, temperature)

        max_tool_rounds = 10

        for round_idx in range(max_tool_rounds + 1):
            if self.is_cloud:
                payload = {
                    "model": self.model_name,
                    "messages": messages,
                    "stream": False,
                    "temperature": temperature,
                    "top_p": 0.9,
                }
            else:
                payload = {
                    "model": self.model_name,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "num_ctx": self.num_ctx,
                        "temperature": temperature,
                        "top_p": 0.9,
                    },
                }
            if tools:
                payload["tools"] = tools

            response = requests.post(self.api_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

            if self.is_cloud:
                msg = data["choices"][0]["message"]
            else:
                msg = data.get("message", {})

            content = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls")

            if not tool_calls:
                if _cache_key is not None:
                    cache_set(_cache_key, content)
                return content

            # Append assistant message with tool_calls to conversation
            if self.is_cloud:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": tc.get("id", f"call_{round_idx}_{i}"), "type": "function", "function": tc["function"]}
                        for i, tc in enumerate(tool_calls)
                    ],
                })
            else:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                })

            # Execute each tool call and append result
            for tc in tool_calls:
                if tc.get("type") != "function":
                    continue
                func = tc.get("function", {})
                name = func.get("name", "")
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}

                if name in skill_map:
                    fn = skill_map[name].fn
                    try:
                        result = fn(**args) if isinstance(args, dict) else fn(args)
                    except Exception as e:
                        result = f"Error executing {name}: {e}"
                else:
                    result = f"Unknown skill '{name}'"

                result_str = str(result)
                if self.is_cloud:
                    messages.append({
                        "role": "tool",
                        "content": result_str,
                        "tool_call_id": tc.get("id", f"call_{round_idx}"),
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "content": result_str,
                        "name": name,
                    })

        return "(max tool call rounds reached)"

    def _execute_stream(self, messages: list, headers: dict, temperature: float) -> str:
        """Stream response from Ollama, validate code syntax on completion."""
        t0 = time.time()
        payload = {
            "model": self.model_name,
            "messages": messages,
            "stream": True,
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": temperature,
                "top_p": 0.9,
            },
        }

        response = requests.post(self.api_url, json=payload, headers=headers, stream=True)
        response.raise_for_status()

        full_content = ""
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            data = json.loads(line)
            token = data.get("message", {}).get("content", "")
            full_content += token
            if data.get("done", False):
                break

        elapsed = time.time() - t0
        print(f"   [Stream] {len(full_content)} chars in {elapsed:.1f}s")

        # Post-generation syntax check on Python code blocks
        blocks = re.findall(r"```(?:python|py)?\n(.*?)```", full_content, re.DOTALL)
        if blocks:
            code = "\n\n".join(blocks)
            is_valid, err = _check_code_syntax(code)
            if not is_valid:
                print(f"   [Syntax Error] {err[:200]}")
                return f"__SYNTAX_ERROR__:{err}\n{full_content}"

        return full_content

# ── Registry ──────────────────────────────────────────────────────────────────

DEFAULT_AGENT_MAP = {
    "Architect":              ("architect.md",              "gemini-2.5-flash",      None),
    "Engineer":               ("code_implementer.md",       "qwen3.6:latest",        None),
    "QA_Tester":              ("integration_auditor.md",    "qwen2.5-coder:14b",     None),
    "Security_Auditor":       ("security.md",               "gemini-2.5-flash",      None),
    "Code_Reviewer":          ("code_reviewer.md",          "qwen3.6:latest",        None),
    "Exploratory_Architect":  ("exploratory_architect.md",  "gemini-2.5-flash",      None),
    "Staff_Onboarding":       ("staff_onboarding.md",       "qwen3.6:latest",        None),
    "Systems_Architect":      ("architecture_review.md",    "qwen3.6:latest",        None),
}

class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, Agent] = {}
        self._skills: dict[str, Skill] = {}

    # ── Agent management ──────────────────────────────────────────────────

    def register_agent(self, agent: Agent):
        self._agents[agent.name] = agent

    def get_agent(self, name: str) -> Agent:
        if name not in self._agents:
            raise KeyError(f"Unknown agent '{name}'. Available: {list(self._agents.keys())}")
        return self._agents[name]

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())

    def load_agent_from_file(self, agent_name: str, persona_path: str, model_name: str = None, api_key: str = None) -> Agent:
        if not os.path.exists(persona_path):
            raise FileNotFoundError(f"Persona file not found: {persona_path}")
        with open(persona_path, "r") as f:
            persona = f.read()

        is_gemini = model_name and "gemini" in model_name.lower()
        agent = Agent(
            name=agent_name,
            system_prompt=persona,
            model_name=model_name or "qwen3.6:latest",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai" if is_gemini else "http://localhost:11434",
            api_key=api_key if is_gemini else None,
        )
        self.register_agent(agent)
        return agent

    def load_default_agents(self, gemini_api_key: str = None):
        for agent_name, (filename, model, _) in DEFAULT_AGENT_MAP.items():
            path = AGENTS_DIR / filename
            if path.exists():
                self.load_agent_from_file(agent_name, str(path), model, gemini_api_key)

    # ── Skill management ──────────────────────────────────────────────────

    def register_skill(self, skill: Skill):
        self._skills[skill.name] = skill

    def get_skill(self, name: str) -> Skill:
        return self._skills[name]

    def list_skills(self) -> list[str]:
        return list(self._skills.keys())

    def get_skills_for_agent(self, agent_name: str) -> list[Skill]:
        agent = self.get_agent(agent_name)
        return [self._skills[s] for s in agent.allowed_skills if s in self._skills]

    # ── Execution ─────────────────────────────────────────────────────────

    def execute_step(self, agent_name: str, task: str, skills_override: list[str] = None) -> str:
        agent = self.get_agent(agent_name)
        return agent.execute(task, self._skills)


# ── Built-in Skill Functions ──────────────────────────────────────────────────

def _check_code_syntax(code: str) -> tuple[bool, str]:
    """Validate Python code syntax via py_compile on a temp file. Returns (is_valid, error_msg)."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    try:
        tmp.write(code)
        tmp.close()
        r = subprocess.run(["python3", "-m", "py_compile", tmp.name], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr.strip() or r.stdout.strip())[:500]
    except subprocess.TimeoutExpired:
        return False, "Timeout checking syntax"
    except Exception as e:
        return False, str(e)[:500]
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def build_dependency_graph(repo_path: str) -> dict[str, list[str]]:
    head = get_git_head(repo_path)
    if head:
        key = make_key("agent:build_dependency_graph", repo_path, head)
        cached = cache_get(key, max_age=86400)
        if cached is not None:
            return cached
    imports_by_file: dict[str, list[str]] = {}
    for root, dirs, files in os.walk(repo_path):
        for f in files:
            if not f.endswith(".py"):
                continue
            fpath = os.path.join(root, f)
            rel = os.path.relpath(fpath, repo_path)
            try:
                with open(fpath) as fh:
                    tree = ast.parse(fh.read())
            except SyntaxError:
                continue
            deps = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        deps.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        deps.add(node.module.split(".")[0])
            imports_by_file[rel] = sorted(deps)

    # Build reverse map: module -> files that import it
    reverse: dict[str, list[str]] = defaultdict(list)
    for importer, deps in imports_by_file.items():
        for dep in deps:
            reverse[dep].append(importer)
    result = dict(reverse)
    if head:
        cache_set(key, result)
    return result


def skill_get_affected_files(target_file: str, repo_path: str = None, graph: dict = None) -> str:
    """Find all files that depend on target_file, to detect cascading breakage."""
    if graph is None:
        if not repo_path:
            return "(no repo_path provided)"
        graph = build_dependency_graph(repo_path)
    module = target_file.replace(".py", "").replace("/", ".")
    affected = set()
    for mod, importers in graph.items():
        if target_file.endswith(".py") and mod in (module, module.split(".")[-1]):
            for imp in importers:
                affected.add(imp)
        elif module.startswith(mod):
            for imp in importers:
                affected.add(imp)
    if not affected:
        return "(no downstream dependents)"
    result = [f"Files affected by changes to {target_file}:"]
    for af in sorted(affected):
        result.append(f"  {af}")
    return "\n".join(result)

def _run_command(cmd: list[str], cwd: str, timeout: int = 60) -> str:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = result.stdout.strip()
        err = result.stderr.strip()
        if err:
            out += f"\nSTDERR:\n{err}" if out else f"STDERR:\n{err}"
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return "(timeout)"
    except FileNotFoundError as e:
        return f"(command not found: {e})"
    except Exception as e:
        return f"(error: {e})"


def skill_read_file(file_path: str) -> str:
    """Read a file from the filesystem."""
    try:
        with open(file_path) as f:
            return f.read()
    except Exception as e:
        return f"Error reading {file_path}: {e}"


def skill_write_file(file_path: str, content: str) -> str:
    """Write content to a file."""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w") as f:
            f.write(content)
        return f"Written: {file_path}"
    except Exception as e:
        return f"Error writing {file_path}: {e}"


def skill_run_formatter(file_path: str, target_repo: str = None) -> str:
    """Run isort, black, and ruff fix on a Python file."""
    cwd = target_repo or os.path.dirname(file_path)
    rel_path = os.path.relpath(file_path, cwd)
    try:
        with open(file_path) as f:
            content_hash = hashlib.sha256(f.read().encode()).hexdigest()
        cache_key = make_key("agent:run_formatter", file_path, content_hash)
        cached = cache_get(cache_key, max_age=86400)
        if cached is not None:
            return cached
    except Exception:
        cache_key = None
    parts = []
    for tool in [
        ["isort", "--profile", "black", rel_path],
        ["black", "--target-version", "py312", rel_path],
        ["ruff", "check", "--fix", rel_path],
    ]:
        try:
            r = subprocess.run(tool, cwd=cwd, capture_output=True, text=True, timeout=30)
            if r.stdout.strip():
                parts.append(r.stdout.strip())
            if r.stderr.strip():
                parts.append(r.stderr.strip())
        except Exception as e:
            parts.append(f"{tool[0]}: {e}")
    result = "\n".join(parts) if parts else "(formatted cleanly)"
    if cache_key:
        cache_set(cache_key, result)
    return result


def skill_validate_syntax(file_path: str) -> str:
    """Check Python syntax validity of a file."""
    try:
        with open(file_path) as f:
            content_hash = hashlib.sha256(f.read().encode()).hexdigest()
        cache_key = make_key("agent:validate_syntax", file_path, content_hash)
        cached = cache_get(cache_key, max_age=86400)
        if cached is not None:
            return cached
    except Exception:
        cache_key = None
    r = subprocess.run(["python3", "-m", "py_compile", file_path], capture_output=True, text=True)
    result = "(syntax OK)" if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip())
    if cache_key:
        cache_set(cache_key, result)
    return result


def skill_run_mypy(file_path: str, target_repo: str = None) -> str:
    """Run mypy type checking on a file."""
    cwd = target_repo or os.path.dirname(file_path)
    rel_path = os.path.relpath(file_path, cwd)
    try:
        with open(file_path) as f:
            content_hash = hashlib.sha256(f.read().encode()).hexdigest()
        cache_key = make_key("agent:run_mypy", file_path, content_hash)
        cached = cache_get(cache_key, max_age=86400)
        if cached is not None:
            return cached
    except Exception:
        cache_key = None
    r = subprocess.run(["uv", "run", "mypy", "--check-untyped-defs", rel_path], cwd=cwd, capture_output=True, text=True, timeout=60)
    result = "(mypy OK)" if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip())
    if cache_key:
        cache_set(cache_key, result)
    return result


def skill_run_pytest(test_path: str, target_repo: str = None) -> str:
    """Run pytest on a specific test file or directory."""
    cwd = target_repo or os.path.dirname(test_path)
    r = subprocess.run(["uv", "run", "pytest", test_path, "-v", "--tb=short"], cwd=cwd, capture_output=True, text=True, timeout=120)
    return r.stdout.strip()[-5000:] if len(r.stdout) > 5000 else r.stdout.strip() or r.stderr.strip()


def skill_git_commit(file_paths: list[str], message: str, target_repo: str = None) -> str:
    """Stage given files and create a git commit."""
    cwd = target_repo or os.getcwd()
    try:
        add = subprocess.run(["git", "add", *file_paths], cwd=cwd, capture_output=True, text=True, timeout=30)
        if add.returncode != 0:
            return f"git add failed: {add.stderr.strip()}"
        r = subprocess.run(["git", "commit", "-m", message], cwd=cwd, capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or r.stderr.strip() or "(commit created)"
    except Exception as e:
        return f"Error: {e}"


def skill_run_lint(file_path: str, target_repo: str = None) -> str:
    """Run ruff linter checks on a Python file."""
    cwd = target_repo or os.path.dirname(file_path)
    rel_path = os.path.relpath(file_path, cwd)
    try:
        with open(file_path) as f:
            content_hash = hashlib.sha256(f.read().encode()).hexdigest()
        cache_key = make_key("agent:run_lint", file_path, content_hash)
        cached = cache_get(cache_key, max_age=86400)
        if cached is not None:
            return cached
    except Exception:
        cache_key = None
    r = subprocess.run(["ruff", "check", rel_path], cwd=cwd, capture_output=True, text=True, timeout=30)
    result = r.stdout.strip() or r.stderr.strip() or "(no lint issues)"
    if cache_key:
        cache_set(cache_key, result)
    return result


def skill_bundle_size(path: str) -> str:
    """Analyze file sizes under a path (for bundle/asset size analysis)."""
    if os.path.isfile(path):
        size = os.path.getsize(path)
        return f"{path}: {size:,} bytes ({size/1024:.1f} KB)"
    total = 0
    entries = []
    for root, dirs, names in os.walk(path):
        for name in names:
            fp = os.path.join(root, name)
            try:
                s = os.path.getsize(fp)
                total += s
                entries.append((s, fp))
            except OSError:
                pass
    entries.sort(reverse=True)
    lines = [f"Total: {total:,} bytes ({total/1024:.1f} KB) across {len(entries)} files"]
    for s, fp in entries[:20]:
        lines.append(f"  {s:>8,}  {os.path.relpath(fp, path)}")
    return "\n".join(lines)


def skill_audit_accessibility(file_path: str) -> str:
    """Scan an HTML file for common accessibility issues (alt text, labels, etc.)."""
    try:
        with open(file_path) as f:
            content = f.read()
    except Exception as e:
        return f"Error reading {file_path}: {e}"
    import re
    issues = []
    imgs = re.findall(r'<img[^>]+>', content, re.IGNORECASE)
    for img in imgs:
        if 'alt=' not in img.lower():
            issues.append(f"  <img> missing alt attribute: {img[:80]}")
    inputs = re.findall(r'<input[^>]+>', content, re.IGNORECASE)
    for inp in inputs:
        if 'aria-label=' not in inp.lower() and 'aria-labelledby=' not in inp.lower() and 'label' not in content.lower()[:500]:
            issues.append(f"  <input> may lack accessible label: {inp[:80]}")
    if '<html' in content.lower() and 'lang=' not in content.lower()[:500]:
        issues.append("  <html> tag missing lang attribute")
    if not issues:
        return "(no common accessibility issues detected)"
    return "Accessibility issues found:\n" + "\n".join(issues)


def build_default_skills(target_repo: str = None) -> list[Skill]:
    return [
        Skill("read_file",         "Read a file from the filesystem",                            skill_read_file,
              {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
        Skill("write_file",        "Write content to a file (creates dirs if needed)",           skill_write_file,
              {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}),
        Skill("run_formatter",     "Format a Python file with isort, black, and ruff",           lambda file_path: skill_run_formatter(file_path, target_repo),
              {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
        Skill("validate_syntax",   "Check Python syntax validity via py_compile",                skill_validate_syntax,
              {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
        Skill("run_mypy",          "Run mypy type checking on a Python file",                    lambda file_path: skill_run_mypy(file_path, target_repo),
              {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
        Skill("run_pytest",        "Run pytest on a test file/directory, returns last 5K chars", lambda test_path: skill_run_pytest(test_path, target_repo),
              {"type": "object", "properties": {"test_path": {"type": "string"}}, "required": ["test_path"]}),
        Skill("git_commit",        "Stage files and create a git commit",                        lambda file_paths, message: skill_git_commit(file_paths, message, target_repo),
              {"type": "object", "properties": {"file_paths": {"type": "array", "items": {"type": "string"}}, "message": {"type": "string"}}, "required": ["file_paths", "message"]}),
        Skill("run_lint",          "Run ruff linter checks on a Python file",                    lambda file_path: skill_run_lint(file_path, target_repo),
              {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
        Skill("bundle_size",       "Analyze file sizes under a path (bundle/asset analysis)",   skill_bundle_size,
              {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
        Skill("audit_accessibility", "Scan HTML file for common accessibility issues",           skill_audit_accessibility,
              {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
        Skill("get_affected_files", "Find files that depend on target_file (cascading breakage)", lambda file_path: skill_get_affected_files(file_path, target_repo),
              {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
    ]

# ── Convenience Builder ───────────────────────────────────────────────────────

def build_default_registry(target_repo: str = None, gemini_api_key: str = None) -> AgentRegistry:
    registry = AgentRegistry()
    registry.load_default_agents(gemini_api_key)
    for skill in build_default_skills(target_repo):
        registry.register_skill(skill)
    return registry
