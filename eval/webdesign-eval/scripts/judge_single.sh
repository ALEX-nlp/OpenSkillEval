#!/usr/bin/env bash
###############################################################################
# judge_single.sh — Web Design VLM Judge evaluation (single file)
#
# Flow: vlm_judge_ext (screenshots + eval_report.json) → inject metadata
#
# Usage:
#   bash eval/webdesign-eval/scripts/judge_single.sh <EVAL_OUTPUT_DIR> [OUTPUT_PATH] [MODEL] [MAX_WORKERS] \
#        [PROVIDER] [BASE_URL] [API_KEY]
#
# Args:
#   EVAL_OUTPUT_DIR  required, Eval Agent output dir (contains screenshots/ and eval_report.json)
#   OUTPUT_PATH      optional, path to save scoring result JSON
#                    default: output/<RUN_ID>/.../<variant>/.../judge_result_<safe_model>.json
#   MODEL            optional, VLM model name (default claude-opus-4-6)
#   MAX_WORKERS      optional, vlm_judge internal API concurrency (default 3)
#   PROVIDER         optional, anthropic or openai (default anthropic)
#   BASE_URL         optional, API base URL
#   API_KEY          optional, API key
#
# Output JSON format:
#   { "eval_dir": "...", "task_family": "...", "case": "...", "variant": "...",
#     "mode": "...", "runner": "...", "run": "...", "trial": "...",
#     "judge_model": "...", "visual_design": {...}, "layout": {...},
#     "content": {...}, "completeness": {...}, "navigation": {...},
#     "interactions": {...}, "responsiveness": {...}, "data_display": {...},
#     "weighted_breakdown": [...], "overall": 3.8, "overall_normalized": 0.76 }
###############################################################################
set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────
# Positional args (kept for backward compatibility with standalone use)
EVAL_DIR="${1:?Usage: bash judge_single.sh <EVAL_DIR> [OUTPUT] [MODEL] [WORKERS] [PROVIDER] [BASE_URL] [API_KEY] [--task-family X --case X --variant X --mode X --runner X --run X --trial X]}"
OUTPUT_ARG="${2:-}"
MODEL="${3:-claude-opus-4-6}"
MAX_WORKERS="${4:-3}"
PROVIDER="${5:-anthropic}"
BASE_URL="${6:-}"
API_KEY="${7:-}"
# Remaining are metadata long flags
shift $(( $# >= 7 ? 7 : $# ))

META_TF="" META_CA="" META_VA="" META_MO="" META_RU="" META_RN="" META_TR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --task-family) META_TF="${2:-}"; shift 2;;
    --case)        META_CA="${2:-}"; shift 2;;
    --variant)     META_VA="${2:-}"; shift 2;;
    --mode)        META_MO="${2:-}"; shift 2;;
    --runner)      META_RU="${2:-}"; shift 2;;
    --run)         META_RN="${2:-}"; shift 2;;
    --trial)       META_TR="${2:-}"; shift 2;;
    *) echo "[error] unknown arg: $1" >&2; exit 1;;
  esac
done

# Sanitize model name for use in filenames
SAFE_MODEL="$(echo "$MODEL" | tr ' /:' '---')"

# RUN_ID: passed in by upper-level script, or auto-generated timestamp
export RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Metadata: explicit flags take precedence, otherwise inferred from EVAL_DIR path ──
# Path inference assumes EVAL_DIR looks like:
#   .../{task_family}/{case}/{variant}/{mode}/{runner}/{run}/{trial}/eval_output/
IFS='/' read -ra _PARTS <<< "$EVAL_DIR"
_N=${#_PARTS[@]}
TASK_FAMILY="${META_TF:-${_PARTS[$((_N-8))]:-unknown}}"
CASE="${META_CA:-${_PARTS[$((_N-7))]:-unknown}}"
VARIANT="${META_VA:-${_PARTS[$((_N-6))]:-unknown}}"
MODE="${META_MO:-${_PARTS[$((_N-5))]:-unknown}}"
RUNNER="${META_RU:-${_PARTS[$((_N-4))]:-unknown}}"
RUN="${META_RN:-${_PARTS[$((_N-3))]:-unknown}}"
TRIAL="${META_TR:-${_PARTS[$((_N-2))]:-unknown}}"

# OUTPUT defaults to a metadata-based subdirectory
if [ -n "$OUTPUT_ARG" ]; then
  OUTPUT="$OUTPUT_ARG"
else
  OUTPUT="${PROJECT_DIR}/output/${RUN_ID}/${TASK_FAMILY}/${CASE}/${VARIANT}/${MODE}/${RUNNER}/${RUN}/${TRIAL}/judge_result_${SAFE_MODEL}.json"
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

REPO_ROOT="${REPO_ROOT_OVERRIDE:-$(cd "${PROJECT_DIR}/../.." && pwd)}"
unset VIRTUAL_ENV
UV="uv --project ${PROJECT_DIR}"

CASE_DIR="${REPO_ROOT}/tasks/${TASK_FAMILY}/shared/cases/${CASE}"
TASK_INPUT="${CASE_DIR}/task_input.json"
SOURCE_BRIEF="${CASE_DIR}/source_brief.md"

echo "[judge] ${MODEL} | ${CASE}/${VARIANT} ${MODE}/${RUNNER}/${RUN}" >&2

# ── Validation ───────────────────────────────────────────────────
err=0
[ -d "$EVAL_DIR" ] || { echo "[error] not a directory: $EVAL_DIR" >&2; err=1; }
[ -f "$TASK_INPUT" ] || { echo "[error] missing: $TASK_INPUT" >&2; err=1; }
[ -f "$SOURCE_BRIEF" ] || { echo "[error] missing: $SOURCE_BRIEF" >&2; err=1; }
[ $err -ne 0 ] && exit 1

if [ -d "${EVAL_DIR}/screenshots" ]; then
  echo "[info] screenshots: $(ls -1 "${EVAL_DIR}/screenshots" | wc -l) files" >&2
else
  echo "[warn] no screenshots/ dir — VLM scoring may be limited" >&2
fi

# ── VLM Judge → temp file ────────────────────────────────────────
RAW_RESULT="$(mktemp /tmp/webdesign_judge_raw_XXXXXX.json)"

JUDGE_CMD=(
  $UV run python "${PROJECT_DIR}/code/vlm_judge_ext.py"
  "$EVAL_DIR"
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

# ── Inject metadata ──────────────────────────────────────────────
$UV run python -c "
import json, sys
with open('$RAW_RESULT') as f:
    data = json.load(f)
meta = {
    'eval_dir':     '$EVAL_DIR',
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
