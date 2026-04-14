"""memory_keeper.gaps — Kanban-memory gap detection and reporting.

Compares completed Claude tasks (ClaudeTaskAdapter) against what is
documented in MEMORY.md (MarkdownMemoryAdapter).

Output files written to ~/.claude/memory-store/gaps/:
    YYYY-MM-DD.md   — human-readable Markdown report
    YYYY-MM-DD.json — machine-readable JSON for downstream processing
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import memory_keeper._config as _cfg_mod
from memory_keeper._utils import _atomic_write
from memory_keeper.adapters.base import Gap, Task
from memory_keeper.adapters.claude_task import ClaudeTaskAdapter
from memory_keeper.adapters.markdown_mem import MarkdownMemoryAdapter

# Output root — will migrate to _cfg_mod.STORE_DIR after _config.py Step 7
_STORE = Path.home() / ".claude" / "memory-store"
_CURSOR = _STORE / ".cursor" / "gaps.json"

__all__ = ["run_gaps"]


# ── Cursor ─────────────────────────────────────────────────────────────────────

def _load_cursor() -> datetime:
    """Return last-run datetime, defaulting to 7 days ago."""
    try:
        if _CURSOR.exists():
            return datetime.fromisoformat(
                json.loads(_CURSOR.read_text(encoding="utf-8"))["last_run"]
            )
    except Exception:
        pass
    return datetime.now() - timedelta(days=7)


def _save_cursor(dt: datetime) -> None:
    _atomic_write(_CURSOR, json.dumps({"last_run": dt.isoformat()}))


# ── Main entry ─────────────────────────────────────────────────────────────────

def run_gaps(since: datetime | None = None) -> dict:
    """Run kanban-memory gap detection; write .md + .json reports.

    Respects _cfg_mod.DRY_RUN — _atomic_write skips file I/O and prints
    a preview instead.

    Args:
        since: Only consider tasks completed after this datetime.
               Defaults to the last stored cursor (7 days ago on first run).

    Returns:
        dict with keys: tasks_total, gaps_count, report_md, report_json.
    """
    if since is None:
        since = _load_cursor()

    kanban = ClaudeTaskAdapter()
    mem_adapter = MarkdownMemoryAdapter()
    project_path = _cfg_mod.MEMORY_DIR

    if not kanban.test(project_path):
        print("  [gaps] ~/.claude/tasks/ not found, skipping")
        return {"tasks_total": 0, "gaps_count": 0}

    tasks = kanban.fetch_done_tasks(project_path, since)
    if not tasks:
        print(f"  [gaps] 无新完成任务 (since {since.strftime('%Y-%m-%d %H:%M')})")
        if not _cfg_mod.DRY_RUN:
            _save_cursor(datetime.now())
        return {"tasks_total": 0, "gaps_count": 0}

    if mem_adapter.test(project_path):
        gaps = mem_adapter.check_coverage(project_path, tasks)
    else:
        gaps = [Gap(task=t, reason="MEMORY.md 不存在") for t in tasks]

    today = _cfg_mod.TODAY
    gap_ids = {g.task.id for g in gaps}
    md_report = _build_md(today, tasks, gaps, gap_ids)
    json_report = _build_json(today, tasks, gaps, gap_ids)

    gaps_dir = _STORE / "gaps"
    md_path = gaps_dir / f"{today}.md"
    json_path = gaps_dir / f"{today}.json"

    _atomic_write(md_path, md_report)
    _atomic_write(json_path, json.dumps(json_report, ensure_ascii=False, indent=2) + "\n")

    if not _cfg_mod.DRY_RUN:
        _save_cursor(datetime.now())

    print(f"  [gaps] {len(gaps)}/{len(tasks)} 任务未沉淀 → {md_path.name}")
    return {
        "tasks_total": len(tasks),
        "gaps_count": len(gaps),
        "report_md": str(md_path),
        "report_json": str(json_path),
    }


# ── Report builders ────────────────────────────────────────────────────────────

def _build_md(
    today: str,
    tasks: list[Task],
    gaps: list[Gap],
    gap_ids: set[str],
) -> str:
    lines = [
        f"# 看板-记忆对账报告 ({today})",
        f"\n**已完成任务**: {len(tasks)} 项 | **未沉淀**: {len(gaps)} 项\n",
    ]

    if gaps:
        lines.append(f"## ⚠️ 未沉淀的已完成任务 ({len(gaps)} items)\n")
        lines.append("| 任务 | ID | 完成时间 | 缺口原因 |")
        lines.append("|------|----|---------|---------|")
        for g in gaps:
            done_str = g.task.done_at.strftime("%Y-%m-%d %H:%M") if g.task.done_at else "—"
            title = g.task.title[:60].replace("|", "｜")
            lines.append(f"| {title} | {g.task.id} | {done_str} | {g.reason} |")
    else:
        lines.append("## ✅ 所有已完成任务均已沉淀\n")

    covered = [t for t in tasks if t.id not in gap_ids]
    if covered:
        lines.append(f"\n## ✅ 已沉淀 ({len(covered)} items)\n")
        for t in covered:
            done_str = t.done_at.strftime("%Y-%m-%d") if t.done_at else "—"
            lines.append(f"- {t.id} — {t.title[:60]} （{done_str}）")

    return "\n".join(lines) + "\n"


def _build_json(
    today: str,
    tasks: list[Task],
    gaps: list[Gap],
    gap_ids: set[str],
) -> dict:
    return {
        "date": today,
        "tasks_total": len(tasks),
        "gaps_count": len(gaps),
        "gaps": [
            {
                "id": g.task.id,
                "title": g.task.title,
                "done_at": g.task.done_at.isoformat() if g.task.done_at else None,
                "reason": g.reason,
            }
            for g in gaps
        ],
        "covered": [
            {
                "id": t.id,
                "title": t.title,
                "done_at": t.done_at.isoformat() if t.done_at else None,
            }
            for t in tasks
            if t.id not in gap_ids
        ],
    }
