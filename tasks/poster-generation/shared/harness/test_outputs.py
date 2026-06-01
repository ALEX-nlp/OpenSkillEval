"""Deterministic checks for poster-generation tasks.

Reads poster configuration from task_input.json and validates the generated
poster image against structural requirements.

This file is copied to /tests/test_outputs.py at runtime by tasks/scripts/load_case_to_dir.sh and run
by /tests/test.sh inside the container.

Stage 1 of evaluation — pure structural / format checks.
"""
from __future__ import annotations

import json
import os
import struct
import zipfile
from pathlib import Path

import pytest

APP_ROOT = Path(os.environ.get("OPENSKILLEVAL_APP_ROOT", "/app"))
TASK_INPUT = APP_ROOT / "benchmark" / "task_input.json"
POSTER = APP_ROOT / "output" / "final_poster.png"
POSTER_PDF = APP_ROOT / "output" / "final_poster.pdf"


# ── helpers ────────────────────────────────────────────────────────────


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def get_png_dimensions(path: Path) -> tuple[int, int]:
    """Read width and height from PNG IHDR chunk."""
    with open(path, "rb") as f:
        sig = f.read(8)
        if sig[:4] != b"\x89PNG":
            raise ValueError("Not a valid PNG file")
        # Skip chunk length (4 bytes) + chunk type 'IHDR' (4 bytes)
        f.read(4)
        chunk_type = f.read(4)
        assert chunk_type == b"IHDR", f"Expected IHDR, got {chunk_type}"
        width = struct.unpack(">I", f.read(4))[0]
        height = struct.unpack(">I", f.read(4))[0]
        return width, height


# ── tests ──────────────────────────────────────────────────────────────


def test_poster_file_exists_and_not_empty() -> None:
    """final_poster.png must exist and be non-trivial (> 10 KB)."""
    assert POSTER.exists() or POSTER_PDF.exists(), (
        f"missing output: neither {POSTER} nor {POSTER_PDF} found"
    )
    target = POSTER if POSTER.exists() else POSTER_PDF
    size = target.stat().st_size
    assert size > 10_000, f"{target.name} too small ({size} bytes), likely corrupt"


def test_poster_is_valid_image() -> None:
    """Poster must be a valid PNG image."""
    if not POSTER.exists():
        pytest.skip("PNG not found, checking PDF fallback only")
    with open(POSTER, "rb") as f:
        sig = f.read(8)
    assert sig[:4] == b"\x89PNG", "final_poster.png is not a valid PNG file"


def test_poster_dimensions_reasonable() -> None:
    """Poster should have reasonable dimensions (at least 800x600 pixels)."""
    if not POSTER.exists():
        pytest.skip("PNG not found, skipping dimension check")
    width, height = get_png_dimensions(POSTER)
    min_dim = 600
    assert width >= min_dim and height >= min_dim, (
        f"Poster too small: {width}x{height}px, expected at least {min_dim}px on each side"
    )


def test_poster_aspect_ratio() -> None:
    """Poster aspect ratio should match task_input.json -> poster.aspect_ratio if specified."""
    if not POSTER.exists():
        pytest.skip("PNG not found, skipping aspect ratio check")
    task = load_json(TASK_INPUT)
    poster_conf = task.get("poster", {})
    expected_ratio_str = poster_conf.get("aspect_ratio", "")
    if not expected_ratio_str:
        pytest.skip("task_input.json has no poster.aspect_ratio, skipping")

    ratio_map = {
        "landscape": (16, 9),
        "portrait": (9, 16),
        "square": (1, 1),
        "A0-landscape": (1189, 841),
        "A0-portrait": (841, 1189),
        "A1-landscape": (841, 594),
        "A1-portrait": (594, 841),
    }

    if expected_ratio_str in ratio_map:
        rw, rh = ratio_map[expected_ratio_str]
    elif ":" in expected_ratio_str:
        parts = expected_ratio_str.split(":")
        rw, rh = float(parts[0]), float(parts[1])
    else:
        pytest.skip(f"Cannot parse aspect ratio '{expected_ratio_str}', skipping")

    exp_ratio = rw / rh
    width, height = get_png_dimensions(POSTER)
    actual_ratio = width / height
    tolerance = 0.15  # More lenient for posters (different rendering engines)
    assert abs(actual_ratio - exp_ratio) / exp_ratio < tolerance, (
        f"Aspect ratio mismatch: got {actual_ratio:.3f} ({width}x{height}), "
        f"expected ~{exp_ratio:.3f} ({expected_ratio_str})"
    )


def test_poster_not_blank() -> None:
    """Poster should not be a single-color blank image."""
    if not POSTER.exists():
        pytest.skip("PNG not found, skipping blank check")
    try:
        from PIL import Image
        img = Image.open(POSTER)
        # Sample pixels from different regions
        w, h = img.size
        pixels = set()
        for x in [w // 4, w // 2, 3 * w // 4]:
            for y in [h // 4, h // 2, 3 * h // 4]:
                pixels.add(img.getpixel((x, y)))
        assert len(pixels) > 1, "Poster appears to be blank (single color across samples)"
    except ImportError:
        pytest.skip("Pillow not available for blank check")
