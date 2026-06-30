import hashlib
import json
import time

import requests

from core.cache import get as cache_get
from core.cache import make_key
from core.cache import set as cache_set


class AutomatedEvaluator:
    """Grades model outputs against structured JSON rubrics using Ollama."""

    def __init__(
        self,
        judge_model: str = "qwen3-coder:latest",
        base_url: str = "http://localhost:11434",
    ):
        self.judge_model = judge_model
        self.base_url = base_url

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

        h = hashlib.sha256(self.judge_model.encode())
        h.update(system_prompt.encode())
        h.update(user_prompt.encode())
        cache_key = make_key("judge:grade_run", h.hexdigest())
        cached = cache_get(cache_key, max_age=86400)
        if cached is not None:
            print("   -> Scored (cached) in 0.0s")
            return cached  # type: ignore[return-value]

        payload = {
            "model": self.judge_model,
            "messages": messages,
            "stream": False,
            "keep_alive": "0",
            "options": {"num_ctx": 32768, "temperature": 0.0},
        }

        t0 = time.time()
        response = requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        result = response.json()
        raw = result.get("message", {}).get("content", "")
        elapsed = time.time() - t0
        print(f"   -> Scored in {elapsed:.1f}s  ({len(raw)} chars)")

        # Strip think tags (DeepSeek-R1 wraps reasoning in <think>...</think>)
        import re

        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        # Remove markdown code fences if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        parsed = json.loads(cleaned)
        cache_set(cache_key, parsed)
        return parsed
