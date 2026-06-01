# Poster Eval

Evaluates poster-generation outputs (`final_poster.png`/`pdf` from harbor jobs) with a VLM judge across three rubrics — visual design, content accuracy vs. source brief, and task completeness — producing per-poster score JSONs and a merged JSONL.

## Architecture

`pipeline.py` walks `jobs_root` for posters, expands `@VAR` references in `--judge` specs from `agent_configs/snippets/judge.snippet`, then spawns one `judge_poster.sh` per `(poster, judge)` pair under a concurrency semaphore. Each shell driver normalizes the input (PDF -> PNG when needed) and invokes `vlm_judge_ext.py`, which runs the three rubric dimensions in parallel against the VLM. Results are merged with harbor metadata and aggregated into a single JSONL.

```
pipeline.py
  └─> judge_poster.sh (per poster × judge)
        ├─> extract_poster.py        # PDF -> PNG (if needed)
        └─> vlm_judge_ext.py          # 3 rubric dims in parallel
              -> judge_result_<model>.json
  <─ merge into pipeline_poster.jsonl
```

## Prerequisites

- Harbor jobs root with the 9-segment layout: `<jobs_root>/<task_family>/<case>/<variant>/<mode>/<runner>/<run>/<trial>/artifacts/final_poster.{png,pdf}`.
- For each case, `tasks/<task_family>/shared/cases/<case>/task_input.json` and `source_brief.md` (plus optional `assets/`) must exist — `judge_poster.sh` validates these and fails fast if missing.
- Judge credentials in `agent_configs/snippets/judge.snippet` (`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` by default), referenced via `@VAR_NAME` in `--judge`.
- `uv` available on `PATH` (the pipeline shells out via `uv run`).
- `docker` only if your poster artifacts require it upstream; this eval itself does not spawn containers.

## Usage

```bash
uv run --project eval/poster-eval python eval/poster-eval/scripts/pipeline.py harbor/smoke_jobs 3 4
```

Resume: re-run the same command with `--run-id <id>` to skip posters that already have a `judge_result_<safe_model>.json` under that run directory; only missing or previously-failed judgements are re-scored.

### Arguments

| Flag | Description | Default |
| --- | --- | --- |
| `jobs_root` | Harbor jobs root directory containing artifacts (e.g. `harbor/smoke_jobs`). Walked to find `final_poster.png`/`pdf`. | (required) |
| `parallel` | Number of posters evaluated concurrently. | `3` |
| `workers_per_model` | VLM API concurrency inside each judge (3 rubric dimensions run in parallel). | `4` |
| `--judge MODEL\|PROVIDER\|BASE_URL\|API_KEY` | Judge spec (repeatable). Any field may use `@VAR_NAME` to pull from `judge.snippet` ENV. | `claude-opus-4-6\|anthropic\|@ANTHROPIC_BASE_URL\|@ANTHROPIC_API_KEY` |
| `--run-id` | Output subdirectory name under `eval/poster-eval/output/`. Re-using the same id resumes (skips posters with existing `judge_result_*.json`). | `eval_result` |

## Output

All outputs live under `eval/poster-eval/output/<run-id>/`. Per-poster results mirror the 9-segment harbor path; the merged JSONL and pipeline log sit at the top of the run directory.

```
eval/poster-eval/output/<run-id>/
├── pipeline_poster.jsonl                 # one JSON object per judge_result
├── pipeline_<jobs_basename>.log          # pipeline-level log
└── <task_family>/<case>/<variant>/<mode>/<runner>/<run>/<trial>/
      └── judge_result_<safe_model>.json  # model name sanitized: / \ : space -> '-'
```

## Scoring

VLM-as-Judge with three dimensions, each scored 1-5:

| Dimension | Range | What it measures |
| --- | --- | --- |
| `visual_design` | 1-5 | Color, layout, typography, consistency — from poster image alone. |
| `content` | 1-5 | Data-accuracy traceability between poster and `source_brief.md` (with inline images). Ignores rendering issues and minor rounding. |
| `completeness` | 1-5 | Poster vs. `task_input.json` requirements: `aspect_ratio`, audience, tone, sections, metrics. |

All three dimensions are submitted concurrently via `ThreadPoolExecutor` (`--max-workers`, default 4). `overall = round(mean(3 scores), 2)`. If any dimension fails, the entire `judge_result` is discarded so the next pipeline run retries it. Default judge is `claude-opus-4-6` via the `anthropic` SDK; `--judge` can be repeated for multi-judge runs.

## Files

| Path | Role |
| --- | --- |
| [scripts/pipeline.py](scripts/pipeline.py) | Async orchestrator: discovers `final_poster.{png,pdf}` under `jobs_root`, resolves judges (`@VAR` expansion from `judge.snippet`), spawns `judge_poster.sh` per `(poster, judge)` with semaphore-bounded concurrency, handles SIGINT cascade (30s SIGINT -> 20s SIGTERM -> SIGKILL), merges all `judge_result_*.json` into `pipeline_poster.jsonl`. |
| [scripts/judge_poster.sh](scripts/judge_poster.sh) | Per-poster judge driver: parses 9-segment harbor path for metadata, converts PDF -> PNG via `tasks/poster-generation/scripts/extract_poster.py` when needed, invokes `code/vlm_judge_ext.py` with provider/base-url/api-key, then injects metadata (`task_family`/`case`/`variant`/`mode`/`runner`/`run`/`trial`/`judge_model`) into the final `judge_result_<safe_model>.json`. |
| [code/vlm_judge_ext.py](code/vlm_judge_ext.py) | VLM-as-Judge implementation: scores Design/Content/Completeness (1-5 each) in parallel via `ThreadPoolExecutor`. Supports `anthropic` SDK or `openai`/Gemini-style REST. Downscales images to fit 8000px / 5MB base64 limit. Loads `source_brief.md` with interleaved inline images for content traceability check. Outputs per-dim `{score, reason}` plus `overall = mean`. |
