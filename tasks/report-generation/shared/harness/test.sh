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
