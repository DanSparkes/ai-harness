"""Shared Ollama payload policies for slow local models.

Two behaviours matter when running large (20+ GB) local models:

* ``keep_alive`` — unloading after every request forces a multi-minute
  cold reload per pass. Default is to keep the model resident; set
  ``OLLAMA_KEEP_ALIVE=0`` to restore unload-per-request (useful when
  swapping models mid-A/B on a memory-constrained machine).
* ``think`` — thinking-capable models (qwen3.5/3.8, gpt-oss, ...) emit
  unbounded hidden reasoning tokens at decode speed (single-digit t/s
  on laptops), which dwarfs the visible answer and blows request
  timeouts. Default disables thinking; set ``OLLAMA_ALLOW_THINK=1`` to
  restore it (e.g. when quality with reasoning is the metric).

Ollama rejects the ``think`` field with HTTP 400 for models without the
thinking capability, so callers should retry once without the field on
that error (see ``strip_think``).
"""

from __future__ import annotations

import os


def ollama_keep_alive() -> str:
    return os.getenv("OLLAMA_KEEP_ALIVE", "30m")


def thinking_allowed() -> bool:
    return os.getenv("OLLAMA_ALLOW_THINK", "0") == "1"


def with_think_disabled(payload: dict) -> dict:
    if not thinking_allowed():
        payload["think"] = False
    return payload


def strip_think(payload: dict) -> bool:
    """Remove the think override from a payload. Returns True if changed."""
    if "think" in payload:
        del payload["think"]
        return True
    return False


def is_unsupported_think_error(response) -> bool:
    """True when a 400 response is Ollama rejecting `think` on a
    non-thinking model (safe to retry without the field)."""
    status = getattr(response, "status_code", None)
    if status != 400:
        return False
    try:
        body = response.text.lower()
    except AttributeError:
        return False
    if "think" not in body:
        return False
    return "not support" in body or "unsupported" in body or "does not" in body
