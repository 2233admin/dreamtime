"""memory_keeper._config — Global configuration and path resolution.

Public API:
    _load_config(path)   — load config.yaml from standard search paths
    _reload_config(cfg)  — apply a loaded config dict to all module globals
    _p(key, default)     — resolve a path value from _paths dict
    _auto_detect_memory_dir() — locate the most-recently-modified memory dir
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


# ── Config loader ──────────────────────────────────────────────────────────────

def _load_config(path: Path | None = None) -> dict:
    """Load config.yaml from standard locations (falls back to empty dict).

    Search order (first found wins):
        1. ~/.claude/memory-store/config.yaml  (new canonical location)
        2. <scripts>/config.yaml               (legacy location)
        3. ~/.config/memory-keeper/config.yaml (XDG fallback)
    """
    candidates = [path] if path else [
        Path.home() / ".claude" / "memory-store" / "config.yaml",
        Path(__file__).parent.parent / "config.yaml",
        Path.home() / ".config/memory-keeper/config.yaml",
    ]
    for c in candidates:
        if c and c.exists():
            if yaml is None:
                raise ImportError("PyYAML required: pip install PyYAML")
            return yaml.safe_load(c.read_text(encoding="utf-8")) or {}
    return {}


def _auto_detect_memory_dir() -> Path:
    """Find Claude Code's memory dir by scanning ~/.claude/projects/*/memory/MEMORY.md."""
    for p in sorted(
        (Path.home() / ".claude/projects").glob("*/memory/MEMORY.md"),
        key=lambda x: x.stat().st_mtime, reverse=True
    ):
        return p.parent
    return Path.home() / ".claude/projects/default/memory"


_cfg: dict = _load_config()
_paths: dict = {}
_api: dict = {}
_behav: dict = {}

API_KEY: str = ""
API_BASE: str = ""
MODEL: str = ""
TODAY: str = ""
HOME: Path = Path.home()

# Runtime flags — set by parse_args() before any task runs
DRY_RUN: bool = False
VERBOSE: bool = False


def _p(key: str, default: Path | None) -> Path | None:
    """Resolve a path string from _paths dict, expanding ~ to HOME.

    Uses explicit ``if v else default`` to avoid treating Path('') as truthy.
    """
    v = _paths.get(key)
    if not v:
        return default
    return Path(str(v).replace("~", str(HOME)))


STORE_DIR: Path = Path()    # ~/.claude/memory-store/ — new output root
MEMORY_DIR: Path = Path()
MEMORY_FILE: Path = Path()
OBSIDIAN_DIR: Path = Path()
OMC_DIR: Path = Path()
INBOX: Path = Path()
CURSOR_FILE: Path = Path()
PENDING_RULES: Path = Path()
DREAMTIME_LOG: Path = Path()
ARCHIVE_DIR: Path = Path()
PROJECT_DIRS: list[Path] = []
OBSIDIAN_SKIP: set[str] = set()
OBSIDIAN_CATS: set[str] = set()
TRIM_THRESHOLD: int = 180
COLD_DAYS: int = 60
NEW_FILE_SKIP: int = 3
HOT_MINUTES: int = 5
DISTILL_MAXLINES: int = 150
OBS_WORKERS: int = 4
DIST_WORKERS: int = 4
SESSION_GLOB: str = "~/.claude/projects/**/*.jsonl"
LOCK_FILE: Path = Path()
PENDING_STORE: Path = Path()
MEMBLOCK_DIRS: list[Path] = []
ACCESS_LOG: Path = Path()


def _reload_config(cfg: dict) -> None:
    """Apply a loaded config dict to all module-level globals."""
    global _paths, _api, _behav, API_KEY, API_BASE, MODEL, TODAY, HOME
    global STORE_DIR, MEMORY_DIR, MEMORY_FILE, OBSIDIAN_DIR, OMC_DIR, INBOX, CURSOR_FILE
    global PENDING_RULES, DREAMTIME_LOG, ARCHIVE_DIR, PROJECT_DIRS
    global OBSIDIAN_SKIP, OBSIDIAN_CATS, TRIM_THRESHOLD, COLD_DAYS
    global NEW_FILE_SKIP, HOT_MINUTES, DISTILL_MAXLINES, OBS_WORKERS
    global DIST_WORKERS, LOCK_FILE, SESSION_GLOB, PENDING_STORE
    global MEMBLOCK_DIRS, ACCESS_LOG

    _paths = cfg.get("paths", {})
    _api = cfg.get("api", {})
    _behav = cfg.get("behavior", {})

    API_KEY = (
        os.environ.get("MK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ARK_KEY", "")
        or _api.get("api_key", "")
    )
    API_BASE = _api.get("base_url", "https://api.openai.com/v1")
    MODEL = _api.get("model", "gpt-4o-mini")
    TODAY = datetime.now().strftime("%Y-%m-%d")
    HOME = Path.home()

    _out = cfg.get("output", {})
    _out_base: str = _out.get("base_dir", "") or ""
    STORE_DIR = (
        _p("output_base_dir", None)
        or (Path(_out_base.replace("~", str(HOME))) if _out_base else None)
        or (HOME / ".claude" / "memory-store")
    )
    MEMORY_DIR = _p("memory_dir", None) or _auto_detect_memory_dir()
    MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
    OBSIDIAN_DIR = _p("obsidian_dir", HOME / "obsidian-vault") or (HOME / "obsidian-vault")
    OMC_DIR = _p("output_dir", HOME / ".omc") or (HOME / ".omc")
    INBOX = OMC_DIR / "inbox.md"
    CURSOR_FILE = OMC_DIR / "memory-keeper-cursor.json"
    PENDING_RULES = OMC_DIR / "pending-rules.md"
    DREAMTIME_LOG = OMC_DIR / "dreamtime-patterns.md"
    ARCHIVE_DIR = OBSIDIAN_DIR / "claude-code-backup"

    PROJECT_DIRS = [Path(str(d).replace("~", str(HOME))) for d in _paths.get("project_dirs", [])] \
                   or [HOME / "projects"]
    OBSIDIAN_SKIP = {"README.md", "CLAUDE.md", "AGENTS.md"}
    OBSIDIAN_CATS = {"decisions", "errors", "research", "projects", "daily"}

    def _int(key: str, default: int) -> int:
        try:
            return int(_behav.get(key, default))
        except (TypeError, ValueError):
            return default

    TRIM_THRESHOLD = _int("trim_threshold_lines", 180)
    COLD_DAYS = _int("cold_project_days", 60)
    NEW_FILE_SKIP = _int("new_file_skip_days", 3)
    HOT_MINUTES = _int("session_hot_minutes", 5)
    DISTILL_MAXLINES = _int("distill_max_lines", 150)
    OBS_WORKERS = _int("obsidian_workers", 4)
    DIST_WORKERS = _int("distill_workers", 4)

    SESSION_GLOB = _paths.get("session_glob", "~/.claude/projects/**/*.jsonl")

    LOCK_FILE = OMC_DIR / "memory-keeper.lock"
    PENDING_STORE = (
        _p("pending_store", None)
        or (STORE_DIR / "dreamtime" / "pending.jsonl")
    )

    # memblock
    global MEMBLOCK_DIRS, ACCESS_LOG
    raw_dirs = _paths.get("memblock_dirs", [])
    MEMBLOCK_DIRS = [Path(str(d).replace("~", str(HOME))) for d in raw_dirs] if raw_dirs else []
    ACCESS_LOG = STORE_DIR / "dreamtime" / "access.jsonl"


_reload_config(_cfg)
