import hashlib
import json
import threading
import time
from contextlib import suppress
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "local-harness"

_local_cache: dict[str, dict] = {}
_local_lock = threading.Lock()

_git_head_cache: dict[str, tuple[str, float]] = {}
_git_head_lock = threading.Lock()


def _ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(key: str) -> Path:
    h = hashlib.sha256(key.encode()).hexdigest()
    return CACHE_DIR / f"{h[:2]}" / f"{h}.json"


def make_key(*parts: str) -> str:
    return "::".join(parts)


def get_git_head(repo_path: str) -> str | None:
    with _git_head_lock:
        now = time.time()
        cached = _git_head_cache.get(repo_path)
        if cached and now - cached[1] < 30:
            return cached[0]
    try:
        import subprocess

        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            head = r.stdout.strip()
            with _git_head_lock:
                _git_head_cache[repo_path] = (head, time.time())
            return head
    except Exception:
        pass
    return None


def get(key: str, max_age: float | None = 86400) -> object | None:
    with _local_lock:
        if key in _local_cache:
            entry = _local_cache[key]
            if max_age is None or time.time() - entry["_ts"] < max_age:
                return entry["_data"]
            del _local_cache[key]

    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            entry = json.load(f)
        age = time.time() - entry.get("_ts", 0)
        if max_age is not None and age > max_age:
            return None
        with _local_lock:
            _local_cache[key] = entry
        return entry["_data"]
    except Exception:
        return None


def set(key: str, data: object) -> None:  # pylint: disable=redefined-builtin
    entry = {"_ts": time.time(), "_data": data, "_key": key}
    with _local_lock:
        _local_cache[key] = entry
    _ensure_cache_dir()
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(Exception), open(path, "w") as f:
        f.write(json.dumps(entry, default=str, ensure_ascii=False))


def invalidate(key_prefix: str | None = None) -> None:
    if key_prefix is None:
        with _local_lock:
            _local_cache.clear()
        import shutil

        if CACHE_DIR.exists():
            shutil.rmtree(str(CACHE_DIR))
        return
    with _local_lock:
        to_delete = [k for k in _local_cache if k.startswith(key_prefix)]
        for k in to_delete:
            del _local_cache[k]
    for p in CACHE_DIR.rglob("*.json"):
        try:
            with open(p) as f:
                data = json.load(f)
            stored_key = data.get("_key", "")
            if stored_key.startswith(key_prefix):
                p.unlink()
        except Exception:
            pass
