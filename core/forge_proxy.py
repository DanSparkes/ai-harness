"""Forge proxy lifecycle manager.

Provides a context manager that starts a Forge guardrails proxy between the
harness and a local Ollama backend, giving every LLM call rescue parsing,
retry nudges, and response validation transparently.

Usage::

    from core.forge_proxy import ForgeProxy
    from core.config import get_config

    cfg = get_config()
    with ForgeProxy(cfg) as forge:
        # forge.url == "http://localhost:8081"
        # All LLM calls route through the proxy
        ...

Env vars (read via RuntimeConfig):
    FORGE_PROXY_ENABLED   enable the proxy (1/true/yes)
    FORGE_PROXY_PORT      listen port (default 8081)
    FORGE_BACKEND_URL     backend URL (default http://localhost:11434)
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.config import RuntimeConfig

# Lazy import: forge-guardrails is an optional dependency.  Importing at module
# level would break harnesses that don't have it installed.
_forge_available: bool | None = None


def _check_forge_available() -> bool:
    """Return True if forge-guardrails is importable."""
    global _forge_available
    if _forge_available is None:
        try:
            from forge.proxy.proxy import ProxyServer  # noqa: F401

            _forge_available = True
        except ImportError:
            _forge_available = False
    return _forge_available


# Reentrancy guard: a single Forge proxy is shared across nested
# ``with ForgeProxy(cfg)`` blocks.  ``_active_server`` holds the running
# ProxyServer and ``_active_depth`` counts how many contexts are using it.
# Only the outermost (owning) context tears the server down, so a harness
# entrypoint (``run_evaluation.py``) and a nested call site (e.g.
# ``run_multipass``) no longer fight over the same port — which previously
# raised ``OSError [Errno 48] address already in use``.
_active_server: object | None = None
_active_depth: int = 0


class ForgeProxy:
    """Context manager that manages a Forge proxy subprocess.

    Reentrant: if a proxy is already running, a nested ``with ForgeProxy(cfg)``
    adopts it instead of binding a second socket. Only the owning (outermost)
    context stops the server on exit — preventing both orphaned processes and
    double-bind crashes.

    Attributes:
        url: The proxy's base URL (e.g. ``http://localhost:8081``).
    """

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._server: Any = None
        self._owns = False
        self.url: str = ""

    def __enter__(self) -> ForgeProxy:
        global _active_server, _active_depth

        if not self._config.forge_enabled:
            return self
        if not _check_forge_available():
            raise ImportError(
                "Forge proxy is enabled (FORGE_PROXY_ENABLED=1) but "
                "forge-guardrails is not installed.  Install it with: "
                "pip install forge-guardrails"
            )

        # Adopt an already-running proxy instead of binding a second socket.
        if _active_server is not None:
            _active_depth += 1
            self._owns = False
            self._server = _active_server
            self.url = getattr(_active_server, "url", "")
            return self

        from forge.proxy.proxy import ProxyServer

        print(f"   🔥 Starting Forge proxy on :{self._config.forge_port}...")
        server = ProxyServer(
            backend_url=self._config.forge_backend_url,
            backend="ollama",
            port=self._config.forge_port,
            budget_tokens=self._config.num_ctx,
            max_retries=3,
            rescue_enabled=True,
            inject_respond_tool=True,
            backend_timeout=self._config.forge_backend_timeout,
        )
        server.start()
        _active_server = server
        _active_depth = 1
        self._owns = True
        self._server = server
        self.url = server.url
        print(f"   🔥 Forge proxy ready at {self.url}")
        return self

    def __exit__(
        self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object
    ) -> None:
        global _active_server, _active_depth

        if self._server is None:
            return

        # Nested (adopting) context: just release our hold and bail early.
        if not self._owns:
            _active_depth = max(0, _active_depth - 1)
            self._server = None
            return

        # Owning context: tear down only when the last user has released.
        _active_depth = max(0, _active_depth - 1)
        if _active_depth == 0:
            try:
                print("   🔥 Stopping Forge proxy...")
                self._server.stop()
            except Exception:
                pass
            _active_server = None
        self._server = None


@contextlib.contextmanager
def forge_context(config: RuntimeConfig):
    """Thin wrapper: yields ForgeProxy when enabled, no-op when disabled.

    Usage::

        with forge_context(cfg) as forge:
            url = forge.url if forge else cfg.base_url
    """
    proxy = ForgeProxy(config)
    with proxy:
        yield proxy if config.forge_enabled else None
