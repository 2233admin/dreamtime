"""memory_keeper.adapters — Adapter layer for tool-neutral data access.

Public API:
    base            — Abstract interfaces (IKanbanAdapter, IMemoryAdapter, ISessionLogAdapter)
    claude_task     — ClaudeTaskAdapter: reads ~/.claude/tasks/**/*.json
    markdown_mem    — MarkdownMemoryAdapter: reads MEMORY.md, task-ID coverage check
    claude_log      — ClaudeLogAdapter: reads .claude/**/*.jsonl (wraps distill logic)
    obsidian        — ObsidianKanbanAdapter: skeleton (not implemented in MVP)
"""
from memory_keeper.adapters.base import (
    Task,
    Gap,
    IKanbanAdapter,
    IMemoryAdapter,
    ISessionLogAdapter,
)
from memory_keeper.adapters.claude_task import ClaudeTaskAdapter
from memory_keeper.adapters.markdown_mem import MarkdownMemoryAdapter
from memory_keeper.adapters.claude_log import ClaudeLogAdapter

__all__ = [
    "Task", "Gap",
    "IKanbanAdapter", "IMemoryAdapter", "ISessionLogAdapter",
    "ClaudeTaskAdapter", "MarkdownMemoryAdapter", "ClaudeLogAdapter",
]
