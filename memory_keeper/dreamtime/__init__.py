"""梦境 (Dreamtime) — session-close memory distillation layer.

When a Claude Code session ends, this module:
1. Distills the recent session JSONL into decisions/gotchas/preferences
2. Synthesizes cross-session patterns via LLM
3. Writes tagged output to ~/.omc/inbox.md for human review

Entry point: python -m memory_keeper.dreamtime [--since-minutes N]
Also wired as a Claude Code Stop hook via `memory-keeper install-hook`.
"""

from memory_keeper.dreamtime.hook import run, install_hook

__all__ = ["run", "install_hook"]
