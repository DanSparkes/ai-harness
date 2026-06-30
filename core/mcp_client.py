import contextlib
import json
import os
import queue
import subprocess
import threading
import uuid
from contextlib import suppress
from typing import Any

JSON_RPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"

USE_NDJSON = True  # newline-delimited JSON (modern MCP stdio)


class MCPError(Exception):
    pass


class MCPConnectionError(MCPError):
    pass


class MCPToolError(MCPError):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        self.data = None
        super().__init__(code, message)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


def _encode_message(msg: dict) -> bytes:
    return json.dumps(msg, ensure_ascii=False).encode() + b"\n"


def _decode_message(data: bytes) -> tuple[dict | None, bytes]:
    newline_pos = data.find(b"\n")
    if newline_pos == -1:
        return None, data
    line = data[:newline_pos]
    remaining = data[newline_pos + 1 :]
    if not line.strip():
        return None, remaining
    try:
        return json.loads(line), remaining
    except json.JSONDecodeError as e:
        raise MCPError(f"Failed to decode MCP message: {e}") from e


class _StdioTransport:
    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ):
        self._command = command
        self._args = args or []
        self._env = env
        self._cwd = cwd
        self._process: subprocess.Popen | None = None
        self._buf = b""
        self._lock = threading.Lock()
        self._response_queue: queue.Queue = queue.Queue()
        self._pending: dict[str, queue.Queue] = {}
        self._reader_thread: threading.Thread | None = None
        self._running = False

    def connect(self):
        merged_env = os.environ.copy()
        if self._env:
            merged_env.update(self._env)
        try:
            self._process = subprocess.Popen(
                [self._command, *self._args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
                cwd=self._cwd,
            )
        except FileNotFoundError as e:
            raise MCPConnectionError(
                f"MCP server command not found: {self._command}. "
                f"Ensure it is installed and available in PATH."
            ) from e

        self._running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def disconnect(self):
        self._running = False
        if self._process:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.wait(timeout=5)
            self._process = None

    def send_request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        msg_id = str(uuid.uuid4())
        request = {
            "jsonrpc": JSON_RPC_VERSION,
            "id": msg_id,
            "method": method,
            "params": params or {},
        }
        result_queue: queue.Queue = queue.Queue()

        with self._lock:
            self._pending[msg_id] = result_queue
            if self._process and self._process.stdin:
                self._process.stdin.write(_encode_message(request))
                self._process.stdin.flush()

        try:
            response = result_queue.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(msg_id, None)
            raise MCPError(
                f"MCP request timed out after {timeout}s: {method}"
            ) from None

        if "error" in response:
            err = response["error"]
            raise MCPToolError(err.get("code", 0), err.get("message", "Unknown error"))
        return response.get("result", {})

    def send_notification(self, method: str, params: dict[str, Any] | None = None):
        notification = {
            "jsonrpc": JSON_RPC_VERSION,
            "method": method,
            "params": params or {},
        }
        with self._lock:
            if self._process and self._process.stdin:
                self._process.stdin.write(_encode_message(notification))
                self._process.stdin.flush()

    def _reader_loop(self):
        import select

        while self._running and self._process and self._process.stdout:
            try:
                r, _, _ = select.select([self._process.stdout], [], [], 0.5)
                if not r:
                    continue
                chunk = os.read(self._process.stdout.fileno(), 65536)
                if not chunk:
                    break
                self._buf += chunk
                while True:
                    msg, self._buf = _decode_message(self._buf)
                    if msg is None:
                        break
                    self._handle_message(msg)
            except Exception:
                break

    def _handle_message(self, msg: dict):
        msg_id = msg.get("id")
        if msg_id is not None:
            with self._lock:
                result_queue = self._pending.pop(str(msg_id), None)
            if result_queue:
                result_queue.put(msg)
        else:
            pass

    def _drain_stderr(self):
        if not self._process or not self._process.stderr:
            return
        with contextlib.suppress(Exception):
            for _ in iter(self._process.stderr.readline, b""):
                pass


class _HTTPTransport:
    def __init__(self, server_url: str, api_key: str | None = None):
        import requests as req_lib

        self._requests = req_lib
        self._server_url = server_url.rstrip("/")
        self._api_key = api_key
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    def connect(self):
        pass

    def disconnect(self):
        pass

    def send_request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        msg_id = str(uuid.uuid4())
        request: dict[str, Any] = {
            "jsonrpc": JSON_RPC_VERSION,
            "id": msg_id,
            "method": method,
            "params": params or {},
        }
        try:
            response = self._requests.post(
                self._server_url, json=request, headers=self._headers, timeout=timeout
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            raise MCPConnectionError(
                f"HTTP transport error for {self._server_url}: {e}"
            ) from e

        if "error" in data:
            err = data["error"]
            raise MCPToolError(err.get("code", 0), err.get("message", "Unknown error"))
        return data.get("result", {})

    def send_notification(self, method: str, params: dict[str, Any] | None = None):
        pass


class _BuiltinTransport:
    def __init__(self, server_instance):
        self._server = server_instance
        self._initialized = False

    def connect(self):
        result = self._server.handle_request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "local-harness", "version": "1.0.0"},
            },
        )
        self._initialized = True
        return result

    def disconnect(self):
        self._initialized = False

    def send_request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        if not self._initialized and method != "initialize":
            raise MCPConnectionError("MCP client not initialized")
        result = self._server.handle_request(method, params or {})
        if isinstance(result, Exception):
            raise result
        return result

    def send_notification(self, method: str, params: dict[str, Any] | None = None):
        pass


class MCPBuiltinServer:
    def handle_request(self, method: str, params: dict[str, Any]) -> Any:
        raise NotImplementedError


class MCPClient:
    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self._transport: _StdioTransport | _HTTPTransport | _BuiltinTransport | None = (
            None
        )
        self._server_info: dict[str, Any] = {}
        self._capabilities: dict[str, Any] = {}
        self._tools_cache: list[dict[str, Any]] | None = None

    def connect(self, init_timeout: float = 15.0):
        transport_type = self.config.get("type", "stdio")
        if transport_type == "stdio":
            command = self.config["command"]
            args = self.config.get("args", [])
            env = self.config.get("env")
            cwd = self.config.get("cwd")
            self._transport = _StdioTransport(command, args, env, cwd)
        elif transport_type == "http":
            url = self.config["url"]
            api_key = self.config.get("api_key")
            self._transport = _HTTPTransport(url, api_key)
        elif transport_type == "builtin":
            module_path = self.config["module"]
            server_instance = self._import_builtin(module_path)
            self._transport = _BuiltinTransport(server_instance)
        else:
            raise MCPError(f"Unknown MCP transport type: {transport_type}")

        self._transport.connect()
        result = self._transport.send_request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "local-harness", "version": "1.0.0"},
            },
            timeout=init_timeout,
        )
        self._server_info = result.get("serverInfo", {})
        self._capabilities = result.get("capabilities", {})
        self._transport.send_notification("notifications/initialized")
        self._tools_cache = None
        return self._server_info

    def disconnect(self):
        if self._transport:
            with suppress(Exception):
                self._transport.send_request("shutdown", timeout=5)
            self._transport.disconnect()
            self._transport = None
        self._tools_cache = None

    def list_tools(self) -> list[dict[str, Any]]:
        if self._tools_cache is not None:
            return self._tools_cache
        if not self._transport:
            raise MCPConnectionError("MCP client not connected")
        result = self._transport.send_request("tools/list")
        self._tools_cache = result.get("tools", [])
        return self._tools_cache

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        if not self._transport:
            raise MCPConnectionError("MCP client not connected")
        result = self._transport.send_request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout
        )
        return result.get("content", [])

    def ping(self) -> bool:
        if not self._transport:
            return False
        try:
            self._transport.send_request("ping", timeout=5)
            return True
        except Exception:
            return False

    @staticmethod
    def _import_builtin(module_path: str) -> MCPBuiltinServer:
        parts = module_path.split(".")
        class_name = parts[-1]
        module_name = ".".join(parts[:-1])
        try:
            import importlib

            mod = importlib.import_module(module_name)
            cls = getattr(mod, class_name)
            return cls()
        except (ImportError, AttributeError) as e:
            raise MCPError(
                f"Failed to load builtin MCP server '{module_path}': {e}"
            ) from e
