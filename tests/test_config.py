"""Tests for core.config: model-family classification, judge separation,
temperature policy, and env-driven config loading. Network-free."""

import pytest

from core import config
from core.config import (
    RuntimeConfig,
    get_config,
    load_config,
    model_family,
    reset_config_cache,
    temperature_for,
)

# ── model_family ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ornith:35b", "ornith"),
        ("ornith-fast:latest", "ornith"),
        ("gemini-2.5-flash", "gemma"),
        ("gpt-oss:20b", "gpt"),
        ("gpt-oss-fixed:latest", "gpt"),
        ("qwen3.6:35b-mlx", "qwen"),
        ("qwen3-coder-128k:latest", "qwen"),
        ("muse-glimmer:30b-mlx", "muse"),
        ("deepseek-r1:32b", "deepseek"),
        # HuggingFace GGUF pull must resolve through its registry path to the
        # underlying family — otherwise gemini architects get gemma judges.
        ("hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q8_0", "gemma"),
        # Fine-tune merges embed the base family mid-name; they must classify
        # to the inherited family, not the tune's own brand.
        ("BhupendraA/ThinkingCap-Qwen3.6-27B-MTP-GGUF:Q4_K_M-MTP", "qwen"),
        ("hf.co/bottlecapai/ThinkingCap-Qwen3.6-27B-GGUF:Q6_K", "qwen"),
    ],
)
def test_model_family_classification(name: str, expected: str) -> None:
    assert model_family(name) == expected


def test_model_family_unknown_falls_back_to_basename() -> None:
    assert model_family("llama3:8b") == "llama3"


# ── resolve_judge (family separation) ─────────────────────────────────────────


def _cfg(local_model: str, cloud_model: str, use_gemini: bool) -> RuntimeConfig:
    return RuntimeConfig(
        use_gemini=use_gemini,
        cloud_model=cloud_model,
        local_model=local_model,
        code_model=config.DEFAULT_CODE_MODEL,
        heavy_reviewer="gpt-oss:20b",
        local_judge="hf.co/x/gemma-9b:Q8_0",
        num_ctx=65536,
        seed=None,
        gemini_api_key="k" if use_gemini else None,
    )


def test_resolve_judge_keeps_different_family() -> None:
    cfg = _cfg(
        local_model="ornith:35b", cloud_model="gemini-2.5-flash", use_gemini=False
    )
    # reasoning model is ornith; judge is gemma -> different -> kept
    assert cfg.resolve_judge() == "hf.co/x/gemma-9b:Q8_0"


def test_resolve_judge_swaps_same_family_to_heavy() -> None:
    # cloud (gemini) architect + gemma judge -> same family -> fall back to heavy
    cfg = _cfg(
        local_model="ornith:35b", cloud_model="gemini-2.5-flash", use_gemini=True
    )
    assert cfg.resolve_judge() == "gpt-oss:20b"


def test_resolve_judge_override_respected_when_different_family() -> None:
    cfg = _cfg(
        local_model="ornith:35b", cloud_model="gemini-2.5-flash", use_gemini=False
    )
    assert cfg.resolve_judge(override="qwen3:32b") == "qwen3:32b"


def test_resolve_judge_override_swapped_when_same_family() -> None:
    cfg = _cfg(
        local_model="ornith:35b", cloud_model="gemini-2.5-flash", use_gemini=False
    )
    # ornith architect, override ornith judge -> same family -> heavy
    assert cfg.resolve_judge(override="ornith:7b") == "gpt-oss:20b"


def test_fallback_model_is_always_local() -> None:
    cfg = _cfg(
        local_model="ornith:35b", cloud_model="gemini-2.5-flash", use_gemini=True
    )
    assert cfg.fallback_model == "ornith:35b"
    assert "gemini" not in cfg.fallback_model


# ── temperature_for ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "role,expected",
    [("audit", 0.0), ("judge", 0.0), ("reasoning", 0.2), ("generation", 0.3)],
)
def test_temperature_policy(role: str, expected: float) -> None:
    assert temperature_for(role) == expected


def test_temperature_override_wins() -> None:
    assert temperature_for("audit", override=0.7) == 0.7


# ── load_config (env) ─────────────────────────────────────────────────────────


def test_load_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("USE_GEMINI", "GEMINI_API_KEY", "LOCAL_MODEL", "EVAL_SEED"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.is_cloud is False
    assert cfg.api_key is None
    assert cfg.reasoning_model == config.DEFAULT_LOCAL_MODEL
    assert cfg.code_model == config.DEFAULT_CODE_MODEL
    assert cfg.seed is None
    # Backend timeout must be generous (forge's 300s default is too short for
    # large local models) and overridable.
    assert cfg.forge_backend_timeout >= 600


def test_default_roles_are_family_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped defaults must give every role a distinct model family so
    no harness silently degrades to same-family grading."""
    for var in (
        "USE_GEMINI",
        "LOCAL_MODEL",
        "CODE_MODEL",
        "HEAVY_REVIEWER",
        "LOCAL_JUDGE",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    # Architect and implementer may share a family (both qwen); the judge must
    # be independent of every role it can grade, and the heavy reviewer must
    # not mirror the architect.
    judge_family = model_family(cfg.local_judge)
    assert judge_family != model_family(cfg.reasoning_model)
    assert judge_family != model_family(cfg.code_model)
    assert judge_family != model_family(cfg.heavy_reviewer)
    assert model_family(cfg.heavy_reviewer) != model_family(cfg.reasoning_model)
    assert cfg.local_judge == config.DEFAULT_LOCAL_JUDGE
    assert cfg.heavy_reviewer == config.DEFAULT_HEAVY_REVIEWER


def test_load_config_forge_backend_timeout_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGE_BACKEND_TIMEOUT", "900")
    assert load_config().forge_backend_timeout == 900


def test_load_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_GEMINI", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setenv("LOCAL_MODEL", "mylocal:1b")
    monkeypatch.setenv("EVAL_SEED", "42")
    cfg = load_config()
    assert cfg.is_cloud is True
    assert cfg.api_key == "secret"
    assert cfg.local_model == "mylocal:1b"
    assert cfg.seed == 42


def test_load_config_bad_seed_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_SEED", "not-a-number")
    assert load_config().seed is None


def test_get_config_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_config_cache()
    monkeypatch.delenv("USE_GEMINI", raising=False)
    a = get_config()
    b = get_config()
    assert a is b  # singleton
    reset_config_cache()
