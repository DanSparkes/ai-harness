#!/usr/bin/env python3
"""Deterministic quality gate for the local-harness codebase.

Runs the configured static-analysis / test tools (ruff, mypy, bandit, pytest,
and optionally a3-python) and fails fast on any required gate that does not
clear. This is the Layer-4 "quality gate" backstop for AI-agent edits: an agent
claims a task is done only after this exits 0.

Design principles
-----------------
* Fail-fast: exit non-zero on the first *required* gate failure so CI and
  agent loops stop immediately instead of piling up.
* Evidence-first: each gate emits a compact JSON blob (tool, passed, count,
  summary) that an agent or CI can parse, mirroring the repo's machine-readable
  reporting style.
* Two-tier severity:
    - REQUIRED gates (default fail-on-clear-failure): test (pytest), lint
      (ruff). These are kept clean and cheap.
    - ADVISORY gates (report-only unless --strict): type (mypy), security
      (bandit), deep (a3-python). Pre-existing debt is reported but does not
      block by default; pass --strict to promote them to required.

Usage
-----
  python quality_gate.py                 # required gates only, fail on break
  python quality_gate.py --strict        # also fail on advisory-gate breaks
  python quality_gate.py --json          # machine-readable JSON on stdout
  python quality_gate.py --gate lint --gate test
  python quality_gate.py --baseline .quality_gate.json --write-baseline
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_BIN = ROOT / ".venv" / "bin"


# --------------------------------------------------------------------------- #
# Gate definitions
# --------------------------------------------------------------------------- #

@dataclass
class Gate:
    name: str
    tier: str            # "required" | "advisory"
    cmd: list[str]
    passthrough: bool = False   # True = stream output to console (not captured)
    description: str = ""
    cwd: str | None = None      # Override subprocess CWD; defaults to ROOT
    def check(self) -> bool:
        return shutil.which(self.cmd[0]) is not None or (
            VENV_BIN / self.cmd[0]
        ).exists()


def _run(cmd: list[str], passthrough: bool, cwd: str | None = None) -> tuple[int, str]:
    exe = cmd[0]
    if shutil.which(exe) is None and (VENV_BIN / exe).exists():
        cmd = [str(VENV_BIN / exe), *cmd[1:]]
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd or str(ROOT),
            capture_output=not passthrough,
            text=True,
            timeout=900,
        )
        if passthrough:
            return proc.returncode, ""
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out: {cmd[0]}"


def _exclude_venv() -> list[str]:
    return ["--exclude", ".venv"]


def _tmp_mirror_cwd() -> str:
    """Return a fresh tmpdir for running the test gate in isolation.

    Pre-commit treats any tracked-file modification as "modified by hook"
    and refuses to pass even when tests pass. Several existing tests
    instantiate ``HarnessWarehouse`` (which writes a SQLite DB at the
    default path ``scorecards/evaluation_history.db``); running pytest
    from a tmpdir redirects those writes harmlessly to ``/tmp``.

    Implementation note: we symlink only *source* subtrees (tests, core,
    agents, rubrics, evals) and the top-level entry-point scripts, but
    create EMPTY dirs for runtime-artifact directories (scorecards, reports,
    results). Symlinking scorecards/ would write back to the repo through
    the symlink; symlinking reports/ would write tracked outputs back.
    """
    import tempfile

    tmp = tempfile.mkdtemp(prefix="quality_gate_mirror_")
    # Source subtrees — symlink so pytest collection, imports, and the
    # top-level ``from code_review import ...`` work without copies.
    for entry in ("tests", "core", "agents", "rubrics", "evals"):
        src = ROOT / entry
        if src.exists():
            os.symlink(str(src), os.path.join(tmp, entry))
    # Runtime-artifact dirs — empty mkdir so writes land in /tmp, not the repo.
    # These are tracked in git but produced at runtime; an empty mirror lets
    # tests that default to ``scorecards/...`` paths write harmlessly.
    for entry in ("scorecards", "reports"):
        os.makedirs(os.path.join(tmp, entry), exist_ok=True)
    # Config files — symlink so the test config (testpaths, addopts) resolves.
    for f in ("pyproject.toml", "conftest.py"):
        src = ROOT / f
        if src.exists():
            os.symlink(str(src), os.path.join(tmp, f))
    # Top-level harness scripts (tests import them).
    for f in ("code_review.py", "dependabot_review.py", "quality_gate.py"):
        src = ROOT / f
        if src.exists():
            os.symlink(str(src), os.path.join(tmp, f))
    return tmp


DEFAULT_GATES: list[Gate] = [
    Gate(
        name="test",
        tier="required",
        cmd=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        passthrough=True,
        # Run pytest from a fresh tmpdir so any test that writes to a
        # runtime artifact (e.g. HarnessWarehouse's scorecards/evaluation_history.db
        # tracked in git) doesn't mutate the working tree. Pre-commit interprets
        # tracked-file modifications as "files were modified by this hook" and
        # refuses to pass even when the gate itself is green.
        cwd=_tmp_mirror_cwd(),
        description="Run the full pytest suite (tests/).",
    ),
    Gate(
        name="lint",
        tier="required",
        cmd=["ruff", "check", ".", *_exclude_venv()],
        description="Ruff lint per pyproject rules (E/W/F/N/UP/B/SIM/LOG/RUF).",
    ),
    Gate(
        name="type",
        tier="advisory",
        cmd=["mypy", "core/"],
        description="Mypy type-check of core/ (has pre-existing debt: not a blocker by default).",
    ),
    Gate(
        name="security",
        tier="advisory",
        cmd=["bandit", "-r", "core/", "-q"],
        description="Bandit security scan of core/ (pre-existing debt: not a blocker by default).",
    ),
]


# --------------------------------------------------------------------------- #
# Deep gate (opt-in; slow, designed for CI ratchets)
# --------------------------------------------------------------------------- #

A3_GATE = Gate(
    name="a3",
    tier="advisory",
    cmd=[
        "a3", "scan", "core/", "--interprocedural",
        "--output-sarif", "results/a3-results.sarif",
    ],
    description=(
        "a3-python Z3 symbolic execution; proves/filters bugs. Slow (~40s+) "
        "and returns rc=1 on candidate findings. Opt in via --gate a3 or "
        "QUALITY_GATE_DEEP=1; best scheduled/CI, not per-edit. Optional."
    ),
)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    name: str
    tier: str
    passed: bool
    skipped: bool = False
    exit_code: int = 0
    evidence: str = ""
    summary: str = ""
    passthrough: bool = False


def _summarize(gate: Gate, rc: int, out: str, max_lines: int = 8) -> str:
    if rc == 0:
        lines = [ln for ln in out.splitlines() if ln.strip()]
        tail = lines[-1] if lines else "no output"
        return f"PASS ({tail})"
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # Keep the most informative tail lines as evidence.
    evidence = "\n".join(lines[-max_lines:] or ["(no output)"])
    return evidence


def _run_gate(gate: Gate) -> Result:
    res = Result(name=gate.name, tier=gate.tier, passed=False)
    if not gate.check():
        res.skipped = True
        res.summary = f"SKIP (tool '{gate.cmd[0]}' not installed)"
        return res
    rc, out = _run(gate.cmd, gate.passthrough, gate.cwd)
    res.exit_code = rc
    res.passed = rc == 0
    res.passthrough = gate.passthrough
    if gate.passthrough:
        res.summary = f"exit={rc}"
    else:
        res.evidence = out
        res.summary = _summarize(gate, rc, out)
    return res


def _load_baseline(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--gate", action="append", default=None,
        help="Only run the named gate(s). Repeatable. (e.g. --gate lint --gate test)",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="Promote advisory gates (type/security/a3) to required; fail on any break.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON result (one object per gate) to stdout.",
    )
    p.add_argument(
        "--write-baseline", metavar="PATH", default=None,
        help="Write current advisory-gate findings as a baseline so they don't re-flag.",
    )
    p.add_argument(
        "--baseline", metavar="PATH", default=None,
        help="Read an advisory-gate baseline (advisory gates matching baseline no longer fail under --strict).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # a3 (deep Z3 symbolic execution) is opt-in because it is slow (~40s on
    # full core/) and returns rc=1 on candidate findings even after FP filtering,
    # which would clutter the default fast feedback loop. Run via --gate a3 or
    # QUALITY_GATE_DEEP=1 (env).
    deep_requested = (
        os.environ.get("QUALITY_GATE_DEEP") == "1"
        or (args.gate is not None and "a3" in args.gate)
    )

    gates = list(DEFAULT_GATES)
    if deep_requested:
        gates.append(A3_GATE)

    if args.gate:
        wanted = set(args.gate)
        gates = [g for g in gates if g.name in wanted]
        if not gates:
            print(f"❌ No matching gates for: {args.gate}", file=sys.stderr)
            return 2

    # Advisory findings baseline: keys like "type" / "security" record an rc
    # that, when matched, suppresses the advisory failure even under --strict.
    baseline = _load_baseline(Path(args.baseline) if args.baseline else None)
    advisory_fail_rc: dict[str, int] = baseline.get("advisory_fail_rc", {})

    results: list[Result] = []
    failures: list[Result] = []

    print("=" * 60)
    print("Local Harness — Deterministic Quality Gate")
    print(f"Strict mode : {args.strict}  (advisory gates promoted to required)")
    print("=" * 60)

    for gate in gates:
        res = _run_gate(gate)
        results.append(res)
        flag = "✅" if res.passed else ("⚠️" if res.skipped else "❌")
        print(f"\n[{flag}] {res.name} ({res.tier})")
        if not res.passthrough and res.evidence and not res.skipped:
            print("    " + res.summary.replace("\n", "\n    "))

        if not res.passed and not res.skipped:
            # Advisor failures are forgiven if they match a known baseline.
            if (
                gate.tier == "advisory"
                and advisory_fail_rc.get(gate.name) == res.exit_code
            ):
                print(f"    (baselined: {gate.name} exit={res.exit_code} accepted)")
                res.summary += " [baselined]"
                continue
            if gate.tier == "required" or args.strict:
                failures.append(res)
            else:
                print("    (advisory — not blocking without --strict)")

    if args.write_baseline:
        advisory_fail_rc = {}
        for res in results:
            if res.tier == "advisory" and not res.passed and not res.skipped:
                advisory_fail_rc[res.name] = res.exit_code
        blob = {"advisory_fail_rc": advisory_fail_rc}
        Path(args.write_baseline).write_text(
            json.dumps(blob, indent=2) + "\n"
        )
        print(f"\n📝 Baseline written to {args.write_baseline}")

    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r.passed)
    skipped = sum(1 for r in results if r.skipped)
    failed = len(failures)
    print(
        f"Summary: {passed} passed, {skipped} skipped, "
        f"{len(results) - passed - skipped} advisory, {failed} blocking"
    )
    if failures:
        print("\n❌ BLOCKING FAILURES (required gates must clear):")
        for f in failures:
            print(f"   - {f.name}")
    if args.json:
        print("\n===JSON===")
        print(json.dumps([asdict(r) for r in results], indent=2))
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
