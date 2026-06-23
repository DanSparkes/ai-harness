import hashlib
import json
import os
import time
import threading
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "local-harness"

_local_cache: dict[str, dict] = {}
_local_lock = threading.Lock()


def _ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(key: str) -> Path:
    h = hashlib.sha256(key.encode()).hexdigest()
    return CACHE_DIR / f"{h[:2]}" / f"{h}.json"


def make_key(*parts: str) -> str:
    return "::".join(parts)


def get_git_head(repo_path: str) -> str | None:
    try:
        import subprocess
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def get_mtime_key(repo_path: str, *globs: str) -> str:
    import glob as glob_mod
    latest = 0.0
    for g in globs:
        for p in glob_mod.glob(os.path.join(repo_path, g), recursive=True):
            try:
                mtime = os.path.getmtime(p)
                if mtime > latest:
                    latest = mtime
            except OSError:
                pass
    return str(latest)


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


def set(key: str, data: object) -> None:
    entry = {"_ts": time.time(), "_data": data, "_key": key}
    with _local_lock:
        _local_cache[key] = entry
    _ensure_cache_dir()
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w") as f:
            json.dump(entry, f, default=str)
    except Exception:
        pass


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
