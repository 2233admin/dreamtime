"""memory_keeper._utils — LLM client, atomic I/O, locks, cursor, and file helpers.

Public API:
    _get_client()          — return (cached) OpenAI-compatible client
    llm(prompt, ...)       — call LLM with retry; raises RuntimeError on all-fail
    llm_json(prompt, ...)  — call LLM and parse first JSON object from response
    _Lock                  — context-manager single-instance file lock
    _atomic_write(path, text) — write via .tmp then rename; no-op in dry-run
    _SeenHashes            — SHA-256 deduplication set with JSON persistence
    _memu_sync(items, ...) — fire-and-forget POST to local memU HTTP server
    load_cursor()          — read last-run timestamp from cursor file
    save_cursor(dt)        — persist last-run timestamp
    _recently_active_jsonl() — JSONL paths modified within N minutes
    _mtime(p)              — safe stat().st_mtime as datetime
    _git_last_commit(path) — last git commit timestamp for a repo
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import threading

from memory_keeper._config import (
    API_BASE,
    API_KEY,
    CURSOR_FILE,
    HOME,
    LOCK_FILE,
    MODEL,
    OMC_DIR,
    SESSION_GLOB,
    TODAY,
    DRY_RUN,
    HOT_MINUTES,
    _behav,
)

__all__ = [
    "_get_client",
    "llm",
    "llm_json",
    "_Lock",
    "_atomic_write",
    "_SeenHashes",
    "_memu_sync",
    "load_cursor",
    "save_cursor",
    "_recently_active_jsonl",
    "_mtime",
    "_git_last_commit",
]

# Module-level cached client; reset to None after _reload_config
_client: object | None = None


def _get_client() -> object:
    """Return the cached OpenAI-compatible client, creating it if needed.

    Re-reads API_KEY and API_BASE from config globals at call time so that
    _reload_config() takes effect without restarting the process.
    """
    import memory_keeper._config as _cfg_mod
    global _client
    if _client is None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "openai package not installed. Run: py -3.11 -m pip install openai"
            ) from exc
        key = _cfg_mod.API_KEY
        base = _cfg_mod.API_BASE
        if not key:
            raise RuntimeError(
                "API key not set. Export MK_API_KEY (or ARK_KEY) env var.\n"
                "  Windows: set MK_API_KEY=your-key in run-memory-keeper.bat\n"
                "  Linux/Mac: export MK_API_KEY=your-key"
            )
        _client = OpenAI(base_url=base, api_key=key)
    return _client


# ── LLM helpers ────────────────────────────────────────────────────────────────

def llm(prompt: str, max_tokens: int = 512, retries: int = 2) -> str:
    """Call the configured LLM with exponential backoff retry.

    Distinguishes rate-limit (429) errors and network timeouts from other
    failures so that each category gets appropriate back-off treatment.

    Args:
        prompt:     User message to send.
        max_tokens: Upper bound on response tokens.
        retries:    Number of additional attempts after the first failure.

    Returns:
        Stripped text content from the first choice.

    Raises:
        RuntimeError: All attempts exhausted.
    """
    import memory_keeper._config as _cfg_mod
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _get_client().chat.completions.create(
                model=_cfg_mod.MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.1,
                timeout=90,
            )
            return r.choices[0].message.content.strip()
        except Exception as e:
            last_exc = e
            if attempt >= retries:
                break
            err_str = str(e).lower()
            # Rate-limit: wait longer; timeout/network: shorter wait
            if "429" in err_str or "rate" in err_str:
                wait = 30 * (attempt + 1)
            elif "timeout" in err_str or "connection" in err_str:
                wait = 10 * (attempt + 1)
            else:
                wait = 5 * (attempt + 1)
            print(f"  [llm] attempt {attempt+1} failed ({e}), retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"llm failed after {retries+1} attempts") from last_exc


def llm_json(prompt: str, max_tokens: int = 800) -> dict | list | None:
    """Call LLM and parse the first JSON object or array from the response.

    Args:
        prompt:     User message to send.
        max_tokens: Upper bound on response tokens.

    Returns:
        Parsed dict or list, or None if no valid JSON found.
    """
    raw = llm(prompt, max_tokens)
    # Find first '{' or '[' — whichever comes first
    obj_start = raw.find('{')
    arr_start = raw.find('[')
    if obj_start == -1 and arr_start == -1:
        return None
    # Pick the earlier one; -1 means not found so treat as infinity
    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        start = arr_start
    else:
        start = obj_start
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw, start)
        if isinstance(obj, (dict, list)):
            return obj
    except Exception:
        pass
    return None


# ── Single-instance lock ───────────────────────────────────────────────────────

class _Lock:
    """File-based single-instance lock.

    Creates LOCK_FILE on enter, removes it on exit.
    Raises SystemExit if a non-stale lock (< 1 h) already exists.
    """

    def __enter__(self) -> "_Lock":
        import memory_keeper._config as _cfg_mod
        lock = _cfg_mod.LOCK_FILE
        omc = _cfg_mod.OMC_DIR
        omc.mkdir(parents=True, exist_ok=True)
        if lock.exists():
            age = time.time() - lock.stat().st_mtime
            if age < 3600:
                raise SystemExit(f"already running (lock age {age:.0f}s), exiting")
            lock.unlink()  # stale lock
        lock.write_text(str(os.getpid()))
        return self

    def __exit__(self, *_: object) -> None:
        import memory_keeper._config as _cfg_mod
        _cfg_mod.LOCK_FILE.unlink(missing_ok=True)


# ── Atomic write helper ────────────────────────────────────────────────────────

def _atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write to .tmp then rename for crash-safe updates.

    In dry-run mode prints a preview without touching the filesystem.
    """
    import memory_keeper._config as _cfg_mod
    if _cfg_mod.DRY_RUN:
        preview = text[:120].replace("\n", "↵")
        print(f"  [dry-run] would write {path} ({len(text)} chars): {preview}…")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    try:
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ── Seen-hashes deduplication ─────────────────────────────────────────────────

class _SeenHashes:
    """Persistent SHA-256-based deduplication set backed by a JSON file.

    Uses the first 16 hex chars of SHA-256(lower(text)) as the hash key,
    which gives negligible collision probability for typical memory entries.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path.home() / ".omc" / "memory-keeper-seen.json")
        self.hashes: set[str] = set()
        self._lock = threading.Lock()
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.hashes = {str(x) for x in data}
                elif isinstance(data, dict):
                    self.hashes = {str(x) for x in data.get("hashes", [])}
        except Exception:
            self.hashes = set()

    def add(self, text: str) -> bool:
        """Add text to the seen set.

        Returns:
            True if text was new (added), False if already seen.
        """
        text = str(text).strip()
        if not text:
            return False
        digest = hashlib.sha256(text.lower().encode()).hexdigest()[:16]
        with self._lock:
            if digest in self.hashes:
                return False
            self.hashes.add(digest)
        return True

    def save(self) -> None:
        """Persist the hash set to disk via atomic write."""
        _atomic_write(self.path, json.dumps(sorted(self.hashes), ensure_ascii=False, indent=2) + "\n")


# ── memU sync ─────────────────────────────────────────────────────────────────

def _memu_sync(items: list[str], item_type: str, today: str) -> None:
    """Fire-and-forget POST of memory items to the local memU HTTP server.

    Args:
        items:     List of text strings to add to memU.
        item_type: Metadata tag, e.g. "decision", "gotcha", "preference".
        today:     ISO date string for the metadata.date field.
    """
    import memory_keeper._config as _cfg_mod
    for item in items:
        body = {
            "messages": [{"role": "user", "content": item}],
            "user_id": _cfg_mod._behav.get("memu_user_id", "default"),
            "metadata": {
                "source": "memory_keeper",
                "date": today,
                "type": item_type,
            },
        }
        try:
            req = urllib.request.Request(
                "http://localhost:8012/add",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as e:
            print(f"  warning: memU sync skipped ({e})")
            continue


# ── Cursor ────────────────────────────────────────────────────────────────────

def load_cursor() -> datetime:
    """Read the last-run timestamp from the cursor JSON file.

    Returns:
        Stored datetime, or (now - 1 day) if the file is missing or corrupt.
    """
    import memory_keeper._config as _cfg_mod
    try:
        cursor = _cfg_mod.CURSOR_FILE
        if cursor.exists():
            return datetime.fromisoformat(json.loads(cursor.read_text())["last_run"])
    except Exception:
        pass
    return datetime.now() - timedelta(days=1)


def save_cursor(dt: datetime) -> None:
    """Persist a last-run timestamp to the cursor JSON file."""
    import memory_keeper._config as _cfg_mod
    _cfg_mod.OMC_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(_cfg_mod.CURSOR_FILE, json.dumps({"last_run": dt.isoformat()}))


# ── Session collision guard ────────────────────────────────────────────────────

def _recently_active_jsonl(within_minutes: int | None = None) -> set[Path]:
    """Return JSONL paths modified within N minutes (avoid reading in-progress sessions).

    Args:
        within_minutes: Window in minutes; defaults to HOT_MINUTES from config.

    Returns:
        Set of Path objects for recently-modified JSONL files.
    """
    import glob as _glob
    import memory_keeper._config as _cfg_mod
    minutes = within_minutes if within_minutes is not None else _cfg_mod.HOT_MINUTES
    cutoff = datetime.now() - timedelta(minutes=minutes)
    hot: set[Path] = set()
    sg = _cfg_mod.SESSION_GLOB.replace("~", str(_cfg_mod.HOME))
    for p in (Path(x) for x in _glob.glob(sg, recursive=True)):
        if "subagents" in str(p):
            continue
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) > cutoff:
                hot.add(p)
        except Exception:
            pass
    return hot


# ── Shared file utils ─────────────────────────────────────────────────────────

def _mtime(p: Path) -> datetime:
    """Return file mtime as datetime; datetime.min on any error."""
    try:
        return datetime.fromtimestamp(p.stat().st_mtime)
    except Exception:
        return datetime.min


def _git_last_commit(path: Path) -> datetime | None:
    """Return the timestamp of the most recent git commit in a repo.

    Args:
        path: Path to the git repository root.

    Returns:
        datetime of last commit, or None if git fails or repo has no commits.
    """
    try:
        ts = subprocess.check_output(
            ["git", "-C", str(path), "log", "-1", "--format=%ct"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
        return datetime.fromtimestamp(int(ts)) if ts else None
    except Exception:
        return None
