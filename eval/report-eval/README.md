# Report Eval

End-to-end evaluation for the report-generation task family: runs an in-Docker evaluator agent to code-verify data accuracy and fidelity against source data, then runs one or more VLM judges for content quality, visualization, and completeness on the generated HTML/PDF reports.

## Architecture

`pipeline.py` discovers `final_report.{html,pdf}` under `JOBS_ROOT`, dedupes per trial, and spawns one driver per report. Each driver runs the stage1 evaluator agent in Docker via `harbor`, then fans out to per-judge VLM scoring.

```
pipeline.py
  -> stage1_eval_agent.sh        (uv run harbor run -> eval_report.json)
       -> judge_single.sh × N    (one per --judge spec)
            -> code/vlm_judge_ext.py
  -> output/<run-id>/pipeline_report.jsonl   (merged)
```

On Ctrl+C the orchestrator cascades SIGINT (30s) -> docker compose label-based cleanup -> SIGTERM (20s) -> SIGKILL.

## Prerequisites

- A harbor jobs root with report-generation runs containing `artifacts/final_report.html` or `.pdf` at the standard harbor path `.../{task_family}/{case}/{variant}/{mode}/{runner}/{run}/{trial}/artifacts/`.
- Matching case data at `tasks/report-generation/shared/cases/<case>/` (`task_input.json`, optional `source_brief.md`, and a `*.csv` data file) reachable from a `tasks/` ancestor of `jobs_root`.
- The stage1 evaluator template at `tasks/report-generation/scripts/evaluation/`.
- Judge credentials defined in `agent_configs/snippets/judge.snippet` and referenced via `@VAR` placeholders in `--judge`. The shell env is whitelisted, so secrets are not inherited from the operator's shell.
- Working `docker` daemon and `uv`. Playwright chromium is auto-installed into `eval/report-eval/.playwright-browsers` on first run if missing.

## Usage

```bash
uv run --project eval/report-eval python eval/report-eval/scripts/pipeline.py \
  <JOBS_ROOT> [PARALLEL] [MAX_WORKERS] \
  --judge 'MODEL|PROVIDER|BASE_URL|API_KEY' \
  --run-id eval_result
```

Resume: reusing the same `--run-id` makes the run incremental — existing per-trial outputs under `output/<run-id>/` are kept and only missing artifacts are produced.

### Arguments

| Flag | Description | Default |
| --- | --- | --- |
| `jobs_root` | Positional (required). Harbor jobs root containing report-generation artifacts (e.g. `harbor/smoke_jobs`). Must have a `tasks/` ancestor. | (required) |
| `parallel` | Positional. Number of reports processed concurrently end-to-end. | `3` |
| `max_workers` | Positional. Per-judge inner VLM API concurrency. | `3` |
| `--judge MODEL\|PROVIDER\|BASE_URL\|API_KEY` | Judge spec, repeatable. Any field may use `@VAR` to reference an ENV from `agent_configs/snippets/judge.snippet`. | `claude-opus-4-6\|anthropic\|@ANTHROPIC_BASE_URL\|@ANTHROPIC_API_KEY` |
| `--run-id` | Output subdirectory name under `eval/report-eval/output/`. Reusing the same id makes the run incremental. | `eval_result` |
| `--eval-agent` | Stage1 evaluator agent name passed to harbor. | `claude-code` |
| `--eval-model` | Model used by the stage1 evaluator agent. | model from first `--judge` |
| `--eval-timeout-mult` | `harbor --timeout-multiplier` for the stage1 evaluator run. | `3.0` |

## Output

Results mirror the input layout under `eval/report-eval/output/<run-id>/`:

```
eval/report-eval/output/<run-id>/
  pipeline_<jobs-basename>.log
  pipeline_report.jsonl
  <task_family>/<case>/<variant>/<mode>/<runner>/<run>/<trial>/
    eval_report.json                 # stage1 in-Docker evaluator output
    harbor.log
    stage1.log
    judge_result_<safe-model>.json   # one per --judge
```

`pipeline_report.jsonl` is the merge of every `judge_result_*.json` produced in the run.

## Scoring

Five 1-5 dimensions per trial. Each `--judge` spec produces its own `judge_result_<safe-model>.json` with per-dimension `score` + `reasoning` and an aggregate `overall` (mean of the five dimensions).

| Dimension | Source | Range |
| --- | --- | --- |
| `content_quality` | VLM judge (`code/vlm_judge_ext.py`) | 1-5 |
| `visualization` | VLM judge | 1-5 |
| `completeness` | VLM judge | 1-5 |
| `data_accuracy` | Stage1 in-Docker evaluator (code-verified against source CSV / `task_input.json`) | 1-5 |
| `fidelity` | Stage1 in-Docker evaluator | 1-5 |

The VLM judge extracts text plus rendered PNGs from HTML (via Playwright chromium) or PDF and sends them as image blocks to the configured provider (`anthropic` or openai-compatible). Dimensions are scored concurrently (`max_workers`). Default judge is `claude-opus-4-6`; pass `--judge` multiple times for multi-judge runs.

## Files

| Path | Role |
| --- | --- |
| [scripts/pipeline.py](scripts/pipeline.py) | Entry point. asyncio orchestrator: discovers reports, dedupes per trial, spawns `stage1_eval_agent.sh` in its own process group, handles graceful Ctrl+C cascade + docker compose cleanup, ensures Playwright chromium, parses `judge.snippet`, and merges all `judge_result_*.json` into `pipeline_report.jsonl`. |
| [scripts/stage1_eval_agent.sh](scripts/stage1_eval_agent.sh) | Per-trial driver. Resolves `REPO_ROOT`/case dir from the artifacts path, builds an evaluator workdir under `/tmp/report_eval_task_*` (copies `tasks/report-generation/scripts/evaluation` + case data + agent output, injects a `COPY` into the Dockerfile), runs `uv run harbor run` to produce `eval_report.json`, then launches one `judge_single.sh` per `--judge` in parallel and label-cleans compose containers/volumes on exit. |
| [scripts/judge_single.sh](scripts/judge_single.sh) | Single-judge driver. Parses artifacts path metadata, exports `provider`/`base_url`/`api_key`, invokes `uv --project eval/report-eval run python code/vlm_judge_ext.py` against the report + `task_input.json` (with `--eval-report` when available), and injects `{task_family, case, variant, mode, runner, run, trial, judge_model}` into `judge_result_<safe-model>.json`. |
| [code/vlm_judge_ext.py](code/vlm_judge_ext.py) | Enhanced VLM-as-Judge. Detects HTML vs PDF, extracts text and renders pages to PNG (HTML via Playwright chromium, PDF natively), calls anthropic or openai-compatible chat APIs concurrently (`--max-workers`) for `content_quality` / `visualization` / `completeness`, merges code-verified `data_accuracy` + `fidelity` from `--eval-report`, and writes a single JSON with per-dimension scores, reasoning, and an `overall`. |

## Special workflow

Playwright chromium is auto-installed on first `pipeline.py` run into `eval/report-eval/.playwright-browsers`. Operators behind restrictive networks can either pre-warm it or point Playwright at their own mirror via `PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST`:

```bash
uv --project eval/report-eval run playwright install chromium
```

Stage1 runs the evaluator agent in Docker via `uv run harbor run`, so a working docker daemon is required. Ctrl+C triggers a graceful cascade (SIGINT 30s -> SIGTERM 20s -> SIGKILL) that also cleans up any `/tmp/report_eval_task_*` compose containers and volumes left behind, matched by the `com.docker.compose.project.working_dir` label.

The shell env passed to subprocesses is whitelisted — judge credentials must come from `agent_configs/snippets/judge.snippet` via `@VAR` placeholders, not from the operator's shell. Set `OPENSKILLEVAL_EXTRA_ENV=KEY1,KEY2` to forward extra names. `pipeline.py` resolves `REPO_ROOT` from `jobs_root` (not from its own script path), so symlinked or bind-mounted checkouts work correctly.
