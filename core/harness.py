"""Shared harness infrastructure for all evaluation entrypoints.

This module eliminates the copy-paste drift across the eight harness scripts
by centralizing three things that every harness does:

1. **Multi-pass execution** — ``run_multipass`` drives a chained conversation
   through ``StatefulHarnessRunner`` (proper system/user/assistant message
   threading, retry, cloud/local fallback). Shared context (project map,
   parser limitations, MCP block) is injected into Pass 1 only, so it is NOT
   re-sent on every pass — fixing the latent token-bloat bug where some
   harnesses re-embedded the full project-map JSON in every pass.

2. **MCP lifecycle** — ``mcp_context`` is a context manager that guarantees
   ``orch.stop()`` runs even when the LLM times out or a judge parse fails,
   preventing orphaned MCP subprocesses.

3. **Grading + archiving** — ``grade_and_archive`` resolves an independent
   judge (different model family from the architect), scores against a rubric,
   and persists the run to the warehouse + report file.

These helpers read all model/API/sampling config from ``core.config`` so the
eight entrypoints no longer carry divergent copies of the config block.
"""

import contextlib
import os
from typing import Any

from core.agent import Agent
from core.config import RuntimeConfig, get_config, temperature_for
from core.forge_proxy import ForgeProxy
from core.judge import AutomatedEvaluator
from core.mcp_orchestrator import MCPOrchestrator, init_orchestrator
from core.runner import StatefulHarnessRunner
from core.warehouse import HarnessWarehouse

# Sentinel so callers can pass ``seed=None`` (disable) explicitly and have it
# differ from "omit seed" (inherit config).
_UNSET = object()


# ── Construction helpers ─────────────────────────────────────────────────────


def build_agent(
    name: str,
    system_prompt: str,
    config: RuntimeConfig | None = None,
    *,
    model_override: str | None = None,
    num_ctx: int | None = None,
    seed: Any = _UNSET,
) -> Agent:
    """Construct an Agent from centralized config.

    ``seed``: pass an int to force, None to disable, or omit (default) to
    inherit the config seed.
    """
    cfg = config or get_config()
    model = model_override or cfg.reasoning_model
    chosen_seed = cfg.seed if seed is _UNSET else seed
    return Agent(
        name=name,
        system_prompt=system_prompt,
        model_name=model,
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        num_ctx=num_ctx or cfg.num_ctx,
        seed=chosen_seed if isinstance(chosen_seed, int) else None,
        use_openai_format=cfg.use_openai_format,
    )


def build_runner(
    config: RuntimeConfig | None = None,
    *,
    model_override: str | None = None,
    temperature: float | None = None,
    num_ctx: int | None = None,
    request_timeout: float | None = None,
) -> StatefulHarnessRunner:
    """Construct a StatefulHarnessRunner from centralized config."""
    cfg = config or get_config()
    # When the Forge proxy is in front, the runner must outlive the proxy's
    # backend timeout — otherwise the proxy returns 502 (backend ReadTimeout)
    # before the runner gives up, killing the run. Add a margin so the runner
    # is always still waiting when the proxy responds.
    if request_timeout is None and cfg.forge_enabled:
        request_timeout = cfg.forge_backend_timeout + 120
    return StatefulHarnessRunner(
        model_name=model_override or cfg.reasoning_model,
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        fallback_model_name=cfg.fallback_model,
        num_ctx=num_ctx or cfg.num_ctx,
        temperature=temperature,
        request_timeout=request_timeout,
        seed=cfg.seed,
        use_openai_format=cfg.use_openai_format,
    )


# ── Multi-pass execution ─────────────────────────────────────────────────────


def run_multipass(
    *,
    system_prompt: str,
    passes: list[str],
    shared_context: str = "",
    config: RuntimeConfig | None = None,
    role: str = "reasoning",
    num_ctx: int | None = None,
    fallback_prompt: str | None = None,
    request_timeout: float | None = None,
) -> tuple[str, str, list[dict]]:
    """Run a chained multi-pass analysis and return (output, model_used, history).

    ``shared_context`` (project map, parser limitations, MCP block, etc.) is
    prepended to the FIRST pass only. The runner threads assistant turns back
    into the conversation, so later passes see earlier analysis without us
    re-sending the bulky context block on every pass.

    ``role`` selects the sampling-temperature policy ("audit", "reasoning",
    "generation", "judge"). Audit/judge work runs deterministically (0.0);
    synthesis runs slightly warmer.
    """
    if not passes:
        raise ValueError("run_multipass requires at least one pass")
    cfg = config or get_config()

    with ForgeProxy(cfg):
        runner = build_runner(
            cfg,
            temperature=temperature_for(role),
            num_ctx=num_ctx,
            request_timeout=request_timeout,
        )

        first = passes[0]
        if shared_context:
            first = f"{shared_context}\n\n{first}"
        pass_prompts = [first, *passes[1:]]

        history = runner.execute_sequence(
            system_prompt=system_prompt,
            passes=pass_prompts,
            fallback_prompt=fallback_prompt,
        )
        if not history:
            return "", runner.model_name, []
        return history[-1]["output"], runner.model_name, history


# ── MCP lifecycle ────────────────────────────────────────────────────────────


@contextlib.contextmanager
def mcp_context(config_path: str | None, target_repo: str | None):
    """Context manager that starts an MCP workbench and guarantees shutdown.

    Yields the orchestrator, or None if no config/repo was provided. The
    orchestrator is always stopped on exit — even on unhandled exceptions —
    so a wedged LLM call can no longer orphan MCP subprocesses.

    Usage::

        with harness.mcp_context(cfg_path, repo) as orch:
            block = orch.build_mcp_context_block(...) if orch else ""
            ...
    """
    orch: MCPOrchestrator | None = None
    if config_path and target_repo:
        orch = init_orchestrator(config_path, target_repo)
    try:
        yield orch
    finally:
        if orch:
            with contextlib.suppress(Exception):
                orch.stop()


# Singleton accessor for harnesses that need imperative access across functions.
_shared_orch: MCPOrchestrator | None = None


def init_shared_mcp(
    config_path: str | None, target_repo: str | None
) -> MCPOrchestrator | None:
    """Lazy singleton MCP orchestrator (mirrors the legacy per-harness pattern).

    Prefer ``mcp_context`` for new code. This exists for harnesses whose
    control flow can't easily wrap in a ``with`` block.
    """
    global _shared_orch
    if _shared_orch is not None:
        return _shared_orch
    if config_path and target_repo:
        _shared_orch = init_orchestrator(config_path, target_repo)
    return _shared_orch


def stop_shared_mcp() -> None:
    """Stop the shared singleton orchestrator if running (safe to call always)."""
    global _shared_orch
    if _shared_orch is not None:
        with contextlib.suppress(Exception):
            _shared_orch.stop()
        _shared_orch = None


def get_shared_mcp() -> MCPOrchestrator | None:
    return _shared_orch


# ── Target repo resolution ───────────────────────────────────────────────────


def resolve_target_repo(
    cli_repo: str | None, env_var: str = "TARGET_REPO"
) -> str | None:
    """Resolve the target repo from a CLI arg, env var, or None."""
    return cli_repo or os.environ.get(env_var)


# ── Grading + archiving ──────────────────────────────────────────────────────


def grade_and_archive(
    *,
    output: str,
    rubric_path: str,
    agent_role: str,
    model_used: str,
    config: RuntimeConfig | None = None,
    judge_context: str = "",
    judge_override: str | None = None,
    report_path: str | None = None,
) -> dict:
    """Score ``output`` against a rubric and persist the run.

    The judge model is resolved to be from a *different* family than
    ``model_used`` (preventing same-family self-grading). On judge failure the
    pipeline continues with an error marker rather than crashing — a malformed
    judge response must not discard a completed review.

    Returns the scores dict.
    """
    cfg = config or get_config()
    judge_model = _resolve_judge_for(model_used, cfg, judge_override)
    evaluator = AutomatedEvaluator(
        judge_model=judge_model,
        base_url=cfg.base_url,
        use_openai_format=cfg.use_openai_format,
    )
    try:
        scores = evaluator.grade_run(output, rubric_path, context=judge_context)
    except Exception as e:
        print(f"   ⚠️  Judge failed ({type(e).__name__}: {e}); continuing.")
        scores = {"_judge_error": str(e)[:200]}

    try:
        HarnessWarehouse().log_run(
            model_name=model_used,
            agent_role=agent_role,
            raw_output=output,
            scores=scores,
        )
    except Exception as e:
        print(f"   ⚠️  Warehouse log failed: {e}")

    if report_path:
        os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(output)
    return scores


def _resolve_judge_for(
    architect_model: str, cfg: RuntimeConfig, override: str | None
) -> str:
    """Pick a judge whose family differs from the architect that produced the output.

    ``RuntimeConfig.resolve_judge`` compares against the *configured* reasoning
    model, but the output may have come from a fallback model after a cloud
    outage. Compare against the actual ``architect_model`` instead.
    """
    candidate = override or cfg.local_judge
    from core.config import model_family

    if model_family(candidate) == model_family(architect_model):
        return cfg.heavy_reviewer
    return candidate


# ── Banner ───────────────────────────────────────────────────────────────────


def banner(title: str, config: RuntimeConfig | None = None, **extra: str) -> None:
    """Print a consistent harness launch banner."""
    cfg = config or get_config()
    line = "=" * 60
    print(line)
    print(title)
    for key, val in extra.items():
        print(f"{key.replace('_', ' ').capitalize():<17}: {val}")
    backend_label = "cloud" if cfg.is_cloud else "local"
    if cfg.forge_enabled:
        backend_label += " + forge"
    print(f"{'Backend':<17}: {backend_label} " f"({cfg.reasoning_model})")
    if cfg.forge_enabled:
        print(f"{'Forge proxy':<17}: http://localhost:{cfg.forge_port}/v1")
    if cfg.seed is not None:
        print(f"{'Seed':<17}: {cfg.seed}")
    print(line + "\n")


# Re-export commonly used symbols for convenience.
__all__ = [
    "banner",
    "build_agent",
    "build_runner",
    "get_shared_mcp",
    "grade_and_archive",
    "init_shared_mcp",
    "mcp_context",
    "resolve_target_repo",
    "run_multipass",
    "stop_shared_mcp",
]
