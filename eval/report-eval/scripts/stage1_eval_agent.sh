#!/usr/bin/env bash
###############################################################################
# stage1_eval_agent.sh — runs the report-generation evaluation Agent (Stage 1) + VLM Judge (Stage 2)
#
# The eval Agent runs inside Docker, reads the agent-produced report + source data,
# verifies Data Accuracy and Fidelity with code, and outputs eval_report.json.
# This script then invokes VLM Judge for visual scoring (Stage 2).
#
# Full flow:
#   1. Locate agent output (artifacts/final_report.*)
#   2. Prepare eval task directory: copy benchmark data + agent output into Dockerfile context
#   3. harbor run executes the eval Agent → eval_report.json
#   4. judge_single.sh runs VLM Judge → judge_result.json
#
# Usage:
#   bash stage1_eval_agent.sh <ARTIFACTS_DIR>
#
# Arguments:
#   ARTIFACTS_DIR    required, the artifacts directory after harbor finishes
#                    e.g. harbor/jobs/.../run-01/.../artifacts/
#
# Environment variables:
#   RUN_ID           optional, specify run ID (default auto-generated timestamp)
#
# Judge list is passed in by pipeline.py via the JUDGES_SPEC env var.
#
# Prerequisites:
#   - uv and harbor are installed
#   - ARTIFACTS_DIR contains final_report.html or final_report.pdf
#   - the matching case data exists at tasks/report-generation/shared/cases/<case>/
###############################################################################
set -euo pipefail

ARTIFACTS_DIR="${1:?Usage: bash stage1_eval_agent.sh <ARTIFACTS_DIR>}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# pipeline.py derives REPO_ROOT from jobs_root and injects it via this env;
# falls back to the legacy logic (derived from script path) on direct bash invocation.
REPO_ROOT="${REPO_ROOT_OVERRIDE:-$(cd "${PROJECT_DIR}/../.." && pwd)}"
EVAL_TASK_TEMPLATE="${REPO_ROOT}/tasks/report-generation/scripts/evaluation"

# Bug fix: export RUN_ID to unify timestamps so child processes (judge_single.sh) write to the same dir
export RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"

# Judge list is injected by pipeline.py via the JUDGES_SPEC env var (one per line,
# format MODEL|PROVIDER|BASE_URL|API_KEY; any field can use @VAR placeholder).
if [ -z "${JUDGES_SPEC:-}" ]; then
  echo "[stage1] ERROR: JUDGES_SPEC not set (pass via pipeline.py's --judge argument)" >&2
  exit 1
fi
mapfile -t JUDGES <<< "$JUDGES_SPEC"
JUDGE_MODELS=()
for _jcfg in "${JUDGES[@]}"; do
  IFS='|' read -r _jm _ _ _ <<< "$_jcfg"
  JUDGE_MODELS+=("$_jm")
done
unset _jcfg _jm

# @VAR_NAME → read real value from env (injected by pipeline.py from judge.snippet)
_expand_at_var() {
  local v="$1"
  if [[ "$v" == @* ]]; then
    local n="${v#@}"
    local r="${!n:-}"
    if [ -z "$r" ]; then
      echo "[stage1] ERROR: --judge references \$$n but it is not defined in env (check agent_configs/snippets/judge.snippet)" >&2
      return 1
    fi
    echo "$r"
  else
    echo "$v"
  fi
}

# ── Parse metadata from artifacts path ───────────────────────────
# .../report-generation/{case}/{variant}/{mode}/{runner}/{run}/{trial}/artifacts
ARTIFACTS_DIR="$(cd "$ARTIFACTS_DIR" && pwd)"
IFS='/' read -ra _P <<< "$ARTIFACTS_DIR"
_N=${#_P[@]}
TASK_FAMILY="${_P[$((_N-8))]}"
CASE="${_P[$((_N-7))]}"
VARIANT="${_P[$((_N-6))]}"
MODE="${_P[$((_N-5))]}"
RUNNER="${_P[$((_N-4))]}"
RUN="${_P[$((_N-3))]}"
TRIAL="${_P[$((_N-2))]}"

CASE_DIR="${REPO_ROOT}/tasks/${TASK_FAMILY}/shared/cases/${CASE}"
SHORT_ID="${CASE}/${VARIANT} ${MODE}/${RUNNER}/${RUN}"

# ── Validation ───────────────────────────────────────────────────
err=0
[ -d "$ARTIFACTS_DIR" ]  || { echo "[stage1] ERROR: artifacts dir missing" >&2; err=1; }
[ -d "$CASE_DIR" ]       || { echo "[stage1] ERROR: case dir missing: $CASE_DIR" >&2; err=1; }
[ -d "$EVAL_TASK_TEMPLATE" ] || { echo "[stage1] ERROR: eval template missing" >&2; err=1; }

# Check for report file
REPORT=""
for name in final_report.html final_report.pdf report.html report.pdf; do
  [ -f "${ARTIFACTS_DIR}/${name}" ] && REPORT="${name}" && break
done
[ -n "$REPORT" ] || { echo "[stage1] ERROR: no report found" >&2; err=1; }
[ $err -ne 0 ] && exit 1

# ── Stage 1: prepare eval task directory ─────────────────────────
EVAL_WORK="$(mktemp -d /tmp/report_eval_task_XXXXXX)"
TASK_LOG="$(mktemp /tmp/report_stage1_log_XXXXXX.log)"
JUDGE_PIDS=()

echo "[stage1] ${SHORT_ID} | ${REPORT} | judges: ${JUDGE_MODELS[*]}" >> "$TASK_LOG"

_docker_cleanup() {
  local project_dir="$1"
  local flt="label=com.docker.compose.project.working_dir=${project_dir}"

  # Get compose project name
  local projects
  projects=$(docker ps -a --filter "$flt" --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | sort -u)

  # Remove containers
  local cids
  cids=$(docker ps -a --filter "$flt" -q 2>/dev/null)
  [ -n "$cids" ] && docker rm -f $cids >/dev/null 2>&1

  # Remove volumes and images by project
  for proj in $projects; do
    [ -z "$proj" ] && continue
    local vids
    vids=$(docker volume ls --filter "label=com.docker.compose.project=${proj}" -q 2>/dev/null)
    [ -n "$vids" ] && docker volume rm -f $vids >/dev/null 2>&1

    local iids
    iids=$(docker images --filter "label=com.docker.compose.project=${proj}" -q 2>/dev/null)
    [ -n "$iids" ] && docker rmi -f $iids >/dev/null 2>&1
  done

  # Fallback: clean leftover images by image-name prefix (when harbor removed containers but not images)
  local work_name
  work_name=$(basename "$(dirname "$project_dir")")
  local prefix
  prefix=$(echo "${work_name:0:32}" | sed 's/[_-]*$//' | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g')
  local orphan_ids
  orphan_ids=$(docker images --filter "reference=${prefix}__*" -q 2>/dev/null)
  [ -n "$orphan_ids" ] && docker rmi -f $orphan_ids >/dev/null 2>&1
}

_cleanup() {
  set +e  # Must disable -e: killing an already-exited process returns non-zero, which would abort the trap and make the script exit non-zero
  for pid in "${JUDGE_PIDS[@]}"; do
    kill "$pid" 2>/dev/null
  done
  wait 2>/dev/null
  _docker_cleanup "${EVAL_WORK}/environment"
  rm -rf "$EVAL_WORK"
  rm -f "$TASK_LOG"
  return 0
}
trap _cleanup EXIT
trap 'echo "[stage1] interrupted" >&2; exit 130' INT TERM

# Copy eval task structure
cp -r "${EVAL_TASK_TEMPLATE}/"* "$EVAL_WORK/"

# Prepare benchmark data
BENCH_DIR="${EVAL_WORK}/environment/benchmark"
rm -rf "$BENCH_DIR"
mkdir -p "$BENCH_DIR"

# Copy source data from the case
cp "${CASE_DIR}/task_input.json" "$BENCH_DIR/"
[ -f "${CASE_DIR}/source_brief.md" ] && cp "${CASE_DIR}/source_brief.md" "$BENCH_DIR/"
for f in "${CASE_DIR}"/*.csv; do
  [ -f "$f" ] && cp "$f" "${BENCH_DIR}/data.csv" && break
done

# Prepare agent output directory (will be COPY'd into /app/output/)
AGENT_OUTPUT="${EVAL_WORK}/environment/agent_output"
mkdir -p "$AGENT_OUTPUT"
cp "${ARTIFACTS_DIR}/${REPORT}" "$AGENT_OUTPUT/"
# Also copy other potentially useful outputs
for f in "${ARTIFACTS_DIR}"/*.html "${ARTIFACTS_DIR}"/*.pdf "${ARTIFACTS_DIR}"/*.json; do
  [ -f "$f" ] && cp "$f" "$AGENT_OUTPUT/" 2>/dev/null || true
done

# Patch Dockerfile: append COPY agent_output → /app/output/
echo "" >> "${EVAL_WORK}/environment/Dockerfile"
echo "# Injected by stage1_eval_agent.sh: copy agent output into container" >> "${EVAL_WORK}/environment/Dockerfile"
echo "COPY agent_output /app/output" >> "${EVAL_WORK}/environment/Dockerfile"

# ── Stage 1: harbor run executes the eval Agent ──────────────────
EVAL_OUT_ROOT="${PROJECT_DIR}/output/${RUN_ID}/${TASK_FAMILY}/${CASE}/${VARIANT}/${MODE}/${RUNNER}/${RUN}/${TRIAL}"
mkdir -p "$EVAL_OUT_ROOT"
EVAL_JOBS="${EVAL_OUT_ROOT}/eval_agent"
mkdir -p "$EVAL_JOBS"

EVAL_REPORT=""

# Check if eval_report.json already exists (skip re-running harbor)
if [ -f "${EVAL_OUT_ROOT}/eval_report.json" ]; then
  EVAL_REPORT="${EVAL_OUT_ROOT}/eval_report.json"
  echo "[stage1] ${SHORT_ID} | eval_report.json already exists, skipping harbor" >> "$TASK_LOG"
elif [ -f "${ARTIFACTS_DIR}/eval_report.json" ]; then
  EVAL_REPORT="${ARTIFACTS_DIR}/eval_report.json"
  cp "$EVAL_REPORT" "${EVAL_OUT_ROOT}/eval_report.json" 2>/dev/null || true
  echo "[stage1] ${SHORT_ID} | eval_report.json found in artifacts, skipping harbor" >> "$TASK_LOG"
else
  echo "[stage1] ${SHORT_ID} | harbor eval agent running..." >> "$TASK_LOG"

  # Clean leftover job directory from the previous failure, otherwise harbor refuses to overwrite
  rm -rf "$EVAL_JOBS"
  mkdir -p "$EVAL_JOBS"

  HARBOR_CMD=(
    uv run harbor run
    -p "$EVAL_WORK"
    --model "${EVAL_MODEL:-claude-opus-4-6}"
    --agent "${EVAL_AGENT:-claude-code}"
    --timeout-multiplier "${EVAL_TIMEOUT_MULT:-3.0}"
    --jobs-dir "$EVAL_JOBS"
    --job-name "eval"
    --agent-kwarg max_turns=100
    --disable-verification
    --artifact "/app/eval_output/eval_report.json"
    --artifact "/app/eval_output"
  )

  HARBOR_LOG="${EVAL_OUT_ROOT}/harbor.log"
  "${HARBOR_CMD[@]}" > "$HARBOR_LOG" 2>&1 || {
    echo "[stage1] ${SHORT_ID} | harbor eval agent FAILED (see ${HARBOR_LOG})" >> "$TASK_LOG"
  }

  # ── Locate eval_report.json ────────────────────────────────────
  FOUND_REPORT=$(find "$EVAL_JOBS" -name "eval_report.json" -type f 2>/dev/null | head -1)
  if [ -n "$FOUND_REPORT" ]; then
    EVAL_REPORT="$FOUND_REPORT"
    cp "$EVAL_REPORT" "${EVAL_OUT_ROOT}/eval_report.json" 2>/dev/null || true
    cp "$EVAL_REPORT" "${ARTIFACTS_DIR}/eval_report.json" 2>/dev/null || true
    echo "[stage1] ${SHORT_ID} | eval_report.json found" >> "$TASK_LOG"
  else
    echo "[stage1] ${SHORT_ID} | eval_report.json NOT found, aborting (will not run VLM judges)" >> "$TASK_LOG"
    cp "$TASK_LOG" "${EVAL_OUT_ROOT}/stage1.log" 2>/dev/null || true
    exit 1
  fi
fi

# ── Stage 2: VLM Judge (multi-model concurrency) ─────────────────
echo "[stage1] ${SHORT_ID} | VLM judges starting..." >> "$TASK_LOG"

JUDGE_PIDS=()
for judge_cfg in "${JUDGES[@]}"; do
  IFS='|' read -r j_model j_provider j_url j_key <<< "$judge_cfg"
  j_model=$(_expand_at_var "$j_model") || exit 1
  j_provider=$(_expand_at_var "$j_provider") || exit 1
  j_url=$(_expand_at_var "$j_url") || exit 1
  j_key=$(_expand_at_var "$j_key") || exit 1
  safe_model="$(echo "$j_model" | tr ' /:' '---')"
  result_file="${EVAL_OUT_ROOT}/judge_result_${safe_model}.json"
  if [ -f "$result_file" ]; then
    echo "[stage1] ${SHORT_ID} | ${j_model} judge_result already exists, skipping" >> "$TASK_LOG"
    continue
  fi
  bash "${SCRIPT_DIR}/judge_single.sh" \
    "${ARTIFACTS_DIR}" "" "$j_model" "${MAX_WORKERS:-3}" "$j_provider" "$j_url" "$j_key" >> "$TASK_LOG" 2>&1 &
  JUDGE_PIDS+=($!)
done

# Wait for all judges to finish
_judge_fail=0
for pid in "${JUDGE_PIDS[@]}"; do
  wait "$pid" || { echo "[stage1] ${SHORT_ID} | judge pid $pid failed" >> "$TASK_LOG"; _judge_fail=1; }
done

# ── Save detailed log to output directory ────────────────────────
cp "$TASK_LOG" "${EVAL_OUT_ROOT}/stage1.log" 2>/dev/null || true

if [ "$_judge_fail" -ne 0 ]; then
  echo "[stage1] ${SHORT_ID} | FAILED (one or more judges failed)" >> "$TASK_LOG"
  cp "$TASK_LOG" "${EVAL_OUT_ROOT}/stage1.log" 2>/dev/null || true
  exit 1
fi

echo "[stage1] ${SHORT_ID} | done" >> "$TASK_LOG"
