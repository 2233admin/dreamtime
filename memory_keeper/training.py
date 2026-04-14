"""memory_keeper.training — Training data extraction from Claude session logs.

Reads JSONL session files, scores each for quality, and writes OpenAI
fine-tuning format records to ~/.claude/memory-store/training-data/.

Quality scoring (include if score >= threshold, default 4):
    +2  session contains code blocks (``` markers in text)
    +3  session contains tool calls (assistant content with tool_use blocks)
    +1  total text length > 300 chars

Output format — one JSON object per line:
    {"messages": [...], "meta": {"quality_score": N, "session_id": "...", ...}}

CLI:
    py -3 training.py [--dry-run] [--since YYYY-MM-DD] [--threshold N]
"""
from __future__ import annotations

import argparse
import glob as _glob
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import memory_keeper._config as _cfg_mod
from memory_keeper._utils import (
    _SeenHashes,
    _atomic_write,
    _mtime,
    _recently_active_jsonl,
)

_STORE = Path.home() / ".claude" / "memory-store"
_CURSOR = _STORE / ".cursor" / "training.json"
_SEEN_PATH = _STORE / ".cursor" / "training-seen.json"
_DEFAULT_THRESHOLD = 4

_REDACT = re.compile(
    r'(api[_-]?key|secret|token|password|passwd|bearer|authorization)'
    r'(?:\s*[:=]\s*\S+|"\s*:\s*"[^"]{4,}")',
    re.IGNORECASE,
)

__all__ = ["run_training"]


# ── Cursor ─────────────────────────────────────────────────────────────────────

def _load_cursor() -> datetime:
    """Return last-run datetime, defaulting to 30 days ago."""
    try:
        if _CURSOR.exists():
            return datetime.fromisoformat(
                json.loads(_CURSOR.read_text(encoding="utf-8"))["last_run"]
            )
    except Exception:
        pass
    return datetime.now() - timedelta(days=30)


def _save_cursor(dt: datetime) -> None:
    _atomic_write(_CURSOR, json.dumps({"last_run": dt.isoformat()}))


# ── Session parser ─────────────────────────────────────────────────────────────

def _parse_session(path: Path) -> tuple[int, list[dict]]:
    """Parse a JSONL session file and return (quality_score, messages).

    Tool-use blocks in assistant content contribute to the quality score
    but are not included in the output messages (only text blocks are kept).

    Args:
        path: Path to a Claude Code JSONL session file.

    Returns:
        (score, messages) — score=0 and messages=[] if session is unusable.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return 0, []

    # Cap to last 450 lines to avoid OOM on huge sessions
    if len(raw) > 450:
        raw = raw[-450:]

    messages: list[dict] = []
    has_code_block = False
    has_tool_call = False
    total_chars = 0

    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue

        msg_type = obj.get("type", "")
        if msg_type not in ("user", "assistant"):
            continue

        content = obj.get("message", {}).get("content", "")

        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    has_tool_call = True
            text = " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )
        else:
            text = str(content)

        text = _REDACT.sub(r'\1: [REDACTED]', text.strip())
        if len(text) < 10:
            continue

        if "```" in text:
            has_code_block = True

        total_chars += len(text)
        messages.append({"role": msg_type, "content": text[:2000]})

    score = 0
    if has_code_block:
        score += 2
    if has_tool_call:
        score += 3
    if total_chars > 300:
        score += 1

    return score, messages


# ── Main entry ─────────────────────────────────────────────────────────────────

def run_training(
    since: datetime | None = None,
    threshold: int = _DEFAULT_THRESHOLD,
) -> dict:
    """Extract high-quality training data from recent session logs.

    Respects _cfg_mod.DRY_RUN — no file writes when set.

    Args:
        since:     Only process sessions with mtime after this datetime.
                   Defaults to last stored cursor (30 days ago on first run).
        threshold: Minimum quality score to include (default: 4).

    Returns:
        dict with keys: sessions_scanned, sessions_written, output_path.
    """
    if since is None:
        since = _load_cursor()

    hot_files = _recently_active_jsonl(within_minutes=5)
    sg = _cfg_mod.SESSION_GLOB.replace("~", str(_cfg_mod.HOME))
    jsonl_files = [
        Path(x)
        for x in _glob.glob(sg, recursive=True)
        if "subagents" not in x
        and _mtime(Path(x)) > since
        and Path(x) not in hot_files
    ]

    if not jsonl_files:
        since_str = since.strftime("%Y-%m-%d %H:%M")
        print(f"  [training] 无新 session (since {since_str})")
        if not _cfg_mod.DRY_RUN:
            _save_cursor(datetime.now())
        return {"sessions_scanned": 0, "sessions_written": 0, "output_path": None}

    seen = _SeenHashes(_SEEN_PATH)
    today = _cfg_mod.TODAY
    output_path = _STORE / "training-data" / f"{today}.jsonl"

    records: list[str] = []
    skipped_quality = 0
    skipped_dup = 0

    for path in jsonl_files:
        score, messages = _parse_session(path)
        if score < threshold or len(messages) < 2:
            skipped_quality += 1
            continue

        # Deduplicate on content fingerprint
        content_key = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        if not seen.add(content_key):
            skipped_dup += 1
            continue

        session_id = path.stem
        # path: .../projects/<encoded-project>/<session>.jsonl
        project = path.parent.name

        record = {
            "messages": messages,
            "meta": {
                "quality_score": score,
                "session_id": session_id,
                "project": project,
                "extracted_at": today,
            },
        }
        records.append(json.dumps(record, ensure_ascii=False))

    written = len(records)

    if records:
        _atomic_write(output_path, "\n".join(records) + "\n")
        if not _cfg_mod.DRY_RUN:
            seen.save()
            _save_cursor(datetime.now())
        print(
            f"  [training] {written}/{len(jsonl_files)} session 写入 {output_path.name}"
            f" (低质量跳过 {skipped_quality}, 重复跳过 {skipped_dup})"
        )
    else:
        print(
            f"  [training] {len(jsonl_files)} session 扫描，无符合阈值记录"
            f" (threshold={threshold}, 低质量 {skipped_quality}, 重复 {skipped_dup})"
        )
        if not _cfg_mod.DRY_RUN:
            _save_cursor(datetime.now())

    return {
        "sessions_scanned": len(jsonl_files),
        "sessions_written": written,
        "output_path": str(output_path) if records else None,
    }


# ── CLI entry point ────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Extract training data from Claude Code session logs"
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Preview results without writing any files")
    p.add_argument("--since", metavar="YYYY-MM-DD",
                   help="Process sessions modified after this date (overrides cursor)")
    p.add_argument(
        "--threshold", type=int, default=_DEFAULT_THRESHOLD,
        help=f"Minimum quality score to include a session (default: {_DEFAULT_THRESHOLD})",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    _cfg_mod.DRY_RUN = args.dry_run

    since: datetime | None = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print(f"Invalid --since date: {args.since!r} (expected YYYY-MM-DD)")
            raise SystemExit(1)

    result = run_training(since=since, threshold=args.threshold)
    print(f"Done: {result}")
