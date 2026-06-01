#!/bin/bash
# Extract pptx files from tar.gz, convert each to PNG screenshots in batch,
# then re-pack into a screenshot tar.gz.
# Usage: bash convert.sh <input.tar.gz> [output.tar.gz]
#
# Output tar.gz structure:
#   {pptx-basename-without-.pptx}/slide_001.png, slide_002.png, ...
#
# Requires: Microsoft PowerPoint, python3, pymupdf (pip3 install pymupdf)

set -euo pipefail

INPUT_TAR="${1:?Usage: bash convert.sh <input.tar.gz> [output.tar.gz]}"
OUTPUT_TAR="${2:-${INPUT_TAR%.tar.gz}_screenshots.tar.gz}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="$SCRIPT_DIR/_work"
EXTRACT_DIR="$WORK_DIR/extracted"
PDF_DIR="$WORK_DIR/pdf"
PNG_DIR="$WORK_DIR/png"
REPAIR_LOG="$WORK_DIR/repair.log"
CURRENT_FILE="$WORK_DIR/current_file"
mkdir -p "$EXTRACT_DIR" "$PDF_DIR" "$PNG_DIR"
touch "$REPAIR_LOG"
: > "$CURRENT_FILE"

AUTO_CLICK_PID=""
cleanup() {
  # Stop the background auto-click process
  if [ -n "$AUTO_CLICK_PID" ]; then
    kill "$AUTO_CLICK_PID" 2>/dev/null || true
    wait "$AUTO_CLICK_PID" 2>/dev/null || true
  fi
  # rm -rf "$WORK_DIR"  # debug mode: keep intermediate files
  echo "  Intermediate files kept at: $WORK_DIR"
}
trap cleanup EXIT

# === Step 1: extract tar.gz ===
# Always extract: new pptx files in subsequent batches accumulate in EXTRACT_DIR;
# same-name files get overwritten (identical content, no effect).
echo "=== Step 1: extract $INPUT_TAR ==="
tar xzf "$INPUT_TAR" -C "$EXTRACT_DIR"
# Strip macOS quarantine attr so PowerPoint doesn't pop the permission dialog.
xattr -rd com.apple.quarantine "$EXTRACT_DIR" 2>/dev/null || true

# Find all pptx files (any nesting depth).
PPTX_FILES=()
while IFS= read -r -d '' f; do
  PPTX_FILES+=("$f")
done < <(find "$EXTRACT_DIR" -name '*.pptx' ! -name '~\$*' -print0 | sort -z)

if [ ${#PPTX_FILES[@]} -eq 0 ]; then
  echo "  No .pptx files found"
  exit 1
fi
echo "  Found ${#PPTX_FILES[@]} pptx files"

# === Step 2: convert pptx -> pdf in batch via PowerPoint AppleScript ===
echo ""
echo "=== Step 2: PPTX -> PDF ==="

# Background loop: auto-click Repair/OK/Continue dialogs.
# When a "Repair" button gets clicked, log the currently-converting filename
# to $REPAIR_LOG.
# Note: the Chinese button names (修复, 确定, 好, 是, 继续) are kept on purpose —
# they match PowerPoint's localized UI on Chinese-locale macOS.
(while true; do
  clicked=$(osascript <<'AUTOCLICK' 2>/dev/null
tell application "System Events"
  if exists process "Microsoft PowerPoint" then
    tell process "Microsoft PowerPoint"
      set repairNames to {"修复", "Repair"}
      set otherNames to {"确定", "好", "OK", "是", "Yes", "继续", "Continue"}
      set allNames to repairNames & otherNames
      repeat with w in windows
        try
          repeat with s in sheets of w
            repeat with b in buttons of s
              try
                set bName to name of b
                if allNames contains bName then
                  click b
                  if repairNames contains bName then
                    return "repair"
                  else
                    return "other"
                  end if
                end if
              end try
            end repeat
          end repeat
        end try
        try
          repeat with b in buttons of w
            try
              set bName to name of b
              if allNames contains bName then
                click b
                if repairNames contains bName then
                  return "repair"
                else
                  return "other"
                end if
              end if
            end try
          end repeat
        end try
      end repeat
    end tell
  end if
  return ""
end tell
AUTOCLICK
)
  if [ "$clicked" = "repair" ]; then
    cur=$(cat "$CURRENT_FILE" 2>/dev/null || echo "unknown")
    echo "$cur" >> "$REPAIR_LOG"
  fi
  sleep 0.5
done) &
AUTO_CLICK_PID=$!
echo "  Background auto-click started for Repair/OK dialogs (PID: $AUTO_CLICK_PID)"
FAILED_FILES=()
SUCCESS_COUNT=0

convert_one_pptx() {
  local full_path="$1"
  local pdf_path="$2"
  osascript <<APPLESCRIPT
tell application "Microsoft PowerPoint"
  activate
  open POSIX file "$full_path"
  delay 3
  save active presentation in POSIX file "$pdf_path" as save as PDF
  close active presentation saving no
end tell
APPLESCRIPT
}

for f in "${PPTX_FILES[@]}"; do
  BASENAME="$(basename "$f" .pptx)"
  FULL_PATH="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
  PDF_PATH="$(cd "$PDF_DIR" && pwd)/${BASENAME}.pdf"

  if [ -s "$PDF_PATH" ]; then
    echo "  skip (PDF already exists): $BASENAME"
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    continue
  fi
  echo "  convert: $BASENAME.pptx -> PDF"
  echo "$BASENAME" > "$CURRENT_FILE"
  if convert_one_pptx "$FULL_PATH" "$PDF_PATH" 2>/dev/null; then
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
  else
    echo "    x failed (file may be corrupt): $BASENAME"
    FAILED_FILES+=("$BASENAME")
  fi
done

echo "  Successfully converted $SUCCESS_COUNT / ${#PPTX_FILES[@]} files"
if [ ${#FAILED_FILES[@]} -gt 0 ]; then
  echo "  Failures:"
  for name in "${FAILED_FILES[@]}"; do
    echo "    - $name"
  done
fi

# === Per-runner failure stats ===
echo ""
echo "=== Per-runner conversion stats ==="
python3 -c "
import sys, collections

failed_names = [x for x in sys.argv[1].split(',') if x]
all_names = [x for x in sys.argv[2].split(',') if x]
repair_log = sys.argv[3]

def parse_runner(name):
    # Filename layout: {case}__{variant}__{mode}__{runner}__{run}
    parts = name.split('__')
    if len(parts) >= 5:
        return parts[-2]  # runner
    return 'unknown'

total = collections.Counter()
failed = collections.Counter()
repair = collections.Counter()     # per-runner Repair-dialog hit count
repair_files = set()                # files that triggered Repair at least once

try:
    with open(repair_log) as fh:
        for line in fh:
            name = line.strip()
            if not name:
                continue
            repair[parse_runner(name)] += 1
            repair_files.add(name)
except FileNotFoundError:
    pass

for name in all_names:
    total[parse_runner(name)] += 1
for name in failed_names:
    failed[parse_runner(name)] += 1

print(f\"{'runner':<55} {'total':>5} {'fail':>5} {'pass':>5} {'rep':>4} {'fail%':>6}\")
print('-' * 85)
for runner in sorted(total):
    t = total[runner]
    f = failed.get(runner, 0)
    p = t - f
    r = repair.get(runner, 0)
    rate = f'{f/t*100:.0f}%' if t > 0 else '-'
    print(f'{runner:<55} {t:>5} {f:>5} {p:>5} {r:>4} {rate:>6}')
print('-' * 85)
tt, ft, rt = sum(total.values()), sum(failed.values()), sum(repair.values())
print(f\"{'TOTAL':<55} {tt:>5} {ft:>5} {tt-ft:>5} {rt:>4} {ft/tt*100:.0f}%\")
print()
print(f'Repair dialog triggered {sum(repair.values())} times across {len(repair_files)} files')
" "$(IFS=,; echo "${FAILED_FILES[*]}")" "$(for f in "${PPTX_FILES[@]}"; do basename "$f" .pptx; done | tr '\n' ',')" "$REPAIR_LOG"

# === Step 3: rasterise every PDF page to PNG via pymupdf ===
echo ""
echo "=== Step 3: PDF -> PNG ==="
python3 -c "
import fitz, sys, os

pdf_dir = sys.argv[1]
png_dir = sys.argv[2]

for pdf_name in sorted(os.listdir(pdf_dir)):
    if not pdf_name.endswith('.pdf'):
        continue
    if pdf_name.startswith('~\$'):
        print(f'  skip temp file: {pdf_name}')
        continue
    base = pdf_name[:-4]
    slide_dir = os.path.join(png_dir, base)
    if os.path.isdir(slide_dir) and any(n.endswith('.png') for n in os.listdir(slide_dir)):
        print(f'  skip (PNG already exists): {base}')
        continue
    os.makedirs(slide_dir, exist_ok=True)

    doc = fitz.open(os.path.join(pdf_dir, pdf_name))
    print(f'  {base}: {len(doc)} pages')
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        pix.save(os.path.join(slide_dir, f'slide_{i+1:03d}.png'))
    doc.close()
" "$PDF_DIR" "$PNG_DIR"

# === Step 4: pack into output tar.gz ===
echo ""
echo "=== Step 4: pack -> $OUTPUT_TAR ==="
# Find the manifest.jsonl from the original pack (in EXTRACT_DIR), copy it into
# PNG_DIR so the final tar is self-contained: the Linux-side pipeline.py can
# locate it automatically without extra arguments.
MANIFEST_SRC="$(find "$EXTRACT_DIR" -name manifest.jsonl -type f 2>/dev/null | head -1)"
if [ -n "$MANIFEST_SRC" ]; then
  cp "$MANIFEST_SRC" "$PNG_DIR/manifest.jsonl"
  echo "  including manifest.jsonl: $MANIFEST_SRC"
else
  echo "  [warn] manifest.jsonl not found in the original pack; pipeline.py will need an explicit --manifest"
fi
# Pack from inside PNG_DIR so the tar's top level is {pptx-name}/ subdirs + manifest.jsonl.
tar czf "$OUTPUT_TAR" -C "$PNG_DIR" .

echo ""
echo "=== Done ==="
echo "Output: $OUTPUT_TAR"
for d in "$PNG_DIR"/*/; do
  [ -d "$d" ] || continue
  n=$(ls "$d"/*.png 2>/dev/null | wc -l | tr -d ' ')
  echo "  $(basename "$d")/  ($n PNG)"
done
