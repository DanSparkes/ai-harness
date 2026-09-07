import re
import time
import os
from dataclasses import dataclass, field

import requests

from core.local_payload import (
    is_unsupported_think_error,
    ollama_keep_alive,
    strip_think,
    with_think_disabled,
)


@dataclass
class _CallStats:
    """Accumulated inference diagnostics for one execute_sequence call.

    All durations are in seconds; token counts are raw Ollama integers.
    Populated from the ``eval_count`` / ``*_duration`` fields Ollama returns
    in every non-streaming response body — these are thrown away by default
    but are the only reliable way to distinguish prompt-eval time from
    generation time from model-load time.
    """

    llm_calls: int = 0          # total HTTP calls that reached the model
    retries: int = 0            # calls that were retried (timeout / 5xx / 429)
    prompt_tokens: int = 0      # tokens evaluated from the prompt
    generated_tokens: int = 0   # tokens the model generated (eval_count)
    load_ms: float = 0.0        # model load time in ms  (load_duration / 1e6)
    prompt_eval_ms: float = 0.0 # prompt eval time in ms (prompt_eval_duration / 1e6)
    generation_ms: float = 0.0  # generation time in ms  (eval_duration / 1e6)
    # Per-pass snapshots so callers can print pass-level lines.
    # Each entry mirrors the fields above but scoped to one pass.
    passes: list[dict] = field(default_factory=list)

    def add_pass(
        self,
        *,
        calls: int,
        retries: int,
        prompt_tokens: int,
        generated_tokens: int,
        load_ms: float,
        prompt_eval_ms: float,
        generation_ms: float,
    ) -> None:
        self.llm_calls += calls
        self.retries += retries
        self.prompt_tokens += prompt_tokens
        self.generated_tokens += generated_tokens
        self.load_ms += load_ms
        self.prompt_eval_ms += prompt_eval_ms
        self.generation_ms += generation_ms
        self.passes.append(
            {
                "calls": calls,
                "retries": retries,
                "prompt_tokens": prompt_tokens,
                "generated_tokens": generated_tokens,
                "load_ms": load_ms,
                "prompt_eval_ms": prompt_eval_ms,
                "generation_ms": generation_ms,
            }
        )

    def pass_summary(self, idx: int) -> str:
        """One-line summary for pass ``idx`` (0-based)."""
        if idx >= len(self.passes):
            return ""
        p = self.passes[idx]
        parts = [f"calls={p['calls']}"]
        if p["retries"]:
            parts.append(f"retries={p['retries']}")
        if p["prompt_tokens"]:
            parts.append(f"prompt_tok={p['prompt_tokens']:,}")
        if p["generated_tokens"]:
            parts.append(f"gen_tok={p['generated_tokens']:,}")
        timings: list[str] = []
        if p["load_ms"] > 0:
            timings.append(f"load={p['load_ms']/1000:.1f}s")
        if p["prompt_eval_ms"] > 0:
            timings.append(f"prompt_eval={p['prompt_eval_ms']/1000:.1f}s")
        if p["generation_ms"] > 0:
            timings.append(f"generation={p['generation_ms']/1000:.1f}s")
        if timings:
            parts.append("  ".join(timings))
        return "  ".join(parts)

    def chunk_summary(self) -> str:
        """Multi-line diagnostic block printed at the end of a chunk."""
        lines = ["   [Inference stats]"]
        lines.append(f"     LLM calls      : {self.llm_calls}")
        if self.retries:
            lines.append(f"     Retries        : {self.retries}")
        if self.prompt_tokens:
            lines.append(f"     Prompt tokens  : {self.prompt_tokens:,}")
        if self.generated_tokens:
            lines.append(f"     Gen tokens     : {self.generated_tokens:,}")
        total_model_ms = self.load_ms + self.prompt_eval_ms + self.generation_ms
        if total_model_ms > 0:
            lines.append(
                f"     Time breakdown : "
                f"load={self.load_ms/1000:.1f}s  "
                f"prompt_eval={self.prompt_eval_ms/1000:.1f}s  "
                f"generation={self.generation_ms/1000:.1f}s"
            )
            if self.generated_tokens and self.generation_ms > 0:
                tps = self.generated_tokens / (self.generation_ms / 1000)
                lines.append(f"     Gen throughput : {tps:.1f} tok/s")
        return "\n".join(lines)


def _parse_ollama_stats(data: dict) -> dict:
    """Extract timing/token fields from an Ollama /api/chat response body.

    All ``*_duration`` fields are nanoseconds; we convert to milliseconds here
    so the rest of the code works in a human-readable unit.  Missing fields
    default to 0 so callers can always add without guarding.

    Fields documented at https://github.com/ollama/ollama/blob/main/docs/api.md
    """
    ns_to_ms = 1e-6
    return {
        "prompt_tokens": int(data.get("prompt_eval_count") or 0),
        "generated_tokens": int(data.get("eval_count") or 0),
        "load_ms": float((data.get("load_duration") or 0) * ns_to_ms),
        "prompt_eval_ms": float((data.get("prompt_eval_duration") or 0) * ns_to_ms),
        "generation_ms": float((data.get("eval_duration") or 0) * ns_to_ms),
    }

# Matches <think>...</think> reasoning blocks emitted by thinking models
# (e.g. Qwen3 ThinkingCap / DeepSeek-R1). Many GGUF builds return these
# inline in `content` rather than Ollama's separate `thinking` field.
_THINK_RE = re.compile(r"<think>" + r".*?</think>" + r"", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Remove <think>...</think> blocks; also handle unclosed <think> tags.
    Returns the cleaned text. If only thinking content existed, returns a
    placeholder indicating the model failed to produce output."""
    # First, strip all closed <think>...</think> blocks
    cleaned = _THINK_RE.sub("", text).strip()

    # If result starts with unclosed <think>, try to extract content after it
    if cleaned.startswith("<think>"):
        # Look for content after the think block (separated by blank line)
        # Capture everything after the first blank line following <think>
        match = re.search(r"<think>[\s\S]*?\n\n([\s\S]+)$", cleaned, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            # Only accept if it looks like actual review content (starts with header, capital, or markdown)
            if candidate and (candidate[0].isupper() or candidate.startswith('#') or candidate.startswith('[')):
                cleaned = candidate
            else:
                return "# Review Generation Failed\n\nThe model produced only reasoning tokens without a final review. This may indicate the model was overloaded or the request timed out during generation."
        else:
            # No blank line found, so no actual content after think block
            return "# Review Generation Failed\n\nThe model produced only reasoning tokens without a final review. This may indicate the model was overloaded or the request timed out during generation."

    if not cleaned:
        return "# Review Generation Failed\n\nThe model produced only reasoning tokens without a final review. This may indicate the model was overloaded or the request timed out during generation."

    return cleaned


class StatefulHarnessRunner:
    """Manages multi-pass execution against local Ollama or cloud OpenAI-compatible models."""

    def __init__(
        self,
        model_name: str,
        base_url: str = "http://localhost:11434",
        api_key: str | None = None,
        fallback_model_name: str | None = None,
        local_fallback_model: str | None = None,
        num_ctx: int = 65536,
        temperature: float | None = None,
        request_timeout: float | None = None,
        seed: int | None = None,
        use_openai_format: bool = False,
    ):
        self.model_name = model_name
        self.fallback_model_name = fallback_model_name or model_name
        self.local_fallback_model = local_fallback_model
        self.api_key = api_key
        self.num_ctx = num_ctx
        self.seed = seed
        self.use_openai_format = use_openai_format

        self.is_cloud = "gemini" in model_name.lower() or bool(api_key)

        self.temperature = (
            temperature
            if temperature is not None
            else (0.0 if "coder" in model_name.lower() else 0.4)
        )

        self.request_timeout = request_timeout or (120 if self.is_cloud else 1200)

        if self.use_openai_format:
            if not base_url.endswith("/chat/completions"):
                base_url = base_url.rstrip("/") + "/chat/completions"
            self.api_url = base_url
        else:
            self.api_url = f"{base_url}/api/chat"

    def unload(self):
        pass

    def _local_options(self, temperature: float) -> dict:
        """Build Ollama options for a local request, including seed if set.

        ``num_predict`` caps per-call output length so a regeneration loop on
        a local 27B model can't run for hours on a single chunk.  The ceiling
        is intentionally generous (4096 tokens ≈ 16-20K chars) — enough for a
        thorough per-chunk review — but tight enough to abort a runaway loop
        within minutes rather than hours.  Override via the NUM_PREDICT env var
        when reviewing very large files that legitimately need more output.
        """

        num_predict = int(os.environ.get("NUM_PREDICT", "4096"))
        opts: dict = {
            "num_ctx": self.num_ctx,
            "temperature": temperature,
            "top_p": 0.9,
            "num_predict": num_predict,
        }
        if self.seed is not None:
            opts["seed"] = self.seed
        return opts

    def _call_with_retry(
        self,
        url: str,
        payload: dict,
        headers: dict,
        max_retries: int = 5,
        base_delay: float = 2.0,
        extra_retry_codes: set[int] | None = None,
        timeout: float | None = None,
    ) -> tuple[requests.Response, int]:
        """POST to ``url`` with retry logic.

        Returns ``(response, calls_made)`` where ``calls_made`` is the total
        number of HTTP requests that actually reached the network (including
        retried attempts).  Previously returned only the response; callers that
        only unpack the first element are unaffected by the added second value.
        """
        retry_codes = {429, 503} | (extra_retry_codes or set())
        request_timeout = timeout if timeout is not None else self.request_timeout
        response: requests.Response | None = None
        calls_made = 0
        for attempt in range(max_retries + 1):
            try:
                calls_made += 1
                response = requests.post(
                    url, json=payload, headers=headers, timeout=request_timeout
                )
            except requests.exceptions.Timeout:
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    print(
                        f"   ⏳ Request timed out. Retrying in {delay}s ({attempt + 1}/{max_retries})..."
                    )
                    time.sleep(delay)
                    continue
                raise
            except requests.exceptions.RequestException as e:
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    print(
                        f"   ⏳ Request error ({type(e).__name__}). Retrying in {delay}s ({attempt + 1}/{max_retries})..."
                    )
                    time.sleep(delay)
                    continue
                raise
            if response.status_code not in retry_codes:
                if is_unsupported_think_error(response) and strip_think(payload):
                    continue
                return response, calls_made
            # On 500 errors, also try removing the think parameter (Forge proxy
            # may return 500 when it doesn't understand the think field)
            if response.status_code == 500 and "think" in payload:
                print(
                    "   ⚠️  Backend 500 with think parameter, retrying without it..."
                )
                strip_think(payload)
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    time.sleep(delay)
                    continue
                return response, calls_made
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                code = response.status_code
                print(
                    f"   \u23f3 Cloud API {code}. Retrying in {delay}s ({attempt + 1}/{max_retries})..."
                )
                time.sleep(delay)
        return response, calls_made

    def execute_sequence(
        self, system_prompt: str, passes: list[str], fallback_prompt: str | None = None
    ) -> list[dict]:
        messages = [{"role": "system", "content": system_prompt}]
        execution_history: list[dict] = []
        stats = _CallStats()

        for idx, pass_prompt in enumerate(passes):
            messages.append({"role": "user", "content": pass_prompt})

            headers = {"Content-Type": "application/json"}
            if self.is_cloud and self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            temperature = self.temperature

            if self.use_openai_format:
                payload = {
                    "model": self.model_name,
                    "messages": messages,
                    "stream": False,
                    "temperature": temperature,
                    "top_p": 0.9,
                }
            else:
                payload = with_think_disabled(
                    {
                        "model": self.model_name,
                        "messages": messages,
                        "stream": False,
                        "keep_alive": ollama_keep_alive(),
                        "options": self._local_options(temperature),
                    }
                )

            print(f"   --- Pass {idx + 1} / {len(passes)} ---")
            pass_t0 = time.time()

            if self.use_openai_format:
                response, calls_made = self._call_with_retry(self.api_url, payload, headers)
            else:
                response, calls_made = self._call_with_retry(
                    self.api_url,
                    payload,
                    headers,
                    max_retries=3,
                    base_delay=5.0,
                    extra_retry_codes={500},
                )

            if self.is_cloud and response.status_code in (429, 503):
                label = (
                    "429" if response.status_code == 429 else "503 (retries exhausted)"
                )
                print(
                    f"   \u26a0\ufe0f Cloud API unavailable ({label}). Falling back to local Ollama..."
                )
                self.is_cloud = False
                self.api_url = "http://localhost:11434/api/chat"
                self.model_name = self.fallback_model_name

                fb_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": fallback_prompt or pass_prompt},
                ]
                fb_payload = with_think_disabled(
                    {
                        "model": self.model_name,
                        "messages": fb_messages,
                        "stream": False,
                        "keep_alive": ollama_keep_alive(),
                        "options": self._local_options(temperature),
                    }
                )
                fb_headers = {"Content-Type": "application/json"}

                assistant_response = ""
                fb_ollama_stats: dict = {}
                fb_calls = 0
                try:
                    fb_response, fb_calls = self._call_with_retry(
                        self.api_url,
                        fb_payload,
                        fb_headers,
                        max_retries=3,
                        base_delay=5.0,
                        extra_retry_codes={500},
                    )
                    fb_response.raise_for_status()
                    fb_data = fb_response.json()
                    fb_ollama_stats = _parse_ollama_stats(fb_data)
                    assistant_response = fb_data.get("message", {}).get(
                        "content", ""
                    ) or fb_data.get("message", {}).get("thinking", "")
                except Exception as e:
                    print(
                        f"   \u274c Local fallback also failed: "
                        f"{type(e).__name__}: {e}"
                    )

                if not assistant_response:
                    assistant_response = (
                        "# Basic Diff Scan\n\n"
                        "Unable to generate review (cloud down and local fallback failed).\n"
                    )

                stats.add_pass(
                    calls=calls_made + fb_calls,
                    retries=max(0, calls_made + fb_calls - 1),
                    **fb_ollama_stats if fb_ollama_stats else {
                        "prompt_tokens": 0,
                        "generated_tokens": 0,
                        "load_ms": 0.0,
                        "prompt_eval_ms": 0.0,
                        "generation_ms": 0.0,
                    },
                )
                elapsed = time.time() - pass_t0
                print(
                    f"   [Done] Pass {idx + 1} in {elapsed:.1f}s  "
                    f"({len(assistant_response)} chars)  "
                    f"{stats.pass_summary(idx)}"
                )

                execution_history.append(
                    {
                        "pass_index": idx + 1,
                        "prompt": "(fallback)",
                        "output": assistant_response,
                    }
                )
                break

            response.raise_for_status()
            response_data = response.json()

            if self.use_openai_format:
                assistant_response = response_data["choices"][0]["message"]["content"]
                # OpenAI/Gemini format doesn't expose Ollama timing fields.
                pass_ollama_stats: dict = {
                    "prompt_tokens": 0,
                    "generated_tokens": 0,
                    "load_ms": 0.0,
                    "prompt_eval_ms": 0.0,
                    "generation_ms": 0.0,
                }
            else:
                pass_ollama_stats = _parse_ollama_stats(response_data)
                assistant_response = response_data.get("message", {}).get("content", "")
                thinking = response_data.get("message", {}).get("thinking", "")
                if not assistant_response and thinking:
                    print(
                        f"   [info] Content empty, using thinking output ({len(thinking)} chars)"
                    )
                    assistant_response = thinking
                elif not assistant_response:
                    print("   ⚠️  LLM returned empty content (no thinking either)")

                    if (
                        self.local_fallback_model
                        and self.local_fallback_model != self.model_name
                    ):
                        print(
                            f"   -> Falling back to local model [{self.local_fallback_model}]..."
                        )
                        fb_messages = [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": fallback_prompt or pass_prompt},
                        ]
                        fb_payload = with_think_disabled(
                            {
                                "model": self.local_fallback_model,
                                "messages": fb_messages,
                                "stream": False,
                                "keep_alive": ollama_keep_alive(),
                                "options": self._local_options(temperature),
                            }
                        )
                        try:
                            fb_response, fb_calls = self._call_with_retry(
                                self.api_url,
                                fb_payload,
                                headers,
                                max_retries=2,
                                base_delay=3.0,
                                extra_retry_codes={500},
                            )
                            fb_response.raise_for_status()
                            fb_data = fb_response.json()
                            # Merge fallback Ollama stats into this pass.
                            fb_stats = _parse_ollama_stats(fb_data)
                            for k, v in fb_stats.items():
                                pass_ollama_stats[k] = (
                                    pass_ollama_stats.get(k, 0) + v
                                )
                            calls_made += fb_calls
                            fallback_output = fb_data.get("message", {}).get(
                                "content", ""
                            ) or fb_data.get("message", {}).get("thinking", "")
                            if fallback_output:
                                print(
                                    f"   -> Fallback succeeded ({len(fallback_output)} chars)"
                                )
                                assistant_response = fallback_output
                        except Exception as e:
                            print(
                                f"   \u274c Local fallback model also failed: "
                                f"{type(e).__name__}: {e}"
                            )

                    if not assistant_response:
                        assistant_response = (
                            "# Basic Diff Scan\n\nUnable to generate review.\n"
                        )

            # Strip <think> reasoning blocks before storing/chaining. GGUF
            # thinking models often inline them in `content`; leaving them in
            # would (a) bloat every subsequent pass — each pass re-sends prior
            # assistant turns — and (b) pollute the saved review report.
            assistant_response = _strip_think(assistant_response)

            retries_this_pass = max(0, calls_made - 1)
            stats.add_pass(
                calls=calls_made,
                retries=retries_this_pass,
                **pass_ollama_stats,
            )

            elapsed = time.time() - pass_t0
            print(
                f"   [Done] Pass {idx + 1} in {elapsed:.1f}s  "
                f"({len(assistant_response)} chars)  "
                f"{stats.pass_summary(idx)}"
            )

            execution_history.append(
                {
                    "pass_index": idx + 1,
                    "prompt": pass_prompt,
                    "output": assistant_response,
                }
            )

            # Feed this pass's output back into the conversation so multi-pass
            # reasoning actually chains. Without this, pass N+1 cannot see
            # pass N's analysis and the "two-pass" design collapses into two
            # independent single-pass prompts.
            messages.append({"role": "assistant", "content": assistant_response})

        if stats.llm_calls:
            print(stats.chunk_summary())

        return execution_history
