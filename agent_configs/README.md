# agent_configs/

Centralized config for every agent supported by OpenSkillEval. Edit once here
and the next task run picks up the change — **no Docker rebuild needed**.

## Layout

- `snippets/<agent>.snippet` — agent-specific environment variables (API keys,
  base URLs, model aliases). Files ship with `<your-…-api-key>` placeholders;
  fill in the relevant fields locally before running a task and **do not
  commit those edits**. Format uses Dockerfile-style `ENV KEY=VALUE` syntax;
  multi-line `\` continuations and quoted values are supported.

## How it's wired up

`tools/runner/run_variants.py` reads the relevant snippet for each task and
injects the variables into the harbor subprocess environment **at run time**:

```
agent_configs/snippets/<agent>.snippet
              │
              │  _parse_snippet_env()  →  {KEY: VALUE, ...}
              ▼
  harbor subprocess env (os.environ within harbor)
              │
              │  harbor.agents.<X>.run() reads ANTHROPIC_BASE_URL /
              │  OPENAI_API_KEY / GEMINI_API_KEY / ... from os.environ
              ▼
  docker exec -e KEY=VALUE  →  container sees the same env
```

**Security note**: API keys are *never* baked into Docker images. They are
passed via `docker exec -e` only when a task actually runs, and live only in
that subprocess's memory.

## Adding a new agent

1. Create `snippets/<agent>.snippet` with the `ENV` declarations the agent's
   harbor implementation reads (e.g. `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`
   for a Claude-style agent).
2. If it's a custom subclass (not vanilla `claude-code` / `codex` / `gemini-cli`
   / `kimi-cli`), register it in `tools/runner/run_variants.py`'s
   `_CUSTOM_AGENTS` dict.

## Editing keys / endpoints

Snippets ship with `<your-…-api-key>` placeholders. Replace with real
credentials before running a task. Real keys are **never** baked into the
Docker images — they're forwarded only via `docker exec -e` at run time, so
the only place they live is your edited file. Don't commit those edits.
