"""Dependency-free BM25 code retriever (lightweight RAG for local Ollama).

The evaluation harnesses parse a structural topography (models, serializers,
views) but deliberately do NOT capture method bodies — so the LLM cannot see
how ``get_queryset``, ``authorize_*``, or service functions actually enforce
authorization. This module fills that gap with lexical retrieval: given a
natural-language query, it returns the most relevant source chunks (function
and class bodies) from the target repo.

Why BM25 and not embeddings?
  - Zero dependencies (no chromadb / numpy / pulled embedding model required).
  - Works fully offline against local Ollama — matches the project philosophy.
  - For code, lexical/identifier matching is strong (queries and source share
    tokens like ``authorize_superuser``, ``get_queryset``).

Public API:
    retrieve_relevant_code(query, repo_path, top_k, max_chars) -> str
        Returns a formatted markdown block of the top-k matching chunks, or "".
    index_codebase(repo_path) -> list[CodeChunk]
        Builds (and caches) the chunk index for a repo.
"""

import ast
import hashlib
import math
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from core.cache import get as cache_get
from core.cache import get_git_head, make_key
from core.cache import set as cache_set

# Reuse the parser's directory exclusions so the retriever never indexes
# migrations, venvs, fixtures, or generated caches.
from core.parser import _walk_py_files

# BM25 parameters (Robertson/Sparck-Jones defaults).
_K1 = 1.5
_B = 0.75

# Per-chunk source cap. A pathological 2000-line function should not dominate
# the index or the returned context.
_MAX_CHUNK_CHARS = 4000

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

# In-process index cache: keyed by (repo, HEAD, working-tree signature),
# holding native CodeChunk objects. The shared disk cache serializes to JSON
# (see core/cache.py, json.dumps default=str), so dataclasses must be
# round-tripped through plain dicts — otherwise a cold process reads back a
# list of strings. This layer keeps object identity + avoids re-tokenizing
# on every call within the same process.
_index_memory: dict[str, tuple[float, list["CodeChunk"]]] = {}
_index_ttl = 86400.0


@dataclass
class CodeChunk:
    file: str
    name: str  # qualified: "ClassName" or "ClassName.method" or "func"
    kind: str  # "class" | "function" | "module"
    line_start: int
    line_end: int
    source: str
    tokens: list[str] = field(default_factory=list)


# ── Tokenization ──────────────────────────────────────────────────────────────


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase search tokens, expanding identifier cases.

    ``get_queryset`` -> ["get", "queryset"]; ``HTTPBearer`` -> ["http",
    "bearer"]; ``authorize_superuser`` -> ["authorize", "superuser"]. Two
    regex passes split lowercase->uppercase and acronym->Camel boundaries so
    acronym-prefixed names (HTTP, URL, API) match their words.
    """
    # Split "HTTPBearer" -> "HTTP Bearer" (acronym run before CamelCase start)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    # Split "getQuery" -> "get Query" (lowercase/digit -> uppercase boundary)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    expanded = text.lower()
    raw = _TOKEN_SPLIT.split(expanded)
    out = []
    for tok in raw:
        if len(tok) < 2:
            continue
        out.append(tok)
    return out


# ── Indexing ──────────────────────────────────────────────────────────────────


def _working_tree_signature(repo_path: str) -> str:
    """Hash (relpath, size, mtime) for every indexed .py file.

    The parser caches by git HEAD only, which hides uncommitted working-tree
    edits. The retriever keys on HEAD + this signature so a freshly edited
    ``authorize_*`` function invalidates the index and is re-parsed.
    """
    parts: list[str] = []
    for root, rel in _walk_py_files(repo_path):
        full = os.path.join(root, rel)
        try:
            st = os.stat(full)
        except OSError:
            continue
        parts.append(f"{rel}:{st.st_size}:{int(st.st_mtime_ns)}")
    if not parts:
        return "empty"
    parts.sort()
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _index_cache_key(repo_path: str, head: str | None, sig: str) -> str:
    return make_key("retrieval:index", repo_path, head or "nohead", sig)


def _chunk_source(source: str) -> str:
    if len(source) <= _MAX_CHUNK_CHARS:
        return source
    return source[:_MAX_CHUNK_CHARS] + "\n# ... [chunk truncated]"


def _chunk_to_dict(c: CodeChunk) -> dict:
    """JSON-safe form for the disk cache (tokens are recomputed on load)."""
    return {
        "file": c.file,
        "name": c.name,
        "kind": c.kind,
        "line_start": c.line_start,
        "line_end": c.line_end,
        "source": c.source,
    }


def _chunk_from_dict(d: object) -> CodeChunk | None:
    """Rebuild a CodeChunk from its cached dict, tolerating stale formats.

    The cache previously persisted dataclasses via ``json.dumps(default=str)``,
    so old on-disk entries are lists of plain strings — those must be rejected
    and rebuilt rather than fed to the BM25 scorer.
    """
    if not isinstance(d, dict):
        return None
    try:
        chunk = CodeChunk(
            file=str(d.get("file", "")),
            name=str(d.get("name", "")),
            kind=str(d.get("kind", "")),
            line_start=int(d.get("line_start", 1)),
            line_end=int(d.get("line_end", 1)),
            source=str(d.get("source", "")),
        )
    except (TypeError, ValueError):
        return None
    chunk.tokens = _tokenize(chunk.source)
    return chunk


def _chunks_from_file(repo_path: str, rel: str) -> list[CodeChunk]:
    full = os.path.join(repo_path, rel)
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError:
        return []

    lines = source.splitlines()
    chunks: list[CodeChunk] = []

    def _make(name: str, kind: str, node: ast.AST) -> None:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start) or start
        snippet = "\n".join(lines[start - 1 : end])
        chunk_src = _chunk_source(snippet)
        chunks.append(
            CodeChunk(
                file=rel,
                name=name,
                kind=kind,
                line_start=start,
                line_end=end,
                source=chunk_src,
                tokens=_tokenize(chunk_src),
            )
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _make(node.name, "function", node)
        elif isinstance(node, ast.ClassDef):
            _make(node.name, "class", node)
            # Method-level chunks for fine-grained retrieval (e.g. get_queryset).
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _make(f"{node.name}.{item.name}", "method", item)
    return chunks


def index_codebase(repo_path: str) -> list[CodeChunk]:
    """Build (and cache) the chunk index for ``repo_path``.

    Cached per (repo, git HEAD, working-tree signature) for one day. Returns
    an empty list if the repo cannot be read.
    """
    if not os.path.isdir(repo_path):
        return []
    head = get_git_head(repo_path)
    sig = _working_tree_signature(repo_path)
    key = _index_cache_key(repo_path, head, sig)

    now = time.time()
    memo = _index_memory.get(key)
    if memo and now - memo[0] < _index_ttl:
        return memo[1]

    cached = cache_get(key, max_age=_index_ttl)
    if cached is not None:
        try:
            restored = [_chunk_from_dict(d) for d in cached]  # type: ignore[attr-defined]
        except TypeError:
            restored = []
        if all(c is not None for c in restored):
            chunks = [c for c in restored if c is not None]
            _index_memory[key] = (now, chunks)
            return chunks

    chunks = []
    for _root, rel in _walk_py_files(repo_path):
        chunks.extend(_chunks_from_file(repo_path, rel))

    cache_set(key, [_chunk_to_dict(c) for c in chunks])
    _index_memory[key] = (now, chunks)
    return chunks


# ── BM25 scoring ──────────────────────────────────────────────────────────────


class _BM25Index:
    """In-memory BM25 scorer over a set of chunks."""

    def __init__(self, chunks: Sequence[CodeChunk]):
        self._chunks = list(chunks)
        self._doc_len = [len(c.tokens) for c in self._chunks]
        self._avgdl = sum(self._doc_len) / len(self._doc_len) if self._doc_len else 0.0
        self._n = len(self._chunks)
        # document frequency per term
        df: dict[str, int] = {}
        for c in self._chunks:
            for term in set(c.tokens):
                df[term] = df.get(term, 0) + 1
        self._df = df

    def _idf(self, term: str) -> float:
        n_t = self._df.get(term, 0)
        if n_t == 0:
            return 0.0
        return math.log(1 + (self._n - n_t + 0.5) / (n_t + 0.5))

    def score(self, query_tokens: Sequence[str]) -> list[tuple[float, int]]:
        """Return [(score, chunk_index)] sorted descending."""
        if not self._chunks or not query_tokens:
            return []
        # Term frequency per chunk, only for query terms.
        q_terms = set(query_tokens)
        scores: list[float] = [0.0] * self._n
        for i, c in enumerate(self._chunks):
            tf: dict[str, int] = {}
            for tok in c.tokens:
                if tok in q_terms:
                    tf[tok] = tf.get(tok, 0) + 1
            if not tf:
                continue
            dl = self._doc_len[i] or 1
            norm = 1 - _B + _B * (dl / self._avgdl) if self._avgdl else 1.0
            s = 0.0
            for term, f in tf.items():
                idf = self._idf(term)
                if idf <= 0:
                    continue
                s += idf * (f * (_K1 + 1)) / (f + _K1 * norm)
            scores[i] = s
        ranked = sorted(((s, i) for i, s in enumerate(scores) if s > 0), reverse=True)
        return ranked


# ── Deduplication ─────────────────────────────────────────────────────────────


def _contained(a: CodeChunk, b: CodeChunk) -> bool:
    """True if chunk a's line range is contained within chunk b's (same file)."""
    if a.file != b.file:
        return False
    return b.line_start <= a.line_start and a.line_end <= b.line_end


def _dedupe(ranked: list[tuple[float, int]], chunks: Sequence[CodeChunk]) -> list[int]:
    """Drop a chunk whose span is contained in an already-selected chunk.

    Prevents returning both a class body and one of its methods for the same
    hit — keeps results non-redundant while preferring the higher-scoring span.
    """
    selected: list[int] = []
    for _, idx in ranked:
        cand = chunks[idx]
        if any(_contained(cand, chunks[s]) for s in selected):
            continue
        # Also skip if cand wholly contains an already-selected finer chunk
        # only when scores are near-equal (prefer specificity). Keep simple:
        # only drop contained (finer into coarser), never the reverse, so we
        # never lose a specific method hit.
        selected.append(idx)
    return selected


# ── Public retrieval ──────────────────────────────────────────────────────────


def retrieve_relevant_code(
    query: str, repo_path: str, top_k: int = 8, max_chars: int = 10000
) -> str:
    """Return a formatted block of the top-k code chunks matching ``query``.

    Returns "" if the repo can't be indexed or nothing matches. The block is
    suitable for direct injection into an LLM prompt.
    """
    if not query or not repo_path or not os.path.isdir(repo_path):
        return ""
    chunks = index_codebase(repo_path)
    if not chunks:
        return ""

    bm25 = _BM25Index(chunks)
    ranked = bm25.score(_tokenize(query))
    if not ranked:
        return ""

    selected = _dedupe(ranked, chunks)[:top_k]
    return _format_chunks([chunks[i] for i in selected], max_chars)


def _format_chunks(selected: list[CodeChunk], max_chars: int) -> str:
    if not selected:
        return ""
    header = "=== Retrieved Code Context (BM25 lexical retrieval, ground truth) ==="
    blocks: list[str] = [header]
    total = len(header)
    for c in selected:
        body = (
            f"\n--- {c.file}:{c.line_start}-{c.line_end} ({c.kind} {c.name}) ---\n"
            f"{c.source}"
        )
        if total + len(body) > max_chars:
            note = "\n... [additional retrieval results truncated]"
            if total + len(note) <= max_chars:
                blocks.append(note)
            break
        blocks.append(body)
        total += len(body)
    return "\n".join(blocks)
