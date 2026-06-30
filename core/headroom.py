from typing import Any

from headroom import CompressConfig, CompressResult
from headroom import compress as headroom_compress

__all__ = ["CompressConfig", "CompressResult", "CompressionManager", "compress"]

DEFAULT_MODEL = "claude-opus-4-20250514"
PROTECT_RECENT_DEFAULT = 0
TARGET_RATIO_DEFAULT = 0.3


def compress(
    messages: list[dict[str, Any]],
    model: str = DEFAULT_MODEL,
    compress_user_messages: bool = True,
    target_ratio: float | None = TARGET_RATIO_DEFAULT,
    protect_recent: int = PROTECT_RECENT_DEFAULT,
    **kwargs: Any,
) -> CompressResult:
    return headroom_compress(
        messages,
        model=model,
        compress_user_messages=compress_user_messages,
        target_ratio=target_ratio,
        protect_recent=protect_recent,
        **kwargs,
    )


class CompressionManager:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        compress_user_messages: bool = True,
        target_ratio: float | None = TARGET_RATIO_DEFAULT,
        protect_recent: int = PROTECT_RECENT_DEFAULT,
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
        result = headroom_compress(
            messages,
            model=self.model,
            compress_user_messages=self.compress_user_messages,
            target_ratio=self.target_ratio,
            protect_recent=self.protect_recent,
        )
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
        result = headroom_compress(
            messages,
            model=self.model,
            compress_user_messages=self.compress_user_messages,
            target_ratio=self.target_ratio,
            protect_recent=self.protect_recent,
        )
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
