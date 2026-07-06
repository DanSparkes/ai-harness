"""Headroom compression stub - bypasses external package dependency."""

from __future__ import annotations

import copy
from typing import Any


class CompressConfig:
    def __init__(self, **kwargs: Any):
        self.__dict__.update(kwargs)


class CompressResult:
    def __init__(
        self,
        messages: list[dict[str, Any]],
        tokens_before: int = 0,
        tokens_after: int = 0,
        compression_ratio: float = 0.0,
    ):
        self.messages = messages
        self.tokens_before = tokens_before
        self.tokens_after = tokens_after
        self.tokens_saved = tokens_before - tokens_after
        self.compression_ratio = compression_ratio


def compress(
    messages: list[dict[str, Any]],
    model: str = "claude-opus-4-20250514",
    compress_user_messages: bool = True,
    target_ratio: float | None = 0.3,
    protect_recent: int = 0,
    **kwargs: Any,
) -> CompressResult:
    return _noop_compress(messages)


def _noop_compress(messages: list[dict[str, Any]]) -> CompressResult:
    """Return messages unchanged as a passthrough."""
    return CompressResult(
        messages=copy.deepcopy(messages),
        tokens_before=0,
        tokens_after=0,
        compression_ratio=0.0,
    )


class CompressionManager:
    def __init__(
        self,
        model: str = "claude-opus-4-20250514",
        compress_user_messages: bool = True,
        target_ratio: float | None = 0.3,
        protect_recent: int = 0,
    ):
        self.model = model
        self.compress_user_messages = compress_user_messages
        self.target_ratio = target_ratio
        self.protect_recent = protect_recent
        self._stats: list[dict[str, Any]] = []

    def compress_context(
        self, text: str, role: str = "user"
    ) -> tuple[str, CompressResult]:
        messages = [{"role": role, "content": text}]
        result = _noop_compress(messages)
        compressed_text = result.messages[0]["content"] if result.messages else text
        self._stats.append(
            {
                "tokens_before": result.tokens_before,
                "tokens_after": result.tokens_after,
                "tokens_saved": result.tokens_saved,
                "compression_ratio": result.compression_ratio,
            }
        )
        return compressed_text, result

    def compress_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], CompressResult]:
        result = _noop_compress(messages)
        self._stats.append(
            {
                "tokens_before": result.tokens_before,
                "tokens_after": result.tokens_after,
                "tokens_saved": result.tokens_saved,
                "compression_ratio": result.compression_ratio,
            }
        )
        return result.messages, result

    @property
    def total_tokens_saved(self) -> int:
        return sum(s["tokens_saved"] for s in self._stats)

    @property
    def total_tokens_before(self) -> int:
        return sum(s["tokens_before"] for s in self._stats)

    @property
    def total_compression_ratio(self) -> float:
        total_before = self.total_tokens_before
        if total_before == 0:
            return 0.0
        return self.total_tokens_saved / total_before

    def summary(self) -> str:
        if not self._stats:
            return "  Headroom: no compression runs yet"
        ratio = self.total_compression_ratio * 100
        return (
            f"  Headroom compression: {self.total_tokens_saved:,} of "
            f"{self.total_tokens_before:,} tokens saved ({ratio:.1f}% reduction)"
        )

    def reset_stats(self):
        self._stats.clear()
