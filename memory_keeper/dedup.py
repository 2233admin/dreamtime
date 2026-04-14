"""memory_keeper.dedup — Cross-JSONL training corpus deduplication report.

Scans ~/.claude/memory-store/training-data/*.jsonl, finds exact/near-duplicate
records by content hash, and writes a dedup report to Obsidian research/.

Public API:
    run_dedup() -> dict
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

import memory_keeper._config as _cfg_mod
from memory_keeper._utils import _atomic_write

_TRAINING_DIR = Path.home() / ".claude" / "memory-store" / "training-data"
_REPORT_DIR = Path("E:/knowledge/04-Research")
_SPACE_RE = re.compile(r"\s+")
_STRIP_RE = re.compile(r"[\W_]+", re.UNICODE)
_USER_ROLES = {"user", "human"}
_ASST_ROLES = {"assistant", "model", "ai"}

__all__ = ["run_dedup"]


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text(item) for item in value if item)
    if isinstance(value, dict):
        return _text(value.get("text") or value.get("content") or value.get("value"))
    return str(value)


def _normalize(text: str) -> str:
    return _STRIP_RE.sub("", _SPACE_RE.sub(" ", text).strip().casefold())


def _preview(text: str, limit: int = 120) -> str:
    clean = _SPACE_RE.sub(" ", text).strip()
    return clean if len(clean) <= limit else clean[:limit - 3] + "..."


def _first_pair(record: dict) -> tuple[str, str]:
    """Extract first user+assistant message pair from a training record."""
    messages = record.get("messages") or record.get("conversation") or []
    user = assistant = ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or msg.get("speaker") or "").lower()
        body = _text(msg.get("content") or msg.get("text") or msg.get("message"))
        if not body:
            continue
        if not user and role in _USER_ROLES:
            user = body
        elif user and not assistant and role in _ASST_ROLES:
            assistant = body
        if user and assistant:
            break
    return user, assistant


def _build_report(total: int, unique: int, dup_rate: float, clusters: dict) -> str:
    today = _cfg_mod.TODAY
    lines = [
        f"# Training Corpus Dedup Report ({today})",
        f"Total records: {total}",
        f"Unique: {unique}",
        f"Duplicate rate: {dup_rate:.2f}%",
        "",
        "## Clusters (top 10 by size)",
    ]
    ranked = sorted(clusters.items(), key=lambda kv: len(kv[1]), reverse=True)[:10]
    for topic, rows in ranked:
        lines.append(f"\n### Cluster: {topic}")
        lines.append(f"- {len(rows)} records, example: {_preview(rows[0]['user'])}")
    return "\n".join(lines) + "\n"


def run_dedup() -> dict:
    """Scan all training-data JSONL files and write a dedup report to Obsidian.

    Returns:
        dict with keys: total, unique, dup_rate, report_path.
    """
    if not _TRAINING_DIR.exists():
        print("  [dedup] training-data/ not found, skipping")
        return {"total": 0, "unique": 0, "dup_rate": 0.0, "report_path": None}

    total = 0
    hashes: dict[str, list[dict]] = defaultdict(list)
    clusters: dict[str, list[dict]] = defaultdict(list)

    for path in sorted(_TRAINING_DIR.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for lineno, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            user, assistant = _first_pair(record)
            if not user or not assistant:
                continue

            total += 1
            digest = hashlib.sha256(
                f"{_normalize(user)}::{_normalize(assistant)}".encode("utf-8")
            ).hexdigest()
            entry = {"file": path.name, "line": lineno, "user": user}
            hashes[digest].append(entry)
            topic = (_SPACE_RE.sub(" ", user).strip()[:50] or "(empty)").replace("\n", " ")
            clusters[topic].append(entry)

    unique = len(hashes)
    dup_rate = round(((total - unique) / total) * 100, 2) if total else 0.0
    report_path = _REPORT_DIR / f"training-dedup-{_cfg_mod.TODAY}.md"
    report = _build_report(total, unique, dup_rate, clusters)

    if not _cfg_mod.DRY_RUN:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(report_path, report)

    print(f"  [dedup] {total} records, {unique} unique, {dup_rate:.1f}% dup → {report_path.name}")
    return {"total": total, "unique": unique, "dup_rate": dup_rate, "report_path": str(report_path)}
