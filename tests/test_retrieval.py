"""Tests for core.retrieval: BM25 indexing, tokenization, ranking, dedup,
and formatting. Network-free; operates on a temp repo."""

import os

import pytest

from core.retrieval import (
    CodeChunk,
    _BM25Index,
    _tokenize,
    index_codebase,
    retrieve_relevant_code,
)

# ── tokenization ──────────────────────────────────────────────────────────────


def test_tokenize_splits_snake_case() -> None:
    assert _tokenize("get_queryset") == ["get", "queryset"]


def test_tokenize_splits_camel_case() -> None:
    toks = _tokenize("HTTPBearerAuth")
    assert "http" in toks and "bearer" in toks and "auth" in toks


def test_tokenize_drops_short_tokens() -> None:
    toks = _tokenize("a b c def")
    assert "def" in toks
    assert "a" not in toks


# ── indexing ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def tiny_repo(tmp_path) -> str:
    """A minimal repo with an auth helper and an unrelated module."""
    (tmp_path / "auth_helper.py").write_text(
        "def authorize_superuser(user, resource):\n"
        "    '''Return True only if user is a superuser.'''\n"
        "    return user.is_superuser and resource.owner_id == user.id\n"
        "\n"
        "class ProfileMixin:\n"
        "    def get_queryset(self, request):\n"
        "        return self.model.objects.filter(owner=request.user)\n"
    )
    (tmp_path / "unrelated.py").write_text(
        "def format_currency(amount):\n" "    return f'${amount:.2f}'\n"
    )
    return str(tmp_path)


def test_index_creates_chunks(tiny_repo: str) -> None:
    chunks = index_codebase(tiny_repo)
    names = {c.name for c in chunks}
    assert "authorize_superuser" in names
    assert "ProfileMixin" in names
    assert "ProfileMixin.get_queryset" in names
    assert "format_currency" in names


def test_index_caches(tiny_repo: str) -> None:
    a = index_codebase(tiny_repo)
    b = index_codebase(tiny_repo)
    assert a is b  # cached object identity (same list returned from cache)


def test_index_disk_round_trip_restores_chunks(tiny_repo: str) -> None:
    """A fresh process reads the disk cache (dicts), not in-memory objects."""
    import core.cache as cache_mod
    from core import retrieval as ret

    index_codebase(tiny_repo)  # builds + persists JSON-safe dicts
    # Simulate a cold process: drop in-process caches so the disk is consulted.
    cache_mod._local_cache.clear()
    ret._index_memory.clear()

    chunks = index_codebase(tiny_repo)
    assert chunks
    assert isinstance(chunks[0], ret.CodeChunk)
    assert chunks[0].tokens  # tokens recomputed from source, not serialized
    assert chunks[0].name


def test_index_rebuilds_stale_string_cache(tiny_repo: str, tmp_path) -> None:
    """Old buggy cache entries persisted dataclasses as plain strings via
    json.dumps(default=str). Those must be rejected and re-indexed."""
    import json
    import time

    import core.cache as cache_mod
    from core import retrieval as ret

    index_codebase(tiny_repo)
    key = ret._index_cache_key(
        tiny_repo,
        cache_mod.get_git_head(tiny_repo),
        ret._working_tree_signature(tiny_repo),
    )
    path = cache_mod._cache_path(key)
    stale = {
        "_ts": time.time(),
        "_key": key,
        "_data": ["CodeChunk(file='auth_helper.py')"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stale))
    cache_mod._local_cache.clear()
    ret._index_memory.clear()

    chunks = index_codebase(tiny_repo)
    assert chunks
    assert isinstance(chunks[0], ret.CodeChunk)
    assert "authorize_superuser" in {c.name for c in chunks}


def test_index_invalidates_on_edit(tiny_repo: str) -> None:
    a = index_codebase(tiny_repo)
    # Edit a file -> mtime changes -> signature changes -> re-index
    os.utime(os.path.join(tiny_repo, "auth_helper.py"), None)
    # Force mtime to differ meaningfully
    import time

    time.sleep(0.01)
    with open(os.path.join(tiny_repo, "auth_helper.py"), "a") as f:
        f.write("\n# edited\n")
    b = index_codebase(tiny_repo)
    assert a is not b  # different object after content change


# ── retrieval ─────────────────────────────────────────────────────────────────


def test_retrieve_ranks_relevant_chunk_first(tiny_repo: str) -> None:
    block = retrieve_relevant_code(
        "authorization superuser permission check", tiny_repo, top_k=3
    )
    assert block  # non-empty
    # The authorize_superuser chunk must be present (it is the relevant hit).
    assert "authorize_superuser" in block
    # If the unrelated currency formatter happens to also be returned, the
    # auth chunk must rank ahead of it.
    if "format_currency" in block:
        assert block.index("authorize_superuser") < block.index("format_currency")


def test_retrieve_returns_empty_for_nothing(tmp_path) -> None:
    assert retrieve_relevant_code("zzz_nomatch_xyz", str(tmp_path), top_k=3) == ""


def test_retrieve_returns_empty_for_missing_repo(tmp_path) -> None:
    missing = str(tmp_path / "does_not_exist")
    assert retrieve_relevant_code("query", missing) == ""


def test_retrieve_empty_query_returns_empty(tiny_repo: str) -> None:
    assert retrieve_relevant_code("", tiny_repo) == ""


def test_retrieve_respects_max_chars(tiny_repo: str) -> None:
    block = retrieve_relevant_code("authorization", tiny_repo, top_k=10, max_chars=100)
    assert len(block) <= 200  # header + truncation note stays near cap


# ── dedup ─────────────────────────────────────────────────────────────────────


def test_dedup_drops_method_contained_in_class() -> None:
    chunks = [
        CodeChunk(
            file="a.py",
            name="Foo",
            kind="class",
            line_start=1,
            line_end=20,
            source="x",
            tokens=["x"],
        ),
        CodeChunk(
            file="a.py",
            name="Foo.bar",
            kind="method",
            line_start=5,
            line_end=8,
            source="x",
            tokens=["x"],
        ),
        CodeChunk(
            file="b.py",
            name="baz",
            kind="function",
            line_start=1,
            line_end=3,
            source="y",
            tokens=["y"],
        ),
    ]
    # Equal scores so order is stable: class(0), method(1), func(2)
    ranked = [(1.0, 0), (1.0, 1), (1.0, 2)]
    from core.retrieval import _dedupe

    selected = _dedupe(ranked, chunks)
    assert 0 in selected  # class kept
    assert 1 not in selected  # method (contained in class) dropped
    assert 2 in selected  # separate file kept


def test_bm25_empty_chunks_returns_no_scores() -> None:
    assert _BM25Index([]).score(["anything"]) == []


def test_bm25_no_query_terms_returns_no_scores(tiny_repo: str) -> None:
    chunks = index_codebase(tiny_repo)
    assert _BM25Index(chunks).score([]) == []
