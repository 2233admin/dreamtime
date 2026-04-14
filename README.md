# dreamtime

**Session-close memory distillation for Claude Code.**

When a Claude Code session ends, dreamtime automatically reads the files you accessed and uses an LLM to distill key insights into your persistent memory store. No more re-explaining context at the start of every session.

## Why

Claude Code has no memory between sessions. Every conversation starts blank.
dreamtime fixes this: at session close, it runs a distillation pass and writes structured memories to `~/.claude/memory/`. The next session starts with context.

## Quick start

```bash
uvx dreamtime --install-hook
```

Requires [uv](https://docs.astral.sh/uv/) and an API key in `~/.claude/memory-store/config.yaml`.

## Configuration

```bash
cp config.yaml.example ~/.claude/memory-store/config.yaml
# Edit: set api.api_key to your key
# Any OpenAI-compatible endpoint works (Anthropic, OpenAI, local relay)
```

## How it works

1. **Stop hook** fires `uvx dreamtime --enqueue` at session close (async, non-blocking).
2. A background worker processes the queue and calls your LLM to distill insights.
3. Distilled memories land in `~/.claude/memory/` as Markdown files.
4. Future sessions load these memories automatically via `MEMORY.md`.

## Commands

```bash
uvx dreamtime --install-hook   # wire up the Stop hook
uvx dreamtime --enqueue        # manually trigger distillation
uvx dreamtime --status         # show queue and last run
```

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) installed
- Claude Code (or any tool that runs Stop hooks)
- An OpenAI-compatible API key

## License

MIT
