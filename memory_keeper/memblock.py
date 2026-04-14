"""memory_keeper.memblock - memory lifecycle helpers."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from memory_keeper._utils import _atomic_write, llm

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)
_REQUIRED_FIELDS = {"name", "description", "type"}
_VALID_TYPES = {"user", "feedback", "project", "reference"}
_DEFAULT_STALE_AFTER = 90


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse simple YAML frontmatter from markdown text."""
    match = _FM_RE.match(text)
    if not match:
        return {}, text

    raw_yaml, body = match.group(1), match.group(2)
    meta: dict[str, Any] = {}
    for line in raw_yaml.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        colon_idx = line.find(":")
        if colon_idx == -1:
            continue
        key = line[:colon_idx].strip()
        value = line[colon_idx + 1 :].strip()
        if key in {"limit", "stale_after"}:
            try:
                meta[key] = int(value)
            except (TypeError, ValueError):
                meta[key] = value
        else:
            meta[key] = value

    return meta, body


def lint_frontmatter(memory_dir: Path) -> list[dict[str, Any]]:
    """Check markdown files for frontmatter compliance."""
    issues: list[dict[str, Any]] = []
    for md in sorted(memory_dir.glob("*.md")):
        if md.name == "MEMORY.md":
            continue

        text = md.read_text(encoding="utf-8", errors="ignore")
        meta, body = parse_frontmatter(text)
        if not meta:
            issues.append(
                {"file": md.name, "issue": "no_frontmatter", "detail": "missing --- block"}
            )
            continue

        missing = _REQUIRED_FIELDS - set(meta.keys())
        if missing:
            issues.append({"file": md.name, "issue": "missing_fields", "missing": sorted(missing)})

        if meta.get("type") and meta["type"] not in _VALID_TYPES:
            issues.append({"file": md.name, "issue": "invalid_type", "detail": meta["type"]})

        limit = meta.get("limit")
        if limit and isinstance(limit, int) and len(body) > limit:
            issues.append(
                {
                    "file": md.name,
                    "issue": "over_limit",
                    "detail": f"{len(body)}/{limit} chars",
                }
            )

    return issues


def git_init(memory_dir: Path) -> bool:
    """Ensure a directory is initialized as a git repository."""
    if (memory_dir / ".git").exists():
        return False

    subprocess.run(["git", "init"], cwd=str(memory_dir), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "dreamtime@localhost"],
        cwd=str(memory_dir),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "dreamtime"],
        cwd=str(memory_dir),
        capture_output=True,
        check=True,
    )
    return True


def git_commit(memory_dir: Path, msg: str) -> bool:
    """Stage and commit all changes if any exist."""
    subprocess.run(["git", "add", "-A"], cwd=str(memory_dir), capture_output=True, check=True)
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(memory_dir),
        capture_output=True,
        check=False,
    )
    if status.returncode == 0:
        return False

    subprocess.run(
        ["git", "commit", "-m", msg, "--no-gpg-sign"],
        cwd=str(memory_dir),
        capture_output=True,
        check=True,
    )
    return True


def compact_over_limit(memory_dir: Path) -> list[str]:
    """Compact files whose body exceeds the configured limit."""
    compacted: list[str] = []
    for md in sorted(memory_dir.glob("*.md")):
        if md.name == "MEMORY.md":
            continue

        text = md.read_text(encoding="utf-8", errors="ignore")
        meta, body = parse_frontmatter(text)
        limit = meta.get("limit")
        if not limit or not isinstance(limit, int) or len(body) <= limit:
            continue

        target = int(limit * 0.85)
        compressed = llm(
            f"将以下内容压缩到 {target} 字符以内，保留所有关键信息和决策，删除冗余细节。"
            f"直接输出压缩后的内容，不要加解释。\n\n{body[: limit * 2]}",
            max_tokens=max(800, target // 2),
        )
        match = _FM_RE.match(text)
        frontmatter_block = text[: match.start(2)] if match else ""
        _atomic_write(md, frontmatter_block + compressed + "\n")
        compacted.append(md.name)

    return compacted


def _file_last_access(md: Path, access_log: Path | None = None) -> datetime | None:
    """Find the most recent access signal for a memory file."""
    last_access: datetime | None = None

    if access_log and access_log.exists():
        try:
            for line in access_log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("file") != md.name:
                    continue
                ts = datetime.fromisoformat(entry["ts"])
                if last_access is None or ts > last_access:
                    last_access = ts
        except Exception:
            pass

    try:
        ts_str = subprocess.check_output(
            ["git", "log", "-1", "--format=%cI", "--", md.name],
            cwd=str(md.parent),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if ts_str:
            git_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
            if last_access is None or git_ts > last_access:
                last_access = git_ts
    except Exception:
        pass

    if last_access is None:
        try:
            last_access = datetime.fromtimestamp(md.stat().st_mtime)
        except Exception:
            pass

    return last_access


def archive_stale(
    memory_dir: Path,
    access_log: Path | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Move stale memory files into archive/."""
    if now is None:
        now = datetime.now()

    archive_dir = memory_dir / "archive"
    archived: list[str] = []

    for md in sorted(memory_dir.glob("*.md")):
        if md.name == "MEMORY.md":
            continue

        text = md.read_text(encoding="utf-8", errors="ignore")
        meta, _ = parse_frontmatter(text)
        stale_after = meta.get("stale_after", _DEFAULT_STALE_AFTER)
        if not isinstance(stale_after, int):
            continue

        last_access = _file_last_access(md, access_log=access_log)
        if last_access is None:
            continue

        age_days = max(0, (now - last_access).days)
        if age_days < stale_after:
            continue

        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / md.name
        if dest.exists():
            dest = archive_dir / f"{md.stem}-{now.strftime('%Y%m%d')}{md.suffix}"
        shutil.move(str(md), str(dest))
        archived.append(md.name)

    return archived


def rebuild_index(memory_dir: Path) -> None:
    """Rebuild MEMORY.md, pruning dangling links and adding missing entries."""
    index_path = memory_dir / "MEMORY.md"
    if not index_path.exists():
        return

    existing_files = {md.name for md in memory_dir.glob("*.md") if md.name != "MEMORY.md"}
    lines = index_path.read_text(encoding="utf-8").splitlines()
    link_re = re.compile(r"\[([^\]]*)\]\(([^)]+\.md)\)")

    referenced: set[str] = set()
    kept_lines: list[str] = []
    for line in lines:
        links = link_re.findall(line)
        if not links:
            kept_lines.append(line)
            continue
        # Remove dangling links but keep valid ones in the same line
        patched = line
        has_valid = False
        for label, fname in links:
            if fname in existing_files:
                referenced.add(fname)
                has_valid = True
            else:
                # Strip the dead link (and surrounding separators like " | ")
                patched = patched.replace(f"[{label}]({fname})", "")
        if has_valid:
            # Clean up leftover separators
            patched = re.sub(r"\s*\|\s*\|\s*", " | ", patched)
            patched = re.sub(r"\s*\|\s*$", "", patched)
            kept_lines.append(patched.rstrip())

    for fname in sorted(existing_files - referenced):
        md_path = memory_dir / fname
        file_text = md_path.read_text(encoding="utf-8", errors="ignore")
        meta, _ = parse_frontmatter(file_text)
        name = meta.get("name", fname.removesuffix(".md"))
        desc = meta.get("description", "")
        kept_lines.append(f"- [{name}]({fname}) — {desc}" if desc else f"- [{name}]({fname})")

    _atomic_write(index_path, "\n".join(kept_lines) + "\n")


def run_memblock(
    memory_dir: Path,
    access_log: Path | None = None,
) -> dict[str, Any]:
    """Run the full memory lifecycle sweep for a directory."""
    import memory_keeper._config as _cfg_mod

    if not (memory_dir / "MEMORY.md").exists():
        return {"issues": [], "compacted": [], "archived": []}

    if _cfg_mod.DRY_RUN:
        return {
            "initialized": False,
            "issues": lint_frontmatter(memory_dir),
            "compacted": [],
            "archived": [],
        }

    initialized = git_init(memory_dir)
    issues = lint_frontmatter(memory_dir)
    # archive_stale must run BEFORE git_commit so that _file_last_access sees clean
    # mtime/access_log signals rather than the pre-sweep commit's "now" timestamp.
    archived = archive_stale(memory_dir, access_log=access_log)
    if archived:
        rebuild_index(memory_dir)
    # Pre-sweep snapshot: safety net captured after archival decisions but before compaction.
    git_commit(memory_dir, f"memblock: pre-sweep snapshot {datetime.now():%Y-%m-%d %H:%M}")
    compacted = compact_over_limit(memory_dir)
    git_commit(memory_dir, f"memblock: lifecycle sweep {datetime.now():%Y-%m-%d %H:%M}")
    return {
        "initialized": initialized,
        "issues": issues,
        "compacted": compacted,
        "archived": archived,
    }


def sweep_all_dirs(step_fn=None) -> list[dict[str, Any]]:
    """Sweep all configured memblock directories. Shared by engine.py and __init__.py."""
    import memory_keeper._config as _cfg_mod
    dirs: list[Path] = list(_cfg_mod.MEMBLOCK_DIRS)
    if _cfg_mod.MEMORY_DIR not in dirs:
        dirs.append(_cfg_mod.MEMORY_DIR)
    results: list[dict[str, Any]] = []
    for mdir in dirs:
        if not mdir.exists():
            continue
        if step_fn:
            r = step_fn("memblock", lambda d=mdir: run_memblock(d, access_log=_cfg_mod.ACCESS_LOG))
        else:
            r = run_memblock(mdir, access_log=_cfg_mod.ACCESS_LOG)
        if r:
            results.append(r)
    return results
