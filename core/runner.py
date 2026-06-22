import requests
import json
import time

class StatefulHarnessRunner:
    """Manages multi-pass execution against any OpenAI-compatible API (mlx-lm, cloud, etc.)."""
    def __init__(self, model_name: str, base_url: str = "http://localhost:8080", api_key: str = None, fallback_model_name: str = None):
        self.model_name = model_name
        self.fallback_model_name = fallback_model_name or model_name
        self.api_key = api_key
        self.is_cloud = bool(api_key)

        if not base_url.endswith("/chat/completions"):
            base_url = base_url.rstrip("/") + "/v1/chat/completions"
        self.api_url = base_url
        self.fallback_api_url = None

    def _call_with_retry(self, url: str, payload: dict, headers: dict, max_retries: int = 5, base_delay: float = 2.0) -> requests.Response:
        retry_codes = {429, 503}
        for attempt in range(max_retries + 1):
            response = requests.post(url, json=payload, headers=headers)
            if response.status_code not in retry_codes:
                return response
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                code = response.status_code
                print(f"   ⏳ API {code}. Retrying in {delay}s ({attempt + 1}/{max_retries})...")
                time.sleep(delay)
        return response

    def execute_sequence(self, system_prompt: str, passes: list[str], fallback_prompt: str = None) -> list[dict]:
        messages = [{"role": "system", "content": system_prompt}]
        execution_history = []

        for idx, pass_prompt in enumerate(passes):
            if self.is_cloud or not execution_history:
                messages.append({"role": "user", "content": pass_prompt})

            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            payload = {
                "model": self.model_name,
                "messages": messages,
                "stream": False,
                "temperature": 0.0 if "coder" in self.model_name else 0.4,
                "top_p": 0.9
            }

            response = self._call_with_retry(self.api_url, payload, headers) if self.is_cloud else requests.post(self.api_url, json=payload, headers=headers)

            if self.is_cloud and response.status_code in (429, 503):
                label = "429" if response.status_code == 429 else "503 (retries exhausted)"
                print(f"   ⚠️ Cloud API unavailable ({label}). Falling back to local mlx-lm...")
                self.is_cloud = False
                fallback_base = "http://localhost:8080"
                self.api_url = fallback_base.rstrip("/") + "/v1/chat/completions"
                self.model_name = self.fallback_model_name

                fb_messages = [{"role": "system", "content": system_prompt},
                               {"role": "user", "content": fallback_prompt or pass_prompt}]
                payload = {
                    "model": self.model_name,
                    "messages": fb_messages,
                    "stream": False,
                    "temperature": 0.4,
                    "top_p": 0.9
                }
                headers = {"Content-Type": "application/json"}
                response = requests.post(self.api_url, json=payload, headers=headers)

                response.raise_for_status()
                response_data = response.json()
                assistant_response = response_data["choices"][0]["message"]["content"]
                if not assistant_response:
                    print(f"   ⚠️ LLM returned empty or unexpected response")
                    print(f"   Response keys: {list(response_data.keys())}")
                    assistant_response = "# Basic Diff Scan\n\nUnable to generate review.\n"

                execution_history.append({
                    "pass_index": idx + 1,
                    "prompt": "(fallback)",
                    "output": assistant_response
                })

                break

            response.raise_for_status()
            response_data = response.json()

            assistant_response = response_data["choices"][0]["message"]["content"]
            if not assistant_response:
                print(f"   ⚠️ LLM returned empty or unexpected response")
                print(f"   Response keys: {list(response_data.keys())}")
                assistant_response = "# Basic Diff Scan\n\nUnable to generate review.\n"

            if self.is_cloud:
                messages.append({"role": "assistant", "content": assistant_response})

            execution_history.append({
                "pass_index": idx + 1,
                "prompt": pass_prompt,
                "output": assistant_response
            })

        return execution_history
