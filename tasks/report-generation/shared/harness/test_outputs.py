"""Deterministic checks for report-generation tasks.

Checks that the agent produced a valid report file (HTML or PDF) named
``final_report.*`` in /app/output/.

This file is copied to /tests/test_outputs.py at runtime by tasks/scripts/load_case_to_dir.sh and run
by /tests/test.sh inside the container.

Stage 1 of evaluation — pure structural / existence checks.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

APP_ROOT = Path(os.environ.get("OPENSKILLEVAL_APP_ROOT", "/app"))
TASK_INPUT = APP_ROOT / "benchmark" / "task_input.json"
OUTPUT_DIR = APP_ROOT / "output"


# ── helpers ────────────────────────────────────────────────────────────


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def find_report() -> Path | None:
    """Find the final report file in the output directory."""
    for name in [
        "final_report.html", "final_report.pdf",
        "final_report.htm",
        "report.html", "report.pdf",
    ]:
        p = OUTPUT_DIR / name
        if p.exists():
            return p
    # Fallback: any HTML or PDF
    for ext in ("*.html", "*.htm", "*.pdf"):
        files = list(OUTPUT_DIR.glob(ext))
        if files:
            return files[0]
    return None


def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".html", ".htm"):
        return "html"
    if suffix == ".pdf":
        return "pdf"
    with open(path, "rb") as f:
        header = f.read(16)
    if header.startswith(b"%PDF"):
        return "pdf"
    if b"<html" in header.lower() or b"<!doctype" in header.lower():
        return "html"
    return "unknown"


def extract_text_from_html(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(entity, char)
    return text


def extract_text_from_pdf(path: Path) -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except ImportError:
        pass
    return ""


def get_report_text(path: Path) -> str:
    fmt = detect_format(path)
    if fmt == "html":
        return extract_text_from_html(path)
    elif fmt == "pdf":
        return extract_text_from_pdf(path)
    return ""


# ── tests ──────────────────────────────────────────────────────────────


def test_report_file_exists() -> None:
    """A report file (HTML or PDF) must exist in /app/output/."""
    report = find_report()
    assert report is not None, (
        f"No report found in {OUTPUT_DIR}. "
        f"Expected final_report.html or final_report.pdf. "
        f"Files present: {[f.name for f in OUTPUT_DIR.iterdir()] if OUTPUT_DIR.exists() else '(dir missing)'}"
    )


def test_report_not_empty() -> None:
    """Report file must be non-trivial (> 500 bytes)."""
    report = find_report()
    if report is None:
        pytest.skip("No report file found")
    size = report.stat().st_size
    assert size > 500, f"Report too small ({size} bytes), likely empty or corrupt"


def test_report_is_valid_format() -> None:
    """Report must be a valid HTML or PDF file."""
    report = find_report()
    if report is None:
        pytest.skip("No report file found")
    fmt = detect_format(report)
    assert fmt in ("html", "pdf"), f"Unknown format: {report.suffix} (detected: {fmt})"

    if fmt == "html":
        text = report.read_text(encoding="utf-8", errors="replace")
        # Must contain at least basic HTML structure
        assert "<" in text and ">" in text, "HTML file contains no tags"
    elif fmt == "pdf":
        with open(report, "rb") as f:
            header = f.read(5)
        assert header == b"%PDF-", "PDF file does not start with %PDF- header"


def test_report_has_content() -> None:
    """Report must contain meaningful text (> 100 chars after stripping tags)."""
    report = find_report()
    if report is None:
        pytest.skip("No report file found")
    text = get_report_text(report)
    assert len(text) > 100, (
        f"Report has very little content ({len(text)} chars). "
        "Expected a substantive report with analysis."
    )


def test_sections_present() -> None:
    """Required section titles from task_input.json must appear in the report."""
    report = find_report()
    if report is None:
        pytest.skip("No report file found")
    if not TASK_INPUT.exists():
        pytest.skip("No task_input.json found")

    task = load_json(TASK_INPUT)
    sections = task.get("required_sections", [])
    if not sections:
        pytest.skip("No required_sections in task_input.json")

    text = get_report_text(report)
    text_lower = text.lower()

    found = 0
    missing = []
    for s in sections:
        title = s.get("title", "")
        if not title:
            continue
        if title.lower() in text_lower:
            found += 1
        else:
            # Try partial match (each word)
            words = title.lower().split()
            if len(words) > 1 and all(w in text_lower for w in words):
                found += 1
            else:
                missing.append(title)

    total = len([s for s in sections if s.get("title")])
    match_rate = found / total if total > 0 else 1.0
    assert match_rate >= 0.6, (
        f"Section coverage too low: {found}/{total} ({match_rate:.0%}). "
        f"Missing: {missing}"
    )


def test_kpis_mentioned() -> None:
    """KPI names from task_input.json should appear in the report."""
    report = find_report()
    if report is None:
        pytest.skip("No report file found")
    if not TASK_INPUT.exists():
        pytest.skip("No task_input.json found")

    task = load_json(TASK_INPUT)
    kpis = task.get("kpis", [])
    if not kpis:
        pytest.skip("No kpis in task_input.json")

    text = get_report_text(report)
    text_lower = text.lower()

    found = 0
    missing = []
    for k in kpis:
        name = k.get("name", "")
        if not name:
            continue
        if name.lower() in text_lower:
            found += 1
        else:
            missing.append(name)

    total = len([k for k in kpis if k.get("name")])
    match_rate = found / total if total > 0 else 1.0
    assert match_rate >= 0.5, (
        f"KPI coverage too low: {found}/{total} ({match_rate:.0%}). "
        f"Missing: {missing}"
    )
