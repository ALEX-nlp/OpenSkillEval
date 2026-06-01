# run_variants.py

Python batch variant runner.

## Prerequisites

Run from the project root (the script derives `PROJECT_ROOT` from its own location, so any CWD works, but the root is the most intuitive):

```bash
cd /path/to/OpenSkillEval
```

The `harbor` CLI is installed via pip into `.venv/`; no local harbor/ source directory is required.

## Basic usage

```bash
# single runner × single variant × 3 rounds
uv run python tools/runner/run_variants.py \
  --runner "claude-code|claude-opus-4-6|10" \
  --runs 3 --resume \
  "data-visualization|data-viz-anthropics|case-ai-evolution-timeline|force-using"

# multiple runners × multiple variants
uv run python tools/runner/run_variants.py \
  --runner "claude-code|claude-opus-4-6|10" \
  --runner "gemini-cli|google/gemini-3.1-pro-preview|5" \
  --runs 3 --resume \
  "data-visualization|data-viz-anthropics|case-ai-evolution-timeline|force-using" \
  "poster-generation|poster-generation-paper-poster|case-health-advocacy-redcross|force-using"

# read entries from a file
uv run python tools/runner/run_variants.py \
  --runner "claude-code|claude-opus-4-6|10" \
  --entries-file entries/data-visualization.txt --resume
```

## CLI arguments

| Argument | Description | Default |
|---|---|---|
| `entries` (positional) | Entry list `"family\|variant\|case[\|mode]"` | — |
| `--runner SPEC` | Runner spec `"agent\|model[\|parallel]"`, may be passed multiple times | — |
| `--jobs-name DIR` | Output subdirectory under the project root | `smoke_jobs/` |
| `--runs N` | Number of rounds per variant × runner | `3` |
| `--start-run N` | Round number to start from | `1` |
| `--parallel N` | Default per-runner concurrency (used when a runner doesn't specify one) | `3` |
| `--resume` | Skip tasks that already have `result.json` | `false` |
| `--entries-file FILE` | Read entries from a file, one per line | — |

### Entry format

```
family|variant|case[|mode]
```

- `family`: `data-visualization`, `poster-generation`, `ppt-generation`, `report-generation`, `web-design`
- `variant`: directory name under `tasks/{family}/variants/`
- `case`: directory name under `tasks/{family}/shared/cases/`
- `mode`: `force-using` (default), `no-force`, `no-skill`

`*-no-skills` variants must be paired with `mode=no-skill`, and vice versa. The runner validates this at startup and exits with an error on any invalid combination.

### Runner format

```
agent|model[|parallel]
```

- `agent`: the agent CLI name (`claude-code`, `gemini-cli`, `codex`, `kimi-cli`, plus custom `claude-code-glm` / `-ds` / `-minimax`)
- `model`: the model identifier (the literal string each backend expects, e.g. `claude-opus-4-6`, `gpt-5.5`, `GLM-5.1`, `deepseek-v4-pro[1m]`)
- `parallel`: this runner's concurrency (optional; falls back to `--parallel` when omitted)

## Output directory layout

```
{project-root}/{jobs-name}/{family}/{case}/{variant}/{mode}/{agent}-{model}/{run-id}/
  └── result.json    ← produced by harbor
```

## Signal handling

- **First Ctrl+C**: graceful shutdown — SIGINT to all harbor process groups → wait 30s → docker container cleanup → SIGTERM → wait 20s → SIGKILL
- **Second Ctrl+C**: immediately SIGKILL all process groups and exit

Timeouts can be tuned via environment variables:

```bash
TASK_INT_TIMEOUT=60 TASK_TERM_TIMEOUT=30 uv run python tools/runner/run_variants.py ...
```

## Environment variables and credentials

Environment variables forwarded by the runner into harbor / docker / the in-container agent are filtered through a **whitelist** (see `_INHERITED_ENV_KEYS` at the top of [run_variants.py](run_variants.py)). Credentials and provider routing must come from [agent_configs/snippets/](../../agent_configs/snippets/); other env vars from the parent shell (e.g. `OPENAI_API_KEY`) are **not** forwarded.

To add custom env vars (corp proxy, CA bundle, etc.) without editing source:

```bash
export OPENSKILLEVAL_EXTRA_ENV="MY_CORP_CA,INTERNAL_PROXY"
uv run python tools/runner/run_variants.py ...
```

## Logs

Run logs are written to `{project-root}/logs/run-variants-{timestamp}.log`. The terminal mirrors them in real time, each line tagged with `[variant|runner]`.
