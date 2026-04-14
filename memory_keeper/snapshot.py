"""memory_keeper.snapshot — Git health metrics per project.

Public API:
    project_snapshot() — scan PROJECT_DIRS; write project-health.json; return summary
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict

import memory_keeper._config as _cfg_mod
from memory_keeper._utils import _atomic_write

__all__ = ["project_snapshot"]


# ── TypedDicts ─────────────────────────────────────────────────────────────────

class ProjectHealth(TypedDict):
    name: str
    path: str
    last_commit: str | None
    stale_branches: list[str]
    dirty: bool


class SnapshotResult(TypedDict):
    total: int
    stale_branch_count: int
    dirty_count: int


# ── Internal helpers ───────────────────────────────────────────────────────────

def _run_git(cwd: Path, *args: str) -> str:
    """Run a git command in the given directory and return stdout.

    Args:
        cwd:  Repository root to run git in.
        args: git subcommand and flags.

    Returns:
        Stripped stdout, or empty string on any error.
    """
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _last_commit_date(d: Path) -> str | None:
    """Return the most-recent commit date as YYYY-MM-DD, or None.

    Args:
        d: Repository root path.
    """
    ts_str = _run_git(d, "log", "-1", "--format=%ct")
    if ts_str.isdigit():
        return datetime.fromtimestamp(int(ts_str)).strftime("%Y-%m-%d")
    return None


def _stale_branches(d: Path, cutoff_ts: float) -> list[str]:
    """Return local branch names whose last committer date is before cutoff.

    Args:
        d:         Repository root path.
        cutoff_ts: POSIX timestamp threshold.

    Returns:
        List of branch name strings.
    """
    branch_out = _run_git(
        d, "branch", "--format=%(refname:short)|%(committerdate:unix)"
    )
    stale: list[str] = []
    for line in branch_out.splitlines():
        if "|" not in line:
            continue
        bname, bts = line.rsplit("|", 1)
        if bts.isdigit() and float(bts) < cutoff_ts:
            stale.append(bname.strip())
    return stale


def _scan_repo(d: Path, cutoff_ts: float) -> ProjectHealth:
    """Collect health metrics for a single git repository.

    Args:
        d:         Repository root path.
        cutoff_ts: POSIX timestamp for stale-branch detection (30 days ago).

    Returns:
        ProjectHealth dict for this repository.
    """
    return {
        "name": d.name,
        "path": str(d),
        "last_commit": _last_commit_date(d),
        "stale_branches": _stale_branches(d, cutoff_ts),
        "dirty": bool(_run_git(d, "status", "--porcelain")),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def project_snapshot() -> SnapshotResult:
    """Scan PROJECT_DIRS for git repos and collect health metrics per repo.

    Writes a full health report to ~/.omc/project-health.json.

    Returns:
        SnapshotResult dict: {total, stale_branch_count, dirty_count}.
    """
    projects: list[ProjectHealth] = []
    stale_total = 0
    dirty_total = 0
    cutoff_ts = (datetime.now() - timedelta(days=30)).timestamp()

    for base in _cfg_mod.PROJECT_DIRS:
        if not base.is_dir():
            continue
        # Scan the base dir itself and one level of subdirectories
        candidates = [base] + [d for d in base.iterdir() if d.is_dir()]
        for d in candidates:
            if not (d / ".git").exists():
                continue
            info = _scan_repo(d, cutoff_ts)
            projects.append(info)
            stale_total += len(info["stale_branches"])
            if info["dirty"]:
                dirty_total += 1

    health = {"updated": _cfg_mod.TODAY, "projects": projects}
    _atomic_write(
        _cfg_mod.OMC_DIR / "project-health.json",
        json.dumps(health, ensure_ascii=False, indent=2)
    )
    return {"total": len(projects), "stale_branch_count": stale_total, "dirty_count": dirty_total}
