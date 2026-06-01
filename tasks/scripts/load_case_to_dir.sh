#!/usr/bin/env bash
###############################################################################
# load_case_to_dir.sh — load a case's data into an arbitrary target directory.
#
# Used by run_variants.py: each task first copies the variant to a tmp dir,
# then calls this script to load the case data into that tmp dir. The 5th
# argument optionally overrides the target directory.
#
# Usage:
#   bash load_case_to_dir.sh <family-root> <case-name> <variant-name> [mode] [target-dir]
#
# Arguments:
#   target-dir  target directory (default: <family-root>/variants/<variant-name>)
#
# Modes:
#   force-using  prepend a skill-reference paragraph to instruction.md
#   no-force     leave instruction.md unchanged (default)
###############################################################################
set -euo pipefail

ROOT_DIR="${1:?Usage: load_case_to_dir.sh <family-root> <case-name> <variant-name> [mode] [target-dir]}"
CASE_NAME="${2:?Usage: load_case_to_dir.sh <family-root> <case-name> <variant-name> [mode] [target-dir]}"
TARGET_VARIANT="${3:?Usage: load_case_to_dir.sh <family-root> <case-name> <variant-name> [mode] [target-dir]}"
MODE="${4:-no-force}"

# ── variant → skill_name mapping (used in force-using mode) ──
declare -A VARIANT_SKILL_MAP=(
  # data-visualization
  [data-viz-anthropics]="data-visualization"
  [data-viz-inferen]="data-visualization"
  [data-viz-mermaid-tools]="mermaid-tools"
  [data-viz-mermaidjs]="mermaidjs-v11"
  [data-viz-tufte]="tufte-data-viz"
  [data-viz-visual-explainer]="visual-explainer"
  [data-viz-visualize]="visualize"
  # poster-generation
  [poster-generation-antv-infographic]="infographic-creator"
  [poster-generation-canvas-design]="canvas-design"
  [poster-generation-paper-poster]="paper-poster"
  [poster-generation-visualize]="visualize"
  # ppt-generation
  [ppt-generation-anthropics-pptx]="pptx"
  [ppt-generation-deer-flow]="ppt-generation"
  [ppt-generation-frontend-slides]="frontend-slides"
  [ppt-generation-minimax-pptx]="pptx-generator"
  [ppt-generation-ppt-master]="ppt-master"
  [ppt-generation-pptx-manipulation]="pptx-manipulation"
  [ppt-generation-powerpoint-pptx]="powerpoint-pptx"
  # report-generation
  [report-generation-business-auto]="report-generator"
  [report-generation-chatgpt-pdf]="report-generator"
  [report-generation-claude-office]="report-generator"
  [report-generation-clawfu]="report-generator"
  [report-generation-devkit]="report-generator"
  [report-generation-excel-report]="excel-report-generator"
  # web-design
  [web-design-expert]="web-design-expert"
  [web-design-frontend-ultimate]="frontend-design-ultimate"
  [web-design-loom]="web-designer"
  [web-design-seo-local-business]="seo-local-business"
  [web-design-superdesign]="frontend-design"
  [web-design-ui-styling]="ui-styling"
  [web-design-ui-ux-pro-max]="ui-ux-pro-max"
  [web-design-web-frameworks]="web-frameworks"
)

CASE_DIR="${ROOT_DIR}/shared/cases/${CASE_NAME}"
HARNESS_DIR="${ROOT_DIR}/shared/harness"
VARIANT_DIR="${5:-${ROOT_DIR}/variants/${TARGET_VARIANT}}"
BENCH_DST="${VARIANT_DIR}/environment/benchmark"

if [ ! -d "${CASE_DIR}" ]; then
  echo "[error] case '${CASE_NAME}' not found at ${CASE_DIR}" >&2
  exit 1
fi

if [ ! -d "${VARIANT_DIR}" ]; then
  echo "[error] target dir '${VARIANT_DIR}' not found" >&2
  exit 1
fi

echo "[load] ${CASE_NAME} → ${TARGET_VARIANT} (mode=${MODE})"

# benchmark data: wipe target then copy in
rm -rf "$BENCH_DST"
mkdir -p "$BENCH_DST"

cp "${CASE_DIR}/task_input.json" "${BENCH_DST}/"
[ -f "${CASE_DIR}/source_brief.md" ]  && cp "${CASE_DIR}/source_brief.md"  "${BENCH_DST}/"
[ -f "${CASE_DIR}/source_data.json" ] && cp "${CASE_DIR}/source_data.json" "${BENCH_DST}/"

if [ -d "${CASE_DIR}/assets" ] && [ "$(ls -A "${CASE_DIR}/assets" 2>/dev/null)" ]; then
  cp -r "${CASE_DIR}/assets" "${BENCH_DST}/"
fi

for f in "${CASE_DIR}"/*.csv; do
  [ -f "$f" ] && cp "$f" "${BENCH_DST}/"
done

# instruction.md → variant root
[ -f "${CASE_DIR}/instruction.md" ] && cp "${CASE_DIR}/instruction.md" "${VARIANT_DIR}/instruction.md"

# force-using: prepend a skill-reference paragraph to instruction.md
if [ "$MODE" = "force-using" ] && [ -f "${VARIANT_DIR}/instruction.md" ]; then
  SKILL_NAME="${VARIANT_SKILL_MAP[$TARGET_VARIANT]:-}"
  if [ -n "$SKILL_NAME" ]; then
    FORCE_LINE="**Follow the provided skill first** — Use the skill named \`${SKILL_NAME}\` as your primary workflow. Each skill has its own generation approach that produces higher-quality results. Leverage the skill's design system, templates, and rendering pipeline to maximize output quality."
    printf '%s\n\n%s' "$FORCE_LINE" "$(cat "${VARIANT_DIR}/instruction.md")" > "${VARIANT_DIR}/instruction.md"
    echo "[load] force-using: prepended skill=\"${SKILL_NAME}\" to instruction.md"
  else
    echo "[warn] force-using: no skill mapping for variant '${TARGET_VARIANT}', instruction.md unchanged" >&2
  fi
fi

# harness files
mkdir -p "${VARIANT_DIR}/solution" "${VARIANT_DIR}/tests"
[ -f "${HARNESS_DIR}/solve.sh" ]        && cp "${HARNESS_DIR}/solve.sh"        "${VARIANT_DIR}/solution/solve.sh"
[ -f "${HARNESS_DIR}/test.sh" ]          && cp "${HARNESS_DIR}/test.sh"          "${VARIANT_DIR}/tests/test.sh"
[ -f "${HARNESS_DIR}/test_outputs.py" ]  && cp "${HARNESS_DIR}/test_outputs.py"  "${VARIANT_DIR}/tests/test_outputs.py"
# instruction.md comes from the case, not from harness (avoid overwriting case-specific instructions)

echo "[load] done"
