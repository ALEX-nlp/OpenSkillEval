# Dataviz Eval

Two-stage automated evaluation of agent-generated data-visualization PNGs: a stage1 dockerised eval agent computes data accuracy from the trajectory and source data, then a multi-model VLM judge scores three additional visual/insight dimensions.

## Architecture

```
pipeline.py
   |-- (per viz, up to --parallel)
   v
stage1_eval_agent.sh  --(uv run harbor run, docker)--> eval_report.json
   |-- (per judge spec, in parallel)
   v
judge_dataviz.sh --> code/vlm_judge_ext.py --> judge_result_<model>.json
   |
   v
pipeline.py merges --> pipeline_dataviz.jsonl
```

`pipeline.py` discovers `*/data-visualization/*/result.png` under the jobs root, spawns one `stage1_eval_agent.sh` per viz in its own process group, and each stage1 fans out to one `judge_dataviz.sh` per `--judge` spec. After all viz finish, the driver merges every `judge_result_*.json` into a single JSONL.

## Prerequisites

- Harbor jobs root populated with runs at `<JOBS_ROOT>/.../data-visualization/<case>/<variant>/<mode>/<runner>/<run>/<trial>/artifacts/result.png` and a sibling `../agent/trajectory.json`.
- Case fixtures at `<repo_root>/tasks/data-visualization/shared/cases/<case>/` containing `task_input.json`, `source_brief.md`, `source_data.json`.
- Judge credentials defined in `agent_configs/snippets/judge.snippet` as `ENV` entries referenced by `@VAR_NAME` placeholders in `--judge` specs.
- `docker` and `uv` installed and runnable by the invoking user.
- `harbor` available on `PATH` for the stage1 dockerised eval agent.

## Usage

```bash
uv run python eval/dataviz-eval/scripts/pipeline.py harbor/smoke_jobs
```

Resume: reusing the same `--run-id` skips viz that already have a `judge_result_<model>.json` for every requested judge, so re-running incrementally fills in missing results.

### Arguments

| Flag | Description | Default |
| --- | --- | --- |
| `jobs_root` | Harbor jobs root directory containing data-visualization runs to evaluate. | (required) |
| `parallel` | Positional: number of viz to evaluate concurrently. | `3` |
| `workers_per_model` | Positional: per-judge VLM API concurrency (rubric dimensions in parallel up to this cap). | `4` |
| `--judge MODEL\|PROVIDER\|BASE_URL\|API_KEY` | Judge spec, repeatable. `API_KEY` may be `@VAR_NAME` referencing an `ENV` entry in `agent_configs/snippets/judge.snippet`. | `claude-opus-4-6\|anthropic\|@ANTHROPIC_BASE_URL\|@ANTHROPIC_API_KEY` |
| `--run-id` | Output sub-directory name under `eval/dataviz-eval/output/`. Reusing the same ID resumes / incrementally evaluates. | `eval_result` |
| `--eval-agent` | Stage1 data-checking agent (`harbor --agent`). | `claude-code` |
| `--eval-model` | Model used by the stage1 eval agent. | model of first `--judge` |
| `--eval-timeout-mult` | `harbor --timeout-multiplier` for stage1. | `3.0` |

## Output

All artifacts land under `eval/dataviz-eval/output/<run-id>/`.

```
eval/dataviz-eval/output/<run-id>/
  pipeline_dataviz.jsonl                  # merged: one judge_result per line + metadata
  pipeline_<jobs_basename>.log            # driver log
  <task_family>/<case>/<variant>/<mode>/<runner>/<run>/<trial>/
    eval_report.json                      # stage1 data_accuracy report
    harbor.log                            # harbor run stdout/stderr
    stage1.log                            # stage1 wrapper log
    eval_agent/                           # stage1 agent working dir snapshot
    judge_result_<safe_model>.json        # one per --judge
```

## Scoring

| Dimension | Range | Source |
| --- | --- | --- |
| `insight_expression` | 1-5 | VLM judge — does the chart convey the goal insight |
| `data_accuracy` | 1-5 | stage1 `eval_report.json` (Docker, vs. `source_data.json`) |
| `visual_quality` | 1-5 | VLM judge — color, layout, labels, typography, finish |
| `completeness` | 1-5 | VLM judge — `task_input` requirements satisfied |

`overall = round(mean(4 dim scores), 2)`. The three VLM dimensions run in parallel via `ThreadPoolExecutor` with `--max-workers` (default `4`) and up to 3 retry rounds per dimension on failure. Default judge is `claude-opus-4-6` via Anthropic; additional `--judge` specs (Anthropic or OpenAI/Gemini-format) run concurrently per viz, each emitting its own `judge_result_<safe_model>.json`.

## Files

| Path | Role |
| --- | --- |
| [scripts/pipeline.py](scripts/pipeline.py) | Async pipeline orchestrator: discovers `result.png` files, dispatches stage1 per viz with concurrency cap, handles Ctrl+C cascade, merges all `judge_result_*.json` into `pipeline_dataviz.jsonl`. |
| [scripts/stage1_eval_agent.sh](scripts/stage1_eval_agent.sh) | Per-viz stage1: copies case data + agent trajectory into a Dockerfile context under `/tmp/dataviz_eval_task_*`, runs `uv run harbor run` to produce `eval_report.json`, then spawns one `judge_dataviz.sh` per judge model in parallel and cleans up compose resources on exit. |
| [scripts/judge_dataviz.sh](scripts/judge_dataviz.sh) | Single-viz VLM judge wrapper: parses metadata from PNG path, sets `ANTHROPIC`/`OPENAI` env, invokes `vlm_judge_ext.py` with viz + `task_input` + `source_brief` + `source_data` + `eval_report`, then injects metadata into the resulting JSON. |
| [code/vlm_judge_ext.py](code/vlm_judge_ext.py) | VLM-as-Judge engine: scores 4 dimensions 1-5; `data_accuracy` is read from the stage1 `eval_report.json` (not the VLM); the other 3 run in a `ThreadPoolExecutor` with up to 3 retry rounds; image auto-downscaled to `<=8000px` / 5MB b64. |

## Special workflow

Stage1 runs a second agent (`claude-code` by default) inside Docker via `uv run harbor run` to compute `data_accuracy` from the trajectory and `source_data.json` before the VLM judge is invoked. The host must have `docker` and `harbor` available.

The pipeline includes an aggressive Ctrl+C cleanup cascade to avoid leaking compose resources:

1. `SIGINT` to all stage1 process groups.
2. `docker compose down -v` plus container/image GC for any `working_dir` under `/tmp/dataviz_eval_task_*`.
3. `SIGTERM` for stragglers.
4. `SIGKILL` as a last resort.

The pipeline sets `REPO_ROOT_OVERRIDE` so symlinked or bind-mounted repos resolve the correct `tasks/` directory. Judge credentials come exclusively from `agent_configs/snippets/judge.snippet` via `@VAR_NAME` placeholders that `stage1_eval_agent.sh` expands at runtime before exporting them into the judge subprocess environment.
