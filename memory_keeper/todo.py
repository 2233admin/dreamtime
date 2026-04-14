"""memory_keeper.todo — Cross-project TODO aggregation, Claude task reader, and progress writer.

Public API:
    todo_scan()            — scan PROJECT_DIRS for TODO/FIXME/HACK/XXX/BUG comments
    claude_task_summary()  — read Claude Code task JSON files; return pending/in_progress
    update_progress(...)   — overwrite ~/.omc/progress.txt with structured run summary
"""
from __future__ import annotations

import json
import re
import warnings
from pathlib import Path
from typing import TypedDict

import memory_keeper._config as _cfg_mod

__all__ = ["todo_scan", "claude_task_summary", "update_progress"]


# ── TypedDicts ─────────────────────────────────────────────────────────────────

class TodoItem(TypedDict):
    file: str
    line: int
    tag: str
    text: str


class TodoResult(TypedDict):
    total: int
    by_project: dict[str, list[TodoItem]]


class TaskItem(TypedDict):
    id: str
    subject: str


class TaskSummary(TypedDict):
    pending: list[TaskItem]
    in_progress: list[TaskItem]
    total: int


# ── Constants ──────────────────────────────────────────────────────────────────

# File extension whitelist for TODO scanning
_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".c", ".cpp", ".sh"}
_TAG_RE = re.compile(r"(TODO|FIXME|HACK|XXX|BUG)\s*[:\-]?\s*(.+)", re.IGNORECASE)
_SKIP_DIRS = {"git", "node_modules", "__pycache__", ".venv", "venv", "dist", "target", ".mypy_cache"}

# Maximum source files per repo before skipping with a warning
_MAX_FILES_PER_REPO = 500


# ── Public API ────────────────────────────────────────────────────────────────

def todo_scan() -> TodoResult:
    """Scan PROJECT_DIRS for TODO/FIXME/HACK/XXX/BUG source-code comments.

    Skips: .git/, node_modules/, __pycache__/, .venv/, dist/, target/
    Each repo is capped at _MAX_FILES_PER_REPO matching files; repos exceeding
    the cap emit a warning and are skipped to avoid stalling long runs.

    Returns:
        TodoResult dict:
            total       — total comment count across all repos
            by_project  — {repo_name: [TodoItem, ...]}
    """
    by_project: dict[str, list[TodoItem]] = {}
    total = 0

    for base in _cfg_mod.PROJECT_DIRS:
        if not base.is_dir():
            continue
        candidates = _collect_repo_candidates(base)
        for repo_dir in candidates:
            items = _scan_repo_todos(repo_dir)
            if items is None:
                continue  # skipped due to file cap
            if items:
                by_project[repo_dir.name] = items
                total += len(items)

    return {"total": total, "by_project": by_project}


def claude_task_summary() -> TaskSummary:
    """Read Claude Code task JSON files and return pending/in_progress tasks.

    The task directory is auto-detected from ~/.claude/tasks/<first-subdir>/,
    or can be overridden via config paths.tasks_dir.

    Returns:
        TaskSummary dict: {pending, in_progress, total}
        Each item: TaskItem {id, subject}
    """
    tasks_dir = _detect_tasks_dir()
    pending: list[TaskItem] = []
    in_progress: list[TaskItem] = []
    total = 0

    if tasks_dir is None or not tasks_dir.exists():
        return {"pending": [], "in_progress": [], "total": 0}

    for f in sorted(
        tasks_dir.glob("*.json"),
        key=lambda p: int(p.stem) if p.stem.isdigit() else 0,
    ):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        status = data.get("status", "")
        total += 1
        item: TaskItem = {"id": data.get("id", f.stem), "subject": data.get("subject", "")}
        if status == "pending":
            pending.append(item)
        elif status == "in_progress":
            in_progress.append(item)

    return {"pending": pending, "in_progress": in_progress, "total": total}


def update_progress(
    snapshot_result: dict,
    distill_result: dict,
    plugin_result: list,
    skill_result: dict,
    todo_result: dict,
    claude_tasks: dict,
) -> None:
    """Overwrite ~/.omc/progress.txt with a structured run summary.

    Called at the end of the main run loop to replace manual progress tracking.
    Fail-open: any write error is printed but does not abort the run.

    Args:
        snapshot_result: Output of project_snapshot().
        distill_result:  Output of distill_sessions().
        plugin_result:   Output of plugin_check().
        skill_result:    Output of skill_health().
        todo_result:     Output of todo_scan().
        claude_tasks:    Output of claude_task_summary().
    """
    from memory_keeper._utils import _atomic_write

    lines = [
        f"# progress.txt — auto-updated {_cfg_mod.TODAY}",
        "",
        "## 项目健康",
        (
            f"扫描: {snapshot_result.get('total', 0)} repos, "
            f"stale分支: {snapshot_result.get('stale_branch_count', 0)}, "
            f"dirty: {snapshot_result.get('dirty_count', 0)}"
        ),
        "",
        "## TODO 汇总",
        f"总计: {todo_result.get('total', 0)} 条",
    ]

    for proj, items in (todo_result.get("by_project") or {}).items():
        lines.append(f"  {proj}: {len(items)} 条")
        for it in items[:3]:
            lines.append(f"    [{it['tag']}] {it['file']}:{it['line']} {it['text']}")
        if len(items) > 3:
            lines.append(f"    ... 还有 {len(items) - 3} 条")

    lines += ["", "## 插件更新"]
    for p in plugin_result:
        status = f"behind {p['behind']}" if p.get("has_update") else "up to date"
        lines.append(f"  {p['name']}: {status}")
    if not plugin_result:
        lines.append("  无插件配置")

    lines += ["", "## Claude Tasks"]
    pending = claude_tasks.get("pending", [])
    in_progress_tasks = claude_tasks.get("in_progress", [])
    if in_progress_tasks:
        lines.append(f"  进行中 ({len(in_progress_tasks)}):")
        for t in in_progress_tasks:
            lines.append(f"    [{t['id']}] {t['subject']}")
    if pending:
        lines.append(f"  待办 ({len(pending)}):")
        for t in pending[:10]:
            lines.append(f"    [{t['id']}] {t['subject']}")
        if len(pending) > 10:
            lines.append(f"    ... 还有 {len(pending) - 10} 条")
    if not in_progress_tasks and not pending:
        lines.append("  暂无任务")

    lines += ["", "## 本次提炼"]
    lines.append(f"  sessions: {distill_result.get('sessions', 0)}")
    lines.append(f"  decisions: {len(distill_result.get('decisions', []))}")
    lines.append(f"  gotchas: {len(distill_result.get('gotchas', []))}")

    try:
        _atomic_write(_cfg_mod.OMC_DIR / "progress.txt", "\n".join(lines) + "\n")
    except Exception as exc:
        print(f"  [warn] update_progress write failed: {exc}")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _collect_repo_candidates(base: Path) -> list[Path]:
    """Return repo root paths to scan within a base directory.

    If the base itself is a git repo it is returned directly.
    Otherwise all immediate subdirectories that are git repos are returned.

    Args:
        base: Top-level directory to inspect.

    Returns:
        List of repository root paths.
    """
    if (base / ".git").exists():
        return [base]
    try:
        return [d for d in base.iterdir() if d.is_dir() and (d / ".git").exists()]
    except PermissionError:
        return []


def _scan_repo_todos(repo_dir: Path) -> list[TodoItem] | None:
    """Scan source files in a single repo for TODO-style comments.

    Args:
        repo_dir: Repository root path.

    Returns:
        List of TodoItem dicts, or None if the repo exceeds _MAX_FILES_PER_REPO
        (in which case a warning is emitted and the repo is skipped).
    """
    try:
        all_files = [
            p for p in repo_dir.rglob("*")
            if p.is_file()
            and p.suffix in _EXT
            and not any(part.lstrip(".") in _SKIP_DIRS or part == ".git" for part in p.parts)
        ]
    except Exception:
        return []

    if len(all_files) > _MAX_FILES_PER_REPO:
        warnings.warn(
            f"todo_scan: {repo_dir.name} has {len(all_files)} matching files "
            f"(> {_MAX_FILES_PER_REPO}), skipping repo",
            stacklevel=4,
        )
        return None

    items: list[TodoItem] = []
    for path in all_files:
        try:
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
            ):
                m = _TAG_RE.search(line)
                if m:
                    items.append({
                        "file": str(path.relative_to(repo_dir)),
                        "line": lineno,
                        "tag": m.group(1).upper(),
                        "text": m.group(2).strip()[:120],
                    })
        except Exception:
            continue

    return items


def _detect_tasks_dir() -> Path | None:
    """Auto-detect Claude Code's personal task directory.

    Claude Code stores tasks under ~/.claude/tasks/<team-name>/.
    Picks the first subdirectory that contains at least one *.json file.
    Explicit config paths.tasks_dir takes precedence.

    Returns:
        Path to the task directory, or None if not found.
    """
    configured = _cfg_mod._paths.get("tasks_dir")
    if configured:
        p = Path(str(configured)).expanduser()
        return p if p.is_dir() else None

    base = _cfg_mod.HOME / ".claude" / "tasks"
    if not base.is_dir():
        return None
    try:
        subdirs = sorted(d for d in base.iterdir() if d.is_dir())
    except PermissionError:
        return None
    for d in subdirs:
        if any(d.glob("*.json")):
            return d
    return None
