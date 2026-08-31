import hashlib
import json
import re
import time
from typing import Any

import requests

from core.local_payload import (
    is_unsupported_think_error,
    ollama_keep_alive,
    strip_think,
    with_think_disabled,
)

from core.cache import get as cache_get
from core.cache import make_key
from core.cache import set as cache_set
from core.config import DEFAULT_LOCAL_JUDGE, OLLAMA_BASE_URL


class AutomatedEvaluator:
    """Grades model outputs against structured JSON rubrics using Ollama."""

    def __init__(
        self,
        judge_model: str = DEFAULT_LOCAL_JUDGE,
        base_url: str = OLLAMA_BASE_URL,
        request_timeout: float | None = None,
        use_openai_format: bool = False,
    ):
        self.judge_model = judge_model
        self.base_url = base_url
        self.use_openai_format = use_openai_format
        # Per-request timeout. Local judges (including via Forge proxy) can
        # stall; bound them generously. Only true cloud judges get a short
        # timeout since latency there is predictable.
        is_cloud = "gemini" in judge_model.lower()
        self.request_timeout = request_timeout or (120 if is_cloud else 600)

    def grade_run(
        self, candidate_output: str, rubric_path: str, context: str = ""
    ) -> dict:
        with open(rubric_path) as f:
            rubric_data = json.load(f)

        metric_descriptions = rubric_data.get("metrics", {})
        instructions = rubric_data.get("instructions", "")

        # Some rubrics (architecture/ADR/IaC) define metrics as nested
        # {"weight": N, "criteria": "..."} objects. Unwrap to the criteria
        # string so the judge prompt carries the actual grading standard —
        # rendering the raw dict repr made the weights read like scores and
        # left the criteria itself out of the prompt.
        unwrapped: dict[str, str] = {}
        for name, desc in metric_descriptions.items():
            if isinstance(desc, dict):
                weight = desc.get("weight")
                criteria = str(desc.get("criteria", "")).strip()
                unwrapped[name] = (
                    f"(weight {weight}) {criteria}"
                    if weight is not None and criteria
                    else criteria
                    or json.dumps(desc, sort_keys=True)
                )
            else:
                unwrapped[name] = str(desc)

        metric_lines = "\n".join(
            f"  - {name}: {desc}" for name, desc in unwrapped.items()
        )
        expected_keys = json.dumps(dict.fromkeys(unwrapped, 3), indent=2)

        context_block = (
            f"\n## Ground Truth Context (Diff + Model Map)\n{context}\n\n"
            if context
            else "\n"
        )
        system_prompt = (
            "You are an objective Quality Assurance Judge. Evaluate the technical analysis "
            "below and score each metric 1 (poor) to 5 (excellent). "
            "Respond with ONLY a valid JSON object. No other text."
        )
        user_prompt = (
            f"## Instructions\n{instructions}\n\n"
            f"## Metrics to Score\n{metric_lines}\n\n"
            f"{context_block}"
            f"## Target Analysis Output\n{candidate_output}\n\n"
            "## Response Format\n"
            "Respond with ONLY a valid JSON object using these exact keys. "
            "Each value must be an integer from 1 to 5. No other text.\n"
            f"Expected format:\n```json\n{expected_keys}\n```\n"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Hash the rubric path AND its contents into the cache key so that
        # editing the rubric file invalidates stale scores even if the
        # candidate output happens to be identical.
        rubric_file_hash = hashlib.sha256(
            rubric_path.encode() + json.dumps(rubric_data, sort_keys=True).encode()
        ).hexdigest()
        h = hashlib.sha256(self.judge_model.encode())
        h.update(system_prompt.encode())
        h.update(user_prompt.encode())
        h.update(rubric_file_hash.encode())
        cache_key = make_key("judge:grade_run", h.hexdigest())
        cached = cache_get(cache_key, max_age=86400)
        if cached is not None:
            print("   -> Scored (cached) in 0.0s")
            return cached  # type: ignore[return-value]

        if self.use_openai_format:
            api_url = (
                f"{self.base_url.rstrip('/')}/chat/completions"
                if not self.base_url.endswith("/chat/completions")
                else self.base_url
            )
        else:
            api_url = f"{self.base_url}/api/chat"

        t0 = time.time()
        # Attempt loop: one retry on transport errors (timeout, 5xx, conn
        # drop) or JSON parse failure. The retry appends a strict repair
        # instruction because the most common parse failure is the judge
        # wrapping its JSON in prose or markdown despite the format spec.
        max_attempts = 2
        parsed: dict | None = None
        last_raw = ""
        for attempt in range(1, max_attempts + 1):
            attempt_messages = messages
            if attempt > 1:
                attempt_messages = [
                    *messages[:-1],
                    {
                        "role": "user",
                        "content": messages[-1]["content"]
                        + "\n\nIMPORTANT: Your previous reply was not valid JSON. "
                        "Reply again with ONLY the raw JSON object — no prose, no "
                        "markdown fences, no explanation. Start with '{' and end "
                        "with '}'.",
                    },
                ]
            if self.use_openai_format:
                payload = {
                    "model": self.judge_model,
                    "messages": attempt_messages,
                    "stream": False,
                    "temperature": 0.0,
                }
            else:
                payload = with_think_disabled(
                    {
                        "model": self.judge_model,
                        "messages": attempt_messages,
                        "stream": False,
                        "keep_alive": ollama_keep_alive(),
                        "options": {"num_ctx": 32768, "temperature": 0.0},
                    }
                )

            try:
                response = requests.post(
                    api_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=self.request_timeout,
                )
                if is_unsupported_think_error(response) and strip_think(payload):
                    response = requests.post(
                        api_url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=self.request_timeout,
                    )
                response.raise_for_status()
                result = response.json()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < max_attempts:
                    print("   ⏳ Judge request timed out/dropped; retrying once...")
                    continue
                raise
            except requests.exceptions.RequestException:
                # HTTP error status: retrying an identical request rarely
                # helps for 4xx, but the repair attempt is cheap insurance
                # for 5xx gateway blips.
                if attempt < max_attempts:
                    continue
                raise

            if self.use_openai_format:
                raw = result["choices"][0]["message"]["content"]
            else:
                raw = result.get("message", {}).get("content", "")
            last_raw = raw

            parsed = self._parse_judge_json_strict(raw)
            if parsed is not None:
                break
            if attempt < max_attempts:
                print("   ⚠️  Judge returned non-JSON; retrying with strict format...")

        elapsed = time.time() - t0
        if parsed is not None:
            print(f"   -> Scored in {elapsed:.1f}s  ({len(last_raw)} chars)")
            cache_set(cache_key, parsed)
            return parsed

        # Fallback: neutral scores so downstream code still gets a dict
        print(
            f"   ⚠️  Judge returned non-JSON after {max_attempts} attempts "
            f"({len(last_raw)} chars); returning neutral scores."
        )
        fallback: dict[str, Any] = dict.fromkeys(unwrapped, 3)
        fallback["_judge_parse_error"] = last_raw[:500]
        return fallback

    @staticmethod
    def _parse_judge_json(raw: str, metric_descriptions: dict) -> dict:
        """Parse the judge's raw output into a metric dict.

        Handles <think> wrappers, markdown code fences, and partial JSON.
        Falls back to neutral scores (3) on parse failure rather than
        raising — a malformed judge response should not blow up the whole
        review pipeline after the review itself succeeded.
        """
        parsed = AutomatedEvaluator._parse_judge_json_strict(raw)
        if parsed is not None:
            return parsed

        # Fallback: neutral scores so downstream code still gets a dict
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        fallback: dict[str, Any] = dict.fromkeys(metric_descriptions, 3)
        fallback["_judge_parse_error"] = cleaned[:500]
        return fallback

    @staticmethod
    def _parse_judge_json_strict(raw: str) -> dict | None:
        """Parse judge output to a metric dict, or None if unparseable.

        Tolerates cross-model formatting variance: <think> reasoning
        wrappers (DeepSeek-R1), markdown ```json fences (Gemini/GPT),
        fences with the payload on the same line as the info string, and
        bare {...} spans surrounded by prose (small local models).
        """
        if not raw or not raw.strip():
            return None
        # Strip think tags (thinking models wrap reasoning in <think>...</think>)
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        # Remove markdown code fences if present (with or without newline
        # after the ```json info string)
        cleaned = re.sub(r"^```(?:json)?[ \t]*\r?\n?", "", cleaned)
        cleaned = re.sub(r"\r?\n?\s*```$", "", cleaned)

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # Last-resort: try to extract the first {...} block
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return None
