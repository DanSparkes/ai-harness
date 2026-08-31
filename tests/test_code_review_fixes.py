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

from code_review import (
    CHUNK_PASS_TIMEOUT_S,
    CHUNK_TARGET_CHARS,
    _allocate_context_budget,
    _clip,
    _domain_key,
    _is_noise_file,
    _parse_diff_hunks,
    _read_capped,
    group_diff_chunks,
    merge_chunk_reviews,
    preprocess_diff,
)
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


# ── Large-diff chunking + noise summarization ────────────────────────────────


_SAMPLE_DIFF = """diff --git a/memores/models.py b/memores/models.py
index aaa..bbb 100644
--- a/memores/models.py
+++ b/memores/models.py
@@ -1,3 +1,4 @@
+class NewModel(models.Model):
+    name = models.CharField(max_length=100)
-    old_thing
diff --git a/memores/serializers/user_serializers.py b/memores/serializers/user_serializers.py
index ccc..ddd 100644
--- a/memores/serializers/user_serializers.py
+++ b/memores/serializers/user_serializers.py
@@ -1,3 +1,4 @@
+class UserSerializer(serializers.Serializer):
+    id = serializers.IntegerField()
diff --git a/memores/migrations/0002_something.py b/memores/migrations/0002_something.py
index eee..fff 100644
--- a/memores/migrations/0002_something.py
+++ b/memores/migrations/0002_something.py
@@ -1,3 +1,5 @@
+import django.db.models.deletion
+migrations.AddField(model_name='x', name='y')
+class Migration(migrations.Migration):
"""


def test_is_noise_file_classifies_auto_generated_paths() -> None:
    assert _is_noise_file("memores/migrations/0001_initial.py")
    assert _is_noise_file("uv.lock")
    assert _is_noise_file("package-lock.json")
    assert _is_noise_file("static/app.min.js")
    assert _is_noise_file("memores/fixtures/some.json")
    assert not _is_noise_file("memores/models.py")
    assert not _is_noise_file("memores/views/user_views.py")


def test_parse_diff_hunks_splits_by_file() -> None:
    hunks = _parse_diff_hunks(_SAMPLE_DIFF)
    assert len(hunks) == 3
    paths = [h["path"] for h in hunks]
    assert paths == [
        "memores/models.py",
        "memores/serializers/user_serializers.py",
        "memores/migrations/0002_something.py",
    ]
    assert all(h["body"] for h in hunks)


def test_preprocess_diff_summarizes_noise_keeps_source() -> None:
    cleaned, noise = preprocess_diff(_SAMPLE_DIFF)
    # Exactly the migration is summarized away.
    assert len(noise) == 1
    assert noise[0]["path"] == "memores/migrations/0002_something.py"
    assert "0002_something" not in cleaned
    # Reviewable source hunks survive intact.
    assert "class NewModel(models.Model):" in cleaned
    assert "class UserSerializer(serializers.Serializer):" in cleaned


def test_group_diff_chunks_preserves_all_user_files() -> None:
    cleaned, _ = preprocess_diff(_SAMPLE_DIFF)
    chunks = group_diff_chunks(cleaned)
    all_files = [f for c in chunks for f in c["files"]]
    assert "memores/models.py" in all_files
    assert "memores/serializers/user_serializers.py" in all_files
    # Noise file must never appear in a review chunk.
    assert all("migrations" not in f for f in all_files)
    assert all(c["body"] for c in chunks)


def test_group_diff_chunks_returns_noise_only_fallback() -> None:
    # A diff that is entirely noise still yields one (empty) chunk so the
    # pipeline doesn't crash downstream.
    chunks = group_diff_chunks("")
    assert len(chunks) == 1
    assert chunks[0]["key"] == "noise-only"


def test_merge_chunk_reviews_single_and_multi() -> None:
    single = merge_chunk_reviews(
        [{"key": "a", "files": ["f1"], "output": "# only\n"}]
    )
    assert single == "# only\n"

    multi = merge_chunk_reviews(
        [
            {"key": "a", "files": ["f1"], "output": "# rev A\n"},
            {"key": "b", "files": ["f2"], "output": "# rev B\n"},
        ]
    )
    assert "## Chunk: a" in multi
    assert "## Chunk: b" in multi
    assert "Files reviewed: f1" in multi

    # Single-chunk passthrough returns the raw (unstripped) output verbatim.
    empty = merge_chunk_reviews(
        [{"key": "a", "files": ["f1"], "output": "   \n"}]
    )
    assert empty == "   \n"


# ── Chunking refinement (12K cap, deterministic domain key) ────────────────


def test_chunk_target_chars_default_is_bounded() -> None:
    """Default chunk size must be small enough that a chunk finishes in
    minutes, not hours. The previous 30K default produced 48-min chunks on
    large test files; 12K keeps each pass bounded.
    """
    assert CHUNK_TARGET_CHARS <= 15000, CHUNK_TARGET_CHARS
    assert CHUNK_TARGET_CHARS >= 4000, CHUNK_TARGET_CHARS


def test_chunk_pass_timeout_is_set() -> None:
    """A per-chunk wall-clock ceiling must exist; otherwise one stuck pass
    can silently burn hours via the runner's 5x exponential-backoff retries.
    """
    assert 60 <= CHUNK_PASS_TIMEOUT_S <= 1800, CHUNK_PASS_TIMEOUT_S


def test_domain_key_uses_explicit_marker() -> None:
    assert _domain_key("memores/views/user_views.py") == "memores/views"
    assert _domain_key("memores/serializers/payment.py") == "memores/serializers"
    assert _domain_key("memores/models/foo.py") == "memores/models"
    assert _domain_key("memores/tests/test_x.py") == "memores/tests"
    assert _domain_key("memores/migrations/0001_initial.py") == "memores/migrations"


def test_domain_key_does_not_collapse_unrelated_subdirs() -> None:
    """Regression for the bug that produced giant 'memores' chunks.

    When a path has no explicit domain marker (views/serializers/etc.), the
    key must include the subdirectory so memores/auth does NOT collide with
    memores/views or memores/billing.
    """
    auth_key = _domain_key("memores/auth/oauth.py")
    billing_key = _domain_key("memores/billing/webhooks.py")
    payments_key = _domain_key("memores/payments/charge.py")
    assert auth_key != billing_key != payments_key
    assert auth_key == "memores/auth"
    assert billing_key == "memores/billing"
    assert payments_key == "memores/payments"


def test_domain_key_handles_root_level_files() -> None:
    # Single-segment paths (rare, e.g. a top-level helper) key on themselves
    # so they remain distinguishable rather than all collapsing to "".
    assert _domain_key("utils.py") == "utils.py"
    assert _domain_key("README.md") == "README.md"


def test_group_diff_chunks_respects_smaller_cap() -> None:
    """A diff with multiple sizable files in the SAME bucket must split
    under the new (smaller) cap. The previous 30K cap let these pile up.
    """
    body = "\n".join(f"+line{i}" for i in range(300))  # ~2K per file

    def hunk(path: str) -> str:
        return (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1,300 @@\n{body}"
        )

    # 4 files in memores/views, each ~2K, total ~8K — fits in one 12K chunk.
    diff = "\n".join(
        hunk(f"memores/views/f{i}.py") for i in range(4)
    )
    chunks = group_diff_chunks(diff)
    assert len(chunks) == 1
    assert all(c["size"] <= CHUNK_TARGET_CHARS for c in chunks)


def test_group_diff_chunks_splits_bucket_over_cap() -> None:
    """Same bucket, files totaling > cap → multiple chunks under that key."""
    body = "\n".join(f"+line{i}" for i in range(500))  # ~3K per file

    def hunk(path: str) -> str:
        return (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1,500 @@\n{body}"
        )

    # 6 files x ~3K = ~18K -> must split into 2 chunks at the 12K cap.
    diff = "\n".join(
        hunk(f"memores/views/f{i}.py") for i in range(6)
    )
    chunks = group_diff_chunks(diff)
    assert len(chunks) >= 2
    assert all(c["size"] <= CHUNK_TARGET_CHARS * 1.5 for c in chunks)


def test_group_diff_chunks_keeps_unrelated_subdirs_separate() -> None:
    """End-to-end: the fix must surface in real chunking, not just _domain_key."""
    body = "\n".join(f"+line{i}" for i in range(100))  # ~700 chars

    def hunk(path: str) -> str:
        return (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1,100 @@\n{body}"
        )

    diff = "\n".join(
        [
            hunk("memores/views/user.py"),
            hunk("memores/auth/oauth.py"),
            hunk("memores/billing/webhooks.py"),
        ]
    )
    chunks = group_diff_chunks(diff)
    keys = [c["key"] for c in chunks]
    assert "memores/views" in keys
    assert "memores/auth" in keys
    assert "memores/billing" in keys
