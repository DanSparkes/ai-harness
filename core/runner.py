import re
import time

import requests

# Matches <think>...</think> reasoning blocks emitted by thinking models
# (e.g. Qwen3 ThinkingCap / DeepSeek-R1). Many GGUF builds return these
# inline in `content` rather than Ollama's separate `thinking` field.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Remove <think>...</think> blocks; never return empty (preserve a
    thinking-only reply intact so a review is never silently dropped)."""
    cleaned = _THINK_RE.sub("", text).strip()
    return cleaned or text


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
        # Reproducibility seed for local Ollama requests (options.seed). Cloud
        # backends ignore it. None lets Ollama choose (non-reproducible).
        self.seed = seed
        # When True, use OpenAI chat-completions format (for Forge proxy or cloud).
        self.use_openai_format = use_openai_format

        self.is_cloud = "gemini" in model_name.lower() or bool(api_key)

        # Explicit temperature override; otherwise pick a sensible default
        # based on the model role (coder models -> deterministic, reasoning
        # models -> slightly creative).
        self.temperature = (
            temperature
            if temperature is not None
            else (0.0 if "coder" in model_name.lower() else 0.4)
        )

        # Per-request timeout. Local models (including via Forge proxy) can
        # stall on GPU/RAM pressure, so bound them generously. Only true cloud
        # calls get a short timeout since latency there is predictable.
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
        """Build Ollama options for a local request, including seed if set."""
        opts = {"num_ctx": self.num_ctx, "temperature": temperature, "top_p": 0.9}
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
    ) -> requests.Response:
        retry_codes = {429, 503} | (extra_retry_codes or set())
        request_timeout = timeout if timeout is not None else self.request_timeout
        response: requests.Response | None = None
        for attempt in range(max_retries + 1):
            try:
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
                return response
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                code = response.status_code
                print(
                    f"   \u23f3 Cloud API {code}. Retrying in {delay}s ({attempt + 1}/{max_retries})..."
                )
                time.sleep(delay)
        # response may be None if every attempt raised Timeout, but mypy
        # with ignore_missing_imports treats requests.Response as Any and
        # does not surface the implicit None possibility here.
        return response

    def execute_sequence(
        self, system_prompt: str, passes: list[str], fallback_prompt: str | None = None
    ) -> list[dict]:
        messages = [{"role": "system", "content": system_prompt}]
        execution_history: list[dict] = []

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
                payload = {
                    "model": self.model_name,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": "0",
                    "options": self._local_options(temperature),
                }

            print(f"   --- Pass {idx + 1} / {len(passes)} ---")
            pass_t0 = time.time()

            if self.use_openai_format:
                response = self._call_with_retry(self.api_url, payload, headers)
            else:
                response = self._call_with_retry(
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
                fb_payload = {
                    "model": self.model_name,
                    "messages": fb_messages,
                    "stream": False,
                    "keep_alive": "0",
                    "options": self._local_options(temperature),
                }
                fb_headers = {"Content-Type": "application/json"}

                assistant_response = ""
                try:
                    fb_response = self._call_with_retry(
                        self.api_url,
                        fb_payload,
                        fb_headers,
                        max_retries=3,
                        base_delay=5.0,
                        extra_retry_codes={500},
                    )
                    fb_response.raise_for_status()
                    fb_data = fb_response.json()
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
            else:
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
                        fb_payload = {
                            "model": self.local_fallback_model,
                            "messages": fb_messages,
                            "stream": False,
                            "keep_alive": "0",
                            "options": self._local_options(temperature),
                        }
                        try:
                            fb_response = self._call_with_retry(
                                self.api_url,
                                fb_payload,
                                headers,
                                max_retries=2,
                                base_delay=3.0,
                                extra_retry_codes={500},
                            )
                            fb_response.raise_for_status()
                            fb_data = fb_response.json()
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

            elapsed = time.time() - pass_t0
            print(
                f"   [Done] Pass {idx + 1} in {elapsed:.1f}s  ({len(assistant_response)} chars)"
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

        return execution_history
