#!/usr/bin/env bash
###############################################################################
# judge_dataviz.sh — Data Visualization VLM Judge evaluation
#
# Flow: vlm_judge_ext -> inject metadata (no format conversion needed, input is PNG)
#
# Usage:
#   bash eval/dataviz-eval/scripts/judge_dataviz.sh <VIZ_PNG_PATH> [OUTPUT_PATH] [MODEL] [MAX_WORKERS] \
#        [PROVIDER] [BASE_URL] [API_KEY]
#
# Args:
#   VIZ_PNG_PATH  required, path to the result.png to evaluate
#   OUTPUT_PATH   optional, output path for score JSON
#                 default: <artifacts>/judge_result_<safe_model>.json
#   MODEL         optional, VLM model name (default claude-opus-4-6)
#   MAX_WORKERS   optional, vlm_judge internal API concurrency (default 4)
#   PROVIDER      optional, anthropic or openai (default anthropic)
#   BASE_URL      optional, API base URL
#   API_KEY       optional, API key
#
# Output JSON format:
#   { "viz_path": "...", "task_family": "...", "case": "...", "variant": "...",
#     "mode": "...", "runner": "...", "run": "...", "trial": "...",
#     "judge_model": "...", "insight_expression": {...}, "data_accuracy": {...},
#     "visual_quality": {...}, "completeness": {...}, "overall": 3.8 }
###############################################################################
set -euo pipefail

# ── Parse args ────────────────────────────────────────────────────
VIZ_PATH="${1:?Usage: bash judge_dataviz.sh <VIZ_PNG_PATH> [OUTPUT] [MODEL] [WORKERS] [PROVIDER] [BASE_URL] [API_KEY]}"
MODEL="${3:-claude-opus-4-6}"
MAX_WORKERS="${4:-4}"
PROVIDER="${5:-anthropic}"
BASE_URL="${6:-}"
API_KEY="${7:-}"
EVAL_REPORT="${8:-}"

# RUN_ID: passed in by upper script, or auto-generated timestamp
export RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"

# Sanitize model name for filename
SAFE_MODEL="$(echo "$MODEL" | tr ' /:' '---')"

# OUTPUT defaults to eval/dataviz-eval/output/, subdirectories by metadata
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ -n "${2:-}" ]; then
  OUTPUT="$2"
else
  # Parse metadata first to construct output path
  IFS='/' read -ra _TP <<< "$VIZ_PATH"
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

# ── API config ───────────────────────────────────────────────────
if [ "$PROVIDER" = "openai" ]; then
  export OPENAI_BASE_URL="${BASE_URL:-${OPENAI_BASE_URL:-https://api.openai.com/v1}}"
  export OPENAI_API_KEY="${API_KEY:-${OPENAI_API_KEY:-}}"
else
  export ANTHROPIC_BASE_URL="${BASE_URL:-${ANTHROPIC_BASE_URL:?ANTHROPIC_BASE_URL must be set (provide via --judge spec or agent_configs/snippets/judge.snippet)}}"
  export ANTHROPIC_API_KEY="${API_KEY:-${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY must be set (provide via --judge spec or agent_configs/snippets/judge.snippet)}}"
fi

# ──────────────────────────────────────────────────────────────────

REPO_ROOT="${REPO_ROOT_OVERRIDE:-$(cd "${PROJECT_DIR}/../.." && pwd)}"
unset VIRTUAL_ENV
UV="uv --project ${PROJECT_DIR}"

# ── Auto-extract metadata from VIZ path ──────────────────────────
# .../{task_family}/{case}/{variant}/{mode}/{runner}/{run}/{trial}/artifacts/result.png
IFS='/' read -ra _PARTS <<< "$VIZ_PATH"
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
SOURCE_DATA="${CASE_DIR}/source_data.json"

echo "[info] viz:         ${VIZ_PATH}" >&2
echo "[info] task_family: ${TASK_FAMILY}  case: ${CASE}  variant: ${VARIANT}" >&2
echo "[info] mode: ${MODE}  runner: ${RUNNER}  run: ${RUN}" >&2
echo "[info] provider: ${PROVIDER}  model: ${MODEL}" >&2
echo "[info] output:      ${OUTPUT}" >&2

# ── Validate ─────────────────────────────────────────────────────
err=0
for f in "$VIZ_PATH" "$TASK_INPUT" "$SOURCE_BRIEF" "$SOURCE_DATA"; do
  [ -f "$f" ] || { echo "[error] missing: $f" >&2; err=1; }
done
[ $err -ne 0 ] && exit 1

# ── Step 1: VLM Judge -> temp file ───────────────────────────────
RAW_RESULT="$(mktemp /tmp/dataviz_judge_raw_XXXXXX.json)"
echo "" >&2
echo ">>> vlm_judge (provider=${PROVIDER}, model=${MODEL}, max_workers=${MAX_WORKERS})" >&2

JUDGE_CMD=(
  $UV run python "${PROJECT_DIR}/code/vlm_judge_ext.py"
  "$VIZ_PATH"
  "$TASK_INPUT"
  "$SOURCE_BRIEF"
  "$SOURCE_DATA"
  --model "$MODEL"
  --max-workers "$MAX_WORKERS"
  --provider "$PROVIDER"
  --output "$RAW_RESULT"
)
[ -n "$BASE_URL" ] && JUDGE_CMD+=(--base-url "$BASE_URL")
[ -n "$API_KEY" ]  && JUDGE_CMD+=(--api-key "$API_KEY")
[ -n "$EVAL_REPORT" ] && JUDGE_CMD+=(--agent-eval-report "$EVAL_REPORT")

"${JUDGE_CMD[@]}"

# ── Step 2: Inject metadata ──────────────────────────────────────
$UV run python -c "
import json, sys
with open('$RAW_RESULT') as f:
    data = json.load(f)
meta = {
    'viz_path':     '$VIZ_PATH',
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

# ── Cleanup ──────────────────────────────────────────────────────
rm -f "$RAW_RESULT"

echo "" >&2
echo ">>> done. result: ${OUTPUT}" >&2
