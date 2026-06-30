import ast
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
        return {
            "tools": [
                {
                    "name": "git_log",
                    "description": "Show commit logs for a file or the repository. Returns commit hashes, authors, dates, and messages.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Optional relative file path to filter history for",
                            },
                            "max_count": {
                                "type": "integer",
                                "description": "Maximum number of commits to return (default 20)",
                            },
                            "branch": {
                                "type": "string",
                                "description": "Branch name (default current branch)",
                            },
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
                            "path": {
                                "type": "string",
                                "description": "Relative file path to get diff for",
                            },
                            "commit": {
                                "type": "string",
                                "description": "Optional commit hash to diff against (default: working tree vs HEAD)",
                            },
                            "staged": {
                                "type": "boolean",
                                "description": "If true, show staged changes only",
                            },
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
                            "path": {
                                "type": "string",
                                "description": "Relative file path to blame",
                            }
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
                            "short": {
                                "type": "boolean",
                                "description": "Use short format (default false)",
                            }
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
                            "all": {
                                "type": "boolean",
                                "description": "Include remote branches (default false)",
                            }
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
                            "commit": {
                                "type": "string",
                                "description": "Commit hash to show",
                            }
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
                            "path": {
                                "type": "string",
                                "description": "Absolute path to the git repository",
                            }
                        },
                        "required": ["path"],
                    },
                },
            ]
        }

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
                ["git", *cmd], cwd=repo, capture_output=True, text=True, timeout=30
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


_default_memory_persist_path: str | None = None


def set_memory_persist_path(path: str):
    global _default_memory_persist_path
    _default_memory_persist_path = path


class MemoryServer(MCPBuiltinServer):
    def __init__(self, persist_path: str | None = None):
        if persist_path is None:
            persist_path = _default_memory_persist_path
        self._facts: dict[str, list[dict]] = {}
        self._tags: dict[str, list[str]] = {}
        self._persist_path = persist_path
        self._dirty = False
        if persist_path:
            self._load_from_disk()

    def _load_from_disk(self):
        if self._persist_path is None:
            return
        try:
            path = Path(self._persist_path)
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
                self._facts = data.get("facts", {})
                self._tags = data.get("tags", {})
        except Exception:
            pass

    def _save_to_disk(self):
        if not self._persist_path:
            return
        try:
            path = Path(self._persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump({"facts": self._facts, "tags": self._tags}, f, indent=2)
        except Exception:
            pass

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
        return {
            "tools": [
                {
                    "name": "remember",
                    "description": "Store a fact or architectural rule in persistent memory. The fact will be recallable across sessions.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "Unique identifier for this fact (e.g., 'auth:snake_case_json')",
                            },
                            "fact": {
                                "type": "string",
                                "description": "The fact or rule to remember",
                            },
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
                            "query": {
                                "type": "string",
                                "description": "Text to search for in stored facts",
                            },
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
                            "key": {
                                "type": "string",
                                "description": "Key of the fact to remove",
                            }
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
                            }
                        },
                        "required": [],
                    },
                },
                {
                    "name": "export_memories",
                    "description": "Export all memories as a JSON string (for persistence across sessions).",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                },
                {
                    "name": "import_memories",
                    "description": "Import memories from a previously exported JSON string.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "data": {
                                "type": "string",
                                "description": "JSON string from export_memories",
                            }
                        },
                        "required": ["data"],
                    },
                },
            ]
        }

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
        self._facts[key] = [{"fact": fact, "tags": tags, "timestamp": time.time()}]
        for tag in tags:
            self._tags.setdefault(tag, []).append(key)
        self._dirty = True
        self._save_to_disk()
        return f"Stored: {key}"

    def _cmd_recall(self, args: dict) -> str:
        query = args.get("query", "").lower()
        tag_filter = args.get("tags", [])
        results = []
        for key, entries in self._facts.items():
            for entry in entries:
                if tag_filter and not any(
                    t in entry.get("tags", []) for t in tag_filter
                ):
                    continue
                if (
                    query
                    and query not in key.lower()
                    and query not in entry["fact"].lower()
                ):
                    continue
                results.append(
                    {"key": key, "fact": entry["fact"], "tags": entry.get("tags", [])}
                )
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
            for _tag, keys in self._tags.items():
                if key in keys:
                    keys.remove(key)
            self._dirty = True
            self._save_to_disk()
            return f"Forgotten: {key}"
        return f"Key not found: {key}"

    def _cmd_list_all(self, args: dict) -> str:
        tag_filter = args.get("tag_filter", [])
        if not self._facts:
            return "(no memories stored)"
        lines = []
        for key, entries in self._facts.items():
            for entry in entries:
                if tag_filter and not any(
                    t in entry.get("tags", []) for t in tag_filter
                ):
                    continue
                tags_str = (
                    f" [{', '.join(entry.get('tags', []))}]"
                    if entry.get("tags")
                    else ""
                )
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
            self._dirty = True
            self._save_to_disk()
            return f"Imported {len(self._facts)} memories"
        except json.JSONDecodeError as e:
            raise MCPToolError(-32000, f"Invalid memory data: {e}") from e


class LspServer(MCPBuiltinServer):
    def __init__(self):
        self._project_path: str | None = None

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
            "serverInfo": {"name": "lsp-server", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        }

    def _list_tools(self, params: dict) -> dict:
        return {
            "tools": [
                {
                    "name": "find_definition",
                    "description": "Find the definition of a Python symbol (class, function, variable). Resolves imports across files. Returns file path, line number, column, and surrounding code context.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "symbol": {
                                "type": "string",
                                "description": "Symbol name to find (e.g. 'Profile', 'get_queryset', 'views.profile_page')",
                            },
                            "from_file": {
                                "type": "string",
                                "description": "Optional relative file path to resolve the symbol from (improves import resolution)",
                            },
                            "project_path": {
                                "type": "string",
                                "description": "Absolute path to the project root (default: auto-detected)",
                            },
                        },
                        "required": ["symbol"],
                    },
                },
                {
                    "name": "find_references",
                    "description": "Find all references to a Python symbol across the project. Returns file paths, line numbers, columns, and surrounding code for each usage.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "symbol": {
                                "type": "string",
                                "description": "Symbol name to search for (e.g. 'Profile', 'UserResponse')",
                            },
                            "in_file": {
                                "type": "string",
                                "description": "Optional: restrict search to a single relative file path",
                            },
                            "project_path": {
                                "type": "string",
                                "description": "Absolute path to the project root (default: auto-detected)",
                            },
                        },
                        "required": ["symbol"],
                    },
                },
                {
                    "name": "list_symbols",
                    "description": "List all classes, functions, and methods defined in a Python file. Returns name, type, line range, and docstring summary.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "description": "Relative path to the Python file to analyze",
                            },
                            "project_path": {
                                "type": "string",
                                "description": "Absolute path to the project root (default: auto-detected)",
                            },
                        },
                        "required": ["file"],
                    },
                },
                {
                    "name": "get_code_context",
                    "description": "Get a block of code surrounding a specific line in a file. Useful for fetching the implementation after a definition is located.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "description": "Absolute path to the file",
                            },
                            "line": {
                                "type": "integer",
                                "description": "1-indexed line number to center the context around",
                            },
                            "lines_before": {
                                "type": "integer",
                                "description": "Number of lines to include before (default 5)",
                            },
                            "lines_after": {
                                "type": "integer",
                                "description": "Number of lines to include after (default 10)",
                            },
                        },
                        "required": ["file", "line"],
                    },
                },
            ]
        }

    def _call_tool(self, params: dict) -> Any:
        name = params.get("name", "")
        args = params.get("arguments", {})
        handlers = {
            "find_definition": self._find_definition,
            "find_references": self._find_references,
            "list_symbols": self._list_symbols,
            "get_code_context": self._get_code_context,
        }
        handler = handlers.get(name)
        if not handler:
            raise MCPToolError(-32601, f"Tool not found: {name}")
        return handler(args)

    def _resolve_project(self, args: dict) -> str:
        project = args.get("project_path") or self._project_path
        if project:
            return project
        if self._project_path:
            return self._project_path
        raise MCPToolError(
            -32000,
            "No project path set. Call with project_path or set via set_project.",
        )

    def _get_code_block(
        self, file_path: str, line: int, before: int = 5, after: int = 10
    ) -> str:
        try:
            with open(file_path, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return "(file not found)"
        start = max(0, line - 1 - before)
        end = min(len(lines), line - 1 + after)
        block = []
        for i in range(start, end):
            block.append(f"{i + 1:>6}: {lines[i].rstrip()}")
        return "\n".join(block)

    def _find_definition(self, args: dict) -> dict:
        symbol = args["symbol"]
        from_file = args.get("from_file")
        project = self._resolve_project(args)

        results = []
        try:
            import jedi

            project_obj = jedi.Project(project)
            if from_file:
                file_path = (
                    os.path.join(project, from_file)
                    if not os.path.isabs(from_file)
                    else from_file
                )
                if os.path.exists(file_path):
                    with open(file_path, encoding="utf-8") as f:
                        source = f.read()
                    script = jedi.Script(
                        code=source, path=file_path, project=project_obj
                    )
                    names = list(script.search(symbol, all_scopes=True))
                else:
                    names = []
            else:
                names = project_obj.search(symbol)

            for n in names:
                d_path = getattr(n, "module_path", None)
                if not d_path:
                    continue
                rel = os.path.relpath(str(d_path), project) if project else str(d_path)
                context = self._get_code_block(str(d_path), n.line)
                results.append(
                    {
                        "file": rel,
                        "line": n.line,
                        "column": getattr(n, "column", 0),
                        "name": n.name,
                        "type": getattr(n, "type", "unknown"),
                        "description": getattr(n, "description", ""),
                        "full_name": getattr(n, "full_name", ""),
                        "code_context": context,
                    }
                )
        except ImportError:
            results = self._find_definition_ast(symbol, project)

        if not results:
            results = self._find_definition_ast(symbol, project)

        return {"symbol": symbol, "definitions": results, "count": len(results)}

    def _find_definition_ast(self, symbol: str, project: str) -> list[dict]:
        results = []
        for root, _dirs, files in os.walk(project):
            for f in files:
                if not f.endswith(".py"):
                    continue
                file_path = os.path.join(root, f)
                try:
                    with open(file_path, encoding="utf-8") as fh:
                        tree = ast.parse(fh.read())
                except SyntaxError:
                    continue
                rel = os.path.relpath(file_path, project) if project else file_path
                for node in ast.iter_child_nodes(tree):
                    if (
                        isinstance(
                            node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                        )
                        and node.name == symbol
                    ):
                        context = self._get_code_block(file_path, node.lineno)
                        results.append(
                            {
                                "file": rel,
                                "line": node.lineno,
                                "column": node.col_offset,
                                "name": node.name,
                                "type": (
                                    "class"
                                    if isinstance(node, ast.ClassDef)
                                    else "function"
                                ),
                                "code_context": context,
                            }
                        )
        return results

    def _find_references(self, args: dict) -> dict:
        symbol = args["symbol"]
        in_file = args.get("in_file")
        project = self._resolve_project(args)

        results = []
        try:
            import jedi

            project_obj = jedi.Project(project)
            if in_file:
                file_path = (
                    os.path.join(project, in_file)
                    if not os.path.isabs(in_file)
                    else in_file
                )
                if os.path.exists(file_path):
                    with open(file_path, encoding="utf-8") as f:
                        source = f.read()
                    script = jedi.Script(
                        code=source, path=file_path, project=project_obj
                    )
                    refs = script.get_references(symbol, all_scopes=True)
                    for r in refs:
                        if r.module_path:
                            rel = os.path.relpath(str(r.module_path), project)
                            context = self._get_code_block(
                                str(r.module_path), r.line, before=2, after=2
                            )
                            results.append(
                                {
                                    "file": rel,
                                    "line": r.line,
                                    "column": r.column,
                                    "name": r.name,
                                    "code_context": context,
                                }
                            )
        except ImportError:
            pass

        return {"symbol": symbol, "references": results, "count": len(results)}

    def _list_symbols(self, args: dict) -> dict:
        file_path = args["file"]
        project = self._resolve_project(args)
        abs_path = (
            os.path.join(project, file_path)
            if not os.path.isabs(file_path)
            else file_path
        )

        if not os.path.exists(abs_path):
            raise MCPToolError(-32000, f"File not found: {file_path}")

        symbols = []
        try:
            with open(abs_path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except SyntaxError as e:
            raise MCPToolError(-32000, f"Syntax error in {file_path}: {e}") from e

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = []
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.append(
                            {"name": item.name, "line": item.lineno, "type": "method"}
                        )
                symbols.append(
                    {
                        "name": node.name,
                        "type": "class",
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                        "methods": methods,
                    }
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not any(
                isinstance(parent, ast.ClassDef)
                for parent in ast.walk(tree)
                if parent is not node and isinstance(parent, ast.ClassDef)
            ):
                symbols.append(
                    {
                        "name": node.name,
                        "type": "function",
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                    }
                )

        return {"file": file_path, "symbols": symbols, "count": len(symbols)}

    def _get_code_context(self, args: dict) -> dict:
        file_path = args["file"]
        line = args["line"]
        before = args.get("lines_before", 5)
        after = args.get("lines_after", 10)

        if not os.path.exists(file_path):
            raise MCPToolError(-32000, f"File not found: {file_path}")

        context = self._get_code_block(file_path, line, before, after)
        return {"file": file_path, "line": line, "code_context": context}
