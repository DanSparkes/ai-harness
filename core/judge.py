import requests
import json

class AutomatedEvaluator:
    """Grades model outputs against structured JSON rubrics via an OpenAI-compatible API (mlx-lm)."""
    def __init__(self, judge_model: str = "mlx-community/Qwen2.5-14B-Instruct-4bit", base_url: str = "http://localhost:8080"):
        self.judge_model = judge_model
        if not base_url.endswith("/chat/completions"):
            base_url = base_url.rstrip("/") + "/v1/chat/completions"
        self.api_url = base_url

    def grade_run(self, candidate_output: str, rubric_path: str, context: str = "") -> dict:
        with open(rubric_path, "r") as f:
            rubric_data = json.load(f)

        metric_descriptions = rubric_data.get("metrics", {})
        instructions = rubric_data.get("instructions", "")

        metric_lines = "\n".join(
            f"  - {name}: {desc}"
            for name, desc in metric_descriptions.items()
        )
        expected_keys = json.dumps(
            {name: 3 for name in metric_descriptions},
            indent=2
        )

        context_block = f"\n## Ground Truth Context (Diff + Model Map)\n{context}\n\n" if context else "\n"
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

        payload = {
            "model": self.judge_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "stream": False,
            "temperature": 0.0
        }

        response = requests.post(self.api_url, json=payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
