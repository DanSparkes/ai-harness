"""Tests for the code_review.py and core/ fixes.

Covers the issues fixed in the prior review pass:
- Token-budget allocator and clip helper
- Stream-capped file reads
- Claim-extraction false-positive filtering
- String-literal-aware field-block extraction
- Judge JSON-parse fallback
- StatefulHarnessRunner constructor params
- Claim-verifier cache lifecycle

These tests are network-free: they exercise pure logic only.
"""

from pathlib import Path

import pytest

from code_review import _allocate_context_budget, _clip, _read_capped
from core.claim_verifier import (
    _extract_field_block,
    _strip_string_literals,
    clear_verify_caches,
    extract_claims,
)
from core.judge import AutomatedEvaluator
from core.runner import StatefulHarnessRunner

# ── Token-budget allocator ────────────────────────────────────────────────────


def test_allocate_budget_returns_positive_split() -> None:
    budget = _allocate_context_budget(persona_chars=4000, diff_chars=30000)
    assert budget["map"] > 0
    assert budget["key_files"] > 0
    assert budget["mcp_project"] > 0
    total = budget["map"] + budget["key_files"] + budget["mcp_project"]
    assert total < 100000


def test_allocate_budget_handles_small_diff() -> None:
    budget = _allocate_context_budget(persona_chars=100, diff_chars=50)
    assert budget["map"] > 0
    assert budget["key_files"] > 0


def test_allocate_budget_floors_remaining_when_diff_huge() -> None:
    # When the diff alone exceeds the budget, we should still hand back
    # a non-trivial floor (not zero / negative) for each block.
    budget = _allocate_context_budget(persona_chars=4000, diff_chars=200000)
    assert budget["map"] >= 1000
    assert budget["key_files"] >= 500


# ── _clip helper ──────────────────────────────────────────────────────────────


def test_clip_truncates_with_label() -> None:
    out = _clip("abcdefghij", 5, "test")
    assert out == "abcde\n... [test truncated at 5 chars]"


def test_clip_passthrough_when_under_limit() -> None:
    assert _clip("abc", 10) == "abc"


def test_clip_default_label() -> None:
    out = _clip("abcdefghij", 5)
    assert "context truncated" in out


# ── _read_capped ──────────────────────────────────────────────────────────────


def test_read_capped_does_not_load_whole_file(tmp_path: Path) -> None:
    big = tmp_path / "big.py"
    big.write_bytes(b"x = 1\n" * 100000)  # ~600KB
    content = _read_capped(big, cap=1000, label="big")
    assert len(content) < 2000
    assert "truncated" in content


def test_read_capped_returns_empty_on_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.py"
    assert _read_capped(missing, cap=100) == ""


def test_read_capped_reads_under_cap(tmp_path: Path) -> None:
    small = tmp_path / "small.py"
    small.write_text("x = 1\n")
    content = _read_capped(small, cap=1000)
    assert "x = 1" in content
    assert "truncated" not in content


# ── Claim extraction: known-models filter ─────────────────────────────────────


def test_extract_claims_without_filter_keeps_false_positives() -> None:
    text = "The.first thing to note is that Course.title has max_length=200."
    claims = extract_claims(text)
    field_claims = {c["text"] for c in claims if c["type"] == "field_exists"}
    # Without filter, capitalized English becomes a false positive
    assert "The.first" in field_claims


def test_extract_claims_with_filter_drops_false_positives() -> None:
    text = (
        "The.first thing to note is that Course.title has max_length=200. This.is fine."
    )
    claims = extract_claims(text, known_models={"Course"})
    field_claims = {c["text"] for c in claims if c["type"] == "field_exists"}
    assert "Course.title" in field_claims
    assert "The.first" not in field_claims
    assert "This.is" not in field_claims


def test_extract_claims_filter_with_empty_known_models_drops_all() -> None:
    text = "Course.title and Foo.bar"
    claims = extract_claims(text, known_models=set())
    field_claims = [c for c in claims if c["type"] == "field_exists"]
    assert field_claims == []


# ── String-literal-aware paren counting ───────────────────────────────────────


def test_strip_string_literals_blanks_double_quoted() -> None:
    line = 'help_text="see (docs) and more)"'
    stripped = _strip_string_literals(line)
    assert "(" not in stripped
    assert ")" not in stripped


def test_strip_string_literals_blanks_single_quoted() -> None:
    line = "default=')' "
    stripped = _strip_string_literals(line)
    assert ")" not in stripped


def test_strip_string_literals_blanks_triple_quoted() -> None:
    line = 'x = """multi (line\nstring)"""\n'
    stripped = _strip_string_literals(line)
    assert "(" not in stripped


def test_extract_field_block_respects_parens_in_string_literals() -> None:
    src = (
        "class Foo(models.Model):\n"
        "    bar = models.CharField(\n"
        "        max_length=100,\n"
        '        help_text="Parens (here) should not break depth tracking",\n'
        '        default=")",\n'
        "    )\n"
        "    next_field = models.IntegerField()\n"
    )
    block = _extract_field_block(src, "bar")
    assert block is not None
    assert "next_field" not in block, "block ran past the field definition"
    assert "max_length=100" in block


def test_extract_field_block_returns_none_when_field_absent() -> None:
    src = "class Foo(models.Model):\n    bar = models.IntegerField()\n"
    assert _extract_field_block(src, "nonexistent") is None


# ── Judge JSON parse fallback ─────────────────────────────────────────────────


def test_parse_judge_json_falls_back_on_garbage() -> None:
    metrics = {"diff_adherence": "x", "factual_accuracy": "y"}
    result = AutomatedEvaluator._parse_judge_json("not json at all", metrics)
    assert result["diff_adherence"] == 3
    assert result["factual_accuracy"] == 3
    assert "_judge_parse_error" in result


def test_parse_judge_json_strips_think_tags_and_code_fences() -> None:
    metrics = {"diff_adherence": "x"}
    raw = '<think>reasoning here</think>\n```json\n{"diff_adherence": 5}\n```'
    result = AutomatedEvaluator._parse_judge_json(raw, metrics)
    assert result["diff_adherence"] == 5


def test_parse_judge_json_recovers_from_partial_json_in_text() -> None:
    metrics = {"diff_adherence": "x"}
    raw = 'Here are the scores:\n{"diff_adherence": 4}\nThanks!'
    result = AutomatedEvaluator._parse_judge_json(raw, metrics)
    assert result["diff_adherence"] == 4


def test_parse_judge_json_passes_through_valid_dict() -> None:
    metrics = {"diff_adherence": "x"}
    raw = '{"diff_adherence": 2}'
    result = AutomatedEvaluator._parse_judge_json(raw, metrics)
    assert result == {"diff_adherence": 2}


# ── StatefulHarnessRunner constructor ─────────────────────────────────────────


def test_runner_defaults_for_local_reasoning_model() -> None:
    runner = StatefulHarnessRunner(model_name="ornith:35b")
    assert runner.temperature == 0.4
    # Local default matches core.agent (1200s): thinking models (27B+) on
    # large prompts legitimately need >10 min for a single generation.
    assert runner.request_timeout == 1200
    assert runner.is_cloud is False


def test_runner_defaults_for_local_coder_model() -> None:
    # Coder models get deterministic temperature by default
    runner = StatefulHarnessRunner(model_name="qwen3-coder:32k")
    assert runner.temperature == 0.0


def test_runner_explicit_overrides() -> None:
    runner = StatefulHarnessRunner(
        model_name="ornith:35b", temperature=0.1, request_timeout=120
    )
    assert runner.temperature == 0.1
    assert runner.request_timeout == 120


def test_runner_cloud_detection_via_gemini_name() -> None:
    runner = StatefulHarnessRunner(model_name="gemini-2.5-flash")
    assert runner.is_cloud is True
    assert runner.request_timeout == 120  # cloud default


def test_runner_cloud_detection_via_api_key() -> None:
    runner = StatefulHarnessRunner(model_name="anything", api_key="sk-fake")
    assert runner.is_cloud is True


def test_runner_backward_compat_with_dependabot_style_call() -> None:
    # dependabot_review.py uses this exact construction; must keep working
    runner = StatefulHarnessRunner(
        model_name="ornith:35b",
        base_url="http://localhost:11434",
        api_key=None,
        fallback_model_name="ornith:35b",
        num_ctx=65536,
    )
    assert runner.num_ctx == 65536
    assert runner.fallback_model_name == "ornith:35b"


# ── Claim-verifier cache lifecycle ────────────────────────────────────────────


def test_clear_verify_caches_is_idempotent() -> None:
    # Calling on empty caches should not raise
    clear_verify_caches()
    clear_verify_caches()


def test_fallback_reviewer_is_a_local_model() -> None:
    # Regression test for the bug where FALLBACK_REVIEWER was set to a cloud
    # model name and got sent to Ollama on cloud-down fallback.
    from code_review import FALLBACK_REVIEWER, LOCAL_MODEL

    assert FALLBACK_REVIEWER == LOCAL_MODEL
    assert "gemini" not in FALLBACK_REVIEWER.lower()


def test_main_returns_int_and_requires_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    # main() should return a non-zero exit code (int) when no repo is configured
    import code_review

    monkeypatch.setattr(code_review, "TARGET_REPO", None)
    monkeypatch.delenv("TARGET_REPO", raising=False)
    # parse_arguments uses argparse which reads sys.argv — give it a clean
    # argv so pytest's own flags don't confuse it.
    monkeypatch.setattr("sys.argv", ["code_review.py"])
    exit_code = code_review.main()
    assert isinstance(exit_code, int)
    assert exit_code != 0
