from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from memory_keeper.memblock import lint_frontmatter, parse_frontmatter


def test_parse_frontmatter_complete():
    text = """---
name: test block
description: a test
type: reference
limit: 4000
stale_after: 90
---

Body content here.
"""
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "test block"
    assert meta["type"] == "reference"
    assert meta["limit"] == 4000
    assert meta["stale_after"] == 90
    assert "Body content" in body


def test_parse_frontmatter_missing_optional():
    text = """---
name: minimal
description: bare minimum
type: user
---

Content.
"""
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "minimal"
    assert meta.get("limit") is None
    assert meta.get("stale_after") is None


def test_parse_frontmatter_no_frontmatter():
    text = "Just plain text, no frontmatter."
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert "plain text" in body


def test_lint_catches_missing_fields(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("---\nname: test\ndescription: ok\n---\nBody", encoding="utf-8")
    index = tmp_path / "MEMORY.md"
    index.write_text("# Index\n", encoding="utf-8")
    issues = lint_frontmatter(tmp_path)
    assert len(issues) == 1
    assert "type" in issues[0]["missing"]


def test_lint_catches_over_limit(tmp_path):
    content = "---\nname: big\ndescription: too big\ntype: project\nlimit: 50\n---\n" + "x" * 100
    (tmp_path / "big.md").write_text(content, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
    issues = lint_frontmatter(tmp_path)
    over = [issue for issue in issues if issue["issue"] == "over_limit"]
    assert len(over) == 1


def test_lint_clean_passes(tmp_path):
    content = "---\nname: ok\ndescription: fine\ntype: user\nlimit: 5000\n---\nShort body."
    (tmp_path / "ok.md").write_text(content, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
    issues = lint_frontmatter(tmp_path)
    assert issues == []


def test_git_init_creates_repo(tmp_path):
    from memory_keeper.memblock import git_init

    assert not (tmp_path / ".git").exists()
    result = git_init(tmp_path)
    assert result is True
    assert (tmp_path / ".git").exists()


def test_git_init_idempotent(tmp_path):
    from memory_keeper.memblock import git_init

    git_init(tmp_path)
    result = git_init(tmp_path)
    assert result is False


def test_git_commit_with_changes(tmp_path):
    from memory_keeper.memblock import git_commit, git_init

    git_init(tmp_path)
    (tmp_path / "test.md").write_text("hello", encoding="utf-8")
    committed = git_commit(tmp_path, "test commit")
    assert committed is True
    log = subprocess.check_output(
        ["git", "-C", str(tmp_path), "log", "--oneline"],
        text=True,
    ).strip()
    assert "test commit" in log


def test_git_commit_no_changes(tmp_path):
    from memory_keeper.memblock import git_commit, git_init

    git_init(tmp_path)
    (tmp_path / "test.md").write_text("hello", encoding="utf-8")
    git_commit(tmp_path, "first")
    committed = git_commit(tmp_path, "second")
    assert committed is False


def test_compact_compresses_over_limit(tmp_path):
    from memory_keeper.memblock import compact_over_limit

    body = "x" * 200
    content = f"---\nname: big\ndescription: test\ntype: project\nlimit: 100\n---\n{body}"
    f = tmp_path / "big.md"
    f.write_text(content, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Index\n", encoding="utf-8")

    with patch("memory_keeper.memblock.llm", return_value="compressed content here"):
        compacted = compact_over_limit(tmp_path)

    assert len(compacted) == 1
    assert compacted[0] == "big.md"
    new_text = f.read_text(encoding="utf-8")
    assert "compressed content here" in new_text


def test_compact_skips_under_limit(tmp_path):
    from memory_keeper.memblock import compact_over_limit

    content = "---\nname: ok\ndescription: test\ntype: project\nlimit: 5000\n---\nShort."
    (tmp_path / "ok.md").write_text(content, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
    compacted = compact_over_limit(tmp_path)
    assert compacted == []


def test_archive_moves_stale_files(tmp_path):
    from memory_keeper.memblock import archive_stale

    content = "---\nname: old\ndescription: old stuff\ntype: project\nstale_after: 0\n---\nOld body."
    (tmp_path / "old.md").write_text(content, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("- [old](old.md) — old stuff\n", encoding="utf-8")

    archived = archive_stale(tmp_path)
    assert "old.md" in archived
    assert not (tmp_path / "old.md").exists()
    assert (tmp_path / "archive" / "old.md").exists()


def test_archive_skips_fresh_files(tmp_path):
    from memory_keeper.memblock import archive_stale

    content = "---\nname: fresh\ndescription: new\ntype: project\nstale_after: 365\n---\nNew."
    (tmp_path / "fresh.md").write_text(content, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
    archived = archive_stale(tmp_path)
    assert archived == []
    assert (tmp_path / "fresh.md").exists()


def test_rebuild_index_removes_archived(tmp_path):
    from memory_keeper.memblock import rebuild_index

    (tmp_path / "alive.md").write_text(
        "---\nname: alive\ndescription: still here\ntype: user\n---\nBody.",
        encoding="utf-8",
    )
    index = tmp_path / "MEMORY.md"
    index.write_text(
        "# Memory\n- [alive](alive.md) — still here\n- [dead](dead.md) — gone\n",
        encoding="utf-8",
    )
    rebuild_index(tmp_path)
    text = index.read_text(encoding="utf-8")
    assert "alive" in text
    assert "dead" not in text


def test_rebuild_index_adds_unindexed(tmp_path):
    from memory_keeper.memblock import rebuild_index

    (tmp_path / "new.md").write_text(
        "---\nname: new thing\ndescription: just created\ntype: reference\n---\nBody.",
        encoding="utf-8",
    )
    index = tmp_path / "MEMORY.md"
    index.write_text("# Memory\n", encoding="utf-8")
    rebuild_index(tmp_path)
    text = index.read_text(encoding="utf-8")
    assert "new.md" in text
    assert "new thing" in text


def test_run_memblock_full_cycle(tmp_path):
    from memory_keeper.memblock import run_memblock

    content = "---\nname: test\ndescription: test block\ntype: user\nlimit: 5000\n---\nBody."
    (tmp_path / "test.md").write_text(content, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Memory\n- [test](test.md) — test block\n", encoding="utf-8")

    result = run_memblock(tmp_path)
    assert "issues" in result
    assert "compacted" in result
    assert "archived" in result
    assert (tmp_path / ".git").exists()


def test_reload_config_sets_memblock_globals(tmp_path):
    import memory_keeper._config as cfg_mod

    cfg = {
        "paths": {
            "memblock_dirs": [str(tmp_path / "mem-a"), str(tmp_path / "mem-b")],
            "output_base_dir": str(tmp_path / "store"),
        },
        "output": {"base_dir": str(tmp_path / "store")},
        "behavior": {},
        "api": {},
    }

    cfg_mod._reload_config(cfg)

    assert cfg_mod.MEMBLOCK_DIRS == [tmp_path / "mem-a", tmp_path / "mem-b"]
    assert cfg_mod.ACCESS_LOG == (tmp_path / "store" / "dreamtime" / "access.jsonl")


def test_run_memblock_dry_run_skips_git(tmp_path, monkeypatch):
    import memory_keeper._config as cfg_mod
    from memory_keeper.memblock import run_memblock

    content = "---\nname: test\ndescription: test block\ntype: user\nlimit: 5000\n---\nBody."
    (tmp_path / "test.md").write_text(content, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Memory\n- [test](test.md) — test block\n", encoding="utf-8")

    monkeypatch.setattr(cfg_mod, "DRY_RUN", True)
    result = run_memblock(tmp_path)

    assert "issues" in result
    assert not (tmp_path / ".git").exists()


def test_run_engine_memblock_uses_configured_dirs(tmp_path, monkeypatch):
    import memory_keeper._config as cfg_mod
    from memory_keeper.engine import run_engine

    mem_a = tmp_path / "mem-a"
    mem_b = tmp_path / "mem-b"
    mem_a.mkdir()
    mem_b.mkdir()

    monkeypatch.setattr(cfg_mod, "MEMBLOCK_DIRS", [mem_a])
    monkeypatch.setattr(cfg_mod, "MEMORY_DIR", mem_b)
    monkeypatch.setattr(cfg_mod, "ACCESS_LOG", tmp_path / "access.jsonl")
    monkeypatch.setattr(cfg_mod, "DRY_RUN", True)

    called: list[Path] = []

    def fake_run_memblock(path, access_log=None):
        called.append(path)
        return {"dir": str(path), "access_log": str(access_log)}

    monkeypatch.setattr("memory_keeper.memblock.run_memblock", fake_run_memblock)
    result = run_engine(only="memblock", since=datetime(2026, 4, 1))

    assert called == [mem_a, mem_b]
    assert len(result["memblock"]) == 2


def test_parse_args_accepts_memblock(monkeypatch):
    import memory_keeper

    monkeypatch.setattr(sys, "argv", ["memory_keeper", "--only", "memblock"])
    args = memory_keeper.parse_args()
    assert args.only == "memblock"


def test_track_read_records_memory_markdown(tmp_path, monkeypatch):
    from memory_keeper.dreamtime.hook import track_read
    import memory_keeper._config as _cfg_mod

    access_log = tmp_path / "dreamtime" / "access.jsonl"
    monkeypatch.setattr(_cfg_mod, "ACCESS_LOG", access_log)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "session-123")

    file_path = tmp_path / "project" / "memory" / "reference_letta_ai_research.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("content", encoding="utf-8")

    track_read(str(file_path))

    assert access_log.exists()
    record = json.loads(access_log.read_text(encoding="utf-8").strip())
    assert record["file"] == "reference_letta_ai_research.md"
    assert record["session"] == "session-123"


def test_track_read_skips_non_memory_or_index(tmp_path, monkeypatch):
    from memory_keeper.dreamtime.hook import track_read

    monkeypatch.setenv("MEMORY_KEEPER_STORE", str(tmp_path))

    non_memory = tmp_path / "project" / "docs" / "note.md"
    non_memory.parent.mkdir(parents=True, exist_ok=True)
    non_memory.write_text("content", encoding="utf-8")
    track_read(str(non_memory))

    memory_index = tmp_path / "project" / "memory" / "MEMORY.md"
    memory_index.parent.mkdir(parents=True, exist_ok=True)
    memory_index.write_text("content", encoding="utf-8")
    track_read(str(memory_index))

    access_log = tmp_path / "dreamtime" / "access.jsonl"
    assert not access_log.exists()


def test_session_start_prints_project_memory(monkeypatch, capsys):
    from memory_keeper.dreamtime.hook import session_start

    monkeypatch.setattr("memory_keeper.dreamtime.hook.load_active_preferences", lambda: ["pref A"])
    monkeypatch.setattr("memory_keeper.dreamtime.hook._project_name", lambda: "full-self-coding")
    monkeypatch.setattr(
        "memory_keeper.dreamtime.hook._find_project_memory",
        lambda project: "---\nname: fsc\n---\nFSC memory body",
    )

    session_start()
    out = capsys.readouterr().out

    assert "## 梦境 · 活跃偏好" in out
    assert "## 梦境 · 项目记忆 (full-self-coding)" in out
    assert "FSC memory body" in out


# ---------------------------------------------------------------------------
# Integration: track_read → _file_last_access → archive_stale (end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_track_read_prevents_archival(tmp_path, monkeypatch):
    """End-to-end: hook writes access.jsonl → memblock reads it → recently-read file survives archival.

    Setup: two files with mtime set 60 days ago, stale_after=30.
    Action: track_read on one file (writes access record with ts=now).
    Check with now=now+5d: read file (access 5d ago < 30) stays, unread file (mtime 65d ago > 30) archived.
    """
    import os as _os
    import time

    import memory_keeper._config as _cfg_mod
    from memory_keeper.dreamtime.hook import track_read
    from memory_keeper.memblock import archive_stale

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    read_file = memory_dir / "project_read.md"
    read_file.write_text(
        "---\nname: read project\ndescription: was read\ntype: project\nstale_after: 30\n---\nBody.",
        encoding="utf-8",
    )

    unread_file = memory_dir / "project_unread.md"
    unread_file.write_text(
        "---\nname: unread project\ndescription: never read\ntype: project\nstale_after: 30\n---\nBody.",
        encoding="utf-8",
    )

    (memory_dir / "MEMORY.md").write_text(
        "# Memory\n- [read](project_read.md)\n- [unread](project_unread.md)\n",
        encoding="utf-8",
    )

    # Backdate both files' mtime to 60 days ago
    old_ts = time.time() - 60 * 86400
    _os.utime(read_file, (old_ts, old_ts))
    _os.utime(unread_file, (old_ts, old_ts))

    # --- Step 1: hook writes access record for read_file (ts = now) ---
    access_log = tmp_path / "dreamtime" / "access.jsonl"
    monkeypatch.setattr(_cfg_mod, "ACCESS_LOG", access_log)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "integration-test-session")

    track_read(str(read_file))

    assert access_log.exists(), "track_read should have created access.jsonl"
    record = json.loads(access_log.read_text(encoding="utf-8").strip())
    assert record["file"] == "project_read.md"

    # --- Step 2: archive_stale with now = 5 days in future ---
    #   read_file: last_access from access.jsonl = now → age = 5 days < stale_after=30 → kept
    #   unread_file: last_access from mtime = 60 days ago → age = 65 days > stale_after=30 → archived
    future = datetime.now() + timedelta(days=5)
    archived = archive_stale(memory_dir, access_log=access_log, now=future)

    # --- Assert: unread file archived, read file survives ---
    assert "project_unread.md" in archived, "unread file should be archived"
    assert "project_read.md" not in archived, "recently-read file should NOT be archived"
    assert read_file.exists(), "read file should still be in memory_dir"
    assert not unread_file.exists(), "unread file should be moved to archive/"
    assert (memory_dir / "archive" / "project_unread.md").exists()


@pytest.mark.integration
def test_e2e_run_memblock_uses_access_log(tmp_path, monkeypatch):
    """End-to-end: run_memblock wires access_log to archive_stale.

    archive_stale runs BEFORE git_commit (pre-sweep snapshot), so mtime/access_log
    signals are not contaminated by the commit's git log timestamp. No git mocking needed.
    """
    import os as _os
    import time

    import memory_keeper._config as _cfg_mod
    from memory_keeper.dreamtime.hook import track_read
    from memory_keeper.memblock import run_memblock

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    stale_content = "---\nname: {name}\ndescription: {desc}\ntype: project\nstale_after: 30\n---\nBody."

    alive = memory_dir / "project_alive.md"
    alive.write_text(
        stale_content.format(name="alive", desc="has access"),
        encoding="utf-8",
    )
    dead = memory_dir / "project_dead.md"
    dead.write_text(
        stale_content.format(name="dead", desc="no access"),
        encoding="utf-8",
    )
    (memory_dir / "MEMORY.md").write_text(
        "# Memory\n- [alive](project_alive.md)\n- [dead](project_dead.md)\n",
        encoding="utf-8",
    )

    # Backdate both files 60 days so mtime-based staleness kicks in
    old_ts = time.time() - 60 * 86400
    _os.utime(alive, (old_ts, old_ts))
    _os.utime(dead, (old_ts, old_ts))

    # Hook records access for "alive" (ts = now)
    access_log = tmp_path / "dreamtime" / "access.jsonl"
    monkeypatch.setattr(_cfg_mod, "ACCESS_LOG", access_log)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "e2e-memblock")
    track_read(str(alive))

    result = run_memblock(memory_dir, access_log=access_log)

    assert "project_dead.md" in result["archived"]
    assert "project_alive.md" not in result["archived"]
    assert alive.exists()
    assert not dead.exists()
