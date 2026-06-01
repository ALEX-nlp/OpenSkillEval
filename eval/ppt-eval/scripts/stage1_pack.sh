#!/usr/bin/env bash
###############################################################################
# stage1_pack.sh — collect all final_deck.pptx into a single tar.gz pack.
#
# Does not modify original pptx files; only reads + copies.
#
# Usage:
#   bash eval/ppt-eval/scripts/stage1_pack.sh <JOBS_ROOT> [OUTPUT_TAR] [--full]
#
# Arguments:
#   JOBS_ROOT   required, jobs root directory (e.g. harbor/smoke_jobs)
#   OUTPUT_TAR  optional, output tar.gz path
#               default: eval_output/pptx_pack_<basename>_<timestamp>.tar.gz
#   --full      optional, ignore history and repack everything
#
# Incremental mechanism:
#   After each pack, record packed file paths into output/.packed_<basename>.txt
#   Next run automatically skips already-packed files, only packs new final_deck.pptx
#
# Output:
#   tar.gz contains:
#     pptx/
#       {variant}__{mode}__{runner}__{run}.pptx   (case prefix auto-added for multi-case)
#       ...
#     manifest.jsonl                              (each line maps filename to metadata)
#
# manifest.jsonl line format:
#   {"pptx_name": "...", "original_path": "...", "task_family": "...",
#    "case": "...", "variant": "...", "mode": "...", "runner": "...",
#    "run": "...", "trial": "..."}
###############################################################################
set -euo pipefail

# echo "PWD=$(pwd)"
# echo "WHOAMI=$(whoami)"
# echo "HOST=$(hostname)"
# echo "ARG1=$1"
# echo "REAL_ARG1=$(readlink -f "$1")"
# ls -ld "$1"
# find "$1" -maxdepth 3 | head -20

# -- Parse arguments ---------------------------------------------------------
FULL_MODE=false
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --full) FULL_MODE=true ;;
    *)      POSITIONAL+=("$arg") ;;
  esac
done
set -- "${POSITIONAL[@]}"

JOBS_ROOT="${1:?Usage: bash stage1_pack.sh <JOBS_ROOT> [OUTPUT_TAR] [--full]}"
JOBS_ROOT="$(cd "$JOBS_ROOT" && pwd)"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROJECT_DIR}/../.." && pwd)"

JOBS_BASENAME="$(basename "$JOBS_ROOT")"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

OUTPUT_DIR="${PROJECT_DIR}/output"
mkdir -p "$OUTPUT_DIR"
OUTPUT_TAR="${2:-${OUTPUT_DIR}/pptx_pack_${JOBS_BASENAME}_${TIMESTAMP}.tar.gz}"

# -- Incremental history file ------------------------------------------------
PACKED_HISTORY="${OUTPUT_DIR}/.packed_${JOBS_BASENAME}.txt"
if [ "$FULL_MODE" = true ]; then
  echo "[info] --full mode, ignoring history, full repack" >&2
  rm -f "$PACKED_HISTORY"
fi
touch "$PACKED_HISTORY"

# -- Collect all final_deck.pptx, filter already packed ----------------------
FOUND_PPTX=()
while IFS= read -r p; do
  FOUND_PPTX+=("$p")
done < <(find "$JOBS_ROOT" -name "final_deck.pptx" -type f 2>/dev/null | sort)

FOUND_TOTAL=${#FOUND_PPTX[@]}
if [ "$FOUND_TOTAL" -eq 0 ]; then
  echo "[error] no final_deck.pptx found under ${JOBS_ROOT}" >&2
  exit 1
fi

# Filter: skip already packed + verify the job actually finished successfully
ALL_PPTX=()
SKIP_PACKED=0
SKIP_INCOMPLETE=0
for p in "${FOUND_PPTX[@]}"; do
  # 1) Skip already packed
  if grep -qxF "$p" "$PACKED_HISTORY"; then
    SKIP_PACKED=$((SKIP_PACKED + 1))
    continue
  fi

  # 2) Verify job completion status
  #    path: .../{run}/{trial}/artifacts/final_deck.pptx
  #    result.json is under {run}/
  TRIAL_DIR="$(dirname "$(dirname "$p")")"
  RUN_DIR="$(dirname "$TRIAL_DIR")"
  RESULT_JSON="${RUN_DIR}/result.json"

  # No result.json -> task still running, skip
  if [ ! -f "$RESULT_JSON" ]; then
    echo "[skip] no result.json (may still be running): $p" >&2
    SKIP_INCOMPLETE=$((SKIP_INCOMPLETE + 1))
    continue
  fi

  # If file's status != ok in artifacts/manifest.json -> skip
  ART_MANIFEST="${TRIAL_DIR}/artifacts/manifest.json"
  if [ -f "$ART_MANIFEST" ]; then
    ART_OK="$(python3 -c "
import json,sys,os
items=json.load(open(sys.argv[1]))
target=os.path.basename(sys.argv[2])
for item in items:
    if item.get('destination','').endswith(target) and item.get('status')=='ok':
        print('ok'); break
else:
    print('fail')
" "$ART_MANIFEST" "final_deck.pptx" 2>/dev/null || echo "fail")"
    if [ "$ART_OK" != "ok" ]; then
      echo "[skip] artifact status not ok: $p" >&2
      SKIP_INCOMPLETE=$((SKIP_INCOMPLETE + 1))
      continue
    fi
  fi

  ALL_PPTX+=("$p")
done

TOTAL=${#ALL_PPTX[@]}
if [ "$TOTAL" -eq 0 ]; then
  echo "[info] found ${FOUND_TOTAL} pptx, ${SKIP_PACKED} already packed, ${SKIP_INCOMPLETE} unfinished/failed, no new files to pack" >&2
  exit 0
fi

echo "=============================================="
echo "  Stage 1: Pack PPTX files"
echo "=============================================="
echo "  jobs_root:    ${JOBS_ROOT}"
echo "  found:        ${FOUND_TOTAL} pptx files"
echo "  skip(packed): ${SKIP_PACKED}"
echo "  skip(unfin):  ${SKIP_INCOMPLETE}"
echo "  new:          ${TOTAL}"
echo "  output:       ${OUTPUT_TAR}"
echo "=============================================="

# -- uv environment ----------------------------------------------------------
unset VIRTUAL_ENV
UV="uv --project ${PROJECT_DIR}"

# -- Create temporary pack dir -----------------------------------------------
PACK_DIR="$(mktemp -d /tmp/pptx_pack_XXXXXX)"
PPTX_DIR="${PACK_DIR}/pptx"
mkdir -p "$PPTX_DIR"
MANIFEST="${PACK_DIR}/manifest.jsonl"

# -- Detect whether case prefix is needed (multi-case scenario) --------------
# Path format: .../{task_family}/{case}/{variant}/{mode}/{runner}/{run}/{trial}/artifacts/final_deck.pptx
declare -A SEEN_CASES
for pptx in "${ALL_PPTX[@]}"; do
  IFS='/' read -ra PARTS <<< "$pptx"
  N=${#PARTS[@]}
  CASE="${PARTS[$(( N - 8 ))]}"
  SEEN_CASES["$CASE"]=1
done

MULTI_CASE=false
if [ "${#SEEN_CASES[@]}" -gt 1 ]; then
  MULTI_CASE=true
  echo "[info] detected multiple cases (${#SEEN_CASES[@]}), filenames will get a case prefix" >&2
fi

# -- Copy + rename + write manifest ------------------------------------------
declare -A NAME_COUNT
COUNT=0

for pptx in "${ALL_PPTX[@]}"; do
  IFS='/' read -ra PARTS <<< "$pptx"
  N=${#PARTS[@]}

  TASK_FAMILY="${PARTS[$(( N - 9 ))]}"
  CASE="${PARTS[$(( N - 8 ))]}"
  VARIANT="${PARTS[$(( N - 7 ))]}"
  MODE="${PARTS[$(( N - 6 ))]}"
  RUNNER="${PARTS[$(( N - 5 ))]}"
  RUN="${PARTS[$(( N - 4 ))]}"
  TRIAL="${PARTS[$(( N - 3 ))]}"

  if [ "$MULTI_CASE" = true ]; then
    NEW_NAME="${CASE}__${VARIANT}__${MODE}__${RUNNER}__${RUN}.pptx"
  else
    NEW_NAME="${VARIANT}__${MODE}__${RUNNER}__${RUN}.pptx"
  fi

  # Handle edge-case name conflicts (same variant/mode/runner/run with multiple trials)
  if [ -f "${PPTX_DIR}/${NEW_NAME}" ]; then
    BASE="${NEW_NAME%.pptx}"
    SEQ="${NAME_COUNT[$NEW_NAME]:-1}"
    SEQ=$((SEQ + 1))
    NAME_COUNT["$NEW_NAME"]=$SEQ
    NEW_NAME="${BASE}__${SEQ}.pptx"
  fi

  cp "$pptx" "${PPTX_DIR}/${NEW_NAME}"
  COUNT=$((COUNT + 1))

  # Write manifest line (use python to generate correct JSON, avoiding shell escaping issues)
  python3 -c "
import json, sys
print(json.dumps({
    'pptx_name':     sys.argv[1],
    'original_path': sys.argv[2],
    'task_family':   sys.argv[3],
    'case':          sys.argv[4],
    'variant':       sys.argv[5],
    'mode':          sys.argv[6],
    'runner':        sys.argv[7],
    'run':           sys.argv[8],
    'trial':         sys.argv[9],
}, ensure_ascii=False))
" "$NEW_NAME" "$pptx" "$TASK_FAMILY" "$CASE" "$VARIANT" "$MODE" "$RUNNER" "$RUN" "$TRIAL" \
  >> "$MANIFEST"

  echo "  [${COUNT}/${TOTAL}] ${NEW_NAME}" >&2
done

# -- Pack --------------------------------------------------------------------
tar czf "$OUTPUT_TAR" -C "$PACK_DIR" pptx manifest.jsonl

# -- Record packed paths -----------------------------------------------------
printf '%s\n' "${ALL_PPTX[@]}" >> "$PACKED_HISTORY"

# -- Cleanup -----------------------------------------------------------------
rm -rf "$PACK_DIR"

echo ""
echo "=============================================="
echo "  Pack complete"
echo "  files:        ${COUNT} (new)"
echo "  total packed: $(wc -l < "$PACKED_HISTORY")"
echo "  output:       ${OUTPUT_TAR}"
echo "=============================================="
