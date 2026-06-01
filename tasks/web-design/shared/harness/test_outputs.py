"""Deterministic checks for web-design tasks.

Reads page structure, sections, navigation from task_input.json
and validates the generated HTML files against them.

This file is copied to /tests/test_outputs.py at runtime by tasks/scripts/load_case_to_dir.sh and run
by /tests/test.sh inside the container.

Stage 1 of evaluation — pure structural / content checks (no browser needed).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

APP_ROOT = Path(os.environ.get("OPENSKILLEVAL_APP_ROOT", "/app"))
TASK_INPUT = APP_ROOT / "benchmark" / "task_input.json"
OUTPUT_DIR = APP_ROOT / "output"


# ── helpers ────────────────────────────────────────────────────────────


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_html(path: Path) -> BeautifulSoup | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return BeautifulSoup(text, "html.parser")


def _get_pages(task: dict) -> list[dict]:
    return task.get("pages", [])


def _get_page_map(task: dict) -> dict[str, str]:
    """page_id → filename mapping."""
    return {p["page_id"]: p.get("file", "index.html") for p in _get_pages(task)}


def _section_in_page(soup: BeautifulSoup, section: str) -> bool:
    """Check if a section name appears as id, class, semantic tag, or in text."""
    sec_lower = section.lower().replace("-", "").replace("_", "")

    # Check ids
    for tag in soup.find_all(id=True):
        if sec_lower in tag["id"].lower().replace("-", "").replace("_", ""):
            return True
    # Check classes
    for tag in soup.find_all(class_=True):
        for cls in tag.get("class", []):
            if sec_lower in cls.lower().replace("-", "").replace("_", ""):
                return True
    # Check semantic tags
    if section.lower() in {t.name.lower() for t in soup.find_all()}:
        return True
    # Fallback: text contains section name
    if sec_lower in soup.get_text().lower().replace("-", "").replace("_", "").replace(" ", ""):
        return True
    return False


# ── tests ──────────────────────────────────────────────────────────────


def test_index_html_exists_and_not_empty() -> None:
    """index.html must exist and be non-trivial (> 100 bytes)."""
    index = OUTPUT_DIR / "index.html"
    assert index.exists(), f"missing output: {index}"
    size = index.stat().st_size
    assert size > 100, f"index.html too small ({size} bytes)"


def test_html_files_are_parseable() -> None:
    """All .html files in output must be parseable by BeautifulSoup."""
    html_files = list(OUTPUT_DIR.glob("*.html"))
    assert html_files, "No .html files found in output directory"
    errors = []
    for f in html_files:
        try:
            soup = BeautifulSoup(f.read_text(encoding="utf-8", errors="replace"), "html.parser")
            assert soup.find("html") or soup.find("body"), f"{f.name}: no <html> or <body>"
        except Exception as e:
            errors.append(f"{f.name}: {e}")
    assert not errors, f"Parse errors: {'; '.join(errors)}"


def test_page_count_matches_task_input() -> None:
    """Number of HTML files must match site.page_count if specified."""
    task = load_json(TASK_INPUT)
    expected = task.get("site", {}).get("page_count")
    if not expected:
        pytest.skip("task_input.json has no site.page_count, skipping")
    actual = len(list(OUTPUT_DIR.glob("*.html")))
    assert actual == expected, f"expected {expected} HTML files, got {actual}"


def test_pages_exist() -> None:
    """Each page specified in pages[] must have its corresponding file."""
    task = load_json(TASK_INPUT)
    pages = _get_pages(task)
    if not pages:
        pytest.skip("task_input.json has no pages[], skipping")
    missing = []
    for p in pages:
        f = OUTPUT_DIR / p.get("file", "index.html")
        if not f.exists():
            missing.append(p.get("file", "index.html"))
    assert not missing, f"Missing page files: {missing}"


def test_sections_present() -> None:
    """At least 60% of specified sections must be found in their pages."""
    task = load_json(TASK_INPUT)
    pages = _get_pages(task)
    has_sections = any(p.get("sections") for p in pages)
    if not has_sections:
        pytest.skip("No sections specified in pages[], skipping")

    total = 0
    found = 0
    details = []
    for p in pages:
        sections = p.get("sections", [])
        if not sections:
            continue
        filename = p.get("file", "index.html")
        soup = load_html(OUTPUT_DIR / filename)
        if soup is None:
            total += len(sections)
            details.append(f"{filename}: file not found")
            continue
        for sec in sections:
            total += 1
            if _section_in_page(soup, sec):
                found += 1
            else:
                details.append(f"{filename}: section '{sec}' not found")

    rate = found / total if total > 0 else 1.0
    summary = f"Sections: {found}/{total} ({rate:.0%})"
    assert rate >= 0.6, summary + "\n  " + "\n  ".join(details[:15])


def test_navigation_links_exist() -> None:
    """Navigation links specified in navigation[] should exist as <a> hrefs."""
    task = load_json(TASK_INPUT)
    navigation = task.get("navigation", [])
    if not navigation:
        pytest.skip("No navigation[] in task_input.json, skipping")

    page_map = _get_page_map(task)
    total = 0
    found = 0
    details = []

    for nav in navigation:
        from_file = page_map.get(nav.get("from", ""), "index.html")
        to_file = page_map.get(nav.get("to", ""), "")
        to_id = nav.get("to", "")

        soup = load_html(OUTPUT_DIR / from_file)
        if soup is None:
            total += 1
            details.append(f"{from_file}: not found")
            continue

        total += 1
        links = soup.find_all("a", href=True)
        matched = False
        for link in links:
            href = link["href"].lower().strip()
            # Match various href patterns
            if to_file and any(
                href == pat
                for pat in [
                    to_file.lower(),
                    f"./{to_file.lower()}",
                    f"/{to_file.lower()}",
                ]
            ):
                matched = True
                break
            if href.endswith(to_file.lower()):
                matched = True
                break
            if href == f"#{to_id.lower()}":
                matched = True
                break
        if matched:
            found += 1
        else:
            details.append(f"{from_file}: no link to {to_file}")

    rate = found / total if total > 0 else 1.0
    summary = f"Nav links: {found}/{total} ({rate:.0%})"
    assert rate >= 0.5, summary + "\n  " + "\n  ".join(details[:10])


def test_viewport_meta() -> None:
    """All pages should have <meta name="viewport"> when responsive is required."""
    task = load_json(TASK_INPUT)
    if not task.get("site", {}).get("responsive", True):
        pytest.skip("site.responsive is false, skipping")

    html_files = list(OUTPUT_DIR.glob("*.html"))
    missing = []
    for f in html_files:
        soup = load_html(f)
        if soup and not soup.find("meta", attrs={"name": "viewport"}):
            missing.append(f.name)
    assert not missing, f"Missing viewport meta: {', '.join(missing)}"


def test_pages_have_content() -> None:
    """Each HTML page must have at least 50 characters of text content."""
    html_files = list(OUTPUT_DIR.glob("*.html"))
    empty = []
    for f in html_files:
        soup = load_html(f)
        if soup is None:
            empty.append(f"{f.name} (unparseable)")
            continue
        text = soup.get_text(strip=True)
        if len(text) < 50:
            empty.append(f"{f.name} ({len(text)} chars)")
    assert not empty, f"Pages with too little content: {', '.join(empty)}"


def test_data_display_content() -> None:
    """Expected data content from data_display[] should appear in pages."""
    task = load_json(TASK_INPUT)
    data_display = task.get("data_display", [])
    if not data_display:
        pytest.skip("No data_display[] in task_input.json, skipping")

    page_map = _get_page_map(task)
    total = 0
    found = 0
    details = []

    for dd in data_display:
        expected = dd.get("expected_content", [])
        if not expected:
            continue
        page_id = dd.get("page", "home")
        filename = page_map.get(page_id, "index.html")
        soup = load_html(OUTPUT_DIR / filename)
        if soup is None:
            total += len(expected)
            details.append(f"{filename}: not found for data '{dd.get('id', '?')}'")
            continue
        page_text = soup.get_text().lower()
        for item in expected:
            total += 1
            if item.lower() in page_text:
                found += 1
            else:
                details.append(f"{filename}: '{item}' not found")

    if total == 0:
        pytest.skip("No expected_content to check")
    rate = found / total
    summary = f"Data content: {found}/{total} ({rate:.0%})"
    assert rate >= 0.4, summary + "\n  " + "\n  ".join(details[:10])


def test_accessibility_basics() -> None:
    """Basic accessibility: html lang, title tag, img alt."""
    index = OUTPUT_DIR / "index.html"
    soup = load_html(index)
    assert soup is not None, "Cannot parse index.html"

    issues = []
    html_tag = soup.find("html")
    if not html_tag or not html_tag.get("lang"):
        issues.append("missing <html lang>")

    title_tag = soup.find("title")
    if not title_tag or not title_tag.get_text(strip=True):
        issues.append("missing or empty <title>")

    imgs = soup.find_all("img")
    no_alt = [img for img in imgs if not img.get("alt")]
    if no_alt:
        issues.append(f"{len(no_alt)}/{len(imgs)} images missing alt")

    assert not issues, f"Accessibility issues: {'; '.join(issues)}"
