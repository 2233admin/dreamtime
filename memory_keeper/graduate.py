"""memory_keeper.graduate — Preference graduation + stale pruning.

Moves classified [accept]/[reject] items from pending-rules.md
to feedback-graduated.md, and prunes unchecked items older than 7 days.

Public API:
    graduate_preferences() -> dict[str, int]
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import memory_keeper._config as _cfg_mod
from memory_keeper._utils import _atomic_write

GRADUATED_PATH = Path.home() / ".claude" / "rules" / "common" / "feedback-graduated.md"
PRUNE_DAYS = 7
MIN_CONTENT_LEN = 15  # skip vague items like "对，继续" or "好的"

_CLASSIFIED_RE = re.compile(r"^- \[(accept|reject)\]\s+(.+)$")
_UNCHECKED_RE = re.compile(r"^- \[ \]\s+(?:\[\d{4}-\d{2}-\d{2}\]\s+)?(.+)$")
_DATE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")
_RULE_NUM_RE = re.compile(r"^(\d+)\.")

__all__ = ["graduate_preferences"]


def graduate_preferences() -> dict[str, int]:
    """Graduate classified preferences and prune stale unchecked items.

    Returns:
        dict with keys: graduated, pruned.
    """
    pending_path = _cfg_mod.PENDING_RULES
    if not pending_path.exists():
        return {"graduated": 0, "pruned": 0}

    lines = pending_path.read_text(encoding="utf-8").splitlines()

    # Load existing graduated content for dedup (lowercase)
    existing_lower: set[str] = set()
    if GRADUATED_PATH.exists():
        for line in GRADUATED_PATH.read_text(encoding="utf-8").splitlines():
            existing_lower.add(line.strip().lower())

    to_graduate: list[tuple[str, str]] = []
    keep_lines: list[str] = []
    pruned = 0
    now = datetime.now()

    for line in lines:
        # Classified items → graduate candidates
        m = _CLASSIFIED_RE.match(line)
        if m:
            label, content = m.group(1), m.group(2).strip()
            if len(content) < MIN_CONTENT_LEN:
                pruned += 1
                continue  # too vague
            if any(content.lower() in existing for existing in existing_lower):
                pruned += 1
                continue  # already graduated
            to_graduate.append((label, content))
            continue  # remove from pending

        # Unchecked items → prune if stale
        um = _UNCHECKED_RE.match(line)
        if um:
            dm = _DATE_RE.search(line)
            if dm:
                try:
                    item_date = datetime.strptime(dm.group(1), "%Y-%m-%d")
                    if (now - item_date).days > PRUNE_DAYS:
                        pruned += 1
                        continue
                except ValueError:
                    pass

        keep_lines.append(line)

    # Append graduated rules
    graduated = 0
    if to_graduate:
        grad_text = GRADUATED_PATH.read_text(encoding="utf-8") if GRADUATED_PATH.exists() else ""

        # Find highest existing rule number
        max_num = 0
        for gline in grad_text.splitlines():
            nm = _RULE_NUM_RE.match(gline.strip())
            if nm:
                max_num = max(max_num, int(nm.group(1)))

        new_rules: list[str] = []
        for label, content in to_graduate:
            max_num += 1
            prefix = "禁止" if label == "reject" else "偏好"
            new_rules.append(f"{max_num}. **{prefix}** — {content}")

        if new_rules:
            if not grad_text.endswith("\n"):
                grad_text += "\n"
            if "## 自动毕业" not in grad_text:
                grad_text += "\n## 自动毕业\n"
            grad_text += "\n".join(new_rules) + "\n"

            if not _cfg_mod.DRY_RUN:
                _atomic_write(GRADUATED_PATH, grad_text)
            graduated = len(new_rules)

    # Rewrite pending (remove graduated + pruned)
    if (graduated or pruned) and not _cfg_mod.DRY_RUN:
        cleaned = "\n".join(keep_lines)
        if cleaned and not cleaned.endswith("\n"):
            cleaned += "\n"
        _atomic_write(pending_path, cleaned)

    if graduated or pruned:
        print(f"  [graduate] {graduated} graduated, {pruned} pruned")

    return {"graduated": graduated, "pruned": pruned}
