"""Tests for quality_gate.py — deterministic gates, semantics, and reporting.

Network-free: every test mocks subprocess to simulate gate outcomes so the
test suite stays fast and deterministic regardless of whether ruff/mypy/etc.
are installed in CI.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

import quality_gate as qg

ROOT = Path(__file__).resolve().parent.parent


# ── Gate detection / availability ────────────────────────────────────────────


def test_default_gates_have_required_test_and_lint() -> None:
    names = {g.name for g in qg.DEFAULT_GATES}
    assert "test" in names and "lint" in names
    by_name = {g.name: g for g in qg.DEFAULT_GATES}
    assert by_name["test"].tier == "required"
    assert by_name["lint"].tier == "required"


def test_advisory_gates_are_distinct_from_required() -> None:
    by_name = {g.name: g for g in qg.DEFAULT_GATES}
    assert by_name["type"].tier == "advisory"
    assert by_name["security"].tier == "advisory"


def test_a3_gate_is_opt_in_not_in_defaults() -> None:
    assert all(g.name != "a3" for g in qg.DEFAULT_GATES)
    assert qg.A3_GATE.tier == "advisory"


# ── Filter / opt-in logic ────────────────────────────────────────────────────


def _fake_passing_result(gate):  # type: ignore[no-untyped-def]
    @dataclass
    class R:
        name = gate.name
        tier = gate.tier
        passed = True
        skipped = False
        exit_code = 0
        evidence = ""
        summary = "exit=0"
        passthrough = gate.passthrough

    return R()


def _force_all_gates_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every gate's ``check()`` to return True regardless of host tools.

    Without this, tests that mock ``subprocess.run`` still see gate.skip=True
    in environments (e.g. system Python without the venv's dev tools)
    where the tools aren't installed. The tests below assert on subprocess
    call counts, so they need every gate to flow through ``_run``.
    """
    monkeypatch.setattr(qg.Gate, "check", lambda self: True)


def test_deep_gate_skipped_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUALITY_GATE_DEEP", raising=False)
    monkeypatch.setattr("sys.argv", ["quality_gate.py"])
    monkeypatch.setattr(qg, "_run_gate", _fake_passing_result)

    with patch.object(qg.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        qg.main()
        called = " ".join(str(args) for args, _ in mock_run.call_args_list)
        assert "a3" not in called, f"a3 ran unexpectedly under default mode: {called}"


def test_deep_gate_runs_with_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUALITY_GATE_DEEP", "1")
    monkeypatch.setattr("sys.argv", ["quality_gate.py"])
    _force_all_gates_available(monkeypatch)
    with patch.object(qg.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        qg.main()
        called = " ".join(str(args) for args, _ in mock_run.call_args_list)
        assert "a3" in called, f"a3 did not run with QUALITY_GATE_DEEP=1: {called}"


def test_gate_filter_narrows_to_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["quality_gate.py", "--gate", "lint"])
    _force_all_gates_available(monkeypatch)
    with patch.object(qg.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout="All clear", stderr=""
        )
        rc = qg.main()
        assert rc == 0
        called_cmds = [args[0] for args, _ in mock_run.call_args_list]
        assert any("ruff" in str(c) for c in called_cmds)
        assert not any("pytest" in str(c) for c in called_cmds)


def test_unknown_gate_filter_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["quality_gate.py", "--gate", "nope"])
    _force_all_gates_available(monkeypatch)
    with patch.object(qg.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        rc = qg.main()
        assert rc == 2


# ── Severity semantics ───────────────────────────────────────────────────────


def test_advisory_failure_does_not_block_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["quality_gate.py", "--gate", "type"])
    _force_all_gates_available(monkeypatch)
    with patch.object(
        qg.subprocess, "run",
        return_value=subprocess.CompletedProcess(
            args=["mypy"], returncode=1, stdout="3 errors", stderr="",
        ),
    ):
        rc = qg.main()
        assert rc == 0


def test_advisory_failure_blocks_under_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv", ["quality_gate.py", "--strict", "--gate", "type"]
    )
    _force_all_gates_available(monkeypatch)
    with patch.object(
        qg.subprocess, "run",
        return_value=subprocess.CompletedProcess(
            args=["mypy"], returncode=1, stdout="3 errors", stderr="",
        ),
    ):
        rc = qg.main()
        assert rc == 1


def test_required_failure_always_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["quality_gate.py", "--gate", "lint"])
    _force_all_gates_available(monkeypatch)
    with patch.object(
        qg.subprocess, "run",
        return_value=subprocess.CompletedProcess(
            args=["ruff"], returncode=1, stdout="1 error", stderr="",
        ),
    ):
        rc = qg.main()
        assert rc == 1


# ── Baseline mechanism ───────────────────────────────────────────────────────


def test_baseline_suppresses_known_advisory_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    baseline_path = tmp_path / "bl.json"
    baseline_path.write_text(json.dumps({"advisory_fail_rc": {"type": 1}}))

    monkeypatch.setattr(
        "sys.argv",
        ["quality_gate.py", "--strict", "--baseline", str(baseline_path), "--gate", "type"],
    )
    _force_all_gates_available(monkeypatch)
    with patch.object(
        qg.subprocess, "run",
        return_value=subprocess.CompletedProcess(
            args=["mypy"], returncode=1, stdout="3 errors", stderr=""
        ),
    ):
        rc = qg.main()
        assert rc == 0


def test_baseline_does_not_suppress_new_advisory_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    baseline_path = tmp_path / "bl.json"
    baseline_path.write_text(json.dumps({"advisory_fail_rc": {"type": 1}}))

    monkeypatch.setattr(
        "sys.argv",
        ["quality_gate.py", "--strict", "--baseline", str(baseline_path), "--gate", "type"],
    )
    _force_all_gates_available(monkeypatch)
    with patch.object(
        qg.subprocess, "run",
        return_value=subprocess.CompletedProcess(
            args=["mypy"], returncode=2, stdout="broken", stderr=""
        ),
    ):
        rc = qg.main()
        assert rc == 1


# ── Helpers ─────────────────────────────────────────────────────────────────


def test_exclude_venv_helper() -> None:
    args = qg._exclude_venv()
    assert ".venv" in args and "--exclude" in args


def test_load_baseline_handles_missing(tmp_path: Path) -> None:
    assert qg._load_baseline(tmp_path / "nope.json") == {}


def test_load_baseline_handles_corrupt(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json{{{")
    assert qg._load_baseline(bad) == {}
