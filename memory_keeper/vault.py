"""memory_keeper.vault — Obsidian/Logseq/Markdown vault format helpers.

Generates Obsidian-native Markdown: YAML frontmatter, callouts, wiki links,
task items, and Dataview inline fields.  No third-party dependencies required.

Public API:
    frontmatter(**props)            — YAML frontmatter block
    callout(kind, title, body)      — Obsidian callout block
    wiki_link(target, alias)        — [[target]] or [[target|alias]]
    task_item(text, done)           — - [ ] / - [x] task line
    tag(name)                       — #tag inline tag
    daily_note_link(date)           — [[YYYY-MM-DD]]
    dataview_field(key, value)      — key:: value inline Dataview field
    format_distill_note(...)        — decisions/gotchas Obsidian note
    format_inbox(...)               — full inbox Obsidian note
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

__all__ = [
    "frontmatter",
    "callout",
    "wiki_link",
    "task_item",
    "tag",
    "daily_note_link",
    "dataview_field",
    "format_distill_note",
    "format_inbox",
]

# ── Low-level primitives ───────────────────────────────────────────────────────

_YAML_SPECIAL = (':', '#', '[', ']', '{', '}', '&', '*', '!', '|', '>', "'", '"')


def frontmatter(**props: Any) -> str:
    """Build a YAML frontmatter block.

    Supports str, int, bool, list values.  Strings containing YAML special
    characters are double-quoted automatically.

    Example:
        frontmatter(title="Inbox", tags=["daily", "mk"], date="2026-01-01")
        →
        ---
        title: Inbox
        tags:
          - daily
          - mk
        date: 2026-01-01
        ---
    """
    lines = ["---"]
    for key, val in props.items():
        if val is None:
            continue
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
        elif isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        else:
            s = str(val)
            if any(c in s for c in _YAML_SPECIAL):
                s = '"' + s.replace('"', '\\"') + '"'
            lines.append(f"{key}: {s}")
    lines.append("---")
    return "\n".join(lines)


def callout(
    kind: str,
    title: str = "",
    body: str = "",
    foldable: bool = False,
    collapsed: bool = False,
) -> str:
    """Generate an Obsidian callout block.

    Supported kinds: note, info, tip, success, warning, danger, question,
    bug, example, quote, abstract, summary, tldr, todo, important, caution,
    failure, error, missing.

    Args:
        kind:      Callout type (case-insensitive, Obsidian normalises it).
        title:     Callout header text.
        body:      Body content, may contain newlines.
        foldable:  Add fold toggle (default: expanded).
        collapsed: Fold toggle collapsed by default (implies foldable=True).

    Example:
        callout("warning", "Risk", "Untested path")
        →
        > [!warning] Risk
        > Untested path
    """
    fold = "-" if collapsed else ("+" if foldable else "")
    header = f"> [!{kind}]{fold}"
    if title:
        header += f" {title}"
    if not body:
        return header
    body_text = body if isinstance(body, str) else str(body)
    body_lines = "\n".join(
        f"> {line}" if line.strip() else ">"
        for line in body_text.splitlines()
    )
    return f"{header}\n{body_lines}"


def wiki_link(target: str, alias: str | None = None) -> str:
    """Generate an Obsidian wiki link.

    Args:
        target: Note name or vault-relative path (no extension needed).
        alias:  Optional display text shown instead of target.

    Example:
        wiki_link("MEMORY")               → [[MEMORY]]
        wiki_link("MEMORY", "My Index")   → [[MEMORY|My Index]]
    """
    if alias:
        return f"[[{target}|{alias}]]"
    return f"[[{target}]]"


def task_item(text: str, done: bool = False) -> str:
    """Generate a GFM / Obsidian task checkbox line.

    Args:
        text: Task description text.
        done: True → [x] completed, False → [ ] open.
    """
    mark = "x" if done else " "
    return f"- [{mark}] {text}"


def tag(name: str) -> str:
    """Return an inline #tag string.

    Args:
        name: Tag name without the leading #.
    """
    return f"#{name}"


def daily_note_link(d: date | datetime | str | None = None) -> str:
    """Return a wiki link to an Obsidian Daily Note.

    Args:
        d: Target date.  Defaults to today.

    Example:
        daily_note_link()             → [[2026-03-29]]
        daily_note_link("2026-01-01") → [[2026-01-01]]
    """
    if d is None:
        d = date.today()
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, str):
        return f"[[{d}]]"
    return f"[[{d.isoformat()}]]"


def dataview_field(key: str, value: str) -> str:
    """Return a Dataview inline field ``key:: value``.

    Used for Dataview queries like ``WHERE source = "memory-keeper"``.

    Example:
        dataview_field("source", "memory-keeper") → "source:: memory-keeper"
    """
    return f"{key}:: {value}"


# ── High-level note builders ───────────────────────────────────────────────────

def format_distill_note(
    kind: str,
    items: list[str],
    today: str,
) -> str:
    """Build an Obsidian-native decisions or gotchas note.

    Each item is rendered as a callout for visual scanning in Obsidian.

    Args:
        kind:  "decisions" or "gotchas".
        items: Distilled strings from this session run.
        today: Date string YYYY-MM-DD for frontmatter and heading.

    Returns:
        Complete Obsidian note content as string.
    """
    _title_map = {"decisions": "Decisions", "gotchas": "Gotchas"}
    _callout_map = {"decisions": "tip", "gotchas": "warning"}

    human_title = _title_map.get(kind, kind.capitalize())
    callout_kind = _callout_map.get(kind, "note")

    fm = frontmatter(
        title=f"Distilled {human_title} {today}",
        date=today,
        tags=["memory-keeper", kind, "distilled"],
        source="memory-keeper",
        type=kind,
    )
    heading = f"# Distilled {human_title} {today}\n"
    body_blocks = []
    for item in items:
        body_blocks.append(callout(callout_kind, "", item))

    return fm + "\n\n" + heading + "\n" + "\n\n".join(body_blocks) + "\n"


def format_inbox(
    today: str,
    new_projects: list[str],
    trim_result: str,
    organized: list[str],
    distill: dict,
    dream: dict,
    plugin_result: list | None = None,
    skill_result: dict | None = None,
    claude_tasks: dict | None = None,
    todo_result: dict | None = None,
) -> str:
    """Build a full Obsidian-native inbox note with frontmatter and callouts.

    Args:
        today:         Date string YYYY-MM-DD.
        new_projects:  Names of newly discovered projects.
        trim_result:   Human-readable result of trim_memory().
        organized:     Move messages from organize_obsidian().
        distill:       Output of distill_sessions().
        dream:         Output of dreamtime().
        plugin_result: Output of plugin_check(); None to omit section.
        skill_result:  Output of skill_health(); None to omit section.
        claude_tasks:  Output of claude_task_summary(); None to omit section.
        todo_result:   Output of todo_scan(); None to omit section.

    Returns:
        Full inbox.md content as string.
    """
    fm = frontmatter(
        title=f"Memory Keeper Inbox {today}",
        date=today,
        tags=["memory-keeper", "inbox", "daily"],
        source="memory-keeper",
        created=today,
    )
    parts: list[str] = [fm, f"# Memory Keeper Inbox {today}\n"]

    # ── New projects ──────────────────────────────────────────────────────────
    if new_projects:
        body = "\n".join(f"- {wiki_link(p)}" for p in new_projects)
        parts.append(callout("note", f"新项目 ({len(new_projects)})", body))

    # ── Memory health ─────────────────────────────────────────────────────────
    mem_lines = [f"trim: {trim_result}"]
    non_trivial = [m for m in organized if m not in ("无散落文件",) and not m.startswith("（")]
    if non_trivial:
        mem_lines.append("整理: " + ", ".join(non_trivial[:5]))
    parts.append(callout("info", "MEMORY.md", "\n".join(mem_lines)))

    # ── Distillation ──────────────────────────────────────────────────────────
    if distill.get("sessions"):
        n_s = distill["sessions"]
        n_a = len(distill.get("actions", []))
        n_d = len(distill.get("decisions", []))
        n_g = len(distill.get("gotchas", []))
        dist_lines = [f"{n_s} sessions → {n_a} actions, {n_d} decisions, {n_g} gotchas"]
        if distill.get("preferences"):
            dist_lines.append(
                f"偏好候选 {len(distill['preferences'])} 条 → "
                f"{wiki_link('pending-rules', 'pending-rules.md')}"
            )
        if distill.get("updated_projects"):
            proj_links = ", ".join(wiki_link(p) for p in distill["updated_projects"])
            dist_lines.append(f"回写: {proj_links}")
        parts.append(callout("success", "对话提炼", "\n".join(dist_lines)))

    # ── Dreamtime ─────────────────────────────────────────────────────────────
    dt_lines: list[str] = []
    if dream.get("user_patterns"):
        dt_lines.append(f"用户模式: {dream['user_patterns']}")
    if dream.get("project_connections"):
        dt_lines.append(f"项目关联: {dream['project_connections']}")
    dt_lines.append(f"记忆健康: {dream.get('memory_health', '')}")
    if dream.get("top_open_thread"):
        dt_lines.append(f"开放问题: {dream['top_open_thread']}")
    parts.append(callout("question", "Dreamtime", "\n".join(dt_lines), foldable=True))

    for rc in dream.get("rule_candidates") or []:
        parts.append(task_item(f"规则候选: \"{rc}\""))

    # ── Plugin updates ────────────────────────────────────────────────────────
    if plugin_result is not None:
        plug_lines: list[str] = []
        has_updates = False
        for p in plugin_result:
            if p.get("has_update"):
                plug_lines.append(f"- {p['name']}: {p['behind']} commits behind")
                has_updates = True
            else:
                plug_lines.append(f"- {p['name']}: up to date")
        if not plugin_result:
            plug_lines.append("无插件配置")
        parts.append(callout("warning" if has_updates else "info",
                             "插件更新", "\n".join(plug_lines)))

    # ── Skill health ──────────────────────────────────────────────────────────
    if skill_result is not None:
        sk_lines: list[str] = []
        dead = skill_result.get("dead_refs", [])
        unused = skill_result.get("unused_30d", [])
        if dead:
            sk_lines.append(f"死引用 ({len(dead)}):")
            for r in dead[:3]:
                sk_lines.append(f"  - {r['skill']}: {r['ref']}")
        if unused:
            sk_lines.append(f"30天未用 ({len(unused)}):")
            for u in unused[:3]:
                sk_lines.append(f"  - {u['skill']}: last {u['last_used']}")
        if not dead and not unused:
            sk_lines.append("所有 skills 正常")
        parts.append(callout("warning" if dead else "info",
                             "Skill 健康", "\n".join(sk_lines)))

    # ── Claude Tasks ──────────────────────────────────────────────────────────
    if claude_tasks is not None:
        ct_lines: list[str] = []
        for t in claude_tasks.get("in_progress", []):
            ct_lines.append(task_item(f"[{t['id']}] {t['subject']}"))
        for t in claude_tasks.get("pending", [])[:5]:
            ct_lines.append(f"- [{t['id']}] {t['subject']}")
        extra = len(claude_tasks.get("pending", [])) - 5
        if extra > 0:
            ct_lines.append(f"- … {extra} more")
        if not ct_lines:
            ct_lines.append("暂无任务")
        total = claude_tasks.get("total", 0)
        parts.append(callout("note", f"Claude Tasks ({total} total)", "\n".join(ct_lines)))

    # ── TODO scan ─────────────────────────────────────────────────────────────
    if todo_result and todo_result.get("total", 0) > 0:
        td_lines = [f"总计 {todo_result['total']} 条"]
        by_proj = todo_result.get("by_project") or {}
        for proj, items in list(by_proj.items())[:5]:
            td_lines.append(f"- {wiki_link(proj)}: {len(items)} 条")
        remaining = len(by_proj) - 5
        if remaining > 0:
            td_lines.append(f"- … {remaining} 个项目")
        parts.append(callout("tip", "TODO 扫描", "\n".join(td_lines), foldable=True))

    return "\n\n".join(parts) + "\n"
