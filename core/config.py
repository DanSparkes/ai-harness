"""Centralized runtime configuration for all evaluation harnesses.

Consolidates the model/API config that was previously copy-pasted (and had
drifted: e.g. code_review.py used ``qwen3.6:35b-mlx`` while every other
harness used ``ornith:35b``). All harnesses now source their model selection,
judge resolution, sampling temperature, and reproducibility seed from here.

Public API:
    load_config()           -> RuntimeConfig (from env vars)
    model_family(name)      -> family string (for judge/architect separation)
    resolve_judge(...)      -> a judge model from a *different* family
    temperature_for(role)   -> role-based sampling temperature policy
    get_config()            -> cached singleton

Env vars:
    USE_GEMINI        (1/true/yes) use the Gemini cloud backend
    GEMINI_API_KEY    cloud API key (required when USE_GEMINI=1)
    CLOUD_MODEL       override default cloud model
    LOCAL_MODEL       override default local Ollama model
    HEAVY_REVIEWER    override adversarial reviewer model
    LOCAL_JUDGE       override local judge model
    NUM_CTX           context window size (default 65536)
    EVAL_SEED         integer seed for reproducible sampling (Ollama)
    FORGE_PROXY_ENABLED   (1/true/yes) enable Forge proxy guardrails (default: on)
    FORGE_PROXY_PORT      proxy listen port (default 8081)
    FORGE_BACKEND_URL     backend URL for Forge to connect to (default http://localhost:11434)
    FORGE_BACKEND_TIMEOUT seconds the proxy waits for the backend (default 1200)
"""

import os
from dataclasses import dataclass

# ── Canonical defaults ────────────────────────────────────────────────────────
DEFAULT_CLOUD_MODEL = "gemini-2.5-flash"
DEFAULT_LOCAL_MODEL = "qwen3.6:35b-mlx"
DEFAULT_HEAVY_REVIEWER = "gpt-oss:20b"
DEFAULT_LOCAL_JUDGE = "gemma4-smart"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_FORGE_PORT = 8081
DEFAULT_FORGE_BACKEND_URL = "http://localhost:11434"

# Model families used to keep the judge architecturally independent of the
# model whose output it grades. Two models from the same family share training
# biases, so a same-family judge is effectively self-grading.
_MODEL_FAMILIES: dict[str, set[str]] = {
    "qwen": {"qwen", "qwen2", "qwen3", "qwen14b", "qwen2.5"},
    "deepseek": {"deepseek"},
    "gemma": {"gemma", "gemini"},
    "gpt": {"gpt-oss", "gpt", "o1", "o3"},
    "codestral": {"codestral", "mistral"},
    "ornith": {"ornith"},
}

_TRUTHY = {"1", "true", "yes", "on"}


def model_family(name: str) -> str:
    """Classify a model name into a family for judge-independence checks.

    Strips the Ollama tag (``:Q8_0``) and any registry path prefix
    (``hf.co/owner/...``) before matching, so a HuggingFace GGUF pull like
    ``hf.co/yuxinlu1/gemma-4-12B-coder...:Q8_0`` is correctly classified as
    ``gemma`` rather than ``hf.co`` — otherwise a gemini architect could be
    graded by a gemma-family judge without triggering family separation.
    """
    lower = name.lower().split(":")[0]  # drop tag like :Q8_0
    lower = lower.rsplit("/", 1)[-1]  # hf.co/path/gemma-x -> gemma-x
    for family, prefixes in _MODEL_FAMILIES.items():
        if any(lower.startswith(p) for p in prefixes):
            return family
    return lower.split("-")[0] if "-" in lower else lower


def temperature_for(role: str, override: float | None = None) -> float:
    """Role-based sampling temperature policy.

    Roles:
        "audit"       security/code review, fact-checking -> 0.0 (deterministic)
        "judge"       rubric scoring                       -> 0.0
        "reasoning"   architecture / onboarding synthesis  -> 0.2
        "generation"  code generation                      -> 0.3
    """
    if override is not None:
        return override
    if role == "generation":
        return 0.3
    if role == "reasoning":
        return 0.2
    # "audit" and "judge" default to fully deterministic sampling.
    return 0.0


def _parse_seed(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass
class RuntimeConfig:
    use_gemini: bool
    cloud_model: str
    local_model: str
    heavy_reviewer: str
    local_judge: str
    num_ctx: int
    seed: int | None
    gemini_api_key: str | None
    forge_enabled: bool = False
    forge_port: int = DEFAULT_FORGE_PORT
    forge_backend_url: str = DEFAULT_FORGE_BACKEND_URL
    # Seconds the Forge proxy waits for the backend model to respond before
    # returning 502. Must be >= the runner's request_timeout so the proxy
    # never gives up before the harness does. 300s (forge's default) is too
    # short for large local models generating long reviews.
    forge_backend_timeout: float = 1200.0

    @property
    def reasoning_model(self) -> str:
        return self.cloud_model if self.use_gemini else self.local_model

    @property
    def is_cloud(self) -> bool:
        return self.use_gemini

    @property
    def use_openai_format(self) -> bool:
        """True when requests should use OpenAI format (cloud OR Forge proxy)."""
        return self.is_cloud or self.forge_enabled

    @property
    def base_url(self) -> str:
        if self.forge_enabled:
            return f"http://localhost:{self.forge_port}/v1"
        if self.is_cloud:
            return GEMINI_BASE_URL
        return OLLAMA_BASE_URL

    @property
    def api_key(self) -> str | None:
        return self.gemini_api_key if self.is_cloud else None

    @property
    def fallback_model(self) -> str:
        # Cloud-down fallback MUST target a local Ollama model; sending a cloud
        # model name to Ollama always 404s. The local model is always valid.
        return self.local_model

    def resolve_judge(self, override: str | None = None) -> str:
        """Pick a judge model from a different family than the reasoning model.

        If the requested judge shares a family with the architect, fall back to
        the heavy reviewer (which is intentionally a different family). This
        prevents a gemini architect from being graded by a gemini-family judge.
        """
        candidate = override or self.local_judge
        if model_family(candidate) == model_family(self.reasoning_model):
            return self.heavy_reviewer
        return candidate


def load_config() -> RuntimeConfig:
    """Build a RuntimeConfig from environment variables."""
    use_gemini = os.getenv("USE_GEMINI", "").lower() in _TRUTHY
    gemini_api_key = os.getenv("GEMINI_API_KEY") if use_gemini else None
    forge_enabled = os.getenv("FORGE_PROXY_ENABLED", "1").lower() in _TRUTHY
    return RuntimeConfig(
        use_gemini=use_gemini,
        cloud_model=os.getenv("CLOUD_MODEL", DEFAULT_CLOUD_MODEL),
        local_model=os.getenv("LOCAL_MODEL", DEFAULT_LOCAL_MODEL),
        heavy_reviewer=os.getenv("HEAVY_REVIEWER", DEFAULT_HEAVY_REVIEWER),
        local_judge=os.getenv("LOCAL_JUDGE", DEFAULT_LOCAL_JUDGE),
        num_ctx=int(os.getenv("NUM_CTX", "65536")),
        seed=_parse_seed(os.getenv("EVAL_SEED")),
        gemini_api_key=gemini_api_key,
        forge_enabled=forge_enabled,
        forge_port=int(os.getenv("FORGE_PROXY_PORT", str(DEFAULT_FORGE_PORT))),
        forge_backend_url=os.getenv("FORGE_BACKEND_URL", DEFAULT_FORGE_BACKEND_URL),
        forge_backend_timeout=float(os.getenv("FORGE_BACKEND_TIMEOUT", "1200")),
    )


_cached: RuntimeConfig | None = None


def get_config() -> RuntimeConfig:
    """Return a process-cached RuntimeConfig (loaded once from env)."""
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached


def reset_config_cache() -> None:
    """Clear the cached config (mainly for tests that mutate env vars)."""
    global _cached
    _cached = None
