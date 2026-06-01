#!/usr/bin/env bash
###############################################################################
# judge_single.sh — Report VLM Judge evaluation (single file)
#
# Flow: [extract (HTML/PDF→PNG+text)] → vlm_judge_ext → inject metadata
#
# Usage:
#   bash report-eval/scripts/judge_single.sh <REPORT_PATH> [OUTPUT_PATH] [MODEL] [MAX_WORKERS] \
#        [PROVIDER] [BASE_URL] [API_KEY]
#
# Arguments:
#   REPORT_PATH   required, path to .html / .pdf file to evaluate (or directory containing final_report.*)
#   OUTPUT_PATH   optional, JSON save path for scoring result
#                 default: <artifacts>/judge_result_<safe_model>.json
#   MODEL         optional, VLM model name (default claude-opus-4-6)
#   MAX_WORKERS   optional, vlm_judge internal API concurrency (default 3)
#   PROVIDER      optional, anthropic or openai (default anthropic)
#   BASE_URL      optional, API base URL
#   API_KEY       optional, API key
#
# Output JSON format:
#   { "report_path": "...", "task_family": "...", "case": "...", "variant": "...",
#     "mode": "...", "runner": "...", "run": "...", "trial": "...",
#     "judge_model": "...", "content_quality": {...}, "visualization": {...},
#     "data_accuracy": {...}, "analysis_depth": {...}, "completeness": {...},
#     "fidelity": {...}, "overall": 3.5 }
###############################################################################
set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────
REPORT_PATH="${1:?Usage: bash judge_single.sh <REPORT_PATH> [OUTPUT] [MODEL] [WORKERS] [PROVIDER] [BASE_URL] [API_KEY]}"
MODEL="${3:-claude-opus-4-6}"
MAX_WORKERS="${4:-3}"
PROVIDER="${5:-anthropic}"
BASE_URL="${6:-}"
API_KEY="${7:-}"

# Sanitize model name for filename use
SAFE_MODEL="$(echo "$MODEL" | tr ' /:' '---')"

# RUN_ID: passed in by parent script, or auto-generated timestamp
export RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"

# OUTPUT defaults under eval/report-eval/output/<RUN_ID>/, split by metadata
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ -n "${2:-}" ] && [ -n "$2" ]; then
  OUTPUT="$2"
else
  # Pre-parse metadata for building output path (parsed again later, no side effects)
  if [ -d "$REPORT_PATH" ]; then
    _TMP_META="$REPORT_PATH/final_report.html"
    [ -f "$_TMP_META" ] || _TMP_META="$REPORT_PATH/final_report.pdf"
    [ -f "$_TMP_META" ] || _TMP_META="$REPORT_PATH"
  else
    _TMP_META="$REPORT_PATH"
  fi
  IFS='/' read -ra _TP <<< "$_TMP_META"
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

# ── Auto-extract metadata from REPORT path ───────────────────────
# .../{task_family}/{case}/{variant}/{mode}/{runner}/{run}/{trial}/artifacts/final_report.*
if [ -d "$REPORT_PATH" ]; then
  _META_PATH="$REPORT_PATH/final_report.html"
  [ -f "$_META_PATH" ] || _META_PATH="$REPORT_PATH/final_report.pdf"
  [ -f "$_META_PATH" ] || _META_PATH="$REPORT_PATH"
else
  _META_PATH="$REPORT_PATH"
fi

IFS='/' read -ra _PARTS <<< "$_META_PATH"
_N=${#_PARTS[@]}
TASK_FAMILY="${_PARTS[$((_N-9))]:-unknown}"
CASE="${_PARTS[$((_N-8))]:-unknown}"
VARIANT="${_PARTS[$((_N-7))]:-unknown}"
MODE="${_PARTS[$((_N-6))]:-unknown}"
RUNNER="${_PARTS[$((_N-5))]:-unknown}"
RUN="${_PARTS[$((_N-4))]:-unknown}"
TRIAL="${_PARTS[$((_N-3))]:-unknown}"

CASE_DIR="${REPO_ROOT}/tasks/${TASK_FAMILY}/shared/cases/${CASE}"
TASK_INPUT="${CASE_DIR}/task_input.json"

# eval_report.json (from Evaluator Agent inside Docker)
if [ -d "$REPORT_PATH" ]; then
  EVAL_REPORT="${REPORT_PATH}/eval_report.json"
else
  EVAL_REPORT="$(dirname "$REPORT_PATH")/eval_report.json"
fi

echo "[judge] ${MODEL} | ${CASE}/${VARIANT} ${MODE}/${RUNNER}/${RUN}" >&2

# ── Validation ───────────────────────────────────────────────────
err=0
if [ -d "$REPORT_PATH" ]; then
  # Directory mode: check for at least one report file inside
  found=0
  for name in final_report.html final_report.pdf report.html report.pdf; do
    [ -f "${REPORT_PATH}/${name}" ] && found=1 && break
  done
  [ $found -eq 0 ] && { echo "[error] no report file found in: $REPORT_PATH" >&2; err=1; }
else
  [ -f "$REPORT_PATH" ] || { echo "[error] missing: $REPORT_PATH" >&2; err=1; }
fi
[ -f "$TASK_INPUT" ] || { echo "[error] missing: $TASK_INPUT" >&2; err=1; }
[ $err -ne 0 ] && exit 1

# ── VLM Judge → temp file ────────────────────────────────────────
RAW_RESULT="$(mktemp /tmp/report_judge_raw_XXXXXX.json)"

LOG_PREFIX="[${MODEL}|${CASE}/${VARIANT}]"

JUDGE_CMD=(
  $UV run python "${PROJECT_DIR}/code/vlm_judge_ext.py"
  "$REPORT_PATH"
  "$TASK_INPUT"
  --model "$MODEL"
  --max-workers "$MAX_WORKERS"
  --provider "$PROVIDER"
  --output "$RAW_RESULT"
  --log-prefix "$LOG_PREFIX"
)
[ -n "$BASE_URL" ] && JUDGE_CMD+=(--base-url "$BASE_URL")
[ -n "$API_KEY" ]  && JUDGE_CMD+=(--api-key "$API_KEY")
[ -f "$EVAL_REPORT" ] && JUDGE_CMD+=(--eval-report "$EVAL_REPORT")

"${JUDGE_CMD[@]}"

# ── Inject metadata ──────────────────────────────────────────────
$UV run python -c "
import json, sys
with open('$RAW_RESULT') as f:
    data = json.load(f)
meta = {
    'report_path':  '$REPORT_PATH',
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

echo "[judge] ${MODEL} | ${CASE}/${VARIANT} ${MODE}/${RUNNER}/${RUN} → done" >&2
