import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from core.mcp_client import MCPBuiltinServer, MCPToolError


class GitServer(MCPBuiltinServer):
    def __init__(self):
        self._repo_path: str | None = None

    def handle_request(self, method: str, params: dict[str, Any]) -> Any:
        handlers = {
            "initialize": self._initialize,
            "tools/list": self._list_tools,
            "tools/call": self._call_tool,
            "ping": lambda _: {},
        }
        handler = handlers.get(method)
        if not handler:
            raise MCPToolError(-32601, f"Method not found: {method}")
        return handler(params)

    def _initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "git-server", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        }

    def _list_tools(self, params: dict) -> dict:
        return {"tools": [
            {
                "name": "git_log",
                "description": "Show commit logs for a file or the repository. Returns commit hashes, authors, dates, and messages.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Optional relative file path to filter history for"},
                        "max_count": {"type": "integer", "description": "Maximum number of commits to return (default 20)"},
                        "branch": {"type": "string", "description": "Branch name (default current branch)"},
                    },
                    "required": [],
                },
            },
            {
                "name": "git_diff",
                "description": "Show uncommitted changes or diff between commits for a specific file.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path to get diff for"},
                        "commit": {"type": "string", "description": "Optional commit hash to diff against (default: working tree vs HEAD)"},
                        "staged": {"type": "boolean", "description": "If true, show staged changes only"},
                    },
                    "required": [],
                },
            },
            {
                "name": "git_blame",
                "description": "Show blame annotation for a file (who last modified each line).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path to blame"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "git_status",
                "description": "Show the working tree status (modified, staged, untracked files).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "short": {"type": "boolean", "description": "Use short format (default false)"},
                    },
                    "required": [],
                },
            },
            {
                "name": "git_branch",
                "description": "List branches or show current branch.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "all": {"type": "boolean", "description": "Include remote branches (default false)"},
                    },
                    "required": [],
                },
            },
            {
                "name": "git_show",
                "description": "Show the details of a specific commit.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "commit": {"type": "string", "description": "Commit hash to show"},
                    },
                    "required": ["commit"],
                },
            },
            {
                "name": "git_set_repo",
                "description": "Set the repository path for subsequent git operations.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path to the git repository"},
                    },
                    "required": ["path"],
                },
            },
        ]}

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments", {})
        handlers = {
            "git_log": self._cmd_log,
            "git_diff": self._cmd_diff,
            "git_blame": self._cmd_blame,
            "git_status": self._cmd_status,
            "git_branch": self._cmd_branch,
            "git_show": self._cmd_show,
            "git_set_repo": self._cmd_set_repo,
        }
        handler = handlers.get(name)
        if not handler:
            raise MCPToolError(-32601, f"Tool not found: {name}")
        result = handler(args)
        return {"content": [{"type": "text", "text": result}]}

    def _git(self, cmd: list[str], cwd: str | None = None) -> str:
        repo = cwd or self._repo_path
        if not repo:
            return "Error: No repository path set. Call git_set_repo first."
        try:
            result = subprocess.run(
                ["git"] + cmd,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return f"Git error: {result.stderr.strip()}"
            return result.stdout.strip()
        except FileNotFoundError:
            return "Error: git is not installed or not in PATH"
        except subprocess.TimeoutExpired:
            return "Error: git command timed out"

    def _cmd_set_repo(self, args: dict) -> str:
        path = args.get("path", "")
        resolved = Path(path).resolve()
        if not resolved.exists():
            raise MCPToolError(-32000, f"Repository path does not exist: {path}")
        git_dir = resolved / ".git"
        if not git_dir.exists():
            raise MCPToolError(-32000, f"Not a git repository: {path}")
        self._repo_path = str(resolved)
        return f"Repository set to: {self._repo_path}"

    def _cmd_log(self, args: dict) -> str:
        cmd = ["log", "--oneline", "--no-decorate"]
        max_count = args.get("max_count", 20)
        cmd.extend([f"-{max_count}"])
        file_path = args.get("path")
        branch = args.get("branch")
        if branch:
            cmd.extend([branch, "--"])
        if file_path:
            cmd.append("--")
            cmd.append(file_path)
        output = self._git(cmd)
        if not output or output.startswith("Error"):
            return output or "(no commits)"
        return output

    def _cmd_diff(self, args: dict) -> str:
        cmd = ["diff"]
        if args.get("staged"):
            cmd.append("--cached")
        commit = args.get("commit")
        if commit:
            cmd.append(commit)
        file_path = args.get("path")
        if file_path:
            cmd.append("--")
            cmd.append(file_path)
        return self._git(cmd) or "(no diff)"

    def _cmd_blame(self, args: dict) -> str:
        file_path = args.get("path", "")
        if not file_path:
            raise MCPToolError(-32000, "path is required for git_blame")
        return self._git(["blame", file_path]) or "(empty)"

    def _cmd_status(self, args: dict) -> str:
        cmd = ["status"]
        if args.get("short"):
            cmd.append("--short")
        return self._git(cmd) or "(clean)"

    def _cmd_branch(self, args: dict) -> str:
        cmd = ["branch"]
        if args.get("all"):
            cmd.append("--all")
        return self._git(cmd) or "(no branches)"

    def _cmd_show(self, args: dict) -> str:
        commit = args.get("commit", "")
        if not commit:
            raise MCPToolError(-32000, "commit is required for git_show")
        return self._git(["show", "--stat", "--patch", commit]) or "(not found)"


class MemoryServer(MCPBuiltinServer):
    def __init__(self):
        self._facts: dict[str, list[dict]] = {}
        self._tags: dict[str, list[str]] = {}

    def handle_request(self, method: str, params: dict[str, Any]) -> Any:
        handlers = {
            "initialize": self._initialize,
            "tools/list": self._list_tools,
            "tools/call": self._call_tool,
            "ping": lambda _: {},
        }
        handler = handlers.get(method)
        if not handler:
            raise MCPToolError(-32601, f"Method not found: {method}")
        return handler(params)

    def _initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "memory-server", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        }

    def _list_tools(self, params: dict) -> dict:
        return {"tools": [
            {
                "name": "remember",
                "description": "Store a fact or architectural rule in persistent memory. The fact will be recallable across sessions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Unique identifier for this fact (e.g., 'auth:snake_case_json')"},
                        "fact": {"type": "string", "description": "The fact or rule to remember"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional tags for categorisation (e.g., ['django', 'api'])",
                        },
                    },
                    "required": ["key", "fact"],
                },
            },
            {
                "name": "recall",
                "description": "Search stored facts by text or tags. Returns all matching memories.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Text to search for in stored facts"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter by tags",
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "forget",
                "description": "Remove a stored fact by its key.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Key of the fact to remove"},
                    },
                    "required": ["key"],
                },
            },
            {
                "name": "list_all",
                "description": "List all stored facts with their keys, tags, and timestamps.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tag_filter": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional tag filter",
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "export_memories",
                "description": "Export all memories as a JSON string (for persistence across sessions).",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "import_memories",
                "description": "Import memories from a previously exported JSON string.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "string", "description": "JSON string from export_memories"},
                    },
                    "required": ["data"],
                },
            },
        ]}

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments", {})
        handlers = {
            "remember": self._cmd_remember,
            "recall": self._cmd_recall,
            "forget": self._cmd_forget,
            "list_all": self._cmd_list_all,
            "export_memories": self._cmd_export,
            "import_memories": self._cmd_import,
        }
        handler = handlers.get(name)
        if not handler:
            raise MCPToolError(-32601, f"Tool not found: {name}")
        result = handler(args)
        return {"content": [{"type": "text", "text": result}]}

    def _cmd_remember(self, args: dict) -> str:
        key = args.get("key", "")
        fact = args.get("fact", "")
        tags = args.get("tags", [])
        if key in self._facts:
            return f"Key '{key}' already exists. Use forget first to update it."
        self._facts[key] = [{
            "fact": fact,
            "tags": tags,
            "timestamp": time.time(),
        }]
        for tag in tags:
            self._tags.setdefault(tag, []).append(key)
        return f"Stored: {key}"

    def _cmd_recall(self, args: dict) -> str:
        query = args.get("query", "").lower()
        tag_filter = args.get("tags", [])
        results = []
        for key, entries in self._facts.items():
            for entry in entries:
                if tag_filter:
                    if not any(t in entry.get("tags", []) for t in tag_filter):
                        continue
                if query and query not in key.lower() and query not in entry["fact"].lower():
                    continue
                results.append({
                    "key": key,
                    "fact": entry["fact"],
                    "tags": entry.get("tags", []),
                })
        if not results:
            return "(no matching memories)"
        lines = []
        for r in results:
            tags_str = f" [{', '.join(r['tags'])}]" if r["tags"] else ""
            lines.append(f"  {r['key']}: {r['fact']}{tags_str}")
        return "\n".join(lines)

    def _cmd_forget(self, args: dict) -> str:
        key = args.get("key", "")
        if key in self._facts:
            del self._facts[key]
            for tag, keys in self._tags.items():
                if key in keys:
                    keys.remove(key)
            return f"Forgotten: {key}"
        return f"Key not found: {key}"

    def _cmd_list_all(self, args: dict) -> str:
        tag_filter = args.get("tag_filter", [])
        if not self._facts:
            return "(no memories stored)"
        lines = []
        for key, entries in self._facts.items():
            for entry in entries:
                if tag_filter and not any(t in entry.get("tags", []) for t in tag_filter):
                    continue
                tags_str = f" [{', '.join(entry.get('tags', []))}]" if entry.get("tags") else ""
                lines.append(f"  {key}: {entry['fact']}{tags_str}")
        return "\n".join(lines) if lines else "(no matching memories)"

    def _cmd_export(self, args: dict) -> str:
        return json.dumps({"facts": self._facts, "tags": self._tags}, indent=2)

    def _cmd_import(self, args: dict) -> str:
        data = args.get("data", "")
        try:
            parsed = json.loads(data)
            self._facts = parsed.get("facts", {})
            self._tags = parsed.get("tags", {})
            return f"Imported {len(self._facts)} memories"
        except json.JSONDecodeError as e:
            raise MCPToolError(-32000, f"Invalid memory data: {e}") from e
