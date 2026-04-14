"""梦境 Stop hook — project-aware session-close distillation.

Called automatically when a Claude Code session ends.
Can also be triggered manually:
    py -3.11 -m memory_keeper.dreamtime --since-minutes 480
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import string
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path


def _project_name() -> str:
    """Infer project name from $CLAUDE_PROJECT_ID, $PWD, or fallback."""
    # Claude Code sets this on session close
    proj_id = os.environ.get("CLAUDE_PROJECT_ID", "")
    if proj_id:
        # Project IDs look like "C--Users-Administrator--projects--foo" → "foo"
        parts = proj_id.replace("--", "/").split("/")
        if parts:
            return parts[-1]

    cwd = Path(os.environ.get("PWD", os.getcwd()))
    # Walk up to find a git root with a recognizable name
    for p in [cwd] + list(cwd.parents)[:3]:
        if (p / ".git").exists():
            return p.name

    return cwd.name or "unknown"


def _obsidian_inbox() -> Path:
    """Obsidian inbox dir — override with DREAMTIME_INBOX env var."""
    custom = os.environ.get("DREAMTIME_INBOX")
    if custom:
        return Path(custom)
    return Path.home() / "梦境"


def _events_jsonl() -> Path:
    """Shard by day: events/YYYY-MM-DD.jsonl."""
    today = datetime.now().strftime("%Y-%m-%d")
    events_dir = _store_dir() / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    return events_dir / f"{today}.jsonl"


def _append_jsonl_locked(path: Path, records: list[dict]) -> None:
    """Append records as JSON lines with an exclusive file lock."""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _access_log_path() -> Path:
    """Path to the access tracking JSONL file — uses _config.ACCESS_LOG for consistency."""
    try:
        from memory_keeper._config import ACCESS_LOG
        if ACCESS_LOG and str(ACCESS_LOG) != ".":
            ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
            return ACCESS_LOG
    except Exception:
        pass
    # fallback for standalone invocation
    d = _store_dir() / "events"
    d.mkdir(parents=True, exist_ok=True)
    return _store_dir() / "access.jsonl"


def track_read(file_path: str) -> None:
    """Record a memory file read event to access.jsonl."""
    p = Path(file_path)
    if p.suffix != ".md":
        return
    if "memory" not in str(p.parent).lower() and ".memory" not in str(p.parent):
        return
    if p.name == "MEMORY.md":
        return
    record = {
        "ts": datetime.now().isoformat(),
        "file": p.name,
        "dir": str(p.parent),
        "session": os.environ.get("CLAUDE_SESSION_ID", ""),
    }
    _append_jsonl_locked(_access_log_path(), [record])


def _write_events(project: str, distill: dict) -> None:
    """Append structured events to daily shard (locked)."""
    ts = datetime.now().isoformat()
    half_life = {"decision": 90, "pitfall": 60, "preference": 180}
    entries: list[dict] = []
    for d in distill.get("decisions", []):
        entries.append({"project": project, "type": "decision", "content": d,
                        "ts": ts, "half_life_days": half_life["decision"]})
    for g in distill.get("gotchas", []):
        entries.append({"project": project, "type": "pitfall", "content": g,
                        "ts": ts, "half_life_days": half_life["pitfall"]})
    for pref in distill.get("preferences", []):
        entries.append({"project": project, "type": "preference", "content": pref,
                        "ts": ts, "half_life_days": half_life["preference"]})
    for prop in distill.get("propositions", []):
        p = prop if isinstance(prop, dict) else (prop.to_dict() if hasattr(prop, 'to_dict') else {"content": str(prop)})
        entries.append({
            "ts": ts,
            "project": project,
            "type": "proposition",
            "sub_type": p.get("type", "unknown"),
            "content": p.get("content", ""),
            "score": p.get("total_score", 0),
            "status": p.get("status", "pending"),
        })
    if entries:
        path = _events_jsonl()
        _append_jsonl_locked(path, entries)
        print(f"[梦境] events → {path} (+{len(entries)})")


def _write_inbox(project: str, distill: dict, dream: dict) -> None:
    """Write human-readable session summary to Obsidian 梦境 folder."""
    _obsidian_inbox().mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M")
    inbox = _obsidian_inbox() / f"{today}-{project}.md"

    prefs = distill.get("preferences", [])
    rules = [r for r in dream.get("rule_candidates", []) if r and r != "null"]
    thread = dream.get("top_open_thread")
    health = dream.get("memory_health", "")
    stale = dream.get("_stale_projects", [])

    # Only write file if there's something worth reviewing
    needs_review = bool(prefs or rules or stale or (thread and thread not in ("null", None)))
    if not needs_review:
        print("[梦境] inbox 无需人工决策，跳过写入")
        return

    lines = [
        "---",
        f"project: {project}",
        f"date: {today}",
        f"sessions: {distill.get('sessions', 0)}",
        f"decisions: {len(distill.get('decisions', []))}",
        f"pitfalls: {len(distill.get('gotchas', []))}",
        f"propositions: {len(distill.get('propositions', []))}",
        "---",
        "",
        f"# 梦境 · {project} · {today} {now}",
        "",
    ]

    if health:
        lines += [f"> {health}", ""]

    if prefs:
        lines += ["## 偏好候选（需要你决定）", ""]
        for p in prefs[:5]:
            lines.append(f"- [ ] {p}")
        lines.append("")

    if rules:
        lines += ["## 规则候选（确认后毕业到 feedback-graduated.md）", ""]
        for r in rules[:3]:
            lines.append(f"- [ ] {r}")
        lines.append("")

    if thread and thread not in ("null", None):
        lines += ["## 最重要未解问题", "", f"{thread}", ""]

    if stale:
        lines += ["## 降级候选（7天无commit，建议PARKED）", ""]
        for p in stale[:10]:
            lines.append(
                f"- [ ] **{p['name']}** — 最后commit {p['last_commit']} "
                f"({p['days_since']}天前)，重启条件: {p.get('restart_hint', '___')}"
            )
        lines.append("")

    inbox.write_text("\n".join(lines), encoding="utf-8")
    print(f"[梦境] inbox → {inbox}")


def _store_dir() -> Path:
    """Base dreamtime store directory — override with MEMORY_KEEPER_STORE env var."""
    base = os.environ.get("MEMORY_KEEPER_STORE")
    if base:
        return Path(base) / "dreamtime"
    return Path.home() / ".claude" / "memory-store" / "dreamtime"


def load_active_preferences(
    store_dir: Path | None = None,
    now: datetime | None = None,
    top_n: int = 5,
) -> list[str]:
    """Load active preferences from events, applying half-life decay.

    Scans events/YYYY-MM-DD.jsonl (daily format) and events.jsonl (legacy).
    Applies exponential decay: score = exp(-ln(2) * age_days / half_life_days).
    Entries with score < 0.15 are excluded. half_life_days=None|0 means never expires.
    Deduplicates by content hash, keeping highest score per unique content.

    Args:
        store_dir: Base dreamtime store directory. Defaults to _store_dir().
        now: Reference time for age calculation. Defaults to datetime.now().
        top_n: Maximum preferences to return.

    Returns:
        Top-N preference strings sorted by score descending. [] if none found.
    """
    if store_dir is None:
        store_dir = _store_dir()
    if now is None:
        now = datetime.now()

    # Collect JSONL files: daily shards first, then legacy monolith
    candidates: list[Path] = []
    events_dir = store_dir / "events"
    if events_dir.exists():
        candidates.extend(sorted(events_dir.glob("*.jsonl")))
    legacy = store_dir / "events.jsonl"
    if legacy.exists():
        candidates.append(legacy)

    if not candidates:
        return []

    # content_hash → (score, content) — keep highest score per unique content
    best: dict[str, tuple[float, str]] = {}

    for path in candidates:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "preference":
                continue

            content: str = entry.get("content", "")
            if not content:
                continue

            half_life: float | None = entry.get("half_life_days")
            ts_str: str = entry.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                ts = now  # treat as freshly written if timestamp is unparseable

            age_days = max((now - ts).total_seconds() / 86400.0, 0.0)

            if half_life is None or half_life == 0:
                score = 1.0  # pinned — never expires
            else:
                score = math.exp(-math.log(2) * age_days / half_life)

            if score < 0.15:
                continue

            key = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if key not in best or score > best[key][0]:
                best[key] = (score, content)

    if not best:
        return []

    ranked = sorted(best.values(), key=lambda x: x[0], reverse=True)
    return [content for _, content in ranked[:top_n]]


def session_start() -> None:
    """Print active preferences + project memory for SessionStart hook additionalContext.

    Claude Code captures stdout from SessionStart hooks as additionalContext,
    injecting it into the next conversation's system prompt.
    """
    prefs = load_active_preferences()
    if prefs:
        lines = ["## 梦境 · 活跃偏好", ""]
        for i, pref in enumerate(prefs, 1):
            lines.append(f"{i}. {pref}")
        lines.append("")
        print("\n".join(lines))

    project = _project_name()
    if project and project != "unknown":
        mem = _find_project_memory(project)
        if mem:
            print(f"## 梦境 · 项目记忆 ({project})\n")
            print(mem)
            print("")


def _find_project_memory(project: str, memory_dirs: list[Path] | None = None) -> str | None:
    """Find and read the most relevant project memory file."""
    if not project or project == "unknown":
        return None
    if memory_dirs is None:
        from memory_keeper._config import MEMORY_DIR
        memory_dirs = [MEMORY_DIR]
    project_lower = project.lower().replace("-", "").replace("_", "")
    if len(project_lower) < 3:
        return None
    def _strip(s: str) -> str:
        return s.lower().replace("-", "").replace("_", "").replace(" ", "")

    # Pass 1: exact match on frontmatter name or aliases field
    for mdir in memory_dirs:
        if not mdir.exists():
            continue
        for md in mdir.glob("*.md"):
            if md.name == "MEMORY.md":
                continue
            try:
                first_lines = md.read_text(encoding="utf-8", errors="ignore")[:500]
                names_to_check: list[str] = []
                for line in first_lines.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("name:"):
                        names_to_check.append(stripped.split(":", 1)[1].strip())
                    elif stripped.startswith("aliases:"):
                        # aliases: fsc, full-self-coding, FSC
                        raw = stripped.split(":", 1)[1].strip()
                        names_to_check.extend(a.strip() for a in raw.split(",") if a.strip())
                if any(_strip(n) == project_lower for n in names_to_check):
                    return md.read_text(encoding="utf-8", errors="ignore")[:2000]
            except Exception:
                pass
    # Pass 2: substring match on filename (min length 4 to avoid false positives)
    for mdir in memory_dirs:
        if not mdir.exists():
            continue
        for md in mdir.glob("*.md"):
            if md.name == "MEMORY.md":
                continue
            fname_lower = md.stem.lower().replace("-", "").replace("_", "")
            if len(fname_lower) >= 4 and (project_lower in fname_lower or fname_lower in project_lower):
                return md.read_text(encoding="utf-8", errors="ignore")[:2000]
    return None


# ---------------------------------------------------------------------------
# Queue helpers (P0-1)
# ---------------------------------------------------------------------------

def _queue_dir(sub: str) -> Path:
    """Return queue/<sub>/ dir, created on demand."""
    d = _store_dir() / "queue" / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def _random4() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def _worker_log() -> Path:
    log = _store_dir() / "queue" / "worker.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    return log


def _try_lock() -> bool:
    """Acquire processing lock. Returns False if another worker is running."""
    lock = _store_dir() / "queue" / "worker.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        if lock.exists():
            age = (datetime.now() - datetime.fromtimestamp(lock.stat().st_mtime)).total_seconds()
            if age < 600:  # 10 min — not stale
                return False
        lock.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        return False


def _release_lock() -> None:
    try:
        (_store_dir() / "queue" / "worker.lock").unlink(missing_ok=True)
    except Exception:
        pass


def _spawn_processor() -> None:
    """Spawn queue processor as detached subprocess (no timeout constraint)."""
    import subprocess
    log = _worker_log()
    scripts_dir = str(Path(__file__).resolve().parent.parent.parent)
    argv = [sys.executable, "-u", "-m", "memory_keeper.dreamtime", "--process-queue"]
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"\n--- spawn {datetime.now().isoformat()} pid={os.getpid()} ---\n")
        f.flush()
        kwargs = dict(stdout=f, stderr=subprocess.STDOUT, cwd=scripts_dir)
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)


def process_queue() -> int:
    """Process all pending jobs. Returns count processed."""
    if not _try_lock():
        print("[梦境] worker already running, skip")
        return 0
    try:
        pending_dir = _queue_dir("pending")
        jobs = sorted(pending_dir.glob("*.json"))
        if not jobs:
            print(f"[梦境] queue empty ({datetime.now().strftime('%H:%M:%S')})")
            return 0

        processed = 0
        for job_path in jobs:
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
                project = job.get("project", "")
                if project:
                    os.environ["CLAUDE_PROJECT_ID"] = project
                since_minutes = job.get("since_minutes", 480)
                print(f"[梦境] processing {job_path.name} (project={project})")
                run(since_minutes=since_minutes, job_path=job_path)
                processed += 1
            except Exception:
                pass  # run() handles dead-letter internally
        print(f"[梦境] queue done: {processed}/{len(jobs)}")
        return processed
    finally:
        _release_lock()


def handle_stop_event(since_minutes: int = 480) -> None:
    """Called by the Stop hook. Writes a job file, then spawns processor."""
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    fname = f"{ts}-{_random4()}.json"
    job = {
        "session_id": os.environ.get("CLAUDE_SESSION_ID", ""),
        "ts": datetime.now().isoformat(),
        "since_minutes": since_minutes,
        "project": _project_name(),
    }
    job_path = _queue_dir("pending") / fname
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[梦境] enqueued → {job_path}")
    _spawn_processor()


def run(since_minutes: int = 480, job_path: Path | None = None) -> None:
    """Distill recent session and synthesize patterns.

    Args:
        since_minutes: How many minutes back to scan. Default 480 (8 hours).
        job_path: Pending job file. Deleted on success; moved to dead/ on failure.
    """
    try:
        # Avoid circular imports — load lazily
        from memory_keeper._utils import _SeenHashes
        from memory_keeper.tasks import distill_sessions, dreamtime

        project = _project_name()
        since = datetime.now() - timedelta(minutes=since_minutes)

        print(f"[梦境] project={project}, since={since.strftime('%H:%M')} ({since_minutes}min ago)")

        seen = _SeenHashes()
        distill_result = distill_sessions(since=since, seen=seen)

        print(f"[梦境] distill: {distill_result['sessions']} sessions, "
              f"{len(distill_result['decisions'])} decisions, "
              f"{len(distill_result['gotchas'])} gotchas")

        dream_result = dreamtime(distill_result)
        props = distill_result.get("propositions", [])
        if props:
            print(f"[梦境] proposition audit: {len(props)} 条命题进入 pending store")
        _write_events(project, distill_result)
        _write_inbox(project, distill_result, dream_result)

        # Graduate classified preferences + prune stale pending
        try:
            from memory_keeper.graduate import graduate_preferences
            graduate_preferences()
        except Exception as exc:
            print(f"[梦境] graduate skipped: {exc}")

        if job_path and job_path.exists():
            job_path.unlink()

    except Exception:
        tb = traceback.format_exc()
        print(f"[梦境] ERROR:\n{tb}", file=sys.stderr)

        if job_path and job_path.exists():
            dead_dir = _queue_dir("dead")
            dead_path = dead_dir / job_path.name
            try:
                content = job_path.read_text(encoding="utf-8")
                dead_path.write_text(
                    content.rstrip() + f"\n\n// traceback\n{tb}",
                    encoding="utf-8",
                )
                job_path.unlink()
                print(f"[梦境] dead-letter → {dead_path}", file=sys.stderr)
            except Exception as move_err:
                print(f"[梦境] failed to move to dead-letter: {move_err}", file=sys.stderr)

        raise


def install_hook() -> None:
    """Inject the 梦境 Stop hook into ~/.claude/settings.json.

    Uses ``uvx memory-keeper`` so the hook works on any machine that has uv
    installed, without requiring a project-local venv or Anaconda.  The first
    invocation may be slightly slower while uv downloads and caches the package;
    subsequent calls are instant because uv caches the tool environment.
    """
    # Guard: uv must be installed for uvx to work.
    if not shutil.which("uvx"):
        print("[梦境] 'uvx' not found on PATH.")
        print("[梦境] Install uv first: https://docs.astral.sh/uv/getting-started/installation/")
        sys.exit(1)

    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        print(f"[梦境] settings.json not found at {settings_path}")
        sys.exit(1)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    # uvx is cross-platform and works once the package is published to PyPI.
    # async=True lets Claude Code continue without waiting for distillation.
    hook_command = "uvx memory-keeper --enqueue"
    hook_entry = {
        "type": "command",
        "command": hook_command,
        "timeout": 60000,
        "async": True,
    }

    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])

    # Check if already installed — match any form of memory_keeper invocation.
    for group in stop_hooks:
        for h in group.get("hooks", []):
            cmd = h.get("command", "")
            if "memory_keeper.dreamtime" in cmd or "memory-keeper" in cmd:
                print("[梦境] hook already installed")
                return

    stop_hooks.append({"hooks": [hook_entry]})
    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    print(f"[梦境] Stop hook installed → {settings_path}")
    print("[梦境] On each new machine, run: uvx memory-keeper --install-hook")


def _cli() -> None:
    p = argparse.ArgumentParser(description="梦境 — session-close memory distillation")
    p.add_argument("--since-minutes", type=int, default=480,
                   help="How many minutes back to scan (default: 480 = 8h)")
    p.add_argument("--install-hook", action="store_true",
                   help="Inject Stop hook into ~/.claude/settings.json")
    p.add_argument("--session-start", action="store_true",
                   help="Print active preferences to stdout (for SessionStart hook)")
    p.add_argument("--enqueue", action="store_true",
                   help="Write job file to queue/pending/ and exit (used by Stop hook)")
    p.add_argument("--process-queue", action="store_true",
                   help="Process all pending jobs in queue/pending/")
    p.add_argument("--track-read", metavar="FILE_PATH",
                   help="Record a memory file read (called by PostToolUse hook)")
    args = p.parse_args()

    if args.track_read:
        track_read(args.track_read)
    elif args.install_hook:
        install_hook()
    elif args.session_start:
        session_start()
    elif args.process_queue:
        process_queue()
    elif args.enqueue:
        handle_stop_event(since_minutes=args.since_minutes)
    else:
        run(since_minutes=args.since_minutes)


if __name__ == "__main__":
    _cli()
