# memory-keeper

Session-close memory distillation for Claude Code.

At the end of every Claude Code session, memory-keeper reads the `.md` files you
accessed and uses an LLM to distill key insights into your persistent memory store.

## Quick start

```bash
# Install the Stop hook into ~/.claude/settings.json
uvx memory-keeper --install-hook
```

Requires [uv](https://docs.astral.sh/uv/) and an API key in `~/.claude/memory-store/config.yaml`.

## Configuration

Copy the example config and fill in your API key:

```bash
cp config.yaml.example ~/.claude/memory-store/config.yaml
```

Edit `config.yaml` — set `api.api_key` to your key.
Any OpenAI-compatible endpoint works (Anthropic, OpenAI, local relay).

## How it works

1. **Stop hook** fires `uvx memory-keeper --enqueue` at session close (async, non-blocking).
2. A background worker processes the queue and calls your LLM to distill insights.
3. Distilled memories land in `~/.claude/memory/` as Markdown files.

## License

MIT
