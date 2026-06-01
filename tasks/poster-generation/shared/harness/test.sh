#!/usr/bin/env bash
set -uo pipefail
cd /app
mkdir -p /logs/verifier

########################################################################
# Stage 1: Deterministic checks (scored 0.0 – 1.0 based on pass ratio)
########################################################################

# Run pytest in JUnit XML mode so we can count pass/fail
python3 -m pytest -q --tb=short --junitxml=/logs/verifier/junit.xml /tests/test_outputs.py 2>&1 || true

# Parse JUnit XML to compute a fractional score
python3 - <<'PYSCORE' || echo "0.0" > /logs/verifier/reward.txt
import xml.etree.ElementTree as ET
from pathlib import Path

junit = Path("/logs/verifier/junit.xml")
reward = Path("/logs/verifier/reward.txt")

if not junit.exists():
    reward.write_text("0.0\n")
    raise SystemExit(0)

tree = ET.parse(junit)
root = tree.getroot()

# JUnit XML: <testsuite tests="N" errors="E" failures="F" skipped="S" ...>
suite = root if root.tag == "testsuite" else root.find("testsuite")
total = int(suite.get("tests", "0"))
errors = int(suite.get("errors", "0"))
failures = int(suite.get("failures", "0"))
skipped = int(suite.get("skipped", "0"))
passed = total - errors - failures - skipped
effective_total = total - skipped

if effective_total == 0:
    score = 0.0
else:
    score = round(passed / effective_total, 4)

reward.write_text(f"{score}\n")
print(f"[verifier] deterministic score: {passed}/{effective_total} tests passed (skipped {skipped}) → reward = {score}")
PYSCORE

########################################################################
# Stage 2: VLM Judge (4 dimensions, 1-5 scale, weighted overall)
########################################################################

STAGE1_SCORE=$(cat /logs/verifier/reward.txt 2>/dev/null || echo "0.0")

# Only run Stage 2 if Stage 1 passed (score > 0)
if python3 -c "import sys; sys.exit(0 if float('${STAGE1_SCORE}') > 0 else 1)" 2>/dev/null; then
    echo "[verifier] Stage 1 passed (${STAGE1_SCORE}), running VLM judge..."

    # Locate the poster file
    POSTER=""
    if [ -f /app/output/final_poster.png ]; then
        POSTER="/app/output/final_poster.png"
    elif [ -f /app/output/final_poster.pdf ]; then
        # Convert PDF to PNG for VLM input
        python3 /app/scripts/extract_poster.py /app/output/final_poster.pdf /logs/verifier 2>&1 || true
        if [ -f /logs/verifier/poster.png ]; then
            POSTER="/logs/verifier/poster.png"
        fi
    fi

    if [ -n "$POSTER" ] && [ -f /app/scripts/vlm_judge.py ]; then
        python3 /app/scripts/vlm_judge.py \
            "$POSTER" \
            /app/benchmark/task_input.json \
            /app/benchmark/source_brief.md \
            --output /logs/verifier/vlm_scores.json \
            2>&1 || echo "[verifier] VLM judge failed, skipping Stage 2"

        # If VLM scores exist, compute combined reward
        if [ -f /logs/verifier/vlm_scores.json ]; then
            python3 - <<'COMBINE'
import json
from pathlib import Path

stage1 = float(Path("/logs/verifier/reward.txt").read_text().strip())
vlm = json.loads(Path("/logs/verifier/vlm_scores.json").read_text())
vlm_overall = vlm.get("overall", 0)

# Normalize VLM score from 1-5 to 0-1 scale
vlm_normalized = max(0, (vlm_overall - 1)) / 4.0

# Stage 1 is gate only; final reward = VLM normalized score
# (Stage 1 already passed if we're here)
Path("/logs/verifier/reward.txt").write_text(f"{vlm_normalized:.4f}\n")
print(f"[verifier] Stage 1: {stage1} (gate passed), VLM: {vlm_overall}/5 → reward = {vlm_normalized:.4f}")
for dim in ["design", "visual_impact", "content", "completeness"]:
    s = vlm.get(dim, {})
    print(f"  {dim}: {s.get('score', '?')}/5 — {s.get('reason', '')[:80]}")
COMBINE
        fi
    else
        echo "[verifier] No poster found or vlm_judge.py missing, skipping Stage 2"
    fi
else
    echo "[verifier] Stage 1 failed (${STAGE1_SCORE}), skipping Stage 2"
fi