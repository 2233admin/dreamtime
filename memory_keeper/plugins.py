"""memory_keeper.plugins — Plugin update checker and skill health reporter.

Public API:
    plugin_check()  — check plugin git repos for upstream commits; report-only
    skill_health()  — dead file references and usage staleness in skill dirs
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict

import memory_keeper._config as _cfg_mod

__all__ = ["plugin_check", "skill_health"]


# ── TypedDicts ─────────────────────────────────────────────────────────────────

class PluginStatus(TypedDict):
    name: str
    path: str
    has_update: bool
    behind: int


class DeadRef(TypedDict):
    skill: str
    ref: str


class SkillUsage(TypedDict):
    skill: str
    last_used: str


class SkillHealthResult(TypedDict):
    dead_refs: list[DeadRef]
    unused_30d: list[SkillUsage]
    usage_top5: list[SkillUsage]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _git_behind(repo: Path) -> int:
    """Return number of commits behind origin/HEAD. Returns -1 on any error.

    Args:
        repo: Path to the git repository root.
    """
    fetch_ok = _cfg_mod._behav.get("plugin_check_fetch", True)
    if fetch_ok:
        try:
            subprocess.run(
                ["git", "fetch", "--quiet"],
                cwd=repo, capture_output=True, timeout=15
            )
        except Exception:
            pass
    try:
        r = subprocess.run(
            ["git", "rev-list", "HEAD..origin/HEAD", "--count"],
            cwd=repo, capture_output=True, text=True, timeout=10
        )
        s = r.stdout.strip()
        return int(s) if s.isdigit() else -1
    except Exception:
        return -1


def _auto_plugin_dirs() -> list[Path]:
    """Auto-detect plugin git repos under ~/.claude/plugins/ and ~/.claude/skills/.

    Returns:
        List of immediate subdirectories that contain a .git directory.
    """
    base = _cfg_mod.HOME / ".claude"
    candidates = []
    for subdir in ["plugins", "skills"]:
        d = base / subdir
        if not d.is_dir():
            continue
        for child in d.iterdir():
            if child.is_dir() and (child / ".git").exists():
                candidates.append(child)
    return candidates


# ── Public API ────────────────────────────────────────────────────────────────

def plugin_check() -> list[PluginStatus]:
    """Check configured plugin git repos for upstream updates.

    Uses paths.plugin_dirs from config if set; otherwise auto-detects repos
    under ~/.claude/plugins/ and ~/.claude/skills/.  Does NOT pull — report only.

    Returns:
        List of PluginStatus dicts: {name, path, has_update, behind}.
    """
    configured = _cfg_mod._paths.get("plugin_dirs", [])
    if configured:
        dirs = [Path(str(d)).expanduser() for d in configured]
    else:
        dirs = _auto_plugin_dirs()

    results: list[PluginStatus] = []
    for d in dirs:
        if not d.exists():
            continue
        behind = _git_behind(d)
        results.append({
            "name": d.name,
            "path": str(d),
            "has_update": behind > 0,
            "behind": behind,
        })
    return results


def skill_health() -> SkillHealthResult:
    """Check skills for dead file references and usage staleness.

    Scans skill .md files for absolute paths that no longer exist (dead refs).
    Cross-references a gstack analytics JSONL file for last-used timestamps.

    Returns:
        SkillHealthResult dict: {dead_refs, unused_30d, usage_top5}.
    """
    skill_dirs = [
        Path(str(d)).expanduser()
        for d in _cfg_mod._paths.get("skill_dirs", [str(_cfg_mod.HOME / ".claude" / "skills")])
    ]
    unused_days = int(_cfg_mod._behav.get("skill_unused_days", 30))

    dead_refs = _find_dead_refs(skill_dirs)
    last_used = _load_skill_usage()

    cutoff = datetime.now() - timedelta(days=unused_days)
    unused_30d: list[SkillUsage] = [
        {"skill": s, "last_used": t.strftime("%Y-%m-%d")}
        for s, t in last_used.items()
        if t < cutoff
    ]
    usage_top5_raw = sorted(last_used.items(), key=lambda x: x[1], reverse=True)[:5]
    usage_top5: list[SkillUsage] = [
        {"skill": s, "last_used": t.strftime("%Y-%m-%d")} for s, t in usage_top5_raw
    ]

    return {
        "dead_refs": dead_refs,
        "unused_30d": unused_30d,
        "usage_top5": usage_top5,
    }


def _find_dead_refs(skill_dirs: list[Path]) -> list[DeadRef]:
    """Scan skill .md files for absolute file paths that no longer exist.

    Only inspects the first 80 lines of each file (frontmatter / config area).

    Args:
        skill_dirs: List of directories containing skill Markdown files.

    Returns:
        List of DeadRef dicts: {skill, ref}.
    """
    dead_refs: list[DeadRef] = []
    path_re = re.compile(r'["\s]((?:[~/]|[A-Za-z]:)[^\s"\'*${}<>]+\.(?:md|py|sh|ts|js|json))')
    for skill_dir in skill_dirs:
        if not skill_dir.is_dir():
            continue
        for md in skill_dir.glob("*.md"):
            try:
                lines = md.read_text(encoding="utf-8", errors="ignore").splitlines()
                text = "\n".join(lines[:80])
            except Exception:
                continue
            seen_in_file: set[str] = set()
            for match in path_re.finditer(text):
                ref_str = match.group(1)
                ref = Path(ref_str).expanduser()
                if ref.is_absolute() and not ref.exists() and ref_str not in seen_in_file:
                    seen_in_file.add(ref_str)
                    dead_refs.append({"skill": md.name, "ref": str(ref)})
    return dead_refs


def _load_skill_usage() -> dict[str, datetime]:
    """Load per-skill last-used timestamps from the gstack analytics JSONL file.

    Returns:
        Dict mapping skill name → most recent usage datetime.
    """
    usage_log = _cfg_mod.HOME / ".gstack" / "analytics" / "skill-usage.jsonl"
    last_used: dict[str, datetime] = {}
    if not usage_log.exists():
        return last_used
    try:
        for line in usage_log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                skill = obj.get("skill", "")
                ts = obj.get("ts", "")
                if skill and ts:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
                    if skill not in last_used or dt > last_used[skill]:
                        last_used[skill] = dt
            except Exception:
                continue
    except Exception:
        pass
    return last_used
