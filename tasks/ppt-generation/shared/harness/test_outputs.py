"""Deterministic checks for ppt-generation tasks.

Reads slide titles and count from task_input.json (the ``slides`` array and
``deck.slide_count`` field) and validates the generated PPTX against them.

This file is copied to /tests/test_outputs.py at runtime by tasks/scripts/load_case_to_dir.sh and run
by /tests/test.sh inside the container.

Stage 1 of evaluation — pure structural / content checks.
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

APP_ROOT = Path(os.environ.get("OPENSKILLEVAL_APP_ROOT", "/app"))
TASK_INPUT = APP_ROOT / "benchmark" / "task_input.json"
DECK = APP_ROOT / "output" / "final_deck.pptx"

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}


# ── helpers ────────────────────────────────────────────────────────────


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def iter_slide_names(zipf: zipfile.ZipFile) -> list[str]:
    names = [
        name
        for name in zipf.namelist()
        if name.startswith("ppt/slides/slide") and name.endswith(".xml")
    ]
    return sorted(
        names, key=lambda name: int(re.search(r"slide(\d+)\.xml$", name).group(1))
    )


def extract_slide_text(zipf: zipfile.ZipFile, slide_name: str) -> str:
    root = ET.fromstring(zipf.read(slide_name))
    texts = [node.text or "" for node in root.findall(".//a:t", NS)]
    return " ".join(part.strip() for part in texts if part and part.strip())


def _get_expected_slide_count(task: dict) -> int:
    """Return expected slide count from either schema variant."""
    deck = task.get("deck", {})
    # New-style: deck.slide_count / Old-style: deck.exact_slide_count
    return deck.get("slide_count") or deck.get("exact_slide_count") or 0


def _get_expected_titles(task: dict) -> list[str]:
    """Return ordered list of expected slide titles from either schema variant."""
    # New-style: slides[].title
    if "slides" in task:
        return [s["title"] for s in task["slides"]]
    # Old-style: required_slide_blueprint[].title
    if "required_slide_blueprint" in task:
        return [s["title"] for s in task["required_slide_blueprint"]]
    return []


def extract_slide_title(zipf: zipfile.ZipFile, slide_name: str) -> str:
    """Extract title text from a slide via title placeholder, fallback to first text."""
    root = ET.fromstring(zipf.read(slide_name))
    # Look for title / center-title placeholder
    for sp in root.findall(".//p:sp", NS):
        nv_pr = sp.find("p:nvSpPr/p:nvPr", NS)
        if nv_pr is not None:
            ph = nv_pr.find("p:ph", NS)
            if ph is not None and ph.get("type") in ("title", "ctrTitle"):
                texts = [t.text or "" for t in sp.findall(".//a:t", NS)]
                title = " ".join(t.strip() for t in texts if t.strip())
                if title:
                    return title
    # Fallback: first non-empty shape text
    for sp in root.findall(".//p:sp", NS):
        texts = [t.text or "" for t in sp.findall(".//a:t", NS)]
        text = " ".join(t.strip() for t in texts if t.strip())
        if text:
            return text
    return ""


def _fuzzy_match(a: str, b: str) -> float:
    """Fuzzy string similarity (0-1)."""
    a_clean = a.strip().lower()
    b_clean = b.strip().lower()
    if a_clean == b_clean:
        return 1.0
    return SequenceMatcher(None, a_clean, b_clean).ratio()


def extract_slide_size(zipf: zipfile.ZipFile) -> tuple[int, int]:
    """Extract slide width and height (EMU) from presentation.xml."""
    pres_xml = zipf.read("ppt/presentation.xml")
    root = ET.fromstring(pres_xml)
    sld_sz = root.find(".//p:sldSz", NS)
    if sld_sz is not None:
        cx = int(sld_sz.get("cx", "0"))
        cy = int(sld_sz.get("cy", "0"))
        return cx, cy
    return 0, 0


# ── tests ──────────────────────────────────────────────────────────────


def test_deck_file_exists_and_not_empty() -> None:
    """final_deck.pptx must exist and be non-trivial (> 10 KB)."""
    assert DECK.exists(), f"missing output: {DECK}"
    size = DECK.stat().st_size
    assert size > 10_000, f"final_deck.pptx too small ({size} bytes), likely corrupt"


def test_deck_is_valid_pptx() -> None:
    """PPTX is a ZIP archive; ensure it can be opened and contains slides."""
    assert zipfile.is_zipfile(DECK), "final_deck.pptx is not a valid ZIP/PPTX"
    with zipfile.ZipFile(DECK, "r") as zipf:
        slide_names = iter_slide_names(zipf)
        assert len(slide_names) > 0, "PPTX contains no slides"


def test_slide_count_matches_task_input() -> None:
    """Slide count must match task_input.json -> deck.slide_count."""
    task = load_json(TASK_INPUT)
    expected = _get_expected_slide_count(task)
    if expected <= 0:
        pytest.skip("task_input.json has no slide_count, skipping")
    with zipfile.ZipFile(DECK, "r") as zipf:
        actual = len(iter_slide_names(zipf))
    assert actual == expected, f"expected {expected} slides, got {actual}"


def test_title_matching() -> None:
    """Slide titles must fuzzy-match the expected blueprint (>=80% match rate)."""
    task = load_json(TASK_INPUT)
    expected_titles = _get_expected_titles(task)
    if not expected_titles:
        pytest.skip("task_input.json has no slide titles, skipping")

    threshold = 0.5
    with zipfile.ZipFile(DECK, "r") as zipf:
        slide_names = iter_slide_names(zipf)
        actual_titles = [extract_slide_title(zipf, name) for name in slide_names]

    matches = 0
    details = []
    for i, (exp, act) in enumerate(zip(expected_titles, actual_titles)):
        score = _fuzzy_match(exp, act)
        matched = score >= threshold
        if matched:
            matches += 1
        details.append(
            f"  Slide {i+1}: expected='{exp}' got='{act}' "
            f"(similarity={score:.2f}, {'MATCH' if matched else 'MISS'})"
        )

    match_rate = matches / len(expected_titles) if expected_titles else 0
    summary = f"Title match: {matches}/{len(expected_titles)} ({match_rate:.0%})"
    assert match_rate >= 0.8, summary + "\n" + "\n".join(details)


def test_aspect_ratio() -> None:
    """Slide aspect ratio must match deck.aspect_ratio if specified."""
    task = load_json(TASK_INPUT)
    deck = task.get("deck", {})
    expected_ratio_str = deck.get("aspect_ratio", "")
    if not expected_ratio_str:
        pytest.skip("task_input.json has no aspect_ratio, skipping")

    parts = expected_ratio_str.split(":")
    if len(parts) != 2:
        pytest.skip(f"Cannot parse aspect ratio '{expected_ratio_str}', skipping")

    exp_ratio = float(parts[0]) / float(parts[1])
    tolerance = 0.05

    with zipfile.ZipFile(DECK, "r") as zipf:
        cx, cy = extract_slide_size(zipf)

    assert cy > 0, "Cannot read slide height from presentation.xml"
    actual_ratio = cx / cy
    assert abs(actual_ratio - exp_ratio) < tolerance, (
        f"Aspect ratio mismatch: got {actual_ratio:.3f}, expected {exp_ratio:.3f}"
    )


def test_deck_contains_non_trivial_content() -> None:
    """Each slide should have meaningful text (> 10 chars), not be blank."""
    with zipfile.ZipFile(DECK, "r") as zipf:
        slide_names = iter_slide_names(zipf)
        empty_slides = []
        for name in slide_names:
            text = extract_slide_text(zipf, name)
            if len(text) < 10:
                idx = int(re.search(r"slide(\d+)\.xml$", name).group(1))
                empty_slides.append(idx)
    assert not empty_slides, f"slides with little/no content: {empty_slides}"
