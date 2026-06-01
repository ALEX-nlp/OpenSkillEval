#!/usr/bin/env bash
set -euo pipefail

# Minimal oracle solution: read task_input.json and generate a valid PPTX
# with the correct number of slides, titles, and aspect ratio.
python3 - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu

APP_ROOT = Path(os.environ.get("OPENSKILLEVAL_APP_ROOT", "/app"))
INPUT = APP_ROOT / "benchmark" / "task_input.json"
OUTPUT_DIR = APP_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

spec = json.loads(INPUT.read_text(encoding="utf-8"))
deck = spec.get("deck", {})

# ── aspect ratio ──────────────────────────────────────────────────────
ratio_str = deck.get("aspect_ratio", "16:9")
parts = ratio_str.split(":")
rw, rh = (float(parts[0]), float(parts[1])) if len(parts) == 2 else (16, 9)

SLIDE_HEIGHT = Emu(6858000)                       # 7.5 inches
SLIDE_WIDTH = Emu(int(SLIDE_HEIGHT * rw / rh))    # scale width to match ratio

prs = Presentation()
prs.slide_width = SLIDE_WIDTH
prs.slide_height = SLIDE_HEIGHT

# ── slides ────────────────────────────────────────────────────────────
slides_spec = spec.get("slides", spec.get("required_slide_blueprint", []))
slide_count = deck.get("slide_count") or deck.get("exact_slide_count") or len(slides_spec) or 1

layout = prs.slide_layouts[1]  # title + content layout

for i in range(slide_count):
    slide = prs.slides.add_slide(layout)
    title = ""
    if i < len(slides_spec):
        title = slides_spec[i].get("title", f"Slide {i + 1}")
    else:
        title = f"Slide {i + 1}"
    slide.shapes.title.text = title
    # Add placeholder body text so the slide is non-trivial (>10 chars)
    body = slide.placeholders[1]
    body.text = f"Content for: {title}"

prs.save(str(OUTPUT_DIR / "final_deck.pptx"))
print(f"[solve] saved {slide_count}-slide deck to {OUTPUT_DIR / 'final_deck.pptx'}")
PY
