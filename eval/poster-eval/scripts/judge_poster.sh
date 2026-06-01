#!/usr/bin/env bash
###############################################################################
# judge_poster.sh — Poster VLM Judge evaluation
#
# Flow: [extract_poster (PDF->PNG)] -> vlm_judge_ext -> inject metadata
#
# Usage:
#   bash poster-eval/judge_poster.sh <POSTER_PATH> [OUTPUT_PATH] [MODEL] [MAX_WORKERS] \
#        [PROVIDER] [BASE_URL] [API_KEY]
#
# Arguments:
#   POSTER_PATH   required, path to the .png or .pdf file to evaluate
#   OUTPUT_PATH   optional, path to save the scoring result JSON
#                 default: <artifacts>/judge_result_<safe_model>.json
#   MODEL         optional, VLM model name (default claude-opus-4-6)
#   MAX_WORKERS   optional, vlm_judge internal API concurrency (default 4)
#   PROVIDER      optional, anthropic or openai (default anthropic)
#   BASE_URL      optional, API base URL
#   API_KEY       optional, API key
#
# Output JSON format:
#   { "poster_path": "...", "task_family": "...", "case": "...", "variant": "...",
#     "mode": "...", "runner": "...", "run": "...", "trial": "...",
#     "judge_model": "...", "visual_design": {...}, "content": {...},
#     "completeness": {...}, "overall": 3.8 }
###############################################################################
set -euo pipefail

# ── Argument parsing ─────────────────────────────────────────────
POSTER_PATH="${1:?Usage: bash judge_poster.sh <POSTER_PATH> [OUTPUT] [MODEL] [WORKERS] [PROVIDER] [BASE_URL] [API_KEY]}"
MODEL="${3:-claude-opus-4-6}"
MAX_WORKERS="${4:-4}"
PROVIDER="${5:-anthropic}"
BASE_URL="${6:-}"
API_KEY="${7:-}"

# RUN_ID: passed in by the upper-level script, or auto-generated timestamp
export RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"

# Sanitize model name for use in filenames
SAFE_MODEL="$(echo "$MODEL" | tr ' /:' '---')"

# OUTPUT defaults to eval/poster-eval/output/, partitioned by metadata
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ -n "${2:-}" ]; then
  OUTPUT="$2"
else
  # Parse metadata first to construct the output path
  IFS='/' read -ra _TP <<< "$POSTER_PATH"
  _TN=${#_TP[@]}
  _TF="${_TP[$((_TN-9))]:-unknown}"
  _CA="${_TP[$((_TN-8))]:-unknown}"
  _VA="${_TP[$((_TN-7))]:-unknown}"
  _MO="${_TP[$((_TN-6))]:-unknown}"
  _RU="${_TP[$((_TN-5))]:-unknown}"
  _RN="${_TP[$((_TN-4))]:-unknown}"
  _TR="${_TP[$((_TN-3))]:-unknown}"
  OUTPUT="${PROJECT_DIR}/output/${RUN_ID}/${_TF}/${_CA}/${_VA}/${_MO}/${_RU}/${_RN}/${_TR}/judge_result_${SAFE_MODEL}.json"
fi
mkdir -p "$(dirname "$OUTPUT")"

# ── API config ──────────────────────────────────────────────────
if [ "$PROVIDER" = "openai" ]; then
  export OPENAI_BASE_URL="${BASE_URL:-${OPENAI_BASE_URL:-https://api.openai.com/v1}}"
  export OPENAI_API_KEY="${API_KEY:-${OPENAI_API_KEY:-}}"
else
  export ANTHROPIC_BASE_URL="${BASE_URL:-${ANTHROPIC_BASE_URL:?ANTHROPIC_BASE_URL must be set (provide via --judge spec or agent_configs/snippets/judge.snippet)}}"
  export ANTHROPIC_API_KEY="${API_KEY:-${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY must be set (provide via --judge spec or agent_configs/snippets/judge.snippet)}}"
fi

# ──────────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "${PROJECT_DIR}/../.." && pwd)"
SCRIPTS_DIR="${REPO_ROOT}/tasks/poster-generation/scripts"
unset VIRTUAL_ENV
UV="uv --project ${PROJECT_DIR}"

# ── Auto-extract metadata from POSTER path ──────────────────────
# .../{task_family}/{case}/{variant}/{mode}/{runner}/{run}/{trial}/artifacts/final_poster.png
IFS='/' read -ra _PARTS <<< "$POSTER_PATH"
_N=${#_PARTS[@]}
TASK_FAMILY="${_PARTS[$((_N-9))]}"
CASE="${_PARTS[$((_N-8))]}"
VARIANT="${_PARTS[$((_N-7))]}"
MODE="${_PARTS[$((_N-6))]}"
RUNNER="${_PARTS[$((_N-5))]}"
RUN="${_PARTS[$((_N-4))]}"
TRIAL="${_PARTS[$((_N-3))]}"

CASE_DIR="${REPO_ROOT}/tasks/${TASK_FAMILY}/shared/cases/${CASE}"
TASK_INPUT="${CASE_DIR}/task_input.json"
SOURCE_BRIEF="${CASE_DIR}/source_brief.md"

echo "[info] poster:      ${POSTER_PATH}" >&2
echo "[info] task_family: ${TASK_FAMILY}  case: ${CASE}  variant: ${VARIANT}" >&2
echo "[info] mode: ${MODE}  runner: ${RUNNER}  run: ${RUN}" >&2
echo "[info] provider: ${PROVIDER}  model: ${MODEL}" >&2
echo "[info] output:      ${OUTPUT}" >&2

# ── Validation ──────────────────────────────────────────────────
err=0
for f in "$POSTER_PATH" "$TASK_INPUT" "$SOURCE_BRIEF"; do
  [ -f "$f" ] || { echo "[error] missing: $f" >&2; err=1; }
done
[ $err -ne 0 ] && exit 1

if [ -d "${CASE_DIR}/assets" ]; then
  echo "[info] assets: $(ls -1 "${CASE_DIR}/assets" | wc -l) files" >&2
else
  echo "[warn] no assets/ directory — Content dimension may not load embedded images" >&2
fi

# ── Step 1: If PDF, convert to PNG ──────────────────────────────
POSTER_PNG="$POSTER_PATH"
EVAL_DIR=""

if [[ "$POSTER_PATH" == *.pdf ]]; then
  EVAL_DIR="$(mktemp -d /tmp/poster_eval_XXXXXX)"
  echo "" >&2
  echo ">>> Step 1/2: extract_poster (PDF→PNG) → ${EVAL_DIR}" >&2
  $UV run python "${SCRIPTS_DIR}/extract_poster.py" "$POSTER_PATH" "$EVAL_DIR"

  if [ -f "${EVAL_DIR}/poster.png" ]; then
    POSTER_PNG="${EVAL_DIR}/poster.png"
    echo "[info] Converted PDF to PNG: ${POSTER_PNG}" >&2
  else
    echo "[error] Failed to convert PDF to PNG" >&2
    exit 1
  fi
else
  echo "" >&2
  echo ">>> Step 1/2: skip (already PNG)" >&2
fi

# ── Step 2: VLM Judge -> temp file ──────────────────────────────
RAW_RESULT="$(mktemp /tmp/poster_judge_raw_XXXXXX.json)"
echo "" >&2
echo ">>> Step 2/2: vlm_judge (provider=${PROVIDER}, model=${MODEL}, max_workers=${MAX_WORKERS})" >&2

JUDGE_CMD=(
  $UV run python "${PROJECT_DIR}/code/vlm_judge_ext.py"
  "$POSTER_PNG"
  "$TASK_INPUT"
  "$SOURCE_BRIEF"
  --model "$MODEL"
  --max-workers "$MAX_WORKERS"
  --provider "$PROVIDER"
  --output "$RAW_RESULT"
)
[ -n "$BASE_URL" ] && JUDGE_CMD+=(--base-url "$BASE_URL")
[ -n "$API_KEY" ]  && JUDGE_CMD+=(--api-key "$API_KEY")

"${JUDGE_CMD[@]}"

# ── Step 3: Inject metadata ─────────────────────────────────────
$UV run python -c "
import json, sys
with open('$RAW_RESULT') as f:
    data = json.load(f)
meta = {
    'poster_path':  '$POSTER_PATH',
    'task_family':  '$TASK_FAMILY',
    'case':         '$CASE',
    'variant':      '$VARIANT',
    'mode':         '$MODE',
    'runner':       '$RUNNER',
    'run':          '$RUN',
    'trial':        '$TRIAL',
    'judge_model':  '$MODEL',
}
meta.update(data)
with open('$OUTPUT', 'w') as f:
    json.dump(meta, f, indent=2, ensure_ascii=False)
"

# ── Cleanup ─────────────────────────────────────────────────────
[ -n "$EVAL_DIR" ] && rm -rf "$EVAL_DIR"
rm -f "$RAW_RESULT"

echo "" >&2
echo ">>> done. result: ${OUTPUT}" >&2
