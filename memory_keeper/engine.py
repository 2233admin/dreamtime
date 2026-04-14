"""memory_keeper.engine — Core Engine: programmatic API for the full pipeline.

Provides run_engine() as the canonical entry point for automation and
OpenClaw skills.  All steps are fail-safe: a single-step failure logs
a warning and continues rather than aborting the whole run.

Pipeline order:
    1. gaps         — kanban-memory gap detection
    2. distill      — session distillation
    3. dreamtime    — cross-session pattern synthesis (LLM)
    4. memblock     — memory lifecycle management
    5. training     — training data extraction
    6. snapshot     — project health snapshot
    7. kanban_sync  — write Obsidian Kanban cards + world-state note
    8. kb_compile   — compile KB raw sources via local Ollama model

Adapters in use (auto-selected):
    Kanban  : ClaudeTaskAdapter  (reads ~/.claude/tasks/**/*.json)
    Memory  : MarkdownMemoryAdapter (reads project MEMORY.md)
    Sessions: ClaudeLogAdapter   (reads .claude/**/*.jsonl)
"""
from __future__ import annotations

import traceback
from datetime import datetime
from typing import Callable

import memory_keeper._config as _cfg_mod
from memory_keeper._utils import load_cursor, save_cursor

__all__ = ["run_engine", "run_step"]


# ── Fail-safe wrapper ──────────────────────────────────────────────────────────

def run_step(name: str, fn: Callable, log_errors: bool = True) -> object:
    """Run fn() with fail-safe: log on error, return None instead of raising.

    Args:
        name:       Human-readable step name for log messages.
        fn:         Zero-argument callable to execute.
        log_errors: If False, suppress the traceback (just print the warning).

    Returns:
        Return value of fn(), or None on error.
    """
    try:
        return fn()
    except Exception as exc:
        if log_errors:
            tb = traceback.format_exc().strip().splitlines()[-1]
            print(f"  [WARN] step '{name}' failed: {exc} ({tb})")
        else:
            print(f"  [WARN] step '{name}' failed: {exc}")
        return None


# ── Main entry ─────────────────────────────────────────────────────────────────

def run_engine(
    only: str | None = None,
    since: datetime | None = None,
    dry_run: bool | None = None,
) -> dict:
    """Run the memory-keeper pipeline with fail-safe dispatch.

    Args:
        only:    If set, run only this step.
                 Choices: gaps | distill | dreamtime | memblock | training | snapshot | kanban_sync | kb_compile | all (default)
        since:   Override cursor date (process data after this datetime).
        dry_run: Override DRY_RUN config flag.  None = use existing flag.

    Returns:
        dict with per-step results (None for skipped/failed steps).
    """
    if dry_run is not None:
        _cfg_mod.DRY_RUN = dry_run

    if since is None:
        since = load_cursor()

    print(f"[{datetime.now():%H:%M:%S}] engine start (only={only or 'all'}, "
          f"since={since:%Y-%m-%d %H:%M}, dry_run={_cfg_mod.DRY_RUN})")

    results: dict = {}

    # ── Step 1: gaps ──────────────────────────────────────────────────────────
    if not only or only == "gaps":
        from memory_keeper.gaps import run_gaps
        print("  [1/6] gaps check...")
        results["gaps"] = run_step("gaps", lambda: run_gaps(since=since))

    # ── Step 2: distill ───────────────────────────────────────────────────────
    if not only or only == "distill":
        from memory_keeper.tasks import distill_sessions
        from memory_keeper._utils import _SeenHashes
        print("  [2/6] session distill...")
        seen = _SeenHashes()
        dr = run_step("distill", lambda: distill_sessions(since=since, seen=seen))
        results["distill"] = dr
        if dr:
            seen.save()
            print(f"        {dr.get('sessions', 0)} sessions, "
                  f"{len(dr.get('decisions', []))} decisions, "
                  f"{len(dr.get('gotchas', []))} gotchas")

    # ── Step 3: dreamtime ─────────────────────────────────────────────────────
    if not only or only == "dreamtime":
        from memory_keeper.tasks import dreamtime
        print("  [3/6] dreamtime analysis...")
        _dr = results.get("distill") or {}
        dream_r = run_step("dreamtime", lambda: dreamtime(_dr))
        results["dream"] = dream_r
        if dream_r and dream_r.get("top_open_thread"):
            print(f"        open thread: {dream_r['top_open_thread'][:60]}")

    # ── Step 4: memblock ─────────────────────────────────────────────────────
    if not only or only == "memblock":
        from memory_keeper.memblock import sweep_all_dirs
        print("  [4/7] memblock lifecycle...")
        results["memblock"] = sweep_all_dirs(step_fn=run_step)

    # ── Step 5: training ──────────────────────────────────────────────────────
    if not only or only == "training":
        from memory_keeper.training import run_training
        print("  [5/7] training extract...")
        results["training"] = run_step("training", lambda: run_training(since=since))

    # ── Step 6: snapshot ──────────────────────────────────────────────────────
    if not only or only == "snapshot":
        from memory_keeper.snapshot import project_snapshot
        print("  [6/7] project snapshot...")
        sr = run_step("snapshot", lambda: project_snapshot())
        results["snapshot"] = sr
        if sr:
            print(f"        {sr.get('total', 0)} repos, "
                  f"{sr.get('stale_branch_count', 0)} stale, "
                  f"{sr.get('dirty_count', 0)} dirty")

    # ── Step 7: kanban_sync ───────────────────────────────────────────────────
    if not only or only == "kanban_sync":
        from memory_keeper.kanban_sync import run_kanban_sync
        print("  [7/7] kanban sync...")
        ks = run_step("kanban_sync", lambda: run_kanban_sync(
            dream=results.get("dream"),
            snapshot=results.get("snapshot"),
            distill=results.get("distill"),
        ))
        results["kanban_sync"] = ks
        if ks:
            print(f"        {ks.get('inserted', 0)} cards inserted, "
                  f"world-state → {ks.get('world_state_path', '')}")

    # ── Step 8: kb_compile ──────────────────────────────────────────────────
    if not only or only == "kb_compile":
        print("  [8/8] kb compile (local LLM)...")
        def _kb_compile():
            import sys, importlib.util
            spec = importlib.util.spec_from_file_location(
                "kb_compile",
                str(_cfg_mod.HOME / ".claude" / "scripts" / "kb" / "kb_compile.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.compile_all()
        kr = run_step("kb_compile", _kb_compile)
        results["kb_compile"] = kr
        if kr:
            total_compiled = sum(r.get("compiled", 0) for r in kr)
            print(f"        {total_compiled} sources compiled across {len(kr)} topics")

    # ── Persist cursor on full run ─────────────────────────────────────────────
    if not only and not _cfg_mod.DRY_RUN:
        dr = results.get("distill") or {}
        save_cursor(dr.get("max_mtime") or datetime.now())

    print(f"[{datetime.now():%H:%M:%S}] engine done")
    return results
