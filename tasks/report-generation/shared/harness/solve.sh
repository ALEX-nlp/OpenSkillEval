#!/usr/bin/env bash
set -euo pipefail

# Minimal oracle solution: read task_input.json and generate a valid HTML report
# with the required sections and a basic KPI table.
python3 - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

APP_ROOT = Path(os.environ.get("OPENSKILLEVAL_APP_ROOT", "/app"))
INPUT = APP_ROOT / "benchmark" / "task_input.json"
OUTPUT_DIR = APP_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

spec = json.loads(INPUT.read_text(encoding="utf-8"))
brief = spec.get("brief", {})
title = brief.get("title", "Report")
sections = spec.get("required_sections", [])
kpis = spec.get("kpis", [])

# Build minimal HTML report
html_parts = [f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; }}
h1 {{ color: #1e40af; }} h2 {{ color: #374151; border-bottom: 1px solid #e5e7eb; padding-bottom: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
th, td {{ border: 1px solid #d1d5db; padding: 8px; text-align: left; }}
th {{ background: #1e40af; color: white; }}
</style></head><body>
<h1>{title}</h1>
<p><em>{brief.get("one_liner", "")}</em></p>
<hr>"""]

# KPI section
if kpis:
    html_parts.append("<h2>Key Metrics</h2><table><tr><th>KPI</th><th>Description</th></tr>")
    for k in kpis:
        html_parts.append(f"<tr><td>{k.get('name','')}</td><td>{k.get('description','')}</td></tr>")
    html_parts.append("</table>")

# Required sections
for s in sections:
    html_parts.append(f"<h2>{s.get('title', 'Section')}</h2>")
    html_parts.append(f"<p>{s.get('objective', 'Content placeholder.')}</p>")

html_parts.append("</body></html>")

output_path = OUTPUT_DIR / "final_report.html"
output_path.write_text("\n".join(html_parts), encoding="utf-8")
print(f"[solve] saved report to {output_path}")
PY
