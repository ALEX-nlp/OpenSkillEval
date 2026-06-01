# Web Design Eval

End-to-end evaluator for web-design agent outputs: runs a Playwright-based eval agent in Docker to produce screenshots plus navigation/interaction pass-rates, then scores each site with one or more VLM judges across eight weighted dimensions.

## Architecture

`pipeline.py` walks the jobs root for web-design artifacts, then fans out one stage1 process per site under an asyncio semaphore. Each stage1 builds a Docker context, runs the eval agent via `harbor`, then forks one judge per `--judge` spec. Each judge invokes `vlm_judge_ext.py` and stamps task metadata onto its result. The driver merges every per-site `judge_result_*.json` into a single JSONL.

```
pipeline.py
  -> stage1_eval_agent.sh           (Docker context + harbor run, per site)
       -> judge_single.sh           (one per --judge spec, parallel)
            -> code/vlm_judge_ext.py
```

## Prerequisites

- harbor-produced web-design artifacts under a `JOBS_ROOT`, laid out as `<JOBS_ROOT>/.../<task_family>/<case>/<variant>/<mode>/<runner>/<run>/<trial>/artifacts/output/index.html`.
- Matching case data at `tasks/web-design/shared/cases/<case>/` (`task_input.json` + `source_brief.md`).
- Eval template at `tasks/web-design/scripts/evaluation/` (Dockerfile + `environment/`).
- `uv` and `docker` on `PATH`; `harbor` installed.
- Judge credentials defined as `ENV` lines in `agent_configs/snippets/judge.snippet`, referenced via `@VAR_NAME` in `--judge` specs.

## Usage

```bash
uv run --project eval/webdesign-eval python eval/webdesign-eval/scripts/pipeline.py harbor/smoke_jobs 3 3
```

Resume: re-run with the same `--run-id` to perform incremental evaluation — any `(artifacts_dir, judge_model)` pair that already has a `judge_result_<safe_model>.json` is skipped.

### Arguments

| Flag | Description | Default |
| --- | --- | --- |
| `jobs_root` | Harbor jobs root directory; pipeline walks it to discover sites. | (required) |
| `parallel` | Sites to run end-to-end concurrently (stage1 Docker + judges). | `3` |
| `max_workers` | VLM API concurrency inside each judge invocation. | `3` |
| `--judge` | Judge spec `MODEL|PROVIDER|BASE_URL|API_KEY`; any field may be `@VAR_NAME` resolved from `judge.snippet`. Repeatable for multi-judge. | single claude-opus-4-6 judge |
| `--run-id` | Output subdirectory name under `output/`; reuse to resume incrementally. | `eval_result` |
| `--eval-agent` | Agent that runs stage1 (Playwright + code checks) inside Docker via harbor. | `claude-code` |
| `--eval-model` | Model used by the stage1 eval agent. | model of first `--judge` |
| `--eval-timeout-mult` | `harbor run --timeout-multiplier` for stage1. | `5.0` |

## Output

All outputs live under `eval/webdesign-eval/output/<run-id>/`. Per-site directories mirror the input path; the top-level run dir also receives the driver log and a merged JSONL of every judge result.

```
eval/webdesign-eval/output/<run-id>/
  pipeline_<jobs_basename>.log
  pipeline_webdesign.jsonl
  <task_family>/<case>/<variant>/<mode>/<runner>/<run>/<trial>/
    judge_result_<safe_model>.json    # one per judge
    stage1.log
    harbor.log
    eval_agent/                       # harbor jobs (screenshots/ + eval_report.json)
```

## Scoring

Eight-dimension weighted score on a 1-5 scale.

| Dimension | Source | Default weight |
| --- | --- | --- |
| Visual Design | VLM (fullpage + crops per page) | 20 |
| Layout | VLM | 15 |
| Content | VLM | 15 |
| Completeness | VLM | 15 |
| Navigation | stage1 `eval_report.json` pass-rate | 15 |
| Interactions | stage1 `eval_report.json` pass-rate | 10 |
| Data Display | stage1 `eval_report.json` pass-rate | 5 |
| Responsiveness | VLM (mobile + tablet) | 5 |

Pass-rate dimensions are normalized from `0-1` into the `1-5` scale. `overall` is the weighted mean (1-5); `overall_normalized = overall / 5`. Default judge is claude-opus-4-6 via Anthropic. Judge spec `MODEL|PROVIDER|BASE_URL|API_KEY` accepts `@VAR` references resolved from `agent_configs/snippets/judge.snippet`. `--judge` is repeatable for multi-judge comparison.

## Files

| Path | Role |
| --- | --- |
| [scripts/pipeline.py](scripts/pipeline.py) | Async orchestrator: discovers artifacts under `jobs_root`, spawns `stage1_eval_agent.sh` per site under a semaphore, handles the SIGINT cascade plus Docker compose leak cleanup, then merges all `judge_result_*.json` into `pipeline_webdesign.jsonl`. |
| [scripts/stage1_eval_agent.sh](scripts/stage1_eval_agent.sh) | Per-site stage1: assembles a Docker context (benchmark data + agent HTML output), runs `uv run harbor run` with the eval agent to produce `screenshots/` and `eval_report.json`, then forks `judge_single.sh` per judge spec from `JUDGES_SPEC`. Handles incremental skip and docker compose cleanup on exit. |
| [scripts/judge_single.sh](scripts/judge_single.sh) | Single VLM judge invocation: calls `uv run python code/vlm_judge_ext.py` with provider/base-url/api-key, then injects task metadata into the resulting `judge_result_<safe_model>.json`. |
| [code/vlm_judge_ext.py](code/vlm_judge_ext.py) | VLM-as-judge engine: scores Visual Design and Responsiveness (1-5) from screenshots via Anthropic or OpenAI API, derives Navigation/Interactions/Data Display pass-rates from stage1 `eval_report.json`, computes weighted `overall` and `overall_normalized`. Supports `--max-workers` concurrent API calls. |

## Special workflow

**Incremental resume.** Re-running with the same `--run-id` skips any `(artifacts_dir, judge_model)` pair that already has a `judge_result_<safe_model>.json`. This makes it cheap to add a new judge to an existing run — only the missing judge fires per site.

**Multi-judge mode.** Pass `--judge` multiple times; per-site judges run as parallel `bash` background processes inside stage1, each producing its own `judge_result_<safe_model>.json`.

**Credential forwarding.** Credentials must live in `agent_configs/snippets/judge.snippet` (parsed by `pipeline.py`). Environment variables are not inherited blindly — only a whitelist passes through. Forward extra names with `OPENSKILLEVAL_EXTRA_ENV="KEY1,KEY2"`.

**Ctrl+C cascade.** A single Ctrl+C triggers a four-phase shutdown:

1. `SIGINT` to all child process groups (30s grace).
2. `docker compose` orphan cleanup — containers and volumes labeled with the `/tmp/webdesign_eval_task_*` working_dir are torn down.
3. `SIGTERM` (20s grace).
4. `SIGKILL`.

Press Ctrl+C twice for immediate `SIGKILL`.
