"""memory_keeper.adapters.claude_task — ClaudeTaskAdapter.

Reads completed tasks from ~/.claude/tasks/**/*.json (Claude Code task system).
This is the primary kanban source for Curry's workflow.

Task JSON format:
    {"id": "87", "subject": "...", "status": "completed", "blocks": [...], "blockedBy": [...]}
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from memory_keeper.adapters.base import IKanbanAdapter, Task

# Global tasks root — matches ~/.claude/tasks/
_TASKS_ROOT = Path.home() / ".claude" / "tasks"


class ClaudeTaskAdapter(IKanbanAdapter):
    """Reads completed tasks from the Claude Code task JSON files.

    Scans all *.json files under ~/.claude/tasks/ regardless of project_path,
    since Claude tasks are stored globally per team, not per project directory.
    Uses file mtime as done_at proxy (no explicit timestamp in task JSON).
    """

    def test(self, project_path: Path) -> bool:
        """Always applicable if ~/.claude/tasks/ exists."""
        return _TASKS_ROOT.exists()

    def fetch_done_tasks(self, project_path: Path, since: datetime) -> list[Task]:
        """Return all completed tasks whose JSON file was modified after `since`.

        Args:
            project_path: Unused (tasks are global, not per-project).
            since:        Only return tasks modified after this datetime.

        Returns:
            List of Task objects with status == "completed".
        """
        tasks: list[Task] = []
        if not _TASKS_ROOT.exists():
            return tasks

        for task_file in _TASKS_ROOT.glob("**/*.json"):
            try:
                mtime = datetime.fromtimestamp(task_file.stat().st_mtime)
                if mtime < since:
                    continue
                data = json.loads(task_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            if data.get("status") != "completed":
                continue

            task_id = str(data.get("id", ""))
            title = data.get("subject", data.get("description", ""))[:200]
            if not title:
                continue

            tasks.append(Task(
                id=f"#{task_id}" if task_id.isdigit() else task_id,
                title=title,
                done_at=mtime,
                metadata={"team": task_file.parent.name, "file": str(task_file)},
            ))

        return tasks
