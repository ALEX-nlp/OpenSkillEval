#!/usr/bin/env bash
set -euo pipefail

# Minimal oracle solution: read task_input.json and generate valid HTML pages
# with the correct structure, sections, and navigation links.
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
site = spec.get("site", {})
pages = spec.get("pages", [])
navigation = spec.get("navigation", [])
brief = spec.get("brief", {})

# Default: at least one page
if not pages:
    pages = [{"page_id": "home", "file": "index.html", "sections": ["hero", "footer"], "objective": "Main page"}]

title = brief.get("title", "Website")
lang = spec.get("language", "en")

# ── Build navigation HTML ────────────────────────────────────────────
page_map = {p["page_id"]: p.get("file", "index.html") for p in pages}

def build_nav_html():
    links = []
    for p in pages:
        pid = p["page_id"]
        f = p.get("file", "index.html")
        links.append(f'<a href="{f}">{pid.replace("-", " ").title()}</a>')
    return " | ".join(links)

nav_html = build_nav_html()

# ── Generate each page ───────────────────────────────────────────────
for page_spec in pages:
    page_id = page_spec["page_id"]
    filename = page_spec.get("file", "index.html")
    sections = page_spec.get("sections", [])
    objective = page_spec.get("objective", "")

    sections_html = ""
    for sec in sections:
        sec_id = sec.lower().replace(" ", "-")
        sections_html += f"""
    <section id="{sec_id}">
      <h2>{sec.replace("-", " ").title()}</h2>
      <p>Content for {sec} section. {objective}</p>
    </section>
"""

    html = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} - {page_id.replace("-", " ").title()}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; padding: 20px; }}
    nav {{ background: #333; color: white; padding: 10px 20px; }}
    nav a {{ color: white; margin-right: 15px; text-decoration: none; }}
    section {{ padding: 20px 0; border-bottom: 1px solid #eee; }}
    h1 {{ color: #333; }}
  </style>
</head>
<body>
  <nav>{nav_html}</nav>
  <header><h1>{title}</h1></header>
  <main>
{sections_html}
  </main>
  <footer><p>&copy; 2025 {title}</p></footer>
</body>
</html>"""

    (OUTPUT_DIR / filename).write_text(html, encoding="utf-8")
    print(f"[solve] saved {filename} with {len(sections)} sections")

print(f"[solve] generated {len(pages)} pages to {OUTPUT_DIR}")
PY
