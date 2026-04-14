"""memory_keeper.adapters.markdown_mem — MarkdownMemoryAdapter.

Checks if completed tasks are mentioned in a MEMORY.md file.

Coverage strategy (in order):
    1. Exact task ID match:  "#87" found verbatim in MEMORY.md
    2. Keyword fallback:     2 meaningful words from title found in MEMORY.md
                             (filters out common Chinese verbs: 实现/完成/添加/修复/更新/优化/删除/重构)
"""
from __future__ import annotations

import re
from pathlib import Path

from memory_keeper.adapters.base import Gap, IMemoryAdapter, Task

# Common action verbs to skip in keyword fallback
_SKIP_WORDS: frozenset[str] = frozenset({
    "实现", "完成", "添加", "修复", "更新", "优化", "删除", "重构",
    "改进", "处理", "创建", "部署", "验证", "测试", "支持", "集成",
    "fix", "add", "update", "remove", "refactor", "create", "implement",
    "the", "and", "for", "with", "from", "into",
})

# Minimum meaningful word length
_MIN_WORD_LEN = 3


def _extract_keywords(title: str) -> list[str]:
    """Extract up to 2 meaningful keywords from a task title."""
    # Split on spaces and common punctuation
    words = re.split(r"[\s:：/\\，,。.]+", title)
    keywords = [
        w for w in words
        if len(w) >= _MIN_WORD_LEN and w.lower() not in _SKIP_WORDS
    ]
    return keywords[:2]


class MarkdownMemoryAdapter(IMemoryAdapter):
    """Checks task coverage in a MEMORY.md file.

    Preferred match: verbatim task ID (e.g., "#87").
    Fallback: 2 meaningful keywords from task title.
    """

    def test(self, project_path: Path) -> bool:
        """Return True if MEMORY.md exists in the project directory."""
        return (project_path / "MEMORY.md").exists()

    def check_coverage(self, project_path: Path, tasks: list[Task]) -> list[Gap]:
        """Return tasks NOT mentioned in MEMORY.md.

        Args:
            project_path: Directory containing MEMORY.md.
            tasks:        Completed tasks to check.

        Returns:
            List of Gap objects for uncovered tasks.
        """
        memory_file = project_path / "MEMORY.md"
        if not memory_file.exists():
            return [Gap(task=t, reason="MEMORY.md 不存在") for t in tasks]

        content = memory_file.read_text(encoding="utf-8", errors="ignore")
        gaps: list[Gap] = []

        for task in tasks:
            # Strategy 1: exact task ID match
            task_id_bare = task.id.lstrip("#")
            if re.search(rf"#\s*{re.escape(task_id_bare)}\b", content):
                continue

            # Strategy 2: keyword fallback
            keywords = _extract_keywords(task.title)
            if keywords and all(kw.lower() in content.lower() for kw in keywords):
                continue

            # Not found — report as gap
            if keywords:
                reason = (
                    f"任务 {task.id} 未见于 MEMORY.md "
                    f"（ID 未匹配，关键词 {keywords} 亦未出现）"
                )
            else:
                reason = f"任务 {task.id} 未见于 MEMORY.md（ID 未匹配，标题无可用关键词）"

            gaps.append(Gap(task=task, reason=reason))

        return gaps
