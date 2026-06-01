"""Deterministic checks for data-visualization tasks.

Validates that the generated image file exists, is a valid image,
and meets minimum quality. Output file is fixed as result.png.

Stage 1 of evaluation — pure structural / file checks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image

APP_ROOT = Path(os.environ.get("OPENSKILLEVAL_APP_ROOT", "/app"))
OUTPUT_DIR = APP_ROOT / "output"
OUTPUT_FILE = "result.png"


# ── tests ──────────────────────────────────────────────────────────────


def test_output_dir_exists() -> None:
    """Output directory must exist."""
    assert OUTPUT_DIR.exists(), f"missing output directory: {OUTPUT_DIR}"


def test_output_file_exists() -> None:
    """The output file result.png must exist."""
    filepath = OUTPUT_DIR / OUTPUT_FILE
    assert filepath.exists(), f"missing output file: {filepath}"


def test_output_is_valid_image() -> None:
    """The output must be a valid, openable image file."""
    filepath = OUTPUT_DIR / OUTPUT_FILE
    if not filepath.exists():
        pytest.skip(f"{OUTPUT_FILE} not found")
    img = Image.open(filepath)
    img.verify()  # raises if corrupt


def test_output_minimum_resolution() -> None:
    """The output image must be at least 800x600 pixels."""
    filepath = OUTPUT_DIR / OUTPUT_FILE
    if not filepath.exists():
        pytest.skip(f"{OUTPUT_FILE} not found")
    img = Image.open(filepath)
    w, h = img.size
    assert w >= 800, f"{OUTPUT_FILE} width {w}px < 800px minimum"
    assert h >= 600, f"{OUTPUT_FILE} height {h}px < 600px minimum"


def test_output_file_not_trivial() -> None:
    """The output file must be non-trivial (> 5 KB), not a blank image."""
    filepath = OUTPUT_DIR / OUTPUT_FILE
    if not filepath.exists():
        pytest.skip(f"{OUTPUT_FILE} not found")
    size = filepath.stat().st_size
    assert size > 5_000, (
        f"{OUTPUT_FILE} is only {size} bytes, likely blank or trivial"
    )
