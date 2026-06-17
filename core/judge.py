import requests
import json

class AutomatedEvaluator:
    """Automates model grading using structured JSON schemas."""
    def __init__(self, judge_model: str = "qwen2.5:14b", base_url: str = "http://localhost:11434"):
        self.judge_model = judge_model
        self.api_url = f"{base_url}/api/generate"

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
        judge_prompt = (
            "You are an objective Quality Assurance Judge. Evaluate the technical analysis "
            "below and score each metric 1 (poor) to 5 (excellent).\n\n"
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
            "prompt": judge_prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0}
        }

        response = requests.post(self.api_url, json=payload)
        response.raise_for_status()
        return json.loads(response.json()["response"])
