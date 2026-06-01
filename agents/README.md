# agents/

Custom subclasses of harbor's `ClaudeCode` agent — one per third-party
Anthropic-compatible endpoint (Zhipu GLM, DeepSeek, MiniMax).

## Why this directory exists

The official `claude-code` agent in harbor talks to Anthropic's API directly.
Several providers (Zhipu, DeepSeek, MiniMax, …) now offer an
**Anthropic-compatible** `/v1/messages` endpoint, so the same `claude` CLI can
target them by overriding `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` (managed
via [agent_configs/snippets/](../agent_configs/snippets/)).

However, those endpoints have small protocol-level quirks (e.g. returning
`None` instead of omitting a key in the `usage` dict) that crash harbor's
upstream `_build_metrics`. So we keep a thin subclass per provider that:

1. Sets its own `name()` so harbor's registry, jobs paths, and snippet
   resolution can tell them apart.
2. Overrides `_build_metrics()` to tolerate provider-specific quirks.

Behavior is otherwise identical to `harbor.agents.installed.claude_code.ClaudeCode`.

## Files

| File | Provider | Endpoint |
|---|---|---|
| [claude_code_glm.py](claude_code_glm.py) | Zhipu BigModel | `https://open.bigmodel.cn/api/anthropic` |
| [claude_code_ds.py](claude_code_ds.py) | DeepSeek | `https://api.deepseek.com/anthropic` |
| [claude_code_minimax.py](claude_code_minimax.py) | MiniMax | `https://api.minimaxi.com/anthropic` |
| `__init__.py` | (empty package marker) | — |

Credentials and base URLs for each provider live in their corresponding
`agent_configs/snippets/<agent>.snippet` file, not here.

## How harbor loads them

[`tools/runner/run_variants.py`](../tools/runner/run_variants.py) registers
them in a dict and passes the `module:class` path to harbor at runtime:

```python
_CUSTOM_AGENTS = {
    "claude-code-glm":     "agents.claude_code_glm:ClaudeCodeGLM",
    "claude-code-ds":      "agents.claude_code_ds:ClaudeCodeDS",
    "claude-code-minimax": "agents.claude_code_minimax:ClaudeCodeMinimax",
}
```

```bash
harbor run --agent-import-path agents.claude_code_glm:ClaudeCodeGLM ...
```

`--agent-import-path` is a first-class harbor feature (see
`harbor/cli/trials.py`, `cli/jobs.py`, `cli/tasks.py`).

The runner sets `PYTHONPATH=<PROJECT_ROOT>` so harbor's subprocess can
`import agents.claude_code_glm` from this directory.

## Adding a new provider

To plug in another Anthropic-compatible endpoint (say, `foo`):

1. Copy any existing file (e.g. `claude_code_glm.py`) to
   `claude_code_foo.py` and adjust the class name + `name()` return value.
2. If `foo`'s API has additional quirks beyond `None` in `usage`, extend
   `_build_metrics()` accordingly.
3. Register in `_CUSTOM_AGENTS` inside
   [`tools/runner/run_variants.py`](../tools/runner/run_variants.py):
   ```python
   "claude-code-foo": "agents.claude_code_foo:ClaudeCodeFoo",
   ```
4. Create `agent_configs/snippets/claude-code-foo.snippet` with `foo`'s
   `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, and any model-mapping env
   (`ANTHROPIC_DEFAULT_OPUS_MODEL` etc.).
