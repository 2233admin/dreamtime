"""memory_keeper.adapters.claude_log — ClaudeLogAdapter.

Reads and distills Claude Code session JSONL files (.claude/**/*.jsonl).
Wraps the existing _filter_jsonl / _distill_one logic from tasks.py into the
ISessionLogAdapter interface.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from memory_keeper.adapters.base import ISessionLogAdapter, Session
from memory_keeper._utils import _mtime, llm_json

# Reuse the same redact pattern as tasks.py
_REDACT = re.compile(
    r'(api[_-]?key|secret|token|password|passwd|bearer|authorization)\s*[:=]\s*\S+',
    re.IGNORECASE
)


def _parse_session_id(path: Path) -> str:
    """Extract session ID from JSONL filename (stem without extension)."""
    return path.stem


def _filter_and_redact(path: Path, max_lines: int = 150) -> tuple[str, int]:
    """Read a JSONL file, filter user/assistant messages, redact secrets.

    Returns:
        (filtered_text, turn_count)
    """
    raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if len(raw) > max_lines * 3:
        raw = raw[-(max_lines * 3):]

    kept: list[str] = []
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
        text = content if isinstance(content, str) else " ".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
        text = _REDACT.sub(r'\1: [REDACTED]', text.strip())
        if len(text) >= 10:
            kept.append(f"[{msg_type}] {text[:500]}")

    return "\n".join(kept[-max_lines:]), len(kept)


class ClaudeLogAdapter(ISessionLogAdapter):
    """Reads Claude Code JSONL session logs for a project."""

    def test(self, project_path: Path) -> bool:
        """Return True if .claude/ directory with JSONL files exists."""
        claude_dir = project_path / ".claude"
        if not claude_dir.exists():
            return False
        return any(claude_dir.glob("*.jsonl"))

    def fetch_sessions(self, project_path: Path, since: datetime) -> list[Session]:
        """Return sessions modified after `since` from project's .claude/ dir."""
        claude_dir = project_path / ".claude"
        if not claude_dir.exists():
            return []

        sessions: list[Session] = []
        for jsonl_path in claude_dir.glob("*.jsonl"):
            mtime = _mtime(jsonl_path)
            if mtime is None or mtime < since:
                continue

            filtered_text, turn_count = _filter_and_redact(jsonl_path)
            if not filtered_text or turn_count < 2:
                continue

            sessions.append(Session(
                session_id=_parse_session_id(jsonl_path),
                project=project_path.name,
                path=jsonl_path,
                started_at=mtime,
                turns=turn_count,
                raw_text=filtered_text,
            ))

        return sessions

    def distill(self, session: Session) -> str:
        """Distill session into a Markdown summary using LLM.

        Returns empty string if session is too short or LLM fails.
        """
        if not session.raw_text or len(session.raw_text) < 100:
            return ""

        result = llm_json(
            f"从以下 Claude Code 对话提炼可持久化知识。\n\n{session.raw_text}\n\n"
            '返回 JSON（无解释）:\n'
            '{"session_project":"项目名","distilled_actions":[{"summary":"做了什么","files":[],"outcome":"结果"}],'
            '"decisions":["决策+理由"],"gotchas":["踩坑"],"preferences":["偏好"]}\n'
            '内容不足则返回 {"empty":true}\n只保留已落地的行动。',
            max_tokens=800
        )
        if not result or result.get("empty"):
            return ""

        lines = [f"# Session {session.session_id} — {session.project}"]
        if result.get("distilled_actions"):
            lines.append("\n## 行动")
            for a in result["distilled_actions"]:
                lines.append(f"- {a.get('summary', '')} → {a.get('outcome', '')}")
        if result.get("decisions"):
            lines.append("\n## 决策")
            for d in result["decisions"]:
                lines.append(f"- {d}")
        if result.get("gotchas"):
            lines.append("\n## 踩坑")
            for g in result["gotchas"]:
                lines.append(f"- {g}")
        return "\n".join(lines)
