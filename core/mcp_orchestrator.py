import json
import os
import sys
from typing import Any

from core.mcp_client import MCPClient, MCPError


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
            },
        },
    },
}


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
        with open(config_path, "r", encoding="utf-8") as f:
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
                client = MCPClient(name, cfg)
                info = client.connect(init_timeout=120.0)
                print(f"  MCP [{name}] connected: {info.get('name', 'unknown')} v{info.get('version', '?')}")
                self._clients[name] = client
                started.append(name)
            except MCPError as e:
                print(f"  MCP [{name}] failed to start: {e}")
            except Exception as e:
                print(f"  MCP [{name}] unexpected error: {e}")
        self._started = True
        return started

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

    def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
        client = self._clients.get(server_name)
        if not client:
            raise MCPError(f"MCP server '{server_name}' is not connected")
        return client.call_tool(tool_name, arguments, timeout=timeout)

    def search_codebase(self, pattern: str, server_name: str = "filesystem") -> list[dict]:
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

    def git_log(self, path: str | None = None, max_count: int = 10, server_name: str = "git") -> str:
        args = {"max_count": max_count}
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

    def remember(self, key: str, fact: str, tags: list[str] | None = None, server_name: str = "memory") -> str:
        try:
            content = self.call_tool(server_name, "remember", {
                "key": key, "fact": fact, "tags": tags or [],
            })
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else "(stored)"
        except MCPError as e:
            return f"(remember error: {e})"

    def recall(self, query: str = "", tags: list[str] | None = None, server_name: str = "memory") -> str:
        args = {}
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

    def think(self, thought: str, server_name: str = "thinking") -> str:
        try:
            content = self.call_tool(server_name, "sequential_thinking", {"thought": thought})
            texts = _parse_text_content(content)
            return "\n".join(texts) if texts else "(thinking complete)"
        except MCPError:
            return "(sequential thinking server not available)"

    def build_mcp_context_block(self) -> str:
        if not self.is_running:
            return ""
        sections = ["=== MCP TOOL WORKBENCH (available tools) ==="]
        sections.append(self.format_tools_for_prompt())
        sections.append("")
        sections.append("=== MCP MEMORY CONTEXT ===")
        memory = self.recall(tags=["active"])
        if memory and memory != "(no memories)":
            sections.append(memory)
        else:
            sections.append("(no active session memories)")
        sections.append("")
        sections.append("=== MCP GIT CONTEXT ===")
        status = self.git_status()
        if status and status != "(no output)":
            sections.append(f"Working tree status:\n{status}")
        sections.append("")
        return "\n".join(sections)

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
        git_name = "git" if "git" in self._clients else None
        if git_name:
            recent = self.git_log(max_count=10)
            if recent and not recent.startswith("(git_log error"):
                parts.append(f"\nRecent git history:\n{recent}")
            status = self.git_status()
            if status and not status.startswith("(git_status error") and status != "(no output)":
                parts.append(f"\nWorking tree:\n{status}")
        memory = self.recall(tags=["architectural_rule", "active"])
        if memory and memory != "(no memories)":
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
