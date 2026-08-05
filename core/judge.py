import hashlib
import json
import re
import time
from typing import Any

import requests

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

        metric_lines = "\n".join(
            f"  - {name}: {desc}" for name, desc in metric_descriptions.items()
        )
        expected_keys = json.dumps(dict.fromkeys(metric_descriptions, 3), indent=2)

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
            payload = {
                "model": self.judge_model,
                "messages": messages,
                "stream": False,
                "temperature": 0.0,
            }
            api_url = (
                f"{self.base_url.rstrip('/')}/chat/completions"
                if not self.base_url.endswith("/chat/completions")
                else self.base_url
            )
        else:
            payload = {
                "model": self.judge_model,
                "messages": messages,
                "stream": False,
                "keep_alive": "0",
                "options": {"num_ctx": 32768, "temperature": 0.0},
            }
            api_url = f"{self.base_url}/api/chat"

        t0 = time.time()
        response = requests.post(
            api_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        result = response.json()
        if self.use_openai_format:
            raw = result["choices"][0]["message"]["content"]
        else:
            raw = result.get("message", {}).get("content", "")
        elapsed = time.time() - t0
        print(f"   -> Scored in {elapsed:.1f}s  ({len(raw)} chars)")

        parsed = self._parse_judge_json(raw, metric_descriptions)
        cache_set(cache_key, parsed)
        return parsed

    @staticmethod
    def _parse_judge_json(raw: str, metric_descriptions: dict) -> dict:
        """Parse the judge's raw output into a metric dict.

        Handles <think> wrappers, markdown code fences, and partial JSON.
        Falls back to neutral scores (3) on parse failure rather than
        raising — a malformed judge response should not blow up the whole
        review pipeline after the review itself succeeded.
        """
        # Strip think tags (DeepSeek-R1 wraps reasoning in <think>...</think>)
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        # Remove markdown code fences if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

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

        # Fallback: neutral scores so downstream code still gets a dict
        print(
            f"   ⚠️  Judge returned non-JSON ({len(raw)} chars); "
            "returning neutral scores."
        )
        fallback: dict[str, Any] = dict.fromkeys(metric_descriptions, 3)
        fallback["_judge_parse_error"] = cleaned[:500]
        return fallback
