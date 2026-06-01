#!/usr/bin/env bash
set -euo pipefail

# Minimal oracle solution: generate a valid poster PNG with correct dimensions.
python3 - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

APP_ROOT = Path(os.environ.get("OPENSKILLEVAL_APP_ROOT", "/app"))
INPUT = APP_ROOT / "benchmark" / "task_input.json"
OUTPUT_DIR = APP_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

spec = json.loads(INPUT.read_text(encoding="utf-8"))
poster = spec.get("poster", {})
brief = spec.get("brief", {})

# ── dimensions ───────────────────────────────────────────────────────
ratio_str = poster.get("aspect_ratio", "landscape")
ratio_map = {
    "landscape": (1920, 1080),
    "portrait": (1080, 1920),
    "square": (1080, 1080),
    "A0-landscape": (2378, 1682),
    "A0-portrait": (1682, 2378),
    "A1-landscape": (1682, 1189),
    "A1-portrait": (1189, 1682),
}
if ratio_str in ratio_map:
    width, height = ratio_map[ratio_str]
elif ":" in ratio_str:
    parts = ratio_str.split(":")
    rw, rh = float(parts[0]), float(parts[1])
    height = 1080
    width = int(height * rw / rh)
else:
    width, height = 1920, 1080

# ── generate poster ──────────────────────────────────────────────────
img = Image.new("RGB", (width, height), color="#F0F4F8")
draw = ImageDraw.Draw(img)

# Title bar
bar_h = height // 6
draw.rectangle([0, 0, width, bar_h], fill="#1E3A8A")

# Title text
title = brief.get("title", "Poster")
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
    small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
except OSError:
    font = ImageFont.load_default()
    small_font = font

bbox = draw.textbbox((0, 0), title, font=font)
tw = bbox[2] - bbox[0]
draw.text(((width - tw) // 2, bar_h // 3), title, fill="white", font=font)

# One-liner
one_liner = brief.get("one_liner", "")
if one_liner:
    bbox2 = draw.textbbox((0, 0), one_liner, font=small_font)
    tw2 = bbox2[2] - bbox2[0]
    draw.text(((width - tw2) // 2, bar_h + 40), one_liner, fill="#334155", font=small_font)

# Placeholder content blocks
block_y = bar_h + 120
block_margin = 60
block_w = (width - 3 * block_margin) // 2
for i in range(4):
    row = i // 2
    col = i % 2
    x = block_margin + col * (block_w + block_margin)
    y = block_y + row * (height // 4)
    draw.rectangle([x, y, x + block_w, y + height // 5], fill="#E2E8F0", outline="#94A3B8")
    draw.text((x + 20, y + 20), f"Section {i+1}", fill="#1F2937", font=small_font)

img.save(str(OUTPUT_DIR / "final_poster.png"), "PNG")
print(f"[solve] saved poster to {OUTPUT_DIR / 'final_poster.png'} ({width}x{height})")
PY
