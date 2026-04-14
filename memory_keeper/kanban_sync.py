"""memory_keeper.kanban_sync — Sync dreamtime + snapshot results to Obsidian Kanban.

Writes new task cards to 任务看板.md and generates a daily world-state
snapshot note under 05-Daily/.

Public API:
    run_kanban_sync(dream, snapshot, distill) → dict
"""
from __future__ import annotations

import json
from pathlib import Path

import memory_keeper._config as _cfg_mod
from memory_keeper import vault
from memory_keeper._utils import _atomic_write
from memory_keeper.adapters.obsidian import insert_cards_to_kanban

__all__ = ["run_kanban_sync"]

_KANBAN_REL = Path("00-Index") / "任务看板.md"
_WORLD_STATE_REL = Path("05-Daily")


def _kanban_path() -> Path:
    return _cfg_mod.OBSIDIAN_DIR / _KANBAN_REL


def _world_state_dir() -> Path:
    return _cfg_mod.OBSIDIAN_DIR / _WORLD_STATE_REL


def _load_health() -> list[dict]:
    """Load projects list from ~/.omc/project-health.json."""
    health_path = _cfg_mod.OMC_DIR / "project-health.json"
    if not health_path.exists():
        return []
    try:
        return json.loads(health_path.read_text(encoding="utf-8")).get("projects", [])
    except (json.JSONDecodeError, OSError):
        return []


def make_dreamtime_cards(dream: dict) -> list[str]:
    """Generate Kanban cards from a DreamResult dict.

    Produces:
      - One ``#open-question`` card for ``top_open_thread``
      - One ``#rule-candidate`` card per entry in ``rule_candidates``

    Args:
        dream: DreamResult dict as returned by tasks.dreamtime().

    Returns:
        List of ``- [ ] ...`` strings ready to insert into the kanban.
    """
    cards: list[str] = []

    thread = (dream.get("top_open_thread") or "").strip()
    if thread:
        cards.append(f"- [ ] [dreamtime] {thread} #open-question")

    for rule in dream.get("rule_candidates") or []:
        rule = (rule or "").strip()
        if rule:
            cards.append(f"- [ ] [规则候选] {rule} #rule-candidate")

    return cards


def make_snapshot_cards(projects: list[dict]) -> list[str]:
    """Generate Kanban cards for dirty git repos active in the last 7 days.

    Filters to repos with both uncommitted changes AND a recent commit (within
    7 days), to avoid flooding the backlog with stale repos.

    Args:
        projects: List of ProjectHealth dicts from project-health.json.

    Returns:
        One ``#infra`` card per recently-active repo with uncommitted changes.
    """
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    return [
        f"- [ ] [snapshot] git commit: {p['name']} #infra"
        for p in projects
        if p.get("dirty") and (p.get("last_commit") or "") >= cutoff
    ]


def write_world_state(
    dream: dict | None,
    distill: dict | None,
    projects: list[dict],
    today: str,
) -> Path:
    """Write a daily world-state snapshot to 05-Daily/world-state-YYYY-MM-DD.md.

    Args:
        dream:    DreamResult dict (may be None).
        distill:  DistillResult dict (may be None).
        projects: ProjectHealth list from project-health.json.
        today:    Date string YYYY-MM-DD.

    Returns:
        Path of the written file.
    """
    hot = sorted(
        [p for p in projects if p.get("last_commit")],
        key=lambda p: p.get("last_commit", ""),
        reverse=True,
    )[:15]
    dirty = [p for p in projects if p.get("dirty")]

    fm = vault.frontmatter(
        date=today,
        generated_by="memory-keeper",
        hot_projects=len(hot),
        pending_count=len(dirty),
    )

    lines: list[str] = [fm, "", f"# World State {today}", ""]

    # HOT projects table
    lines += ["## HOT 项目 (最近15个)", ""]
    if hot:
        lines += ["| 项目 | 最后提交 | 未提交 |", "|------|---------|--------|"]
        for p in hot:
            flag = "✓" if p.get("dirty") else ""
            lines.append(f"| {p['name']} | {p.get('last_commit', '—')} | {flag} |")
    else:
        lines.append("_无数据_")
    lines.append("")

    # PENDING dirty repos
    lines += ["## PENDING", ""]
    if dirty:
        for p in dirty:
            lines.append(f"- [ ] git commit: {p['name']} #infra")
    else:
        lines.append("_无待提交仓库_")
    lines.append("")

    # Dreamtime insights
    if dream:
        lines += ["## Dreamtime 洞察", ""]
        for key, label in [
            ("user_patterns", "用户模式"),
            ("project_connections", "项目关联"),
            ("memory_health", "记忆健康"),
            ("top_open_thread", "开放问题"),
        ]:
            val = (dream.get(key) or "").strip()
            if val:
                lines.append(f"**{label}**: {val}")
        rules = [r for r in (dream.get("rule_candidates") or []) if r]
        if rules:
            lines += ["", "规则候选:"] + [f"- {r}" for r in rules]
        lines.append("")

    # Today's distill summary
    if distill:
        lines += [
            "## 今日提炼",
            "",
            f"- sessions: {distill.get('sessions', 0)}",
            f"- 决策: {len(distill.get('decisions', []))}",
            f"- 踩坑: {len(distill.get('gotchas', []))}",
            "",
        ]

    lines += ["---", "[[00-Index/任务看板]]", ""]

    out_path = _world_state_dir() / f"world-state-{today}.md"
    _atomic_write(out_path, "\n".join(lines))
    return out_path


def run_kanban_sync(
    dream: dict | None = None,
    snapshot: dict | None = None,
    distill: dict | None = None,
) -> dict:
    """Main entry: generate cards → insert into kanban → write world-state.

    Args:
        dream:    DreamResult dict (optional — only dreamtime cards if present).
        snapshot: SnapshotResult dict (unused; per-repo data from health.json).
        distill:  DistillResult dict (optional — summary in world-state only).

    Returns:
        dict: {inserted, skipped, world_state_path, cards}
    """
    today = _cfg_mod.TODAY
    projects = _load_health()

    cards: list[str] = []
    if dream:
        cards += make_dreamtime_cards(dream)
    cards += make_snapshot_cards(projects)

    inserted = insert_cards_to_kanban(
        _kanban_path(), cards, dry_run=_cfg_mod.DRY_RUN
    )

    ws_path = write_world_state(dream, distill, projects, today)
    # _atomic_write already prints dry-run message; no duplicate print needed here.

    return {
        "inserted": inserted,
        "skipped": len(cards) - inserted,
        "world_state_path": str(ws_path),
        "cards": cards,
    }
