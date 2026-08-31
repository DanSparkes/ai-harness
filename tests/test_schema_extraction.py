"""Tests for cross-model schema extraction, judge JSON robustness, and
empty-artifact guards."""

import json
import os
import sqlite3


from core.judge import AutomatedEvaluator
from core.parser import (
    extract_json_payloads,
    find_section,
    parse_first_json_payload,
    validate_pipeline_schema,
)
from core.warehouse import HarnessWarehouse

VALID_STEP = {
    "step": 1,
    "name": "Add model field",
    "target_file": "app/models.py",
    "task": "Add foo = models.CharField(max_length=10) to Bar",
}
VALID_PLAN = {"feature_name": "f", "target_workspace": "/repo", "pipeline": [VALID_STEP]}


# ── extract_json_payloads: cross-model formatting variance ──────────────────


def test_fenced_json_with_newline():
    text = "Intro\n```json\n" + json.dumps(VALID_PLAN) + "\n```\nOutro"
    assert extract_json_payloads(text) == [VALID_PLAN]


def test_fenced_json_without_newline_after_info_string():
    text = "```json" + json.dumps(VALID_PLAN) + "```"
    assert extract_json_payloads(text) == [VALID_PLAN]


def test_bare_fence():
    text = "```\n" + json.dumps(VALID_PLAN) + "\n```"
    assert extract_json_payloads(text) == [VALID_PLAN]


def test_unfenced_brace_span():
    text = "Here is the plan: " + json.dumps(VALID_PLAN) + " hope it works"
    assert extract_json_payloads(text) == [VALID_PLAN]


def test_think_wrapper_stripped():
    text = "<think>reasoning...</think>\n```json\n" + json.dumps(VALID_PLAN) + "\n```"
    assert extract_json_payloads(text) == [VALID_PLAN]


def test_malformed_blocks_skipped():
    text = "```json\n{not valid,}\n```\n```json\n" + json.dumps(VALID_PLAN) + "\n```"
    assert extract_json_payloads(text) == [VALID_PLAN]


def test_duplicates_deduped():
    body = json.dumps(VALID_PLAN)
    text = f"```json\n{body}\n```\n```json\n{body}\n```"
    assert extract_json_payloads(text) == [VALID_PLAN]


def test_parse_first_json_payload_none_on_garbage():
    assert parse_first_json_payload("no json here at all") is None


# ── find_section ──────────────────────────────────────────────────────────────


def test_find_section_numbered_and_plain_headers():
    md = "# Title\n\n## 1. A\ncontent-a\n\n## 2. Implementation Pipeline\npipe-body\n\n## 3. C\ncontent-c\n"
    assert find_section(md, r"##\s+2\.?\s*Implementation Pipeline") == "pipe-body"
    md2 = "## Implementation Pipeline\nbody-only"
    assert find_section(md2, r"##\s+4\.?\s*Implementation Pipeline", r"##\s+Implementation Pipeline") == "body-only"


def test_find_section_missing_returns_none():
    assert find_section("## Other\nstuff", r"##\s+Implementation Pipeline") is None


def test_find_section_stops_at_next_heading():
    md = "## Implementation Pipeline\njson here\n## Next Section\nlater"
    assert find_section(md, r"##\s+Implementation Pipeline") == "json here"


# ── validate_pipeline_schema ─────────────────────────────────────────────────


def test_valid_plan_passes():
    assert validate_pipeline_schema(VALID_PLAN) == []


def test_missing_pipeline_fails():
    errors = validate_pipeline_schema({"feature_name": "f"})
    assert errors and "pipeline" in errors[0]


def test_empty_pipeline_fails():
    errors = validate_pipeline_schema({"pipeline": []})
    assert errors


def test_step_missing_required_keys_fails():
    errors = validate_pipeline_schema({"pipeline": [{"step": 1}]})
    joined = "; ".join(errors)
    for key in ("name", "target_file", "task"):
        assert key in joined


def test_non_list_pipeline_fails():
    errors = validate_pipeline_schema({"pipeline": "not-a-list"})
    assert errors


# ── Judge strict parsing ──────────────────────────────────────────────────────


def test_judge_strict_parse_variants():
    good = json.dumps({"factual_accuracy": 4})
    assert AutomatedEvaluator._parse_judge_json_strict(good) == {
        "factual_accuracy": 4
    }
    assert AutomatedEvaluator._parse_judge_json_strict(f"```json\n{good}\n```") == {
        "factual_accuracy": 4
    }
    assert AutomatedEvaluator._parse_judge_json_strict(f"```json{good}```") == {
        "factual_accuracy": 4
    }
    assert AutomatedEvaluator._parse_judge_json_strict(
        f"<think>hmm</think>Here: {good} done"
    ) == {"factual_accuracy": 4}


def test_judge_strict_parse_failure_returns_none():
    assert AutomatedEvaluator._parse_judge_json_strict("") is None
    assert AutomatedEvaluator._parse_judge_json_strict("total garbage") is None


def test_judge_lenient_fallback_still_neutral():
    fallback = AutomatedEvaluator._parse_judge_json(
        "garbage", {"factual_accuracy": "", "concision": ""}
    )
    assert fallback["factual_accuracy"] == 3
    assert "_judge_parse_error" in fallback


# ── Empty-artifact guards ─────────────────────────────────────────────────────


def test_warehouse_refuses_empty_output(tmp_path):
    db = str(tmp_path / "scorecards" / "history.db")
    wh = HarnessWarehouse(db_path=db)
    wh.log_run("model", "role", "", {"a": 1})
    wh.log_run("model", "role", "   \n", {"a": 1})
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0]
    assert count == 0


def test_warehouse_logs_valid_output_and_creates_dir(tmp_path):
    db = str(tmp_path / "nested" / "scorecards" / "history.db")
    wh = HarnessWarehouse(db_path=db)
    wh.log_run("model", "role", "real output", {"a": 1})
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT model_name, raw_output, scores, timestamp FROM evaluation_runs"
        ).fetchone()
    assert row[0] == "model"
    assert row[1] == "real output"
    assert json.loads(row[2]) == {"a": 1}
    # Timezone-aware UTC timestamp (no deprecated naive utcnow)
    assert row[3].endswith("+00:00")


def test_grade_and_archive_skips_empty_output(tmp_path, monkeypatch):
    from core import harness

    monkeypatch.setattr(harness, "AutomatedEvaluator", None)  # must not be used
    rubric = tmp_path / "r.json"
    rubric.write_text(json.dumps({"metrics": {"a": "desc"}, "instructions": ""}))
    report = str(tmp_path / "reports" / "out.md")
    scores = harness.grade_and_archive(
        output="",
        rubric_path=str(rubric),
        agent_role="test",
        model_used="m",
        report_path=report,
    )
    assert scores == {"_error": "empty output"}
    assert not os.path.exists(report)


# ── Judge retry on parse failure ──────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": self._content}}


def test_judge_retries_on_bad_json_then_succeeds(tmp_path, monkeypatch):
    from core import judge as judge_mod

    rubric = tmp_path / "r.json"
    rubric.write_text(json.dumps({"metrics": {"a": "desc"}, "instructions": ""}))

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return _FakeResponse("Sure! Here are my thoughts: not json at all")
        return _FakeResponse('{"a": 4}')

    monkeypatch.setattr(judge_mod.requests, "post", fake_post)
    ev = judge_mod.AutomatedEvaluator(judge_model="m", base_url="http://x")
    scores = ev.grade_run("candidate output", str(rubric))
    assert scores == {"a": 4}
    assert len(calls) == 2
    # Retry must append the strict-format repair instruction.
    assert "not valid JSON" in calls[1]["messages"][-1]["content"]


def test_judge_neutral_after_retry_exhausted(tmp_path, monkeypatch):
    from core import judge as judge_mod

    rubric = tmp_path / "r.json"
    rubric.write_text(json.dumps({"metrics": {"a": "desc"}, "instructions": ""}))

    monkeypatch.setattr(
        judge_mod.requests,
        "post",
        lambda *a, **k: _FakeResponse("still not json"),
    )
    ev = judge_mod.AutomatedEvaluator(judge_model="m", base_url="http://x")
    scores = ev.grade_run("candidate output", str(rubric))
    assert scores["a"] == 3
    assert "_judge_parse_error" in scores


def test_judge_unwraps_nested_rubric_criteria(tmp_path, monkeypatch):
    """Rubrics with {weight, criteria} metric objects must render criteria."""
    from core import judge as judge_mod

    rubric = tmp_path / "r.json"
    rubric.write_text(
        json.dumps(
            {
                "instructions": "instr",
                "metrics": {
                    "evidence_quality": {
                        "weight": 30,
                        "criteria": "cite specific files",
                    }
                },
            }
        )
    )
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["prompt"] = json["messages"][-1]["content"]
        return _FakeResponse('{"evidence_quality": 5}')

    monkeypatch.setattr(judge_mod.requests, "post", fake_post)
    ev = judge_mod.AutomatedEvaluator(judge_model="m", base_url="http://x")
    scores = ev.grade_run("out", str(rubric))
    assert scores == {"evidence_quality": 5}
    # Criteria text reaches the judge; the weight is surfaced, not a dict repr.
    assert "cite specific files" in captured["prompt"]
    assert "weight 30" in captured["prompt"]
    assert "'weight'" not in captured["prompt"]
