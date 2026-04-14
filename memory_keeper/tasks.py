"""memory_keeper.tasks — Core maintenance task functions.

Public API:
    discover_new_projects() — scan PROJECT_DIRS for new git repos; add to MEMORY.md
    trim_memory()           — archive cold lines from MEMORY.md when over threshold
    organize_obsidian()     — classify and move loose .md files into subdirectories
    distill_sessions(since, seen) — extract decisions/gotchas/preferences from JSONL
    dreamtime(distill_result)     — cross-session pattern synthesis via LLM
    write_inbox(...)        — write daily summary to ~/.omc/inbox.md
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict

import memory_keeper._config as _cfg_mod
from memory_keeper.vault import format_distill_note, format_inbox
from memory_keeper._utils import (
    _SeenHashes,
    _atomic_write,
    _git_last_commit,
    _memu_sync,
    _mtime,
    _recently_active_jsonl,
    llm,
    llm_json,
)

__all__ = [
    "discover_new_projects",
    "trim_memory",
    "_classify_file",
    "organize_obsidian",
    "_filter_jsonl",
    "_distill_one",
    "_project_file_map",
    "distill_sessions",
    "dreamtime",
    "write_inbox",
]


# ── TypedDicts ─────────────────────────────────────────────────────────────────

class DistillResult(TypedDict):
    sessions: int
    actions: list[dict]
    decisions: list[str]
    gotchas: list[str]
    preferences: list[str]
    updated_projects: list[str]
    max_mtime: datetime


class DreamResult(TypedDict):
    user_patterns: str | None
    project_connections: str | None
    memory_health: str
    top_open_thread: str | None
    rule_candidates: list[str]


# ── Task 1: Discover new projects ─────────────────────────────────────────────

def discover_new_projects() -> list[str]:
    """Scan PROJECT_DIRS for git repos absent from MEMORY.md; add one-liner entries.

    Returns:
        List of newly added project names.
    """
    memory_file = _cfg_mod.MEMORY_FILE
    if not memory_file.exists():
        return []
    known = set(re.findall(r"\*\*([^*]+)\*\*", memory_file.read_text(encoding="utf-8")))
    recent_cutoff = datetime.now() - timedelta(days=3)
    added: list[str] = []

    for base in _cfg_mod.PROJECT_DIRS:
        if not base.exists():
            continue
        for d in base.iterdir():
            if not d.is_dir() or not (d / ".git").exists():
                continue
            if d.name in known:
                continue
            last = _git_last_commit(d)
            if not last or last < recent_cutoff:
                continue

            ref = _read_project_ref(d)
            line = llm(
                f"为以下项目生成一行 MEMORY.md 条目，格式：\n"
                f"- **项目名**: `路径` → 一句话描述\n\n"
                f"项目名: {d.name}\n路径: {d}\n参考:\n{ref[:200]}\n\n只输出那一行。"
            )
            text = memory_file.read_text(encoding="utf-8")
            m = re.search(r"(## 项目.*?)(\n## )", text, re.DOTALL)
            if m:
                line_clean = next(
                    (ln for ln in line.splitlines() if ln.strip().startswith("- **")), ""
                )
                if not line_clean:
                    continue
                _atomic_write(memory_file, text[:m.end(1)] + f"\n{line_clean}" + text[m.end(1):])
                added.append(d.name)

    return added


def _read_project_ref(d: Path) -> str:
    """Read README or recent git log as context for LLM project description.

    Args:
        d: Repository root path.

    Returns:
        Up to 300-character reference string.
    """
    for fn in ["README.md", "README.txt"]:
        if (d / fn).exists():
            return (d / fn).read_text(encoding="utf-8", errors="ignore")[:300]
    try:
        return subprocess.check_output(
            ["git", "-C", str(d), "log", "-3", "--oneline"],
            stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        return ""


# ── Task 2: MEMORY.md trim ────────────────────────────────────────────────────

import math as _math


def _score_entry(line: str) -> float:
    """Score a MEMORY.md index line for retention priority.

    Formula: importance × confidence × recency_decay × open_loop_boost
    (access_boost and graph_centrality omitted — not reliably measurable from text alone)

    Returns:
        Float score; higher = more important to keep. Returns 1.0 for non-project lines
        (headers, blank lines, infrastructure entries without a git path).
    """
    # Non-bullet lines (headers, blank) always kept
    if not line.strip().startswith("- "):
        return 1.0

    # Importance: hot keywords signal core infrastructure or active project
    hot_keywords = {
        "hook", "infrastructure", "memory", "gitea", "openclaw", "cluster",
        "warehouse", "surplus", "shinkaevolve", "memu", "memory-keeper",
        "p_risk", "forge", "fsc", "full-self-coding", "kohaku", "pageindex",
        "gitnexus", "understand", "materialism",
    }
    cold_keywords = {
        "fish speech", "agent-s", "animelayer", "k-atana", "agentmouth",
        "openfang", "winforge", "agent-s", "amm", "chainmiku",
    }
    line_lower = line.lower()
    if any(k in line_lower for k in cold_keywords):
        importance = 0.4
    elif any(k in line_lower for k in hot_keywords):
        importance = 1.0
    else:
        importance = 0.7

    # Confidence: infer from markers in the line
    if "✅" in line or "完成" in line or "verified" in line.lower():
        confidence = 1.0
    elif "冷藏" in line or "待" in line or "pending" in line.lower():
        confidence = 0.6
    else:
        confidence = 0.8

    # Recency decay: look for git path and use last commit date
    recency_decay = 0.5  # default: unknown recency
    path_match = re.search(r"`([^`]+)`", line)
    if path_match:
        p = Path(path_match.group(1)).expanduser()
        if p.is_absolute() and p.exists() and (p / ".git").exists():
            last = _git_last_commit(p)
            if last:
                days_ago = (datetime.now() - last).days
                recency_decay = _math.exp(-days_ago / 60.0)
            else:
                recency_decay = 0.3
        else:
            recency_decay = 0.6  # non-git path (doc links, etc.)

    # Open loop boost
    open_loop_boost = 1.3 if re.search(r'TODO|待续|BLOCKED|待实施|pending', line, re.IGNORECASE) else 1.0

    return importance * confidence * recency_decay * open_loop_boost


def trim_memory() -> str:
    """Archive cold project lines from MEMORY.md using score-based prioritization.

    Lines with score < SCORE_DEMOTE threshold are archived; if still over budget
    after scoring, lowest-score lines are removed until within TRIM_THRESHOLD.
    Archived lines are written to ARCHIVE_DIR/memory-archive-DATE.md.

    Returns:
        Human-readable status string.
    """
    memory_file = _cfg_mod.MEMORY_FILE
    if not memory_file.exists():
        return "MEMORY.md 不存在"
    lines = memory_file.read_text(encoding="utf-8").splitlines()
    if len(lines) <= _cfg_mod.TRIM_THRESHOLD:
        return f"行数正常 ({len(lines)} 行)"

    # Score every line
    scored = [(line, _score_entry(line)) for line in lines]

    # Primary pass: archive score < 0.3 (clearly cold)
    SCORE_DEMOTE = 0.3
    to_archive = [line for line, score in scored if score < SCORE_DEMOTE]
    to_keep_scored = [(line, score) for line, score in scored if score >= SCORE_DEMOTE]

    # Secondary pass: if still over budget, remove lowest-score lines first
    budget = _cfg_mod.TRIM_THRESHOLD
    if len(to_keep_scored) > budget:
        # Sort by score ascending, trim from bottom (exclude header/blank lines score=1.0)
        sortable = [(i, line, score) for i, (line, score) in enumerate(to_keep_scored)
                    if line.strip().startswith("- ") and score < 1.0]
        sortable.sort(key=lambda x: x[2])
        excess = len(to_keep_scored) - budget
        demote_indices = {i for i, _, _ in sortable[:excess]}
        extra_archive = [line for i, (line, _) in enumerate(to_keep_scored) if i in demote_indices]
        to_archive.extend(extra_archive)
        to_keep_scored = [(line, score) for i, (line, score) in enumerate(to_keep_scored)
                          if i not in demote_indices]

    if not to_archive:
        return f"超 {_cfg_mod.TRIM_THRESHOLD} 行 ({len(lines)}) 但无可归档条目（评分均≥{SCORE_DEMOTE}）"

    archive_dir = _cfg_mod.ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        archive_dir / f"memory-archive-{_cfg_mod.TODAY}.md",
        f"# MEMORY.md 归档 {_cfg_mod.TODAY}\n\n" + "\n".join(to_archive)
    )
    to_keep = [line for line, _ in to_keep_scored]
    _atomic_write(memory_file, "\n".join(to_keep))
    return f"归档 {len(to_archive)} 条（评分<{SCORE_DEMOTE} 或超预算），剩余 {len(to_keep)} 行"


# ── Task 3: Obsidian file organization ────────────────────────────────────────

def _classify_file(f: Path) -> tuple[Path, str]:
    """LLM-classify a loose .md file into one of OBSIDIAN_CATS.

    Args:
        f: Path to the Markdown file.

    Returns:
        Tuple of (path, category_string).  Falls back to "daily" on error.
    """
    content = f.read_text(encoding="utf-8", errors="ignore")[:600]
    try:
        cat = llm(
            f"文件名: {f.name}\n内容:\n{content}\n\n"
            "分类到：decisions / errors / research / projects / daily\n只返回一个词。",
            max_tokens=10
        ).strip().lower().split()[0]
        if cat in _cfg_mod.OBSIDIAN_CATS:
            return f, cat
    except Exception:
        pass
    return f, "daily"


def organize_obsidian(min_age_days: int | None = None) -> list[str]:
    """Classify and move loose root-level .md files into category subdirectories.

    Args:
        min_age_days: Skip files newer than this many days (default: NEW_FILE_SKIP).

    Returns:
        List of human-readable move/skip messages.
    """
    if min_age_days is None:
        min_age_days = _cfg_mod.NEW_FILE_SKIP
    obsidian_dir = _cfg_mod.OBSIDIAN_DIR
    if not obsidian_dir.exists():
        return ["Obsidian 目录不存在"]

    cutoff = datetime.now() - timedelta(days=min_age_days)
    to_classify: list[Path] = []
    skipped_new = 0

    for f in obsidian_dir.glob("*.md"):
        if f.name in _cfg_mod.OBSIDIAN_SKIP:
            continue
        if _mtime(f) > cutoff:
            skipped_new += 1
            continue
        to_classify.append(f)

    if not to_classify:
        result = ["无散落文件"]
        if skipped_new:
            result.append(f"（{skipped_new} 个新文件跳过）")
        return result

    moved: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_cfg_mod.OBS_WORKERS) as pool:
        for f, cat in pool.map(_classify_file, to_classify):
            try:
                dest = obsidian_dir / cat / f.name
                if dest.exists():
                    dest = obsidian_dir / cat / f"{f.stem}-{_cfg_mod.TODAY}{f.suffix}"
                if _cfg_mod.DRY_RUN:
                    moved.append(f"[dry-run] {f.name} → {cat}/")
                else:
                    dest.parent.mkdir(exist_ok=True)
                    f.rename(dest)
                    moved.append(f"{f.name} → {cat}/")
            except Exception as e:
                moved.append(f"SKIP {f.name}: {e}")

    if skipped_new:
        moved.append(f"（跳过 {skipped_new} 个 {min_age_days} 天内新文件）")
    return moved


# ── Task 4: Session distillation ──────────────────────────────────────────────

_REDACT = re.compile(
    r'(api[_-]?key|secret|token|password|passwd|bearer|authorization)'
    r'(?:\s*[:=]\s*\S+|"\s*:\s*"[^"]{4,}")',
    re.IGNORECASE
)


def _filter_jsonl(path: Path, max_lines: int | None = None) -> str:
    """Extract and redact user/assistant messages from a JSONL session file.

    Args:
        path:      Path to the JSONL session file.
        max_lines: Maximum number of filtered lines to return (default: DISTILL_MAXLINES).

    Returns:
        Newline-joined filtered message strings.
    """
    if max_lines is None:
        max_lines = _cfg_mod.DISTILL_MAXLINES
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

    return "\n".join(kept[-max_lines:])


def _distill_one(path: Path) -> dict | None:
    """Distill a single JSONL session into structured knowledge via LLM.

    Args:
        path: Path to the JSONL session file.

    Returns:
        Parsed dict with keys: session_project, distilled_actions, decisions,
        gotchas, preferences — or None if the session is too short.
    """
    filtered = _filter_jsonl(path)
    if not filtered or len(filtered) < 100:
        return None
    return llm_json(
        f"从以下 Claude Code 对话提炼可持久化知识。\n\n{filtered}\n\n"
        '返回 JSON（无解释）:\n'
        '{"session_project":"项目名","distilled_actions":[{"summary":"做了什么","files":[],"outcome":"结果"}],'
        '"decisions":["决策+理由"],"gotchas":["踩坑"],"preferences":["偏好"]}\n'
        '内容不足则返回 {"empty":true}\n只保留已落地的行动。',
        max_tokens=800
    )


def _project_file_map() -> dict[str, Path]:
    """Build a mapping of project-name (lowercase) → project memory file path.

    Reads MEMORY.md and extracts **project** → [file.md] links.

    Returns:
        Dict of {lowercase_name: resolved_path} for existing files only.
    """
    memory_file = _cfg_mod.MEMORY_FILE
    memory_dir = _cfg_mod.MEMORY_DIR
    if not memory_file.exists():
        return {}
    pat = re.compile(r'\*\*([^*]+)\*\*[^[]*\[([^\]]+\.md)\]')
    return {
        m.group(1).strip().lower(): memory_dir / m.group(2)
        for m in pat.finditer(memory_file.read_text(encoding="utf-8"))
        if (memory_dir / m.group(2)).exists()
    }


def distill_sessions(since: datetime, seen: _SeenHashes | None = None) -> dict:
    """Distill all JSONL sessions modified after *since* into structured knowledge.

    In dry-run mode the function still classifies sessions via LLM but skips
    all file writes (handled downstream by _atomic_write).

    Args:
        since: Only process JSONL files with mtime after this datetime.
        seen:  Deduplication set; created fresh if not provided.

    Returns:
        DistillResult-shaped dict with keys: sessions, actions, decisions,
        gotchas, preferences, updated_projects, max_mtime.
    """
    import glob as _glob
    seen = seen or _SeenHashes()
    hot_files = _recently_active_jsonl(within_minutes=5)
    sg = _cfg_mod.SESSION_GLOB.replace("~", str(_cfg_mod.HOME))
    jsonl_files = [
        Path(x) for x in _glob.glob(sg, recursive=True)
        if "subagents" not in x and _mtime(Path(x)) > since and Path(x) not in hot_files
    ]
    if hot_files:
        print(f"    跳过 {len(hot_files)} 个活跃 session（5分钟内有写入）")
    empty_result: dict = {"sessions": 0, "actions": [], "decisions": [], "gotchas": [],
                          "preferences": [], "updated_projects": []}
    if not jsonl_files:
        return empty_result

    print(f"    {len(jsonl_files)} session files...")
    all_actions, all_decisions, all_gotchas, all_prefs = [], [], [], []
    max_mtime = since

    with concurrent.futures.ThreadPoolExecutor(max_workers=_cfg_mod.DIST_WORKERS) as pool:
        for path, result in zip(jsonl_files, pool.map(_distill_one, jsonl_files)):
            m = _mtime(path)
            if m > max_mtime:
                max_mtime = m
            if not result or result.get("empty"):
                continue
            session_project = result.get("session_project", "")
            actions = result.get("distilled_actions", [])
            for a in actions:
                a["_session_project"] = session_project
            all_actions.extend(actions)
            for item in result.get("decisions", []):
                if seen.add(item):
                    all_decisions.append(item)
            for item in result.get("gotchas", []):
                if seen.add(item):
                    all_gotchas.append(item)
            for item in result.get("preferences", []):
                if seen.add(item):
                    all_prefs.append(item)

    # Write to Obsidian (skipped automatically in dry-run by _atomic_write)
    _write_distill_to_obsidian(all_decisions, all_gotchas)

    # Write progress back to individual project memory files
    updated_projects = _write_project_progress(all_actions)

    # Persist preference candidates
    _write_pending_prefs(all_prefs)

    # Proposition-level audit (dreamtime rubric v1)
    # Feeds distill output (decisions + gotchas + preferences) to judge for scoring.
    # Does NOT re-extract from raw conversation — avoids doubling LLM calls.
    propositions: list = []
    distill_items = all_decisions + all_gotchas + all_prefs
    if distill_items:
        try:
            from memory_keeper.proposition import proposition_audit
            props = proposition_audit(distill_items, f"batch-{len(jsonl_files)}sessions")
            propositions.extend(props)
            if propositions:
                print(f"    proposition audit: {len(propositions)} 条命题写入 pending store")
        except Exception as exc:  # noqa: BLE001
            print(f"    [warn] proposition audit failed: {exc}")

    return {
        "sessions": len(jsonl_files),
        "actions": all_actions,
        "decisions": all_decisions,
        "gotchas": all_gotchas,
        "preferences": all_prefs,
        "updated_projects": updated_projects,
        "max_mtime": max_mtime,
        "propositions": propositions,
    }


def _write_distill_to_obsidian(
    all_decisions: list[str],
    all_gotchas: list[str],
) -> None:
    """Write today's distilled decisions and gotchas to Obsidian subdirectories.

    Args:
        all_decisions: Deduplicated decision strings.
        all_gotchas:   Deduplicated gotcha strings.
    """
    obsidian_dir = _cfg_mod.OBSIDIAN_DIR
    for items, subdir, kind in [
        (all_decisions, "decisions", "decisions"),
        (all_gotchas,   "errors",    "gotchas"),
    ]:
        if items:
            dest = obsidian_dir / subdir / f"distilled-{_cfg_mod.TODAY}.md"
            _atomic_write(dest, format_distill_note(kind, items, _cfg_mod.TODAY))


def _write_project_progress(all_actions: list[dict]) -> list[str]:
    """Append today's action summaries to matching project memory files.

    Args:
        all_actions: Action dicts with keys: _session_project, summary.

    Returns:
        List of project keys that were updated.
    """
    project_map = _project_file_map()
    updated_projects: list[str] = []
    by_project: dict[str, list[str]] = {}
    for a in all_actions:
        proj = (a.get("_session_project") or "").lower().strip()
        if proj and a.get("summary"):
            by_project.setdefault(proj, []).append(a["summary"])

    for proj_key, summaries in by_project.items():
        target = next(
            (fp for name, fp in project_map.items() if proj_key in name or name in proj_key),
            None
        )
        if not target:
            continue
        content = target.read_text(encoding="utf-8")
        if _cfg_mod.TODAY in content:
            continue
        _atomic_write(
            target,
            content + f"\n## 进展 {_cfg_mod.TODAY}\n" + "\n".join(f"- {s}" for s in summaries[:3]),
        )
        updated_projects.append(proj_key)

    return updated_projects


def _write_pending_prefs(all_prefs: list[str]) -> None:
    """Append new preference candidates to the pending-rules file.

    Args:
        all_prefs: Deduplicated preference strings from this run.
    """
    if not all_prefs:
        return
    _cfg_mod.OMC_DIR.mkdir(parents=True, exist_ok=True)
    header = "# 偏好规则候选\n\n待 review 后写入 feedback-graduated.md\n\n"
    existing = _cfg_mod.PENDING_RULES.read_text(encoding="utf-8") \
        if _cfg_mod.PENDING_RULES.exists() else header
    _atomic_write(
        _cfg_mod.PENDING_RULES,
        existing + "\n".join(f"- [ ] [{_cfg_mod.TODAY}] {p}" for p in all_prefs) + "\n",
    )


# ── Task 5: Dreamtime reflection ──────────────────────────────────────────────

def dreamtime(distill_result: dict) -> dict:
    """Cross-session pattern synthesis via LLM; accumulates insights over time.

    Reads active projects' recent commit messages and open threads from the
    memory directory, then asks the LLM to synthesize patterns, health status,
    and rule candidates.  New insights are appended (not overwritten) to
    DREAMTIME_LOG.

    Args:
        distill_result: Output of distill_sessions(); used for session/decision counts.

    Returns:
        DreamResult-shaped dict (keys: user_patterns, project_connections,
        memory_health, top_open_thread, rule_candidates).
    """
    memory_file = _cfg_mod.MEMORY_FILE
    memory_dir = _cfg_mod.MEMORY_DIR
    memory_lines = len(memory_file.read_text(encoding="utf-8").splitlines()) \
        if memory_file.exists() else 0
    memory_files = len(list(memory_dir.glob("*.md")))

    active = _collect_active_projects()
    open_threads = _collect_open_threads(memory_dir)
    stale = _collect_stale_projects()

    stale_summary = json.dumps(stale[:15], ensure_ascii=False) if stale else "无"

    result = llm_json(
        f"作为记忆维护 agent，分析以下上下文，返回 JSON。\n\n"
        f"活跃项目: {json.dumps(active[:10], ensure_ascii=False)}\n"
        f"今日提炼: {distill_result['sessions']}个session, "
        f"{len(distill_result['decisions'])}条决策, {len(distill_result['gotchas'])}条踩坑\n"
        f"记忆状态: MEMORY.md {memory_lines}行, {memory_files}个文件\n"
        f"开放问题: {open_threads}\n"
        f"停滞项目(7天无commit): {stale_summary}\n\n"
        '{"user_patterns":"洞察或null","project_connections":"关联或null",'
        '"memory_health":"状态一句话","top_open_thread":"最重要问题或null",'
        '"rule_candidates":["跨项目通用偏好"]}',
        max_tokens=400
    ) or {"memory_health": f"MEMORY.md {memory_lines}行，{memory_files}个文件"}
    result["_stale_projects"] = stale

    # Accumulate pattern insights — append, never overwrite
    patterns = result.get("user_patterns") or result.get("project_connections")
    if patterns and patterns not in ("null", None):
        _cfg_mod.OMC_DIR.mkdir(parents=True, exist_ok=True)
        header = "# Dreamtime 模式累积\n\n"
        existing = _cfg_mod.DREAMTIME_LOG.read_text(encoding="utf-8") \
            if _cfg_mod.DREAMTIME_LOG.exists() else header
        _atomic_write(_cfg_mod.DREAMTIME_LOG, existing + f"\n## {_cfg_mod.TODAY}\n- {patterns}\n")

    return result


def _collect_stale_projects(days: int | None = None) -> list[dict]:
    """Return git repos with no commit in the last N days.

    Only includes repos with last commit >= 2023 to exclude ancient forks/clones.
    Appends a restart_hint via a single batched LLM call.

    Args:
        days: Inactivity threshold; defaults to env DREAMTIME_PARK_DAYS or 14.

    Returns:
        List of dicts: name, last_commit, days_since, restart_hint.
    """
    import os as _os
    from datetime import timezone
    threshold = days if days is not None else int(_os.environ.get("DREAMTIME_PARK_DAYS", "14"))
    cutoff = datetime.now() - timedelta(days=threshold)
    min_year = 2023  # Exclude ancient forks
    stale: list[dict] = []
    for base in _cfg_mod.PROJECT_DIRS:
        if not base.exists():
            continue
        for d in base.iterdir():
            if not d.is_dir() or not (d / ".git").exists():
                continue
            try:
                ts_str = subprocess.check_output(
                    ["git", "-C", str(d), "log", "-1", "--format=%cI"],
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
                if not ts_str:
                    continue
                last = datetime.fromisoformat(ts_str).astimezone(timezone.utc).replace(tzinfo=None)
                if last.year < min_year or last >= cutoff:
                    continue
                # Skip GSD worktree task branches (fsc-task-N pattern)
                import re as _re
                if _re.search(r'-fsc-task-\d+$|-worktree-\d+$', d.name):
                    continue
                # Include last commit subject for LLM context
                subject = subprocess.check_output(
                    ["git", "-C", str(d), "log", "-1", "--format=%s"],
                    stderr=subprocess.DEVNULL, text=True
                ).strip()[:80]
                stale.append({
                    "name": d.name,
                    "last_commit": last.strftime("%Y-%m-%d"),
                    "days_since": (datetime.utcnow() - last).days,
                    "_subject": subject,
                    "restart_hint": "",
                })
            except Exception:
                pass
    stale.sort(key=lambda x: x["days_since"])
    if not stale:
        return stale

    # Batch LLM call: infer restart hints for all stale projects at once
    entries = [f'{p["name"]} (last: {p["_subject"]})' for p in stale]
    hints_raw = llm_json(
        "以下是一些停滞项目，根据项目名和最后一次commit推断一句话重启条件（中文，15字以内）。"
        "返回JSON: {\"项目名\": \"重启条件\", ...}\n\n"
        + "\n".join(f"- {e}" for e in entries),
        max_tokens=300
    ) or {}

    for p in stale:
        p["restart_hint"] = hints_raw.get(p["name"], "有明确需求时重启")
        del p["_subject"]

    return stale


def _collect_active_projects() -> list[dict]:
    """Return recent git commit summaries for active projects.

    Returns:
        List of dicts with keys: name, commits (up to 200 chars).
    """
    active: list[dict] = []
    for base in _cfg_mod.PROJECT_DIRS:
        if not base.exists():
            continue
        for d in base.iterdir():
            if not d.is_dir() or not (d / ".git").exists():
                continue
            try:
                log = subprocess.check_output(
                    ["git", "-C", str(d), "log", "-3", "--format=%s"],
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
                if log:
                    active.append({"name": d.name, "commits": log[:200]})
            except Exception:
                pass
    return active


def _collect_open_threads(memory_dir: Path) -> list[str]:
    """Scan memory .md files for TODO/待续/BLOCKED/待实施 markers.

    Args:
        memory_dir: Directory containing project memory Markdown files.

    Returns:
        Up to 5 matching line strings.
    """
    open_threads: list[str] = []
    _open_pat = re.compile(r'TODO|待续|BLOCKED|待实施')
    try:
        for md in memory_dir.glob("*.md"):
            for line in md.read_text(encoding="utf-8", errors="ignore").splitlines():
                if _open_pat.search(line):
                    open_threads.append(line.strip())
                    if len(open_threads) >= 5:
                        return open_threads
    except Exception:
        pass
    return open_threads


# ── Task 6: Write inbox ───────────────────────────────────────────────────────

def write_inbox(
    new_projects: list[str],
    trim_result: str,
    organized: list[str],
    distill: dict,
    dream: dict,
    plugin_result: list | None = None,
    skill_result: dict | None = None,
    claude_tasks: dict | None = None,
    todo_result: dict | None = None,
) -> None:
    """Write the daily summary report to ~/.omc/inbox.md.

    Args:
        new_projects:  Names of newly discovered projects.
        trim_result:   Human-readable result of trim_memory().
        organized:     Move messages from organize_obsidian().
        distill:       Output dict from distill_sessions().
        dream:         Output dict from dreamtime().
        plugin_result: Output list from plugin_check(), or None to skip section.
        skill_result:  Output dict from skill_health(), or None to skip section.
        claude_tasks:  Output dict from claude_task_summary(), or None to skip.
        todo_result:   Output dict from todo_scan(), or None to skip section.
    """
    _cfg_mod.OMC_DIR.mkdir(parents=True, exist_ok=True)
    content = format_inbox(
        today=_cfg_mod.TODAY,
        new_projects=new_projects,
        trim_result=trim_result,
        organized=organized,
        distill=distill,
        dream=dream,
        plugin_result=plugin_result,
        skill_result=skill_result,
        claude_tasks=claude_tasks,
        todo_result=todo_result,
    )
    _atomic_write(_cfg_mod.INBOX, content)


