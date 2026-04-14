"""memory_keeper.pending — pending.jsonl read/write/status management.

Public API:
    append_pending(propositions)   — append propositions to pending.jsonl (locked)
    load_pending(status)           — read propositions filtered by status
    update_status(id, status)      — mark a proposition approved/rejected
    collect_approved()             — return approved propositions, mark as consumed
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import fcntl as _fcntl  # Unix only
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # Windows: graceful fallback

import memory_keeper._config as _cfg_mod

if TYPE_CHECKING:
    from memory_keeper.proposition import Proposition

__all__ = ["append_pending", "load_pending", "update_status", "collect_approved"]

# Lazy resolution so _config is fully loaded first
def _pending_path() -> Path:
    return _cfg_mod.PENDING_STORE


# ── File-locked JSONL append ──────────────────────────────────────────────────

def _locked_write_lines(path: Path, lines: list[str]) -> None:
    """Append lines to path with exclusive file lock (fcntl on Unix, fallback on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        if _HAS_FCNTL:
            try:
                _fcntl.flock(fh, _fcntl.LOCK_EX)
            except OSError:
                pass
        for line in lines:
            fh.write(line + "\n")
        if _HAS_FCNTL:
            try:
                _fcntl.flock(fh, _fcntl.LOCK_UN)
            except OSError:
                pass


def _rewrite_locked(path: Path, records: list[dict]) -> None:
    """Atomically rewrite entire pending.jsonl (for status updates)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


# ── Public API ────────────────────────────────────────────────────────────────

def append_pending(propositions: "list[Proposition]") -> None:
    """Append propositions to pending.jsonl.

    Deduplicates by content before writing — won't add if identical content exists.
    Read + write are inside the same lock to prevent concurrent double-write.
    """
    if not propositions:
        return

    path = _pending_path()

    if _cfg_mod.DRY_RUN:
        print(f"[dry-run] would append {len(propositions)} proposition(s) to {path}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    # Open in r+ mode (or create) with lock held for both read and append
    with open(path, "a+", encoding="utf-8") as fh:
        if _HAS_FCNTL:
            try:
                _fcntl.flock(fh, _fcntl.LOCK_EX)
            except OSError:
                pass

        # Read existing contents while holding lock
        fh.seek(0)
        existing_contents: set[str] = set()
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                existing_contents.add(rec.get("content", "").strip())
            except json.JSONDecodeError:
                pass

        # Append new (deduped) at end
        for prop in propositions:
            if prop.content.strip() in existing_contents:
                continue
            fh.write(json.dumps(prop.to_dict(), ensure_ascii=False) + "\n")
            existing_contents.add(prop.content.strip())

        if _HAS_FCNTL:
            try:
                _fcntl.flock(fh, _fcntl.LOCK_UN)
            except OSError:
                pass


def load_pending(status: str | None = None) -> "list[Proposition]":
    """Read propositions from pending.jsonl, optionally filtered by status."""
    from memory_keeper.proposition import Proposition

    path = _pending_path()
    if not path.exists():
        return []

    results: list[Proposition] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            prop = Proposition.from_dict(rec)
            if status is None or prop.status == status:
                results.append(prop)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    return results


def update_status(prop_id: str, new_status: str) -> bool:
    """Update status of a proposition by id. Returns True if found."""
    path = _pending_path()
    if not path.exists():
        return False

    records: list[dict] = []
    found = False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("id") == prop_id:
                rec["status"] = new_status
                found = True
            records.append(rec)
        except json.JSONDecodeError:
            pass

    if found and not _cfg_mod.DRY_RUN:
        _rewrite_locked(path, records)

    return found


def collect_approved() -> "list[Proposition]":
    """Return all approved propositions and mark them as consumed."""
    from memory_keeper.proposition import Proposition

    path = _pending_path()
    if not path.exists():
        return []

    approved: list[Proposition] = []
    records: list[dict] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("status") in ("approved", "auto_approved"):
                approved.append(Proposition.from_dict(rec))
                rec["status"] = "consumed"
            records.append(rec)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    if approved and not _cfg_mod.DRY_RUN:
        _rewrite_locked(path, records)

    return approved
