# PPT Eval

End-to-end evaluator for generated `final_deck.pptx` artifacts: a Linux Stage-1 packer collects decks, a manual macOS PowerPoint+PyMuPDF step rasterises slides, and a Stage-2 Python pipeline drives a multi-dimensional VLM judge.

## Architecture

Three-machine spawn chain. Stage 1 packs decks on the Linux jobs host; the Mac bridge converts pptx to slide PNGs via PowerPoint; Stage 2 (`pipeline.py`) fans the screenshot tar out to one `vlm_judge_ext.py` subprocess per `(deck, judge)` pair.

```
stage1_pack.sh (Linux)          mac_code/convert.sh (macOS)         pipeline.py (Linux)
   final_deck.pptx           ->     PowerPoint -> PDF -> PNG     ->     vlm_judge_ext.py
   -> pptx_pack_*.tar.gz            -> screenshots tar.gz                -> pipeline_ppt.jsonl
```

## Prerequisites

- A `JOBS_ROOT` containing PPT generation outputs at the 9-level path `.../{task_family}/{case}/{variant}/{mode}/{runner}/{run}/{trial}/artifacts/final_deck.pptx`, with `result.json` `status==ok`.
- A macOS machine with Microsoft PowerPoint installed plus `python3` with `PyMuPDF` for the screenshot step.
- A way to move the pptx pack from the Linux host to the Mac and the screenshot tar back (operator-provided).
- `agent_configs/snippets/judge.snippet` with `ENV` lines defining `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` (or `OPENAI_*`) referenced by `@VAR` in `--judge` specs.
- `tasks/ppt-generation/shared/cases/<case>/task_input.json` and `source_brief.md` present in the repo.
- `uv` and Docker available on the Linux host.

## Usage

```bash
uv run python eval/ppt-eval/scripts/pipeline.py \
    <SCREENSHOTS_TAR> [MANIFEST] [PARALLEL] [WORKERS_PER_DECK] \
    --judge "MODEL|PROVIDER|BASE_URL|API_KEY" \
    --run-id eval_result
```

Resume: reusing the same `--run-id` skips any deck whose `judge_result_<safe_model>.json` already exists under the run directory; only missing `(deck, judge)` pairs are re-spawned.

### Arguments

| Flag | Description | Default |
| --- | --- | --- |
| `screenshots_tar` | Path to screenshot tar(.gz) produced by Mac `convert.sh` (contains `slide_*.png` subdirs + `manifest.jsonl`). | (required) |
| `manifest` | Path to `manifest.jsonl`. If omitted, it is found inside the extracted tar. | — |
| `parallel` | Number of decks to evaluate concurrently. | `3` |
| `workers_per_deck` | VLM API concurrency inside each deck. | `3` |
| `--judge` | Judge spec `MODEL|PROVIDER|BASE_URL|API_KEY`, repeatable. Fields support `@VAR` expansion from `agent_configs/snippets/judge.snippet`. | `claude-opus-4-6|anthropic|@ANTHROPIC_BASE_URL|@ANTHROPIC_API_KEY` |
| `--run-id` | Output subdirectory name under `eval/ppt-eval/output/`. Reusing the same id incrementally resumes. | `eval_result` |
| `--cache-dir` | Screenshot extraction cache directory (avoids slow PFS IO). | `eval.bak/ppt-eval/output` |

## Output

Outputs are written under `eval/ppt-eval/output/<run-id>/`:

```
eval/ppt-eval/output/<run-id>/
  pipeline_ppt.log                       # full pipeline log
  pipeline_ppt.jsonl                     # merged results, one line per (deck, judge)
  <task_family>/<case>/<variant>/<mode>/<runner>/<run>/<trial>/
      judge_result_<safe_model>.json     # per-deck judge JSON
```

Stage 1 additionally produces `eval/ppt-eval/output/pptx_pack_<jobs_basename>_<timestamp>.tar.gz` plus `eval/ppt-eval/output/.packed_<jobs_basename>.txt` for incremental dedup.

## Scoring

The VLM judge (default `claude-opus-4-6`) scores each deck on 4 dimensions, each on a 1-5 scale:

| Dimension | Granularity |
| --- | --- |
| Content | per-slide, averaged |
| Design | per-slide, averaged |
| Completeness | deck level |
| Fidelity | deck level |

Overall is the arithmetic mean of the four dimensions. Multiple judges are configured by repeating `--judge MODEL|PROVIDER|BASE_URL|API_KEY` (fields support `@VAR` expansion from `agent_configs/snippets/judge.snippet`). `pipeline.py` spawns one `vlm_judge_ext.py` subprocess per `(deck, judge)`, with up to `parallel * len(judges)` concurrent subprocesses and `workers_per_deck` VLM threads inside each. Existing `judge_result_<safe_model>.json` files are skipped for resume.

## Files

| Path | Role |
| --- | --- |
| [scripts/pipeline.py](scripts/pipeline.py) | Stage 2 entry: extracts screenshot tar, pairs decks x judges, spawns `vlm_judge_ext.py` via `uv run`, merges results into `pipeline_ppt.jsonl`. Async semaphore concurrency, `SIGINT`->`SIGTERM`->`SIGKILL` graceful cascade, resume via `--run-id`. |
| [scripts/stage1_pack.sh](scripts/stage1_pack.sh) | Stage 1: scans `<JOBS_ROOT>` for `final_deck.pptx`, filters by `result.json` status, copies+renames to `{variant}__{mode}__{runner}__{run}.pptx` (with case prefix when multi-case), writes `manifest.jsonl`, and tars the result. |
| [code/vlm_judge_ext.py](code/vlm_judge_ext.py) | VLM-as-Judge engine spawned per deck. Scores slides on 4 dimensions (Content, Design, Completeness, Fidelity). Multi-provider (`anthropic`/`openai`), `--max-workers` `ThreadPoolExecutor` concurrency, incremental flush. |
| [code/mac_code/convert.sh](code/mac_code/convert.sh) | Mac-side conversion: extracts pptx tar, drives Microsoft PowerPoint via AppleScript to export PDF (with auto-clicker for repair/confirm dialogs), then PyMuPDF rasterises PDFs to `slide_NNN.png` at 2x. Re-tars into a self-contained screenshot tar.gz. |

## Special workflow

PPT eval has a unique 3-machine workflow because `pptx` -> slide PNG conversion requires Microsoft PowerPoint on macOS.

### Stage 1 — Linux server (pack)

```bash
bash eval/ppt-eval/scripts/stage1_pack.sh <JOBS_ROOT>
```

Collects every `final_deck.pptx` whose `result.json` `status==ok` and packs them into `eval/ppt-eval/output/pptx_pack_<jobs_basename>_<timestamp>.tar.gz`. Also writes `.packed_<jobs_basename>.txt` for incremental dedup.

### Mac bridge — Microsoft PowerPoint + `python3` + `PyMuPDF`

Get the Stage 1 pptx tar onto the Mac, run the converter, then get the screenshot tar back to the Linux host:

```bash
bash eval/ppt-eval/code/mac_code/convert.sh <input.tar.gz> [output.tar.gz]
```

The script extracts the tar, strips macOS quarantine xattrs, then drives PowerPoint via AppleScript to export each pptx as PDF. A background `osascript` polling loop auto-clicks `Repair` / `OK` / `Continue` dialogs and logs which files triggered repair. PyMuPDF (2x matrix) rasterises each PDF page to `slide_NNN.png`, the original `manifest.jsonl` is copied into the bundle, and everything is re-tarred into a self-contained screenshots `tar.gz`.

### Stage 2 — Linux server (judge)

Bring the screenshot tar back to the Linux host, then:

```bash
uv run python eval/ppt-eval/scripts/pipeline.py <screenshots.tar.gz>
```

`pipeline.py` auto-detects `manifest.jsonl` inside the tar, so no other files need to be shipped between machines.
