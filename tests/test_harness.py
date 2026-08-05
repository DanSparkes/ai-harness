"""Tests for core.harness: shared multi-pass context injection, seed plumbing,
MCP lifecycle, and judge-error resilience. Network-free."""

import pytest

from core import harness
from core.config import RuntimeConfig
from core.harness import _resolve_judge_for, build_agent, build_runner


def _cfg(use_gemini: bool = False) -> RuntimeConfig:
    return RuntimeConfig(
        use_gemini=use_gemini,
        cloud_model="gemini-2.5-flash",
        local_model="ornith:35b",
        heavy_reviewer="gpt-oss:20b",
        local_judge="hf.co/x/gemma-9b:Q8_0",
        num_ctx=65536,
        seed=42,
        gemini_api_key="k" if use_gemini else None,
    )


# ── seed plumbing ─────────────────────────────────────────────────────────────


def test_build_agent_inherits_config_seed() -> None:
    agent = build_agent("A", "sys", config=_cfg())
    assert agent.seed == 42


def test_build_agent_explicit_none_seed_disables() -> None:
    agent = build_agent("A", "sys", config=_cfg(), seed=None)
    assert agent.seed is None


def test_build_agent_explicit_seed_overrides() -> None:
    agent = build_agent("A", "sys", config=_cfg(), seed=7)
    assert agent.seed == 7


def test_build_runner_carries_seed() -> None:
    runner = build_runner(_cfg())
    assert runner.seed == 42


def test_build_runner_forge_timeout_outlives_proxy() -> None:
    """When Forge is enabled the runner must wait longer than the proxy's
    backend timeout, otherwise a slow model surfaces as a proxy 502."""
    cfg = _cfg()
    cfg.forge_enabled = True
    cfg.forge_backend_timeout = 1200.0
    runner = build_runner(cfg)
    assert runner.request_timeout > cfg.forge_backend_timeout


def test_build_runner_explicit_timeout_not_overridden() -> None:
    cfg = _cfg()
    cfg.forge_enabled = True
    runner = build_runner(cfg, request_timeout=500)
    assert runner.request_timeout == 500


def test_runner_local_options_include_seed() -> None:
    runner = build_runner(_cfg())
    opts = runner._local_options(0.0)
    assert opts["seed"] == 42
    assert opts["temperature"] == 0.0


def test_runner_local_options_omit_seed_when_none() -> None:
    runner = build_runner(_cfg())
    runner.seed = None
    assert "seed" not in runner._local_options(0.0)


# ── run_multipass: shared context injected into pass 1 only ───────────────────


class _FakeRunner:
    """Records the passes handed to execute_sequence; returns canned history."""

    def __init__(self, *_, **__):
        self.model_name = "fake-model"
        self.captured = None

    def execute_sequence(self, *, system_prompt, passes, fallback_prompt=None):
        self.captured = {
            "system_prompt": system_prompt,
            "passes": list(passes),
            "fallback_prompt": fallback_prompt,
        }
        return [
            {"pass_index": i + 1, "prompt": p, "output": f"OUT{i}"}
            for i, p in enumerate(passes)
        ]


def test_run_multipass_prepends_shared_context_to_first_pass_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRunner()
    monkeypatch.setattr(harness, "build_runner", lambda *a, **k: fake)

    out, model, hist = harness.run_multipass(
        system_prompt="SYS",
        passes=["PASS1", "PASS2", "PASS3"],
        shared_context="SHARED",
        config=_cfg(),
    )
    assert fake.captured is not None
    passes = fake.captured["passes"]
    assert len(passes) == 3
    assert passes[0] == "SHARED\n\nPASS1"  # context prepended to pass 1
    assert passes[1] == "PASS2"  # later passes unmodified
    assert passes[2] == "PASS3"
    assert model == "fake-model"
    assert out == "OUT2"  # last pass output
    assert len(hist) == 3


def test_run_multipass_no_shared_context(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRunner()
    monkeypatch.setattr(harness, "build_runner", lambda *a, **k: fake)
    harness.run_multipass(
        system_prompt="SYS", passes=["ONLY"], shared_context="", config=_cfg()
    )
    assert fake.captured is not None
    assert fake.captured["passes"] == ["ONLY"]


def test_run_multipass_empty_passes_raises() -> None:
    with pytest.raises(ValueError):
        harness.run_multipass(system_prompt="SYS", passes=[], config=_cfg())


# ── MCP context manager ───────────────────────────────────────────────────────


def test_mcp_context_yields_none_without_repo() -> None:
    with harness.mcp_context("some_config.json", None) as orch:
        assert orch is None


def test_mcp_context_yields_none_without_config(tmp_path) -> None:
    with harness.mcp_context(None, str(tmp_path)) as orch:
        assert orch is None


def test_mcp_context_stops_on_exception(tmp_path) -> None:
    """Even if the body raises, the context manager must not propagate a
    second failure from stop() (it suppresses). We simulate an orchestrator."""
    stopped = {"count": 0}

    class FakeOrch:
        def stop(self):
            stopped["count"] += 1

    monkeypatch_orch = FakeOrch()

    # Patch init_orchestrator to return our fake so we can observe stop().
    import core.harness as hmod

    orig = hmod.init_orchestrator
    hmod.init_orchestrator = lambda cfg, repo: monkeypatch_orch  # type: ignore
    try:
        with (
            pytest.raises(RuntimeError),
            hmod.mcp_context("cfg.json", str(tmp_path)) as orch,
        ):
            assert orch is monkeypatch_orch
            raise RuntimeError("boom")
        assert stopped["count"] == 1  # stop() ran despite the exception
    finally:
        hmod.init_orchestrator = orig


# ── _resolve_judge_for (compares against ACTUAL architect model) ──────────────


def test_resolve_judge_for_uses_actual_model_not_config() -> None:
    # Config reasoning is ornith, but output came from a fallback gemini model.
    # The judge must be chosen against the ACTUAL model, not the configured one.
    cfg = _cfg()  # reasoning = ornith, judge = gemma-9b
    # If output came from gemini (gemma family) and judge is gemma -> swap
    judge = _resolve_judge_for("gemini-2.5-flash", cfg, override=None)
    assert judge == cfg.heavy_reviewer


def test_resolve_judge_for_keeps_different_family() -> None:
    cfg = _cfg()
    judge = _resolve_judge_for("ornith:35b", cfg, override=None)
    assert judge == cfg.local_judge


# ── grade_and_archive: judge failure is non-fatal ─────────────────────────────


def test_grade_and_archive_survives_judge_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def boom(*a, **k):
        raise RuntimeError("judge exploded")

    monkeypatch.setattr(harness.AutomatedEvaluator, "grade_run", boom)
    report = tmp_path / "out.md"
    scores = harness.grade_and_archive(
        output="some review",
        rubric_path="rubrics/security_rubric.json",
        agent_role="test",
        model_used="ornith:35b",
        config=_cfg(),
        report_path=str(report),
    )
    assert "_judge_error" in scores
    assert report.read_text() == "some review"  # report still written


def test_resolve_target_repo_prefers_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TARGET_REPO", "/from/env")
    assert harness.resolve_target_repo("/from/cli") == "/from/cli"
    assert harness.resolve_target_repo(None) == "/from/env"
    monkeypatch.delenv("TARGET_REPO", raising=False)
    assert harness.resolve_target_repo(None) is None
