"""memory_keeper.adapters.obsidian — ObsidianKanbanAdapter + kanban writer.

Two public surfaces:
  - ObsidianKanbanAdapter (skeleton): read completed tasks (not yet implemented)
  - insert_cards_to_kanban(): write new cards to an Obsidian Kanban board file
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from memory_keeper.adapters.base import IKanbanAdapter, Task


class ObsidianKanbanAdapter(IKanbanAdapter):
    """Reads completed tasks from an Obsidian kanban.md file.

    Status: Skeleton only — not implemented in MVP.
    Activate when: project directory contains kanban.md (Obsidian Kanban plugin).
    """

    def test(self, project_path: Path) -> bool:
        return (project_path / "kanban.md").exists()

    def fetch_done_tasks(self, project_path: Path, since: datetime) -> list[Task]:
        raise NotImplementedError(
            "ObsidianKanbanAdapter is not implemented in MVP. "
            "Use ClaudeTaskAdapter for ~/.claude/tasks/ based workflows."
        )


# ── Kanban writer ──────────────────────────────────────────────────────────────

def insert_cards_to_kanban(
    kanban_path: Path,
    cards: list[str],
    dry_run: bool = False,
) -> int:
    """Insert new cards at the top of ## Backlog, skipping duplicates.

    Deduplication is substring-based: if the card's core text (without the
    ``- [ ] `` prefix) already appears anywhere in the file, the card is
    skipped.  The ``%% kanban:settings %%`` block at the end of the file is
    preserved untouched.

    Args:
        kanban_path: Absolute path to the Obsidian kanban .md file.
        cards:       List of ``- [ ] text #tag`` strings to insert.
        dry_run:     If True, print a preview and return the count without writing.

    Returns:
        Number of cards actually inserted (0 if file absent or all duplicates).
    """
    if not kanban_path.exists() or not cards:
        return 0

    text = kanban_path.read_text(encoding="utf-8")

    # Only dedup against open tasks (- [ ]), not completed (- [x]) or free text.
    # A card completed → Done column must be re-insertable in future runs.
    open_tasks = {
        line.lstrip().removeprefix("- [ ] ").strip()
        for line in text.splitlines()
        if line.lstrip().startswith("- [ ]")
    }
    # Also dedup within the incoming batch (e.g. repeated rule_candidates).
    seen: set[str] = set()
    new_cards: list[str] = []
    for c in cards:
        key = c.removeprefix("- [ ] ").strip()
        if key not in open_tasks and key not in seen:
            seen.add(key)
            new_cards.append(c)

    if not new_cards:
        return 0

    if dry_run:
        print(f"  [dry-run] kanban: would insert {len(new_cards)} card(s):")
        for c in new_cards:
            print(f"    {c}")
        return len(new_cards)

    lines = text.splitlines(keepends=True)
    insert_idx: int | None = None
    for i, line in enumerate(lines):
        if line.rstrip() == "## Backlog":
            # Skip one blank line immediately after the heading (structural noise)
            next_i = i + 1
            if next_i < len(lines) and lines[next_i].strip() == "":
                next_i += 1
            insert_idx = next_i
            break

    if insert_idx is None:
        return 0

    card_lines = [c + "\n" for c in new_cards]
    # Ensure a blank line separates the inserted block from what follows.
    if insert_idx < len(lines) and lines[insert_idx].strip() != "":
        card_lines.append("\n")
    lines = lines[:insert_idx] + card_lines + lines[insert_idx:]

    from memory_keeper._utils import _atomic_write
    _atomic_write(kanban_path, "".join(lines))
    return len(new_cards)
