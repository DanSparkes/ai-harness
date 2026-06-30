import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from core.cache import CACHE_DIR
from core.mcp_client import MCPClient, MCPError
from core.mcp_servers import set_memory_persist_path

MCP_CONFIG_SCHEMA = {
    "mcp_servers": {
        "type": "object",
        "patternProperties": {
            "^[a-zA-Z_][a-zA-Z0-9_]*$": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["stdio", "http", "builtin"]},
                    "command": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "url": {"type": "string"},
                    "api_key": {"type": "string"},
                    "module": {"type": "string"},
                    "env": {"type": "object"},
                    "cwd": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "description": {"type": "string"},
                },
                "required": ["type"],
            }
        },
    }
}
MCP_DEFAULT_INIT_TIMEOUT = 15.0  # Seconds to wait per-server initialize


def _tool_to_openai_format(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema", {})
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": schema,
        },
    }


def _tool_to_prompt_format(server_name: str, tool: dict[str, Any]) -> str:
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})
    required = schema.get("required", [])
    param_lines = []
    for pname, pinfo in props.items():
        req_mark = " (required)" if pname in required else ""
        ptype = pinfo.get("type", "string")
        pdesc = pinfo.get("description", "")
        param_lines.append(f"      {pname}: {ptype}{req_mark} — {pdesc}")
    params_str = "\n".join(param_lines) if param_lines else "      (no parameters)"
    return (
        f"    [{server_name}] {tool['name']}\n"
        f"      Description: {tool.get('description', '')}\n"
        f"      Parameters:\n{params_str}"
    )


class MCPOrchestrator:
    def __init__(self, config_path: str | None = None, target_repo: str | None = None):
        self._clients: dict[str, MCPClient] = {}
        self._server_config: dict[str, dict] = {}
        self._target_repo = target_repo
        self._started = False
        if config_path:
            self.load_config(config_path)

    def load_config(self, config_path: str):
        if not os.path.exists(config_path):
            print(f"  MCP config not found: {config_path}")
            return
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
        servers = raw.get("mcp_servers", {})
        for name, cfg in servers.items():
            if cfg.get("enabled", True):
                self._server_config[name] = cfg

    def set_target_repo(self, repo_path: str):
        self._target_repo = repo_path

    def start(self) -> list[str]:
        started = []
        for name, cfg in self._server_config.items():
            try:
                cfg = self._resolve_template_vars(cfg)
                client = MCPClient(name, cfg)
                # Per-server timeout: use shorter timeouts for HTTP servers,
                # stdio defaults to 15s (prevents single hung server from holding
                # up the whole workbench). Config can override via init_timeout key.
                server_timeout = self._server_config[name].get("init_timeout")
                if not server_timeout:
                    transport_type = cfg.get("type", "stdio")
                    if transport_type == "http":
                        server_timeout = 10.0
                    else:
                        server_timeout = MCP_DEFAULT_INIT_TIMEOUT
                info = client.connect(init_timeout=server_timeout)
                print(
                    f"  MCP [{name}] connected: {info.get('name', 'unknown')} v{info.get('version', '?')}"
                )
                self._clients[name] = client
                started.append(name)
            except MCPError as e:
                print(f"  MCP [{name}] failed to start: {e}")
            except Exception as e:
                print(f"  MCP [{name}] unexpected error: {type(e).__name__}: {e}")
        self._started = True
        return started

    def _resolve_template_vars(self, cfg: dict) -> dict:
        """Replace {{REPO_PATH}} in config values with the target repo path."""
        if not self._target_repo:
            return cfg
        resolved: dict[str, Any] = {}
        for key, val in cfg.items():
            if isinstance(val, str):
                resolved[key] = val.replace("{{REPO_PATH}}", self._target_repo)
            elif isinstance(val, list):
                resolved[key] = [
                    (
                        item.replace("{{REPO_PATH}}", self._target_repo)
                        if isinstance(item, str)
                        else item
                    )
                    for item in val
                ]
            elif isinstance(val, dict):
                resolved[key] = self._resolve_template_vars(val)
            else:
                resolved[key] = val
        return resolved

    def stop(self):
        for name, client in self._clients.items():
            try:
                client.disconnect()
                print(f"  MCP [{name}] disconnected")
            except Exception as e:
                print(f"  MCP [{name}] disconnect error: {e}")
        self._clients.clear()
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started and len(self._clients) > 0

    def get_client(self, name: str) -> MCPClient | None:
        return self._clients.get(name)

    def get_all_tools(self) -> dict[str, list[dict[str, Any]]]:
        result = {}
        for name, client in self._clients.items():
            try:
                tools = client.list_tools()
                if tools:
                    result[name] = tools
            except Exception:
                result[name] = []
        return result

    def format_tools_as_openai_functions(self) -> list[dict[str, Any]]:
        functions = []
        for name, client in self._clients.items():
            try:
                tools = client.list_tools()
                for tool in tools:
                    func = _tool_to_openai_format(tool)
                    func["function"]["name"] = f"{name}_{tool['name']}"
                    functions.append(func)
            except Exception:
                pass
        return functions

    def format_tools_for_prompt(self, server_filter: list[str] | None = None) -> str:
        sections = []
        for name, client in self._clients.items():
            if server_filter and name not in server_filter:
                continue
            try:
                tools = client.list_tools()
                if not tools:
                    continue
                tool_lines = []
                for tool in tools:
                    tool_lines.append(_tool_to_prompt_format(name, tool))
                sections.append(f"  [{name}] server — tools:\n" + "\n".join(tool_lines))
            except Exception:
                sections.append(f"  [{name}] server — (error listing tools)")
        if not sections:
            return "  (no MCP tools available)"
        return "\n\n".join(sections)

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> Any:
        client = self._clients.get(server_name)
        if not client:
            raise MCPError(f"MCP server '{server_name}' is not connected")
        return client.call_tool(tool_name, arguments, timeout=timeout)

    def search_codebase(
        self, pattern: str, server_name: str = "filesystem"
    ) -> list[str]:
        try:
            content = self.call_tool(server_name, "search_files", {"pattern": pattern})
            return _parse_text_content(content)
        except MCPError:
            return []

    def read_file(self, path: str, server_name: str = "filesystem") -> str | None:
        try:
            content = self.call_tool(server_name, "read_file", {"path": path})
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else None
        except MCPError:
            return None

    def list_directory(self, path: str, server_name: str = "filesystem") -> list[str]:
        try:
            content = self.call_tool(server_name, "list_directory", {"path": path})
            return _parse_text_content(content)
        except MCPError:
            return []

    def git_log(
        self, path: str | None = None, max_count: int = 10, server_name: str = "git"
    ) -> str:
        args: dict[str, Any] = {"max_count": max_count}
        if path:
            args["path"] = path
        try:
            content = self.call_tool(server_name, "git_log", args)
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else "(no output)"
        except MCPError as e:
            return f"(git_log error: {e})"

    def git_diff(self, path: str | None = None, server_name: str = "git") -> str:
        args = {}
        if path:
            args["path"] = path
        try:
            content = self.call_tool(server_name, "git_diff", args)
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else "(no output)"
        except MCPError as e:
            return f"(git_diff error: {e})"

    def git_status(self, server_name: str = "git") -> str:
        try:
            content = self.call_tool(server_name, "git_status")
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else "(no output)"
        except MCPError as e:
            return f"(git_status error: {e})"

    def remember(
        self,
        key: str,
        fact: str,
        tags: list[str] | None = None,
        server_name: str = "memory",
    ) -> str:
        try:
            content = self.call_tool(
                server_name, "remember", {"key": key, "fact": fact, "tags": tags or []}
            )
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else "(stored)"
        except MCPError as e:
            return f"(remember error: {e})"

    def recall(
        self,
        query: str = "",
        tags: list[str] | None = None,
        server_name: str = "memory",
    ) -> str:
        args: dict[str, Any] = {}
        if query:
            args["query"] = query
        if tags:
            args["tags"] = tags
        try:
            content = self.call_tool(server_name, "recall", args)
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else "(no memories)"
        except MCPError as e:
            return f"(recall error: {e})"

    def lsp_find_definition(
        self, symbol: str, from_file: str | None = None, server_name: str = "lsp"
    ) -> str:
        args: dict[str, Any] = {"symbol": symbol}
        if from_file:
            args["from_file"] = from_file
        try:
            result = self.call_tool(server_name, "find_definition", args, timeout=30)
            texts = _parse_text_content(result)
            return "\n".join(texts) if texts else "(not found)"
        except MCPError as e:
            return f"(lsp_find_definition error: {e})"

    def lsp_list_symbols(self, file: str, server_name: str = "lsp") -> str:
        try:
            result = self.call_tool(
                server_name, "list_symbols", {"file": file}, timeout=30
            )
            texts = _parse_text_content(result)
            return "\n".join(texts) if texts else "(no symbols)"
        except MCPError as e:
            return f"(lsp_list_symbols error: {e})"

    def lsp_get_code_context(
        self,
        file: str,
        line: int,
        before: int = 5,
        after: int = 10,
        server_name: str = "lsp",
    ) -> str:
        try:
            result = self.call_tool(
                server_name,
                "get_code_context",
                {
                    "file": file,
                    "line": line,
                    "lines_before": before,
                    "lines_after": after,
                },
                timeout=15,
            )
            texts = _parse_text_content(result)
            return "\n".join(texts) if texts else "(empty)"
        except MCPError as e:
            return f"(lsp_get_code_context error: {e})"

    def headroom_compress(self, content: str, server_name: str = "headroom") -> str:
        try:
            result = self.call_tool(
                server_name, "headroom_compress", {"content": content}, timeout=120
            )
            texts = _parse_text_content(result)
            return "\n".join(texts) if texts else "(empty)"
        except MCPError as e:
            return f"(headroom_compress error: {e})"

    def headroom_retrieve(self, hash_key: str, server_name: str = "headroom") -> str:
        try:
            content = self.call_tool(
                server_name, "headroom_retrieve", {"hash": hash_key}, timeout=30
            )
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else "(empty)"
        except MCPError as e:
            return f"(headroom_retrieve error: {e})"

    def headroom_stats(self, server_name: str = "headroom") -> str:
        try:
            content = self.call_tool(server_name, "headroom_stats", timeout=30)
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else "(empty)"
        except MCPError as e:
            return f"(headroom_stats error: {e})"

    def think(self, thought: str, server_name: str = "thinking") -> str:
        try:
            content = self.call_tool(
                server_name, "sequential_thinking", {"thought": thought}
            )
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else "(thinking complete)"
        except MCPError:
            return "(sequential thinking server not available)"

    def build_mcp_context_block(self, tags: list[str] | None = None) -> str:
        if not self.is_running:
            return ""
        sections = ["=== MCP TOOL WORKBENCH (available tools) ==="]
        sections.append(self.format_tools_for_prompt())
        sections.append("")
        sections.append("=== MCP MEMORY CONTEXT ===")
        memory = self.recall(tags=tags or ["active"])
        if memory and memory != "(no memories)":
            sections.append(memory)
        else:
            sections.append("(no active session memories)")
        sections.append("")
        git_block = self.build_git_context()
        if git_block:
            sections.append(git_block)
        sections.append("")
        return "\n".join(sections)

    def build_git_context(self, max_count: int = 10) -> str:
        if not self.is_running:
            return ""
        parts = []
        try:
            status = self.git_status()
            if status and status != "(no output)":
                parts.append(f"Working Tree:\n{status}")
        except Exception:
            pass
        try:
            recent = self.git_log(max_count=max_count)
            if recent and not recent.startswith("("):
                parts.append(f"Recent Commits:\n{recent}")
        except Exception:
            pass
        return "\n\n".join(parts)

    def recall_tagged(self, tags: list[str], default: str = "") -> str:
        if not self.is_running:
            return default
        try:
            memory = self.recall(tags=tags)
            if memory and memory != "(no memories)":
                return memory
        except Exception:
            pass
        return default

    def _discover_django_app_labels(self) -> list[str]:
        labels: list[str] = []
        if not self._target_repo:
            return labels
        repo = Path(self._target_repo)
        for entry in repo.iterdir():
            if entry.is_dir() and (entry / "models.py").exists():
                labels.append(entry.name)
        return labels

    def build_django_live_context(self) -> tuple[str, dict]:
        if not self.is_running:
            return "", {}
        django_client = self.get_client("django")
        if not django_client:
            return "", {}

        raw = {}
        sections = []

        try:
            app_info = self.call_tool("django", "application_info", timeout=30)
            if app_info:
                combined = "\n".join(
                    c["text"]
                    for c in app_info
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                raw["application_info"] = combined
                sections.append(f"=== Live Django App Info ===\n{combined}")
        except Exception:
            pass

        try:
            urls = self.call_tool("django", "list_urls", timeout=30)
            if urls:
                combined = "\n".join(
                    c["text"]
                    for c in urls
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                raw["urls"] = combined
                sections.append(f"=== Live Django URL Patterns ===\n{combined}")
        except Exception:
            pass

        app_labels = self._discover_django_app_labels()
        try:
            kwargs = {"app_labels": app_labels} if app_labels else {}
            models = self.call_tool("django", "list_models", kwargs, timeout=30)
            if models:
                combined = "\n".join(
                    c["text"]
                    for c in models
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                raw["models"] = combined
                sections.append(
                    f"=== Live Django Models (from django-ai-boost) ===\n{combined}"
                )
        except Exception:
            pass

        try:
            schema = self.call_tool("django", "database_schema", timeout=30)
            if schema:
                combined = "\n".join(
                    c["text"]
                    for c in schema
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                raw["database_schema"] = combined
                sections.append(f"=== Live Database Schema ===\n{combined}")
        except Exception:
            pass

        return "\n\n".join(sections), raw

    def build_codebase_memory_context(
        self, timeout_index: float = 120.0
    ) -> tuple[str, dict]:
        if not self.is_running:
            return "", {}
        server = self.get_client("codebase-memory")
        if not server:
            return "", {}
        if not self._target_repo:
            return "", {}

        raw = {}
        sections = []
        project_name = self._target_repo.lstrip("/").replace("/", "-")

        # 1. Index (idempotent — content-hash based, only re-parses changed files)
        try:
            result = self.call_tool(
                "codebase-memory",
                "index_repository",
                {"repo_path": self._target_repo},
                timeout=timeout_index,
            )
            if result:
                combined = "\n".join(
                    c["text"]
                    for c in result
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                raw["index_result"] = combined
        except Exception:
            pass

        # 2. Architecture overview (languages, packages, entry points, routes,
        #    hotspots, boundaries, layers, clusters, file tree)
        try:
            arch = self.call_tool(
                "codebase-memory",
                "get_architecture",
                {"aspects": ["all"], "project": project_name},
                timeout=30,
            )
            if arch:
                combined = "\n".join(
                    c["text"]
                    for c in arch
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                raw["architecture"] = combined
                sections.append(
                    f"=== Codebase Architecture (from codebase-memory-mcp) ===\n{combined}"
                )
        except Exception:
            pass

        # 3. Graph schema (node/edge counts for reference)
        try:
            schema = self.call_tool(
                "codebase-memory",
                "get_graph_schema",
                {"project": project_name},
                timeout=15,
            )
            if schema:
                combined = "\n".join(
                    c["text"]
                    for c in schema
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                raw["graph_schema"] = combined
                sections.append(f"=== Codebase Memory Graph Schema ===\n{combined}")
        except Exception:
            pass

        return "\n\n".join(sections), raw

    def discover_project_context(self) -> str:
        parts = []
        parts.append("=== MCP CODEBASE EXPLORATION ===")
        fs_name = "filesystem" if "filesystem" in self._clients else None
        if fs_name and self._target_repo:
            listing = self.list_directory(self._target_repo, fs_name)
            if listing:
                parts.append("Project root contents:")
                for entry in listing:
                    parts.append(f"  {entry}")
        git_block = self.build_git_context(max_count=10)
        if git_block:
            parts.append(f"\n{git_block}")
        memory = self.recall_tagged(tags=["architectural_rule", "active"])
        if memory:
            parts.append(f"\nArchitectural rules from memory:\n{memory}")
        return "\n".join(parts)

    def execute_tool_call_from_llm(self, tool_call: dict[str, Any]) -> str:
        func_name = tool_call.get("name", "")
        args = tool_call.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return f"Error: invalid JSON arguments: {args}"
        parts = func_name.split("_", 1)
        if len(parts) < 2:
            return f"Error: tool name '{func_name}' missing server prefix"
        server_name = parts[0]
        tool_name = parts[1]
        try:
            content = self.call_tool(server_name, tool_name, args)
            return _format_content_for_llm(content)
        except MCPError as e:
            return f"Error calling {server_name}/{tool_name}: {e}"
        except Exception as e:
            return f"Unexpected error calling {server_name}/{tool_name}: {e}"


def init_orchestrator(config_path: str, target_repo: str) -> MCPOrchestrator | None:
    if not os.path.exists(config_path):
        return None
    memory_path = str(CACHE_DIR / "memories.json")
    set_memory_persist_path(memory_path)
    orch = MCPOrchestrator(config_path, target_repo=target_repo)
    started = orch.start()
    if started:
        with suppress(Exception):
            orch.call_tool("git", "git_set_repo", {"path": target_repo})
        return orch
    return None


def _parse_text_content(content: Any) -> list[str]:
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                val = item.get("text", "")
                if val:
                    texts.append(val)
        return texts
    if isinstance(content, str):
        return [content]
    return []


def _format_content_for_llm(content: Any) -> str:
    texts = _parse_text_content(content)
    return "\n".join(texts) if texts else "(empty result)"
