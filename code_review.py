import argparse
import contextlib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from core.agent import build_dependency_graph, skill_get_affected_files
from core.claim_verifier import annotate_review, extract_claims, verify_claims
from core.config import get_config, temperature_for
from core.diff_audit import (
    audit_review_against_invariants,
    build_invariant_block,
    parse_diff_invariants,
)
from core.forge_proxy import ForgeProxy
from core.git_provider import GitDiffProvider
from core.judge import AutomatedEvaluator
from core.mcp_orchestrator import init_orchestrator
from core.parser import DjangoTopographer, minify_markdown
from core.retrieval import retrieve_relevant_code
from core.runner import StatefulHarnessRunner
from core.warehouse import HarnessWarehouse

# ==============================================================================
# MODEL & API CONFIGURATION (sourced centrally from core.config)
# ==============================================================================
# Previously this block was copy-pasted across every harness and had drifted:
# code_review.py used ``qwen3.6:35b-mlx`` while every other harness used
# ``ornith:35b``. All harnesses now read from core.config so runs are
# comparable. Override any value via env vars (LOCAL_MODEL, USE_GEMINI, ...).
_cfg = get_config()

LOCAL_MODEL = _cfg.local_model
REASONING_ARCHITECT = _cfg.reasoning_model
ARCHITECT_API_BASE = _cfg.base_url
ARCHITECT_API_KEY = _cfg.api_key
# Local Fallback: Ollama-served model used when cloud API is unavailable.
# MUST be a model name that Ollama has pulled locally. Using a cloud model
# name here would cause the local fallback path to always fail with 404.
FALLBACK_REVIEWER = _cfg.fallback_model  # always == LOCAL_MODEL
# Local Judge: Scores the review against a rubric (family-separated centrally)
LOCAL_JUDGE = _cfg.resolve_judge()
# ==============================================================================

TARGET_REPO = os.environ.get("TARGET_REPO")
MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.python.json")

# Total character budget for the prompt context sent to the model. With
# num_ctx=65536 tokens, ~100K chars is a safe upper bound that leaves room
# for the model's own multi-pass output. Override via env var if needed.
TOTAL_CONTEXT_BUDGET = int(os.environ.get("CODE_REVIEW_BUDGET_CHARS", "100000"))

_mcp_orch = None
_mcp_lock = threading.Lock()


def init_mcp(repo_path: str | None = None, config_path: str | None = None):
    """Initialize the global MCP orchestrator. Thread-safe singleton."""
    global _mcp_orch
    with _mcp_lock:
        if _mcp_orch is not None:
            return _mcp_orch
        cfg_path = config_path or MCP_CONFIG_PATH
        path = repo_path or TARGET_REPO or os.getcwd()
        orch = init_orchestrator(cfg_path, path)
        if orch:
            _mcp_orch = orch
        return orch


# ── Context budgeting helpers ────────────────────────────────────────────────


def _clip(text: str, limit: int, label: str = "context") -> str:
    """Truncate text to limit chars with a marker if it exceeds."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{label} truncated at {limit} chars]"


def build_retraction_banner(audit_note: str) -> str:
    """Prominent top-of-report retraction when the audit finds contradictions.

    Placed BEFORE the review so the reader sees the invalidation before the
    verdict — previously the audit note was appended at the bottom and read as
    an afterthought while the fabricated headline finding stood unchallenged.
    """
    body = audit_note.strip("\n")
    return (
        "# ⚠️ REVIEW INVALIDATED BY MACHINE AUDIT\n\n"
        "The deterministic diff audit found the following review claims "
        "CONTRADICT the machine-derived invariants. The review asserts false "
        "schema facts — **the verdict and reliability scores below are "
        "invalidated** until these findings are corrected.\n\n"
        f"{body}\n\n"
        "---\n\n"
    )


def build_scores_section(scores: dict, audit_note: str) -> str:
    """Append the judge's reliability scores to the report.

    Scores previously only reached the warehouse, so a reader of the report
    never saw the factual_accuracy penalty a contradiction should trigger.
    """
    lines = ["\n\n---\n", "## 📊 Review Reliability Scores"]
    if audit_note:
        lines.append(
            "> ⚠️ Machine audit invalidated this review: factual_accuracy and "
            "claim_grounding were clamped to 1."
        )
    if scores.get("_judge_error"):
        lines.append(f"\nJudge failed: {scores['_judge_error']}")
    else:
        lines.append("")
        for key, value in sorted(scores.items()):
            if key.startswith("_"):
                lines.append(f"**{key}**: {value}")
            else:
                lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _read_capped(path: Path, cap: int = 8000, label: str = "file") -> str:
    """Read up to `cap` bytes from `path` without loading the whole file.

    Prevents OOM on pathological inputs (e.g. multi-MB generated models.py).
    """
    try:
        with path.open("rb") as f:
            raw = f.read(cap + 1)
    except OSError:
        return ""
    text = raw[:cap].decode("utf-8", errors="replace")
    if len(raw) > cap:
        text += f"\n# ... [{label} truncated at {cap} chars]"
    return text


def _allocate_context_budget(
    persona_chars: int, diff_chars: int, *, total_chars: int = TOTAL_CONTEXT_BUDGET
) -> dict[str, int]:
    """Partition the model's character budget across context blocks.

    The diff is always kept in full (clipping it would skip real changes).
    Everything else is allocated from the remaining budget.
    """
    # Reserve room for: system persona, the full diff, and a generous
    # allowance for the model's pass-1 output (which feeds into pass 2 once
    # the multi-pass chain is active).
    reserved = persona_chars + diff_chars + 20_000
    remaining = max(5_000, total_chars - reserved)
    return {
        "map": int(remaining * 0.50),
        "key_files": int(remaining * 0.30),
        "mcp_project": int(remaining * 0.20),
    }


def _diff_identifiers(raw_diff: str, limit: int = 40) -> str:
    """Build a BM25 retrieval query from the diff's own code identifiers.

    Retrieval keyed on the symbols actually in the +/- lines (class/function/
    field names) returns the real implementations touched by the change — e.g.
    for a soft-delete refactor it surfaces ``SoftDeleteModel`` / ``is_deleted``,
    the precise context that prevents false migration claims. This replaces a
    previous query built from filenames + generic English words ("usage callers
    implementation helpers"), which retrieved mostly noise.
    """
    tokens: set[str] = set()
    for line in raw_diff.splitlines():
        if not line.startswith(("+", "-")):
            continue
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", line):
            tokens.add(tok)
    # Prefer code-shaped tokens (mixed case / underscores / digits) over prose.
    codeish = sorted(
        t
        for t in tokens
        if ("_" in t)
        or (t != t.lower() and t != t.upper())
        or any(c.isdigit() for c in t)
    )
    return " ".join(codeish[:limit])


def _blast_radius_block(
    target_repo: str, changed_files: list[str], max_chars: int = 4000
) -> str:
    """List files that import each changed module, for grounded blast-radius.

    The reviewer persona's #1 focus is "Blast Radius Analysis", but without a
    caller list it can only guess at impact. This reuses the same dependency
    graph as the feature harness (cached per git HEAD) so blast-radius claims
    can be verified, not invented.
    """
    try:
        graph = build_dependency_graph(target_repo)
    except Exception:
        return ""
    header = "=== Import-Graph Blast Radius (files that import each changed module) ==="
    parts = [header]
    total = len(header)
    py_files = [f for f in changed_files if f.endswith(".py")]
    # Give each changed module a slice of the budget so a hugely-imported file
    # (e.g. models.py) still shows a meaningful caller sample rather than being
    # skipped wholesale when its full importer list exceeds the cap.
    per_file = max(800, max_chars // max(1, len(py_files)))
    for f in py_files:
        try:
            affected = skill_get_affected_files(f, graph=graph)
        except Exception:
            continue
        if not affected or affected == "(no downstream dependents)":
            continue
        body = f"\n{affected}"
        if len(body) > per_file:
            body = body[:per_file] + f"\n  ... [{f}: importer list truncated]"
        room = max_chars - total
        if room <= 100:
            parts.append("\n... [blast-radius truncated]")
            break
        if len(body) > room:
            body = body[:room] + f"\n  ... [{f}: truncated]"
        parts.append(body)
        total += len(body)
    return "\n".join(parts) if len(parts) > 1 else ""


def build_mcp_context() -> str:
    orch = _mcp_orch
    if not orch:
        return ""
    return orch.build_mcp_context_block(
        tags=["code_review", "architectural_rule"],
        exclude_tools_from=["gortex"],
    )


# ── Large-diff chunking + noise-file summarization ─────────────────────────


# Auto-generated / low-signal files where a full hunk dump adds noise without
# reviewable source. Migrations and lockfiles are the dominant offenders; the
# reviewer's persona already instructs it not to demand schema migrations
# (and diff invariants ground that deterministically). We keep a compressed
# signature (paths + added/removed counts) rather than the full diff body.
_NOISE_RE = re.compile(
    r"/(migrations?|fixtures?|node_modules|dist|build)/"
    r"|\.(lock|json|min\.js|min\.css|map)$"
    r"|_pb2?\.py$"
    r"\.pyc$",
    re.IGNORECASE,
)


def _is_noise_file(path: str) -> bool:
    """True for auto-generated files whose full diff adds no reviewable signal.

    Matching these keeps a lockfile bump or a synthetic migration out of every
    chunk's prompt while still recording that it changed (see
    ``summarize_hunk``), so the reviewer isn't blind to the fact.
    """
    if _NOISE_RE.search(path):
        return True
    # Django migration check (migrations/0001_initial.py) caught above; this
    # covers a bare "migrations.py" module name too.
    return bool(re.search(r"(^|/)migrations?\.py$", path))


def _parse_diff_hunks(raw_diff: str) -> list[dict]:
    """Split a unified git diff into per-file records.

    Each record carries the file path, its hunk text, and running hunk size.
    The ``path`` includes the leading ``a/``-free dotted path from the
    ``+++ b/...`` header. Files that only show rename indicators without diff
    bodies (binary / rename-only) are still captured with ``body=""``.
    """
    files: list[dict] = []
    current: dict | None = None
    for line in raw_diff.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                files.append(current)
            current = {"path": "", "lines": [line], "size": len(line) + 1}
            m = re.search(r"b/(\S+)\s*$", line)
            if m and " a/" in line:
                current["path"] = m.group(1)
            continue
        if current is not None:
            # The concrete new-path header (+++ b/...) is authoritative;
            # use it to fill the path if the diff --git header didn't carry it.
            if line.startswith("+++ b/") and not current["path"]:
                current["path"] = line[6:]
            current["lines"].append(line)
            current["size"] += len(line) + 1
        elif line.startswith("+++ b/"):
            # Unlikely without a preceding diff --git header, but be safe.
            current = {
                "path": line[6:],
                "lines": [line],
                "size": len(line) + 1,
            }
    if current is not None:
        files.append(current)
    for f in files:
        f["body"] = "\n".join(f["lines"])
    return files


def _hunk_signature(body: str) -> str:
    """Compress a noise-file hunk into 'N+/M-' counts so it stays visible but tiny."""
    lines = body.splitlines()
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    changed = sum(1 for ln in lines if ln.startswith(("+", "-")))
    return f"({added:+} / {removed:-} lines changed, {changed} lines total)"


def preprocess_diff(raw_diff: str) -> tuple[str, list[dict]]:
    """Summarize noise files and return (cleaned_diff, noise_records).

    ``cleaned_diff`` keeps every reviewable (non-noise) file's diff intact and
    replaces noise files with a one-line signature. ``noise_records`` lists the
    summarized files for the report/meta section.
    """
    hunks = _parse_diff_hunks(raw_diff)
    kept: list[str] = []
    noise: list[dict] = []
    for h in hunks:
        if not h["path"]:
            kept.append(h["body"])
            continue
        if _is_noise_file(h["path"]):
            noise.append(
                {"path": h["path"], "signature": _hunk_signature(h["body"])}
            )
        else:
            kept.append(h["body"])
    cleaned = "\n".join(kept)
    return cleaned, noise


# Per-chunk char budget for multi-chunk reviews. Default 8000 keeps each
# review pass small enough that the 27B-class local model doesn't get
# stuck in regeneration loops on large files (observed: 4-hour chunks
# on 12-20K-char inputs producing 5K-15K chars of output). Smaller chunks
# also run faster, so the per-chunk timeout rarely fires. The previous
# 20000 default split too few chunks (~9 per PR) and triggered long
# runaway loops; the original 30000 default was even worse. Override via
# env if your model handles larger inputs reliably.
CHUNK_TARGET_CHARS = int(os.environ.get("CODE_REVIEW_CHUNK_CHARS", "8000"))


# Minimum total cleaned-diff size before chunking kicks in. Below this
# threshold we always run a single detailed pass, even if `group_diff_chunks`
# would have split into multiple buckets. Rationale: chunking loses cross-
# file context (each chunk sees files in isolation, can't spot patterns
# that span files), pays per-chunk overhead (system prompt + project map +
# key files + invariants are re-prepended to every chunk), and produces a
# concatenated final report with no synthesis. A 30-50K-char diff fits in
# a 65K-token context window with room for shared context; splitting into
# 5-8 chunks of 5-8K chars each is strictly worse for review quality and
# wall-clock.
CHUNK_THRESHOLD_CHARS = int(
    os.environ.get("CODE_REVIEW_CHUNK_THRESHOLD_CHARS", "80000")
)


def _resolve_chunk_timeout(
    *,
    request_timeout: float | None,
    no_chunk_timeout: bool,
    env_timeout_s: int,
) -> float | None:
    """Decide the per-chunk ``request_timeout`` value for multi-chunk runs.

    Precedence (highest first):
        1. ``--no-chunk-timeout`` / env ``CODE_REVIEW_CHUNK_TIMEOUT_S=0`` →
           ``None`` (disabled — runner uses its own default, no chunk cap).
        2. User ``--timeout`` (already applied at the runner level for every
           non-chunk call; we reuse it here as a per-chunk ceiling).
        3. Env default ``CODE_REVIEW_CHUNK_TIMEOUT_S`` (default 600s).

    The disabled case (1) MUST win over the user ``--timeout`` because the
    forge-backend path silently sets ``request_timeout`` to a 45-min ceiling
    when Forge is enabled — that would override ``--no-chunk-timeout``
    and re-enable a cap the user explicitly disabled.
    """
    if no_chunk_timeout or env_timeout_s <= 0:
        return None
    if request_timeout is not None:
        return float(request_timeout)
    return float(env_timeout_s)


# Heartbeat interval for multi-chunk runs when --no-chunk-timeout is set.
# Every HEARTBEAT_INTERVAL_S while a chunk is in flight we print a "still
# alive" line so the user can confirm progress and Ctrl-C if clearly stuck.
HEARTBEAT_INTERVAL_S = int(os.environ.get("CODE_REVIEW_HEARTBEAT_S", "1800"))


def _run_with_heartbeat(
    *,
    label: str,
    started_monotonic: float,
    interval_s: int,
    clock: Callable[[], float] | None = None,
):
    """Return a context manager that prints a heartbeat every ``interval_s``.

    The heartbeat only fires AFTER the first interval has passed (so a chunk
    that finishes in <30 min never triggers one). Daemon-threaded so it dies
    cleanly with the parent process; ``Event``-driven so it shuts down
    immediately when the ``with`` block exits — no trailing prints after
    the chunk finishes.

    ``clock`` is injectable for tests; defaults to ``time.monotonic``.
    Yields nothing. The caller is responsible for actually doing the work
    inside the ``with`` block.
    """
    import threading

    stop = threading.Event()
    clock = clock or time.monotonic

    def _beat() -> None:
        while not stop.wait(interval_s):
            elapsed = clock() - started_monotonic
            print(
                f"\n   ⏳ {label} still running "
                f"({elapsed/60:.1f}m elapsed, "
                f"Ctrl-C to abort) ",
                flush=True,
            )

    thread = threading.Thread(target=_beat, daemon=True, name="chunk-heartbeat")
    thread.start()

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            stop.set()
            thread.join(timeout=2)
            return False

    return _Ctx()

# Hard per-chunk wall-clock cap. Each chunk review is one execute_sequence
# call; if it doesn't return inside this window we abort the whole run with
# a clear message instead of burning hours on a single stuck pass.
#
# Default 1800s (30 min) is the longest we let a chunk run. With the
# 8000-char default chunk size most chunks finish in 5-10 min on a 27B
# model; the 30-min cap only fires on genuine outliers (regeneration
# loops, very large test files). The outer wall-clock guard aborts at
# 2x this (60 min) to preserve elapsed work. Set
# CODE_REVIEW_CHUNK_TIMEOUT_S=0 to disable the cap entirely — useful
# when you trust the model and need to wait through a slow chunk.
CHUNK_PASS_TIMEOUT_S = int(
    os.environ.get("CODE_REVIEW_CHUNK_TIMEOUT_S", "1800")
)


# Domain markers inside a path that justify aggregating all files under that
# marker into one chunk. Order matters only for the tiebreak when a path
# happens to contain more than one (rare).
_DOMAIN_MARKERS = {"models", "views", "serializers", "tests", "migrations"}


def _domain_key(path: str) -> str:
    """Return a stable, path-disambiguating bucket key for a changed file.

    Strategy (in priority order):
      1. If the path contains an explicit domain marker (e.g. ``memores/views``),
         key on ``<root>/<marker>`` so all files in that tier cluster.
      2. Otherwise, key on the first two path segments (``root/sub``). This
         is the bugfix for the previous behavior, which fell back to just the
         top directory — that collapsed unrelated subtrees like
         ``memores/auth`` and ``memores/billing`` into a single ``memores``
         bucket and produced giant chunks.
      3. Single-segment paths (rare; usually generated noise) key on the
         segment itself so they're distinguishable.
    """
    parts = path.split("/")
    for i, part in enumerate(parts):
        if part in _DOMAIN_MARKERS:
            return "/".join(parts[: i + 1])
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0] or "(root)"


def _file_hunk_size(h: dict) -> int:
    return int(h.get("size") or 0)


def group_diff_chunks(raw_diff: str) -> list[dict]:
    """Group a diff into review chunks bounded by CHUNK_TARGET_CHARS.

    Files are grouped by directory/domain prefix when possible (so related
    files — e.g. all serializers in one change — land together and reviewers
    see them in context), then packed greedily under the per-chunk char cap.
    A single file larger than the cap becomes its own chunk (one review pass
    still covers it fully; the cap is a target, not a hard truncation).
    """
    hunks = _parse_diff_hunks(raw_diff)
    hunks = [h for h in hunks if h["path"] and not _is_noise_file(h["path"])]

    # Group by domain, preserving file order within each group.
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for h in hunks:
        k = _domain_key(h["path"])
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(h)

    chunks: list[dict] = []
    for key in order:
        bucket = buckets[key]
        # Split a bucket further if it alone exceeds the target.
        chunk: list[dict] = []
        chunk_size = 0
        for h in bucket:
            size = _file_hunk_size(h)
            if chunk and chunk_size + size > CHUNK_TARGET_CHARS:
                chunks.append(_finalize_chunk(chunk, key))
                chunk = []
                chunk_size = 0
            chunk.append(h)
            chunk_size += size
        if chunk:
            chunks.append(_finalize_chunk(chunk, key))

    # Emergency guard: if grouping produced zero chunks (only noise files),
    # fall back to a single empty chunk so the pipeline still runs.
    if not chunks:
        chunks = [_finalize_chunk([], "noise-only")]
    return chunks


def _finalize_chunk(files: list[dict], key: str) -> dict:
    files = [f for f in files if f.get("body")]
    body = "\n".join(f.get("body", "") for f in files)
    return {
        "key": key,
        "files": [f["path"] for f in files],
        "body": body,
        "size": len(body),
    }


def merge_chunk_reviews(chunk_reviews: list[dict]) -> str:
    """Combine per-chunk review outputs into a single final report.

    Each chunk's markdown headings (file paths) remain authoritative; we
    assemble them with clear separators. A per-chunk header keeps findings
    attributable to their review unit.
    """
    if len(chunk_reviews) == 1:
        return chunk_reviews[0]["output"]
    parts = []
    for cr in chunk_reviews:
        body = (cr.get("output") or "").strip()
        if not body:
            continue
        header = (
            f"## Chunk: {cr['key']}\n"
            f"Files reviewed: {', '.join(cr['files']) or '(none)'}\n\n"
        )
        parts.append(header + body)
    return "\n\n---\n\n".join(parts)


def collect_key_file_context(
    target_repo: str, changed_files: list[str], max_chars: int = 15000
) -> str:
    """Read key source files referenced by changed files to give the reviewer ground truth.

    Collects:
    - Model definition files (models.py) for any changed model-related file
    - settings.py for any changed settings-related file
    - Migration files referenced by the diff

    Returns a markdown block with file contents, capped at max_chars.
    """
    repo_path = Path(target_repo).resolve()
    sections: list[str] = []
    total_chars = 0

    # Per-file cap; smaller of (2000, max_chars // 4) so we never blow the
    # whole budget on a single file.
    per_file_cap = min(8000, max(2000, max_chars // 4))

    # Determine which app directories are affected
    affected_app_dirs: set[Path] = set()
    for fpath in changed_files:
        full = repo_path / fpath
        if not full.exists():
            continue
        # Walk up to find the app directory (contains models.py)
        for parent in full.parents:
            if (parent / "models.py").exists():
                affected_app_dirs.add(parent)
                break

    # Collect models.py for affected apps
    for app_dir in sorted(affected_app_dirs):
        models_file = app_dir / "models.py"
        if not models_file.exists():
            continue
        content = _read_capped(models_file, cap=per_file_cap, label="models.py")
        if not content:
            continue
        rel = models_file.relative_to(repo_path)
        sections.append(f"### {rel}\n```python\n{content}\n```")
        total_chars += len(content)
        if total_chars >= max_chars:
            break

    # Collect settings.py if any settings-related file was changed
    settings_changed = any(
        "settings" in f.lower() or "config" in f.lower() for f in changed_files
    )
    if settings_changed and total_chars < max_chars:
        for settings_path in repo_path.rglob("settings.py"):
            if "migrations" in str(settings_path) or "fixtures" in str(settings_path):
                continue
            try:
                content = _read_capped(
                    settings_path, cap=per_file_cap * 2, label="settings.py"
                )
                if not content:
                    continue
                # Only include translation-related settings sections
                lines = content.split("\n")
                relevant_lines: list[str] = []
                in_relevant_section = False
                for line in lines:
                    if (
                        "TRANSLATION" in line.upper()
                        or "MODELTRANSLATION" in line.upper()
                    ):
                        in_relevant_section = True
                    elif (
                        line.strip()
                        and not line.startswith(" ")
                        and not line.startswith("#")
                    ):
                        in_relevant_section = False
                    if in_relevant_section:
                        relevant_lines.append(line)
                if relevant_lines:
                    snippet = "\n".join(relevant_lines[:100])
                    rel = settings_path.relative_to(repo_path)
                    sections.append(
                        f"### {rel} (translation-related settings)\n```python\n{snippet}\n```"
                    )
                    total_chars += len(snippet)
                break
            except Exception:
                continue

    if not sections:
        return ""

    header = "## Key Source File Contents (for fact-checking)\n"
    block = header + "\n\n".join(sections)
    if total_chars >= max_chars:
        block += f"\n\n... [context capped at ~{max_chars} chars]"
    return block


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Local-Cloud Hybrid Code Review Engine"
    )
    parser.add_argument(
        "--target",
        "-t",
        default="main",
        help="Target branch to merge into (default: main)",
    )
    parser.add_argument(
        "--source",
        "-s",
        default="develop",
        help="Source branch containing new changes (default: develop)",
    )
    parser.add_argument(
        "--project-context",
        "-c",
        default=None,
        help="Path to a project-specific context file (markdown) with domain knowledge to inject into the review",
    )
    parser.add_argument(
        "--repo",
        "-r",
        default=None,
        help="Path to the target repository (overrides TARGET_REPO env var and defaults)",
    )
    parser.add_argument(
        "--mcp-config",
        "-m",
        default=None,
        help="Path to MCP server config file (overrides MCP_CONFIG env var and default)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Per-request timeout in seconds for LLM calls. Defaults to 600s "
            "for local models, 120s for cloud."
        ),
    )
    parser.add_argument(
        "--no-chunk-timeout",
        action="store_true",
        help=(
            "Disable the per-chunk wall-clock cap for multi-chunk reviews. "
            "WARNING: with --no-chunk-timeout a chunk that gets stuck in a "
            "regeneration loop (observed: 4-hour chunks on 12-20K-char "
            "inputs) will run until completion. The heartbeat prints every "
            "30 min so you can Ctrl-C manually. Prefer the default 30-min "
            "cap unless you have a specific reason to wait. Equivalent to "
            "CODE_REVIEW_CHUNK_TIMEOUT_S=0."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    target_branch = args.target
    source_branch = args.source

    target_repo = args.repo or os.environ.get("TARGET_REPO", TARGET_REPO)
    mcp_config_path = args.mcp_config or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)

    if not target_repo:
        print("Error: No target repository specified.")
        print("Set TARGET_REPO env var or pass --repo /path/to/project")
        return 1

    print(f"{'=' * 60}")
    print("Launching Local Code Review Engine (Hybrid Mode)")
    print(f"Target Project   : {target_repo}")
    print(f"Cloud Architect  : {REASONING_ARCHITECT}")
    print(f"Local JSON Judge : {LOCAL_JUDGE}")
    print(f"Review Delta     : {target_branch} <--- {source_branch}")
    print("Review Lens      : Ponytail concision + reuse constraints active")
    print(f"{'=' * 60}\n")

    start_time = time.time()

    # 1. Gather Global Structural Picture
    print("📦 Step 1a: Parsing global project topography...")
    topographer = DjangoTopographer(target_repo)
    project_map = topographer.scan_project()

    # 2. Gather Local Line Changes Picture
    print("🔍 Step 1b: Extracting Git modifications...")
    git_layer = GitDiffProvider(target_repo)
    raw_diff = git_layer.get_diff(target_branch, source_branch)
    changed_files = git_layer.get_changed_files(target_branch, source_branch)

    if not raw_diff or not raw_diff.strip():
        print("❌ Error: No Git differences found between these branches. Exiting.")
        return 1

    print(f"   [Git] Detected changes across {len(changed_files)} files.")
    if len(raw_diff) > 50_000:
        print(
            f"   ⚠️  Diff is large ({len(raw_diff):,} chars). Context budget "
            "for map/key files will be reduced; review may be incomplete."
        )

    # 4. Load Reviewer Persona
    persona_path = "agents/code_reviewer.md"
    if not os.path.exists(persona_path):
        print(f"❌ Error: System prompt missing at {persona_path}")
        return 1

    with open(persona_path, encoding="utf-8") as f:
        system_agent_prompt = f.read()

    # 4b. Initialize MCP workbench for richer context
    print("   Initializing MCP workbench...")
    orch = init_mcp(repo_path=target_repo, config_path=mcp_config_path)
    if orch:
        print("   [Done] MCP workbench active (tools + git + memory)\n")
    else:
        print("   [Skipped] No MCP config found\n")

    try:
        return _run_review(
            target_repo=target_repo,
            target_branch=target_branch,
            source_branch=source_branch,
            raw_diff=raw_diff,
            changed_files=changed_files,
            project_map=project_map,
            system_agent_prompt=system_agent_prompt,
            project_context_path=args.project_context,
            request_timeout=args.timeout,
            no_chunk_timeout=args.no_chunk_timeout,
            start_time=start_time,
        )
    except KeyboardInterrupt:
        print("\n⛔ Review interrupted by user.")
        return 130
    except Exception as e:
        print(f"\n❌ Review failed: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
        return 2
    finally:
        # Always release MCP subprocesses / HTTP connections, even on failure.
        # Without this, an LLM timeout or judge parse error would orphan them.
        if _mcp_orch:
            with contextlib.suppress(Exception):
                _mcp_orch.stop()


def _run_review(
    *,
    target_repo: str,
    target_branch: str,
    source_branch: str,
    raw_diff: str,
    changed_files: list[str],
    project_map: dict,
    system_agent_prompt: str,
    project_context_path: str | None,
    request_timeout: float | None,
    no_chunk_timeout: bool = False,
    start_time: float,
) -> int:
    """Execute the review pipeline. Returns process exit code."""

    # 4c. Load project-specific context file if provided
    project_context_block = ""
    project_context_basename = ""
    if project_context_path:
        if os.path.exists(project_context_path):
            if os.path.isdir(project_context_path):
                print(
                    f"   [Warning] Project context path is a directory, "
                    f"skipping: {project_context_path}\n"
                )
            else:
                with open(project_context_path, encoding="utf-8") as f:
                    project_context_block = minify_markdown(f.read())
                project_context_basename = os.path.basename(project_context_path)
                print(f"   [Loaded] Project context from {project_context_path}\n")
        else:
            print(
                f"   [Warning] Project context file not found: {project_context_path}\n"
            )
    else:
        print("   [Skipped] No project context file specified (-c to add)\n")

    # 4d. Preprocess the diff for large/branchy changes: summarize
    # auto-generated noise (migrations, lockfiles, minified bundles, fixtures)
    # down to a signature so they don't bloat every prompt, then group the
    # remaining reviewable files into size-bounded chunks. Below the
    # CHUNK_THRESHOLD_CHARS threshold we always run a single detailed pass
    # even if group_diff_chunks would have split; chunking loses cross-file
    # context and pays per-chunk overhead, which hurts review quality for
    # diffs that fit comfortably in the model's context window.
    cleaned_diff, _noise_records = preprocess_diff(raw_diff)
    review_chunks = group_diff_chunks(cleaned_diff)
    multi_chunk = (
        len(review_chunks) > 1 and len(cleaned_diff) > CHUNK_THRESHOLD_CHARS
    )
    if multi_chunk:
        chunked_diff = "\n\n".join(c["body"] for c in review_chunks)
    elif len(review_chunks) > 1:
        # Single-pass mode (below threshold) but the chunker wanted to split
        # — use the full cleaned diff so the model sees every file.
        chunked_diff = cleaned_diff
    else:
        chunked_diff = review_chunks[0]["body"]
    if multi_chunk:
        sizes = ", ".join(f"{c['key']}:{c['size']:,}" for c in review_chunks)
        print(
            f"   [Large diff] Split review into {len(review_chunks)} chunks "
            f"({sizes}) — each reviewed as its own pass, then merged."
        )
    elif len(review_chunks) > 1:
        # Diff is split-able but small enough to keep whole. Force a single
        # chunk so the model sees all files together and the report is
        # synthesized, not concatenated. We rebuild chunked_diff below to
        # include every hunk — no need to mutate the chunk list itself.
        print(
            f"   [Diff {len(cleaned_diff):,} chars, below {CHUNK_THRESHOLD_CHARS:,} "
            f"threshold] Reviewing as a single pass for cross-file context."
        )

    # 5. Compute a single token-budget allocation shared by all prompts.
    # Uses the CLEANED diff (post noise-summarization). For multi-chunk diffs
    # only one chunk (~CHUNK_TARGET_CHARS) streams into any single review call,
    # so we reserve just that bounded amount and give map/key-file context a
    # healthy slice instead of letting a 150K+ diff starve it.
    if multi_chunk:
        reserve_diff_chars = min(len(chunked_diff), CHUNK_TARGET_CHARS)
    else:
        reserve_diff_chars = len(cleaned_diff)
    budget = _allocate_context_budget(
        persona_chars=len(system_agent_prompt), diff_chars=reserve_diff_chars
    )

    project_map_json = json.dumps(project_map, default=str, separators=(",", ":"))
    changed_files_json = json.dumps(changed_files, separators=(",", ":"))

    # 5b. Collect key file contents for fact-checking (model definitions, settings)
    print("📚 Step 1c: Collecting key source files for fact-checking...")
    key_file_context = collect_key_file_context(
        target_repo, changed_files, max_chars=budget["key_files"]
    )
    if key_file_context:
        print(f"   [Done] Collected key file context ({len(key_file_context)} chars)")
    else:
        print("   [Skipped] No key files matched changed files")

    # 1d. BM25 retrieval of method/function bodies touched by the diff. The
    # directory-heuristic key-file collection only finds models.py for the
    # affected app; semantic retrieval surfaces the actual callers, service
    # helpers, and usage sites referenced by the changed code — directly
    # improving blast-radius and claim-verification fidelity.
    rag_query = _diff_identifiers(raw_diff) or (
        " ".join(
            os.path.basename(f).split(".")[0].replace("-", "_") for f in changed_files
        )
    )
    rag_block = retrieve_relevant_code(
        rag_query, target_repo, top_k=6, max_chars=budget["key_files"]
    )
    if rag_block:
        print(f"   [Done] BM25 retrieval ({len(rag_block)} chars)\n")
        # Fold into key_file_context so it flows through every existing prompt
        # interpolation and the judge context without editing each template.
        key_file_context = (key_file_context + "\n\n" + rag_block).strip()
    else:
        print("   [Skipped] No BM25 retrieval results\n")

    # 1d-bis. Import-graph blast radius: list files that import each changed
    # module so the reviewer can VERIFY blast-radius claims instead of guessing.
    # Capped tightly: this is a list of file PATHS (low information density vs.
    # code), and a hugely-imported file (e.g. models.py) can otherwise produce
    # 16k+ chars that bloats prefill — especially slow on large thinking models.
    blast_block = _blast_radius_block(
        target_repo, changed_files, max_chars=min(budget["key_files"], 5000)
    )
    if blast_block:
        print(f"   [Done] Import-graph blast radius ({len(blast_block)} chars)\n")
        key_file_context = (key_file_context + "\n\n" + blast_block).strip()
    else:
        print("   [Skipped] No downstream dependents found\n")

    # 1e. Machine-derived diff invariants (deterministic schema facts). These
    # are parsed from the raw diff, not inferred, so the model cannot
    # hallucinate that a migration is needed to ADD a column the diff actually
    # REMOVES (e.g. a field relocated to an abstract base). Injected into every
    # prompt variant and used for the post-hoc audit below.
    invariants = parse_diff_invariants(changed_files, raw_diff)
    invariant_block = build_invariant_block(invariants, key_file_context)
    if invariant_block:
        print(
            f"   [Done] Diff invariants derived "
            f"({len(invariant_block)} chars, "
            f"{len(invariants.field_changes)} field changes)\n"
        )
    else:
        print("   [Skipped] No model-field invariants in this diff\n")

    # 6. Build context sections. Each section is clipped to its allocated
    # share so the combined prompt stays within the model's context window.
    mcp_block = build_mcp_context() if _mcp_orch else ""
    mcp_budget = budget["mcp_project"] // 2
    project_budget = budget["mcp_project"] - mcp_budget

    mcp_block = _clip(mcp_block, mcp_budget, "MCP context")
    project_context_block = _clip(
        project_context_block, project_budget, "project context"
    )
    clipped_map = _clip(project_map_json, budget["map"], "project map")

    mcp_prompt_section = (
        f"\n\n### MCP-Augmented Context (Live Project State)\n{mcp_block}"
        if mcp_block
        else ""
    )
    project_context_section = (
        f"\n\n### Project-Specific Context ({project_context_basename})\n{project_context_block}"
        if project_context_block
        else ""
    )
    key_file_section = f"\n\n{key_file_context}" if key_file_context else ""
    invariant_section = f"\n\n{invariant_block}" if invariant_block else ""

    # Single-pass fallback: used for local-only mode and cloud API failures.
    # Must be self-contained (no chained history), so include everything.
    fallback_prompt = f"""Below is the project model map (field names for fact-checking) and the git diff.{mcp_prompt_section}{project_context_section}{key_file_section}{invariant_section}

## Project Model Map
```json
{clipped_map}
```

## Git Diff
```diff
{raw_diff}
```

## Review Instructions
Review the diff above. For each changed file, evaluate whether the changes are correct.

### Mandatory Rules (violations will be flagged):
1. **CITE DIFF LINES** — For every issue, quote the actual `+` or `-` lines from the diff. Example: `in profiles/models.py line 42: +   is_active = BooleanField(default=True)`
2. **NO FABRICATED EXAMPLES** — Never invent function names, permission codenames, method signatures, or field names. If you cannot see them in the diff, the project map, or the key source file contents, do not mention them.
3. **UNCERTAIN MEANS UNCERTAIN** — If you aren't sure whether a change is correct, say `UNCERTAIN: [what you're unsure about]`. Do not hedge with vague wording.
4. **CORRECT IS A FINDING** — If a change looks correct, say "Looks correct" explicitly. A review that finds no issues is valid.
5. **NO GENERIC ADVICE** — Do not give generic Django/python architecture lectures. Only comment on what the diff actually changes.
6. **FILE-BY-FILE** — Cover each changed file in order. For each one: (a) what changed, (b) is it correct, (c) any issues found.
7. **TEST COVERAGE & VALIDITY** — For every changed source file, check that corresponding test files exist and adequately cover the new/modified logic. Flag tests that use vague or tautological assertions (e.g., `assert response.status_code == 200` without checking response body, or `assert True`). Tests must assert meaningful outcomes — request that tests validate actual state changes, error messages, or data transformations.
8. **CONCISION & REUSE (Ponytail lens)** — Flag over-engineering in the diff:
   - Could existing codebase utilities or helpers have been reused instead of writing new code?
   - Could stdlib or an already-installed package have handled this without a new dependency?
   - Does the change add speculative abstractions or future-proofing not required by the feature?
   - Is the diff unnecessarily large for what it accomplishes?
   - Flag with "OVER-ENGINEERED" where applicable, noting what could be simplified.
9. **VERIFY CLAIMS** — When stating field attributes (max_length, null, choices), cross-reference the project topography map or key source file contents. Do not assume defaults without evidence.
10. **GROUND TO INVARIANTS** — The Machine-Derived Diff Invariants section is deterministic ground truth parsed from the raw diff. If an invariant says a field was REMOVED or MOVED (column already exists), never claim a schema-altering migration (AddField OR RemoveField) is required for it — both would be wrong, and RemoveField would DROP the existing column. Any finding contradicting an invariant is a fabrication, not a finding.

Format as markdown with file paths as headings."""

    # Two-pass reasoning. The runner now chains assistant turns, so Pass 2
    # can see Pass 1's analysis. We therefore put ALL shared context (map,
    # key files, MCP, project rules) into Pass 1 only and leave Pass 2 to
    # focus on the diff plus a reference to Pass 1's context. This roughly
    # halves the token cost of the multi-pass flow.
    pass1 = f"""[Pass 1: Context Alignment & Blast Radius Mapping]
Here is the global system layout of the app (models with their fields, serializers, views):
{clipped_map}

Here are the files changed in this branch:
{changed_files_json}{mcp_prompt_section}{project_context_section}{key_file_section}{invariant_section}

Analyze the structural intersection. Which upstream modules, views, or serializers could break or be impacted by changes to these specific files?
Identify potential vulnerabilities or scaling defects introduced by the patch.

Keep this analysis focused — Pass 2 will use it as grounding for the line-by-line review."""

    pass2 = f"""[Pass 2: Detailed Code Review]
Using the structural analysis from Pass 1 above (project map, key source files, MCP context, project rules), review the raw lines of code changed in this branch:
```diff
{raw_diff}
```
{invariant_section}

Generate your final review report. Evaluate line changes, ensure patterns are clean, verify things are getting better and not worse, and generate code corrections where needed. Do not re-fetch context — use only what Pass 1 established.

### Mandatory Rules (violations will be flagged):
1. **CITE DIFF LINES** — For every issue, quote the actual `+` or `-` lines from the diff.
2. **NO FABRICATED EXAMPLES** — Never invent function names, permission codenames, method signatures, or field names. Use only what is visible in the diff, the project map, or the key source file contents from Pass 1.
3. **UNCERTAIN MEANS UNCERTAIN** — If you aren't sure, say `UNCERTAIN: [what you're unsure about]`.
4. **CORRECT IS A FINDING** — If a change looks correct, say "Looks correct" explicitly.
5. **NO GENERIC ADVICE** — Only comment on what the diff actually changes.
6. **FILE-BY-FILE** — Cover each changed file in order: (a) what changed, (b) is it correct, (c) any issues found.
7. **TEST COVERAGE & VALIDITY** — Check tests exist for changed source files and flag weak/tautological assertions.
8. **CONCISION & REUSE (Ponytail lens)** — Flag over-engineering where existing utilities, stdlib, or smaller diffs would have sufficed.
9. **VERIFY CLAIMS** — Cross-reference field attributes against the project map / key source files from Pass 1. Do not assume defaults without evidence.
10. **GROUND TO INVARIANTS** — The Machine-Derived Diff Invariants section is deterministic ground truth parsed from the raw diff. If an invariant says a field was REMOVED or MOVED (column already exists), never claim a schema-altering migration (AddField OR RemoveField) is required for it — both would be wrong, and RemoveField would DROP the existing column. Any finding contradicting an invariant is a fabrication, not a finding.

Follow the markdown schema and headers defined in your system prompt."""

    passes = [pass1, pass2]

    # 7. Execute Reasoning Pass
    print(f"🤖 Step 2: Processing Review via [{REASONING_ARCHITECT}]...")
    pass_start = time.time()

    # Outlive the Forge proxy's backend timeout so a slow local model doesn't
    # surface as a proxy 502 (unless the user explicitly set --timeout).
    if request_timeout is None and _cfg.forge_enabled:
        request_timeout = _cfg.forge_backend_timeout + 120

    runner = StatefulHarnessRunner(
        model_name=REASONING_ARCHITECT,
        base_url=ARCHITECT_API_BASE,
        api_key=ARCHITECT_API_KEY,
        fallback_model_name=FALLBACK_REVIEWER,
        local_fallback_model=LOCAL_JUDGE,
        num_ctx=65536,
        request_timeout=request_timeout,
        seed=_cfg.seed,
        use_openai_format=_cfg.use_openai_format,
        # Code review is an audit/fact-checking task. Running hot (the runner
        # default of 0.4) directly fuels the confabulation class of errors
        # (e.g. demanding a migration for a column the diff removes). Match the
        # security harness: deterministic sampling at the audit temperature.
        temperature=temperature_for("audit"),
    )

    if multi_chunk:
        # Large diff: run each size-bounded chunk as its OWN fresh conversation
        # so the model never sees a 150K+ diff that overflows its context window.
        # Every chunk still receives the full shared context (map, key files,
        # MCP, invariants) so its findings stay grounded in the same facts.
        #
        # Per-chunk wall-clock cap. The runner's default request_timeout is
        # ~20 min for cloud (Forge proxy) and ~20 min for local; combined with
        # its 5x exponential-backoff retries a single stuck chunk can burn
        # 40+ minutes. For chunked review we tighten the request timeout AND
        # enforce an outer wall-clock so the whole run can't silently hang.
        #
        # Set CODE_REVIEW_CHUNK_TIMEOUT_S=0 or pass --no-chunk-timeout to disable
        # the cap entirely — chunks run as long as the model needs (useful for
        # local 27B+ models where quality > latency and there's no API rate-limit
        # pressure to escape).
        chunk_request_timeout = _resolve_chunk_timeout(
            request_timeout=request_timeout,
            no_chunk_timeout=no_chunk_timeout,
            env_timeout_s=CHUNK_PASS_TIMEOUT_S,
        )
        runner.request_timeout = chunk_request_timeout

        # Outer wall-clock guard disabled when timeout is disabled.
        chunk_budget: float = (
            float("inf") if chunk_request_timeout is None
            else chunk_request_timeout * 2
        )

        chunk_reviews: list[dict] = []
        run_start = time.time()
        for idx, chunk in enumerate(review_chunks, 1):
            elapsed = time.time() - run_start
            timeout_note = (
                "no per-pass cap"
                if chunk_request_timeout is None
                else f"per-pass timeout {chunk_request_timeout}s"
            )
            print(
                f"   --- Chunk {idx}/{len(review_chunks)} "
                f"[{chunk['key']}] ({len(chunk['files'])} files, "
                f"{chunk['size']:,} chars) "
                f"[elapsed {elapsed/60:.1f}m, {timeout_note}] ---"
            )
            chunk_pass = f"""[Code Review — Chunk {idx}/{len(review_chunks)}]
Below is the project model map, key source files, MCP live state, project rules, and diff invariants for the whole change, followed by the diff chunk you are responsible for reviewing.
{clipped_map}
{key_file_section}
{mcp_prompt_section}{project_context_section}

Files in this chunk:
{json.dumps(chunk['files'], separators=(",", ":"))}
{invariant_section}

## Git Diff (this chunk only)
```diff
{chunk['body']}
```

Review ONLY the files shown in this chunk's diff. For each changed file evaluate: (a) what changed, (b) is it correct, (c) any issues found. Cite real diff lines; never fabricate names; if unsure say `UNCERTAIN: ...`.

### Mandatory Rules (violations will be flagged):
1. **CITE DIFF LINES** — For every issue, quote the actual `+` or `-` lines from this chunk's diff.
2. **NO FABRICATED EXAMPLES** — Never invent function names, method signatures, or field names that aren't in this chunk's diff, the project map, or the key source files above.
3. **UNCERTAIN MEANS UNCERTAIN** — If you aren't sure whether a change is correct, say `UNCERTAIN: [what you're unsure about]`.
4. **CORRECT IS A FINDING** — If a change looks correct, say "Looks correct" explicitly.
5. **NO GENERIC ADVICE** — Only comment on what this chunk's diff actually changes.
6. **FILE-BY-FILE** — Cover each file in this chunk in order: (a) what changed, (b) is it correct, (c) any issues found.
7. **TEST COVERAGE & VALIDITY** — For every changed source file, check corresponding test files exist and flag weak/tautological assertions.
8. **CONCISION & REUSE (Ponytail lens)** — Flag over-engineering where existing utilities, stdlib, or a smaller diff would have sufficed. Flag with "OVER-ENGINEERED" where applicable.
9. **VERIFY CLAIMS** — Cross-reference field attributes against the project map / key source files above. Do not assume defaults without evidence.
10. **GROUND TO INVARIANTS** — The Machine-Derived Diff Invariants are deterministic ground truth. If an invariant says a field was REMOVED or MOVED (column already exists), never claim a schema-altering migration (AddField OR RemoveField) is required for it.

Format as markdown with file paths as headings. Review ONLY your chunk — do not review files from other chunks."""

            chunk_t0 = time.time()
            # Heartbeat only when the cap is disabled. With a cap, the
            # cap-abort would fire long before any heartbeat — keeping the
            # output clean for the bounded case.
            heartbeat_ctx = (
                _run_with_heartbeat(
                    label=f"Chunk {idx}/{len(review_chunks)} [{chunk['key']}]",
                    started_monotonic=time.monotonic(),
                    interval_s=HEARTBEAT_INTERVAL_S,
                )
                if chunk_request_timeout is None
                else contextlib.nullcontext()
            )
            with heartbeat_ctx:
                history = runner.execute_sequence(
                    system_prompt=system_agent_prompt,
                    passes=[chunk_pass],
                    fallback_prompt=None,
                )
            chunk_elapsed = time.time() - chunk_t0

            # Per-chunk wall-clock sanity check. The runner's internal retries
            # can stretch a stuck request past the request_timeout ceiling
            # (5x exponential backoff = ~6x the per-request budget). If a chunk
            # ate >2x its allotted budget we abort the run cleanly with what we
            # have rather than burning hours on one stuck pass. When the cap is
            # disabled (CHUNK_PASS_TIMEOUT_S=0) chunk_budget is inf and this
            # check never fires — chunks run until the model returns.
            if chunk_elapsed > chunk_budget:
                print(
                    f"\n⛔ CHUNK {idx} exceeded wall-clock budget "
                    f"({chunk_elapsed/60:.1f}m > "
                    f"{chunk_budget/60:.1f}m). "
                    "Aborting remaining chunks to preserve elapsed work."
                )
                print(
                    f"   Partial review covers {len(chunk_reviews)} of "
                    f"{len(review_chunks)} chunks — saved below."
                )
                break

            if not history:
                print(f"   ⚠️  Chunk {idx} produced no output; skipping.")
                continue
            chunk_reviews.append(
                {"key": chunk["key"], "files": chunk["files"], "output": history[-1]["output"]}
            )

        if not chunk_reviews:
            print("❌ Error: All review chunks produced no output.")
            return 2
        final_review = merge_chunk_reviews(chunk_reviews)
    else:
        history = runner.execute_sequence(
            system_prompt=system_agent_prompt,
            passes=passes,
            fallback_prompt=fallback_prompt,
        )
        if not history:
            print(
                "❌ Error: Review produced no output (both primary and fallback failed)."
            )
            return 2
        final_review = history[-1]["output"]

    model_used = runner.model_name

    print(
        f"   [Done] Review generated via {model_used} in {time.time() - pass_start:.2f}s"
    )

    # 8. Verify claims against codebase
    print("🔍 Step 3: Verifying claims against codebase...")
    verify_start = time.time()

    # Restrict claim extraction to known model names so generic prose like
    # "The.first" or "This.is" doesn't pollute the claim count.
    # NOTE: the topographer stores models under the "class" key (not "name"),
    # so we read "class" — the previous "name" lookup always yielded an empty
    # set and silently dropped every field-existence claim.
    known_models = {
        m.get("class") or m.get("name") or "" for m in project_map.get("models", [])
    }
    known_models.discard("")
    claims = extract_claims(final_review, known_models=known_models)
    verification_report = verify_claims(
        claims, project_map, key_file_context, target_repo=target_repo
    )

    # 8b. Machine audit FIRST so the claim-verification summary and the report
    # reflect contradictions instead of reporting a misleading clean pass.
    audit_note = audit_review_against_invariants(
        final_review, invariants, key_file_context
    )
    if audit_note:
        print("   [Audit] Flagged review claims contradicting diff invariants")
        verification_report.audit_contradictions = audit_note.count(
            "contradict the machine-derived diff invariants"
        )
    final_review = annotate_review(final_review, verification_report)

    # 8c. When the audit invalidates findings, surface the retraction at the
    # TOP of the report — before the verdict — so it cannot be missed. The
    # bottom-of-report footnote that previously carried this correction read
    # as an afterthought while the fabricated finding stayed front and center.
    if audit_note:
        final_review = build_retraction_banner(audit_note) + final_review

    print(f"   [Done] Claim verification in {time.time() - verify_start:.2f}s")
    print(f"   {verification_report.to_summary()}")

    # 9. Evaluate review quality
    print(f"⚖️ Step 4: Checking review quality via Local Judge [{LOCAL_JUDGE}]...")
    judge_start = time.time()

    # Provide the diff + project map + key file contents as ground truth.
    # The diff is clipped to a generous window; tell the judge explicitly
    # when it cannot see the full diff so it doesn't down-score claims it
    # can't verify. For multi-chunk reviews, use the cleaned/chunked diff
    # (noise summarized away) which faithfully represents what the reviewer
    # saw, rather than blindly clipping the raw first-20K.
    judge_source_diff = chunked_diff if multi_chunk else raw_diff
    diff_visible = judge_source_diff[:20000]
    diff_truncated = len(judge_source_diff) > 20000
    judge_diff_note = (
        "\n(Note: diff truncated to first 20000 chars for judge context.)\n"
        if diff_truncated
        else ""
    )
    audit_warning = (
        "\n\n⚠️ SCORING DIRECTIVE: The review under evaluation carries a "
        "'REVIEW INVALIDATED BY MACHINE AUDIT' banner at its top flagging "
        "claims that CONTRADICT the diff invariants above. Those flagged "
        "claims assert false schema facts — score factual_accuracy and "
        "claim_grounding at 1 regardless of other qualities.\n"
        if audit_note
        else ""
    )
    judge_context = (
        f"Diff ({len(judge_source_diff)} chars total"
        + (", showing first 20000" if diff_truncated else "")
        + f"):\n```diff\n{diff_visible}\n```{judge_diff_note}\n\n"
        f"Project Map:\n{_clip(project_map_json, 8000, 'project map')}\n\n"
        f"{_clip(key_file_context, 4000, 'key file context')}"
        + (
            f"\n\n{_clip(invariant_block, 3000, 'diff invariants')}"
            if invariant_block
            else ""
        )
        + audit_warning
    )
    evaluator = AutomatedEvaluator(
        judge_model=LOCAL_JUDGE,
        base_url=_cfg.base_url,
        use_openai_format=_cfg.use_openai_format,
    )
    try:
        scores = evaluator.grade_run(
            final_review, "rubrics/code_review_rubric.json", context=judge_context
        )
    except Exception as e:
        print(
            f"   ⚠️  Judge failed ({type(e).__name__}: {e}); continuing without scores."
        )
        scores = {"_judge_error": str(e)[:200]}

    # Deterministic enforcement of the scoring directive: when the audit
    # flagged invariant contradictions, the judge may not honor the directive
    # on its own — clamp the grounded metrics so the penalty is guaranteed.
    if audit_note and not scores.get("_judge_error"):
        for metric in ("factual_accuracy", "claim_grounding"):
            if metric in scores:
                scores[metric] = 1
        scores["_audit_invalidated"] = (
            "Machine audit flagged invariant contradictions; factual_accuracy "
            "and claim_grounding clamped to 1."
        )

    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"📊 Review Reliability Scores: {scores}")

    # 10. Log and Export Artifacts
    print("🗄️ Step 5: Archiving run data...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=model_used,
        agent_role=f"Staff Code Review ({source_branch})",
        raw_output=final_review,
        scores=scores,
    )

    report_filename = "reports/automated_code_review.md"
    os.makedirs("reports", exist_ok=True)
    report_text = final_review + build_scores_section(scores, audit_note)
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(report_text)

    if _mcp_orch:
        _mcp_orch.remember(
            f"review:{source_branch}:complete",
            f"Code review completed for {source_branch} -> {target_branch}. Report: {report_filename}",
            tags=["code_review", source_branch, "complete"],
        )

    total_duration = time.time() - start_time
    print(f"\n✅ Report saved to: {report_filename}")
    print(f"⏱️ Total Time: {total_duration:.2f}s  Model: {model_used}")
    return 0


if __name__ == "__main__":
    with ForgeProxy(get_config()):
        sys.exit(main())
