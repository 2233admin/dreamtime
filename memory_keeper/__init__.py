"""memory_keeper — Claude Code memory maintenance daemon.

Re-exports all public symbols so callers can use:
    import memory_keeper as mk
    mk.distill_sessions(...)
    mk._reload_config(cfg)
    mk._SeenHashes()

Public API (by module):
    _config  — _cfg, _load_config, _reload_config, _p, _paths, _api, _behav + all globals
    _utils   — _get_client, llm, llm_json, _Lock, _atomic_write, _SeenHashes,
               _memu_sync, load_cursor, save_cursor, _recently_active_jsonl,
               _mtime, _git_last_commit
    tasks    — discover_new_projects, trim_memory, organize_obsidian,
               distill_sessions, dreamtime, write_inbox
    snapshot — project_snapshot
    plugins  — plugin_check, skill_health
    todo     — todo_scan, claude_task_summary, update_progress
"""
from __future__ import annotations

import argparse
from datetime import datetime

# ── Re-export config namespace ─────────────────────────────────────────────────
from memory_keeper._config import (
    _cfg,
    _load_config,
    _auto_detect_memory_dir,
    _reload_config,
    _p,
    _paths,
    _api,
    _behav,
    API_KEY,
    API_BASE,
    MODEL,
    TODAY,
    HOME,
    DRY_RUN,
    VERBOSE,
    STORE_DIR,
    MEMORY_DIR,
    MEMORY_FILE,
    OBSIDIAN_DIR,
    OMC_DIR,
    INBOX,
    CURSOR_FILE,
    PENDING_RULES,
    DREAMTIME_LOG,
    ARCHIVE_DIR,
    PROJECT_DIRS,
    OBSIDIAN_SKIP,
    OBSIDIAN_CATS,
    TRIM_THRESHOLD,
    COLD_DAYS,
    NEW_FILE_SKIP,
    HOT_MINUTES,
    DISTILL_MAXLINES,
    OBS_WORKERS,
    DIST_WORKERS,
    SESSION_GLOB,
    LOCK_FILE,
)

# ── Re-export utils namespace ──────────────────────────────────────────────────
from memory_keeper._utils import (
    _get_client,
    llm,
    llm_json,
    _Lock,
    _atomic_write,
    _SeenHashes,
    _memu_sync,
    load_cursor,
    save_cursor,
    _recently_active_jsonl,
    _mtime,
    _git_last_commit,
)

# ── Re-export tasks namespace ──────────────────────────────────────────────────
from memory_keeper.tasks import (
    discover_new_projects,
    trim_memory,
    _classify_file,
    organize_obsidian,
    _filter_jsonl,
    _distill_one,
    _project_file_map,
    distill_sessions,
    dreamtime,
    write_inbox,
)
from memory_keeper.memblock import run_memblock

__all__ = [
    # config
    "_cfg", "_load_config", "_auto_detect_memory_dir", "_reload_config",
    "_p", "_paths", "_api", "_behav",
    "API_KEY", "API_BASE", "MODEL", "TODAY", "HOME",
    "DRY_RUN", "VERBOSE",
    "STORE_DIR",
    "MEMORY_DIR", "MEMORY_FILE", "OBSIDIAN_DIR", "OMC_DIR",
    "INBOX", "CURSOR_FILE", "PENDING_RULES", "DREAMTIME_LOG", "ARCHIVE_DIR",
    "PROJECT_DIRS", "OBSIDIAN_SKIP", "OBSIDIAN_CATS",
    "TRIM_THRESHOLD", "COLD_DAYS", "NEW_FILE_SKIP", "HOT_MINUTES",
    "DISTILL_MAXLINES", "OBS_WORKERS", "DIST_WORKERS", "SESSION_GLOB", "LOCK_FILE",
    # utils
    "_get_client", "llm", "llm_json", "_Lock", "_atomic_write", "_SeenHashes",
    "_memu_sync", "load_cursor", "save_cursor", "_recently_active_jsonl",
    "_mtime", "_git_last_commit",
    # tasks
    "discover_new_projects", "trim_memory", "_classify_file", "organize_obsidian",
    "_filter_jsonl", "_distill_one", "_project_file_map",
    "distill_sessions", "dreamtime", "write_inbox",
    "run_memblock",
    # entry points
    "parse_args", "main",
]


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for memory-keeper."""
    p = argparse.ArgumentParser(description="memory-keeper — Claude Code memory maintenance")
    p.add_argument("--dry-run", "-n", action="store_true",
                   help="Show what would be done without writing anything")
    p.add_argument("--only",
                   choices=["projects", "trim", "obsidian", "distill", "dreamtime",
                            "snapshot", "gaps", "training", "preference", "dedup",
                            "kanban_sync", "memblock"],
                   help="Run only one task")
    p.add_argument("--since", metavar="YYYY-MM-DD",
                   help="Override cursor date (process sessions since this date)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print extra detail")
    p.add_argument("--config", metavar="PATH",
                   help="Path to config.yaml (default: script dir or ~/.config/memory-keeper/)")
    p.add_argument("--serve", action="store_true",
                   help="Start A2A server mode (long-running HTTP service)")
    p.add_argument("--port", type=int, default=8713,
                   help="A2A server port (default: 8713)")
    return p.parse_args()


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    """Entry point: parse args, load config, acquire lock, run pipeline."""
    import memory_keeper._config as _cfg_mod
    args = parse_args()
    cfg = _load_config(None if not args.config else __import__("pathlib").Path(args.config))
    _reload_config(cfg)
    _cfg_mod.DRY_RUN = args.dry_run
    _cfg_mod.VERBOSE = args.verbose

    if args.serve:
        from memory_keeper_a2a import serve  # noqa: PLC0415
        serve(args.port)
        return

    if args.dry_run:
        print("[dry-run] no files will be written")

    with _Lock():
        _run(args)


def _try_step(name: str, fn) -> object:
    """Run fn() with fail-safe: log exception and return None on error."""
    try:
        return fn()
    except Exception as exc:
        print(f"  [WARN] step '{name}' failed: {exc}")
        return None


def _run(args: argparse.Namespace) -> None:
    """Execute the maintenance pipeline for a single run."""
    import memory_keeper._config as _cfg_mod
    seen = _SeenHashes()
    try:
        print(f"[{datetime.now():%H:%M:%S}] memory-keeper start")

        if args.since:
            since = datetime.strptime(args.since, "%Y-%m-%d")
        else:
            since = load_cursor()
        print(f"  cursor: since {since:%Y-%m-%d %H:%M}")

        only = args.only

        new_projects: list[str] = []
        if not only or only == "projects":
            new_projects = discover_new_projects()
            for p in new_projects:
                print(f"  + {p}")

        snapshot_result: dict = {}
        if not only or only == "snapshot":
            print("  project snapshot...")
            from memory_keeper.snapshot import project_snapshot
            snapshot_result = project_snapshot()
            print(f"  snapshot: {snapshot_result['total']} repos, "
                  f"{snapshot_result['stale_branch_count']} stale branches, "
                  f"{snapshot_result['dirty_count']} dirty")

        trim_result = "跳过"
        if not only or only == "trim":
            trim_result = trim_memory()
            print(f"  memory: {trim_result}")

        organized: list[str] = []
        if not only or only == "obsidian":
            organized = organize_obsidian()
            for m in organized:
                print(f"  obsidian: {m}")

        distill_result: dict = {"sessions": 0, "actions": [], "decisions": [],
                                "gotchas": [], "preferences": [], "updated_projects": []}
        if not only or only == "distill":
            print("  distilling...")
            distill_result = distill_sessions(since=since, seen=seen)
            print(f"  distill: {distill_result['sessions']} sessions, "
                  f"{len(distill_result['actions'])} actions, "
                  f"{len(distill_result['decisions'])} decisions, "
                  f"{len(distill_result['gotchas'])} gotchas")
            if _cfg_mod._behav.get("memu_sync", False):
                _memu_sync(distill_result["decisions"], "decision", _cfg_mod.TODAY)
                _memu_sync(distill_result["gotchas"], "gotcha", _cfg_mod.TODAY)
                _memu_sync(distill_result["preferences"], "preference", _cfg_mod.TODAY)

        dream_result: dict = {}
        if not only or only == "dreamtime":
            print("  dreamtime...")
            dream_result = dreamtime(distill_result)
            print(f"  dreamtime: {dream_result.get('memory_health', '')}")

        if not only or only == "memblock":
            print("  memblock lifecycle...")
            from memory_keeper.memblock import sweep_all_dirs
            sweep_all_dirs(step_fn=_try_step)

        if not only or only == "gaps":
            print("  gaps check...")
            from memory_keeper.gaps import run_gaps
            _try_step("gaps", lambda: run_gaps(since=since))

        if not only or only == "training":
            print("  training extract...")
            from memory_keeper.training import run_training
            _try_step("training", lambda: run_training(since=since))

        if not only or only == "preference":
            print("  preference classify...")
            from memory_keeper.preference import run_preference
            _try_step("preference", lambda: run_preference(distill_result.get("preferences", [])))

        if not only or only == "dedup":
            print("  corpus dedup...")
            from memory_keeper.dedup import run_dedup
            _try_step("dedup", lambda: run_dedup())

        if not only or only == "kanban_sync":
            print("  kanban sync...")
            from memory_keeper.kanban_sync import run_kanban_sync
            ks = _try_step("kanban_sync", lambda: run_kanban_sync(
                dream=dream_result or None,
                snapshot=snapshot_result or None,
                distill=distill_result or None,
            ))
            if ks:
                print(f"  kanban: {ks.get('inserted', 0)} cards inserted → {ks.get('world_state_path', '')}")

        plugin_result: list = []
        skill_result: dict = {"dead_refs": [], "unused_30d": [], "usage_top5": []}
        if not only:
            from memory_keeper.plugins import plugin_check, skill_health
            plugin_result = plugin_check()
            skill_result = skill_health()

        todo_result: dict = {"total": 0, "by_project": {}}
        claude_tasks: dict = {"pending": [], "in_progress": [], "total": 0}
        if not only:
            from memory_keeper.todo import todo_scan, claude_task_summary
            print("  scanning todos...")
            todo_result = todo_scan()
            print(f"  todos: {todo_result['total']} items across {len(todo_result['by_project'])} projects")
            claude_tasks = claude_task_summary()
            print(f"  tasks: {claude_tasks['total']} total, "
                  f"{len(claude_tasks['in_progress'])} in_progress, "
                  f"{len(claude_tasks['pending'])} pending")

        if not only:
            write_inbox(
                new_projects, trim_result, organized, distill_result, dream_result,
                plugin_result=plugin_result,
                skill_result=skill_result,
                claude_tasks=claude_tasks,
                todo_result=todo_result,
            )
            save_cursor(distill_result.get("max_mtime") or datetime.now())

        if not only:
            from memory_keeper.todo import update_progress
            update_progress(snapshot_result, distill_result, plugin_result, skill_result, todo_result, claude_tasks)

        print(f"[{datetime.now():%H:%M:%S}] done: {_cfg_mod.TODAY}")
    finally:
        seen.save()
