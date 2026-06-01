#!/usr/bin/env python3
"""Enhanced VLM-as-Judge for report-generation evaluation.

Based on tasks/report-generation/scripts/vlm_judge.py, extended with:
  - Multi-provider support (anthropic / openai)
  - --base-url and --api-key overrides
  - --max-workers concurrent API calls (3 VLM dimensions in parallel)
  - Incremental result flushing
  - Agent eval_report.json integration for Data Accuracy & Fidelity

5 dimensions (1-5 scale each):
  VLM-scored (this script):
  - Content Quality:  text quality, info density, clarity, organization
  - Visualization:    chart type, color, labels, readability
  - Completeness:     task requirement fulfillment

  Agent-scored (from eval_report.json):
  - Data Accuracy:    numerical correctness vs source data (code-verified)
  - Fidelity:         factual consistency with source data (code-verified)

Usage:
    python vlm_judge_ext.py <report_path> <task_input_path> \
        [--eval-report EVAL_REPORT_PATH] \
        [--model MODEL] [--provider anthropic|openai] [--base-url URL] [--api-key KEY] \
        [--max-workers N] [--output PATH]

Outputs JSON with per-dimension scores and reasoning.
"""

import argparse
import base64
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
import requests as _requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─── Provider helpers ────────────────────────────────────────────────────

_CLIENT = None
_PROVIDER = "anthropic"
_GEMINI_BASE_URL = ""
_GEMINI_API_KEY = ""


def init_client(provider: str, base_url: str, api_key: str):
    """Initialise the module-level VLM client. base_url / api_key are
    forwarded from judge_single.sh --base-url / --api-key, which originate
    from agent_configs/snippets/judge.snippet."""
    global _CLIENT, _PROVIDER, _GEMINI_BASE_URL, _GEMINI_API_KEY
    _PROVIDER = provider

    if provider == "openai":
        _GEMINI_BASE_URL = base_url
        _GEMINI_API_KEY = api_key
    else:
        _CLIENT = anthropic.Anthropic(base_url=base_url, api_key=api_key)


def _img_block(b64: str, media_type: str = "image/png") -> dict:
    """Return an image content block for the active provider."""
    if _PROVIDER == "openai":
        return {"inline_data": {"mime_type": media_type, "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


# ─── Report extraction helpers ──────────────────────────────────────────

def detect_format(report_path: Path) -> str:
    """Detect report format from extension and content."""
    suffix = report_path.suffix.lower()
    if suffix in (".html", ".htm"):
        return "html"
    if suffix == ".pdf":
        return "pdf"
    with open(report_path, "rb") as f:
        header = f.read(16)
    if header.startswith(b"%PDF"):
        return "pdf"
    if b"<html" in header.lower() or b"<!doctype" in header.lower():
        return "html"
    return "unknown"


def extract_text_from_html(html_path: Path) -> str:
    """Extract plain text from an HTML file."""
    text = html_path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(entity, char)
    return text


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract text from a PDF using pdftotext or pypdf."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except ImportError:
        pass
    return ""


MAX_IMAGE_HEIGHT = 8000


def _start_local_server(directory: Path) -> tuple[http.server.HTTPServer, int]:
    """Start a local HTTP server serving *directory* on a random port."""
    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer(("127.0.0.1", 0), lambda *a, **kw: handler(*a, directory=str(directory), **kw))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def screenshot_html(html_path: Path, output_dir: Path,
                    viewport_w: int = 1440, viewport_h: int = 900) -> list[Path]:
    """Render HTML report to screenshot(s) using Playwright.

    Uses a local HTTP server instead of file:// so that external CDN resources
    (e.g. Chart.js, ECharts) can load normally.

    - Short reports (total height <= MAX_IMAGE_HEIGHT): single full-page screenshot.
    - Long reports (total height > MAX_IMAGE_HEIGHT): scrolls by viewport_h and
      captures one screenshot per "page". All images are passed together to the
      VLM in a single API call for holistic evaluation.
    """
    server = None
    try:
        from playwright.sync_api import sync_playwright

        # Serve the HTML directory over HTTP so CDN scripts load correctly.
        html_dir = html_path.resolve().parent
        server, port = _start_local_server(html_dir)
        url = f"http://127.0.0.1:{port}/{html_path.resolve().name}"

        with sync_playwright() as p:
            _cdn_cache = str(Path(tempfile.gettempdir()) / "playwright_cdn_cache")
            browser = p.chromium.launch(args=[f"--disk-cache-dir={_cdn_cache}"])
            page = browser.new_page(viewport={"width": viewport_w, "height": viewport_h})
            _t0 = time.time()
            page.goto(url, wait_until="domcontentloaded", timeout=180000)
            _t1 = time.time()
            print(f"[screenshot] domcontentloaded: {_t1 - _t0:.1f}s", file=sys.stderr)
            try:
                page.wait_for_load_state("networkidle", timeout=180000)
            except Exception:
                pass  # networkidle is best-effort; some pages never fully quiet down
            _t2 = time.time()
            print(f"[screenshot] networkidle: {_t2 - _t1:.1f}s", file=sys.stderr)

            # Wait for dynamic charts to finish rendering.
            # domcontentloaded + optional networkidle ensures JS/CSS are loaded.
            # Then we poll until: (1) DOM stops changing (SVG/Plotly charts),
            # (2) all <canvas> elements have non-empty content (Chart.js/ECharts),
            # and (3) browser has completed paint cycles.
            # Stable = 3 consecutive checks with no change (~1.5s steady).
            # Timeout = 30 checks × 500ms = 15s max.
            # page.evaluate("""() => {
            #     return new Promise((resolve) => {
            #         let lastDomLen = 0;
            #         let stableCount = 0;
            #         let totalChecks = 0;
            #         const check = () => {
            #             totalChecks++;
            #             const domLen = document.body.innerHTML.length;
            #             const canvases = document.querySelectorAll('canvas');
            #             let allCanvasReady = true;
            #             canvases.forEach(c => {
            #                 try {
            #                     const blank = document.createElement('canvas');
            #                     blank.width = c.width; blank.height = c.height;
            #                     if (c.toDataURL() === blank.toDataURL())
            #                         allCanvasReady = false;
            #                 } catch(e) {}
            #             });
            #             if (domLen === lastDomLen && allCanvasReady)
            #                 stableCount++;
            #             else {
            #                 stableCount = 0;
            #                 lastDomLen = domLen;
            #             }
            #             if (stableCount >= 4 || totalChecks >= 10) resolve();
            #             else setTimeout(check, 3000);
            #         };
            #         setTimeout(check, 8000);
            #     });
            # }""")

            # Try to disable chart entrance animations so we don't have to
            # wait for them to finish.  Safe to call even if the library
            # isn't present — the try/catch makes it a no-op.
            page.evaluate("""() => {
                try {
                    // ECharts: find all instances and disable animation
                    if (typeof echarts !== 'undefined') {
                        const doms = document.querySelectorAll('[_echarts_instance_]');
                        doms.forEach(dom => {
                            const inst = echarts.getInstanceByDom(dom);
                            if (inst) inst.setOption({animation: false}, {lazyUpdate: false});
                        });
                    }
                } catch(e) {}
                try {
                    // Chart.js: disable animation on all instances
                    if (typeof Chart !== 'undefined' && Chart.instances) {
                        const instances = Object.values ? Object.values(Chart.instances) : [];
                        instances.forEach(c => {
                            if (c.options) c.options.animation = false;
                        });
                    }
                } catch(e) {}
            }""")

            page.evaluate("""() => {
                return new Promise((resolve) => {
                    let stableCount = 0;
                    let lastState = "";

                    // Sample a fingerprint from a canvas so we detect
                    // content changes (animations, lazy rendering).
                    const canvasFingerprint = (c) => {
                        try {
                            if (!c.width || !c.height) return "empty";
                            const ctx = c.getContext("2d");
                            if (!ctx) return "noctx";
                            // Sample a few scattered pixels across the canvas
                            const w = c.width, h = c.height;
                            const points = [
                                [0, 0], [w>>1, 0], [w-1, 0],
                                [0, h>>1], [w>>1, h>>1], [w-1, h>>1],
                                [0, h-1], [w>>1, h-1], [w-1, h-1]
                            ];
                            let fp = "";
                            for (const [x, y] of points) {
                                const px = ctx.getImageData(
                                    Math.min(x, w-1), Math.min(y, h-1), 1, 1
                                ).data;
                                fp += px[0] + "," + px[1] + "," + px[2] + "," + px[3] + ";";
                            }
                            return fp;
                        } catch(e) {
                            return "cross-origin";
                        }
                    };

                    const getState = () => {
                        const domLen = document.body ? document.body.innerHTML.length : 0;

                        const imgs = Array.from(document.images || []);
                        const imgsReady = imgs.every(img => img.complete);

                        const fontsReady = document.fonts ? document.fonts.status === "loaded" : true;

                        const canvases = Array.from(document.querySelectorAll("canvas"));
                        const canvasFPs = canvases.map(canvasFingerprint);
                        const canvasHasContent = canvasFPs.every(fp =>
                            fp !== "empty" && fp !== "noctx" &&
                            fp !== "0,0,0,0;".repeat(9)
                        );

                        const svgs = Array.from(document.querySelectorAll("svg"));
                        const svgState = svgs.map(svg => svg.innerHTML.length).join(",");

                        return JSON.stringify({
                            domLen,
                            imgsReady,
                            fontsReady,
                            canvasCount: canvases.length,
                            canvasHasContent,
                            canvasFPs,
                            svgCount: svgs.length,
                            svgState,
                        });
                    };

                    const check = () => {
                        const state = getState();
                        if (state === lastState) stableCount++;
                        else {
                            stableCount = 0;
                            lastState = state;
                        }

                        const parsed = JSON.parse(state);
                        const ready =
                            parsed.imgsReady &&
                            parsed.fontsReady &&
                            (parsed.canvasCount === 0 || parsed.canvasHasContent) &&
                            (parsed.svgCount === 0 || parsed.svgState.length > 0);

                        if ((ready && stableCount >= 3) || stableCount >= 8) {
                            resolve();
                        } else {
                            setTimeout(check, 2000);
                        }
                    };

                    setTimeout(check, 10000);
                });
            }""")
            _t3 = time.time()
            print(f"[screenshot] stability poll: {_t3 - _t2:.1f}s | total wait: {_t3 - _t0:.1f}s", file=sys.stderr)



            total_height = page.evaluate("document.body.scrollHeight")
            pages: list[Path] = []
            detail_pages: list[Path] = []

            # Step 1: detail shots first (1440×900 each).
            # Scrolling through the page gives charts extra time to render.
            if total_height > viewport_h:
                idx = 0
                y = 0
                while y < total_height:
                    page.evaluate(f"window.scrollTo(0, {y})")
                    page.wait_for_timeout(3000)
                    idx += 1
                    out = output_dir / f"detail_{idx:03d}.png"
                    page.screenshot(path=str(out))  # viewport-only
                    detail_pages.append(out)
                    y += viewport_h

            # Step 2: overview AFTER details — by now all charts have rendered.
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)
            overview_path = output_dir / "overview.png"
            page.screenshot(path=str(overview_path), full_page=True)
            if total_height > MAX_IMAGE_HEIGHT:
                from PIL import Image
                img = Image.open(overview_path)
                scale = MAX_IMAGE_HEIGHT / img.height
                img = img.resize((int(img.width * scale), MAX_IMAGE_HEIGHT), Image.LANCZOS)
                img.save(overview_path)

            # Final order: overview first (for VLM), then details
            pages.append(overview_path)
            pages.extend(detail_pages)

            browser.close()
        return pages
    except Exception as e:
        print(f"[extract] Playwright screenshot failed: {e}", file=sys.stderr)
        return []
    finally:
        if server:
            server.shutdown()


def screenshot_pdf(pdf_path: Path, output_dir: Path) -> list[Path]:
    """Convert PDF pages to PNGs using PyMuPDF (fitz)."""
    import fitz
    doc = fitz.open(str(pdf_path))
    pages = []
    for i, page in enumerate(doc):
        mat = fitz.Matrix(150 / 72, 150 / 72)  # 150 DPI
        pix = page.get_pixmap(matrix=mat)
        out = output_dir / f"page_{i+1:03d}.png"
        pix.save(str(out))
        pages.append(out)
    doc.close()
    return pages


def find_report(report_arg: Path) -> Path | None:
    """Find the report file, trying common names if path is a directory."""
    if report_arg.is_file():
        return report_arg
    if report_arg.is_dir():
        for name in ["final_report.html", "final_report.pdf",
                      "report.html", "report.pdf"]:
            candidate = report_arg / name
            if candidate.exists():
                return candidate
        for ext in ("*.html", "*.pdf"):
            files = list(report_arg.glob(ext))
            if files:
                return files[0]
    return None


# ─── Rubric prompts ─────────────────────────────────────────────────────

RUBRIC_CONTENT_QUALITY = """\
Evaluate the **content quality** of this report across two aspects: writing quality AND analysis depth.

A. Writing & Structure:
- Organization: clear headings, logical flow, well-structured executive summary
- Clarity: well-written, grammatically correct, easy to understand
- Information density: appropriate amount of content (not too sparse, not overwhelming)
- Logical consistency: no self-contradicting claims or misleading interpretations

B. Analysis Depth:
- Are insights substantive (backed by statistics specific data) or surface-level (just restating numbers)?
- Does the analysis go beyond descriptive statistics (e.g., correlation, attribution, comparison)?
- Are conclusions and recommendations data-driven and specific (not generic advice)?
- Is there unnecessary redundancy (e.g., same data presented in multiple formats without adding value)?

Scoring (1-5):
- 5: Excellent writing AND deep analysis — insights are substantive, data-driven, logically consistent, with no redundancy
- 4: Good writing and organization, but some sections lack analytical depth or have minor redundancy
- 3: Adequate structure but analysis is mostly surface-level, or notable redundancy/inconsistency
- 2: Poor organization, stiff writing, and shallow analysis
- 1: Chaotic structure, grammar errors, no real analysis

Return JSON: {"score": <1-5>, "reason": "<brief explanation>"}
"""

RUBRIC_VISUALIZATION = """\
Evaluate the **visualization quality** of this report.

Criteria:
- Chart type selection: appropriate for the data (bar for comparison, line for trend, pie for composition, etc.)
- Color scheme: professional, harmonious, accessible
- Labels and annotations: axis titles, data labels, legends, units
- Readability: data is immediately clear from the visualization
- Tables: properly formatted, aligned, with clear headers

Scoring (1-5):
- 5: Chart types perfectly matched to data, professional colors, complete annotations, data immediately clear
- 4: Good chart types, has titles and labels, good visual effect
- 3: Basic charts present, but type selection or labels could be improved
- 2: Few charts or wrong types, missing labels
- 1: No charts or charts are incomprehensible

Return JSON: {"score": <1-5>, "reason": "<brief explanation>"}
"""


RUBRIC_COMPLETENESS = """\
Evaluate the **task completeness** of this report.

Go through EVERY requirement below and check whether it is substantively addressed in the report. Do NOT just check if a section heading exists — verify that the actual content within each section fulfills what was asked. For example, if a KPI is required "segmented by genre and year", check that BOTH segmentations are present, not just one.

Task requirements:
{task_requirements}

Scoring (1-5):
- 5: Every requirement substantively addressed — all sections have the requested content, all KPIs present with required segmentation
- 4: Core requirements met, but 1-2 minor details missing (e.g., a KPI lacks one segmentation dimension)
- 3: Most requirements met, but notable gaps (e.g., a section is present but missing key requested analysis)
- 2: Only partially satisfied — multiple sections lack required content
- 1: Key requirements not met

Return JSON: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""


# ─── Helpers ─────────────────────────────────────────────────────────────

def load_images_as_base64(png_paths: list[Path]) -> list[dict]:
    """Load report page PNGs as base64 for API calls."""
    images = []
    for png in png_paths:
        with open(png, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        images.append({"path": str(png), "base64": b64})
    return images


def build_task_requirements(task_input: dict) -> str:
    """Extract human-readable requirements from task_input.json."""
    parts = []

    report = task_input.get("report", {})
    if "type" in report:
        parts.append(f"- Report type: {report['type']}")
    if "audience" in report:
        parts.append(f"- Target audience: {report['audience']}")
    if "tone" in report:
        parts.append(f"- Tone: {report['tone']}")

    brief = task_input.get("brief", {})
    if "title" in brief:
        parts.append(f"- Title: {brief['title']}")
    if "goal" in brief:
        parts.append(f"- Goal: {brief['goal']}")

    sections = task_input.get("required_sections", [])
    if sections:
        parts.append(f"- Must include these {len(sections)} sections:")
        for s in sections:
            parts.append(f"  - \"{s.get('title', '?')}\": {s.get('objective', '')}")

    kpis = task_input.get("kpis", [])
    if kpis:
        parts.append(f"- Must cover these KPIs:")
        for k in kpis:
            parts.append(f"  - {k.get('name', '?')}: {k.get('description', '')}")

    dims = task_input.get("analysis_dimensions", [])
    if dims:
        parts.append(f"- Analysis dimensions: {', '.join(dims)}")

    return "\n".join(parts) if parts else "No specific requirements provided."


# ─── VLM call ────────────────────────────────────────────────────────────

def _parse_vlm_text(raw: str) -> dict:
    """Extract JSON from VLM response text."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{[^}]*"score"\s*:\s*\d[^}]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        print(f"[VLM] Failed to parse JSON: {raw}", file=sys.stderr)
        return {"score": 0, "reason": f"JSON parse error: {raw[:200]}"}


def call_vlm(messages: list, model: str, max_retries: int = 3) -> dict:
    """Call the VLM API with exponential backoff retry."""
    for attempt in range(max_retries + 1):
        try:
            if _PROVIDER == "openai":
                # Convert messages to Gemini native contents/parts format
                contents = []
                for msg in messages:
                    parts = []
                    for block in msg.get("content", []):
                        if isinstance(block, str):
                            parts.append({"text": block})
                        elif block.get("type") == "text":
                            parts.append({"text": block["text"]})
                        elif "inline_data" in block:
                            parts.append(block)
                    contents.append({"role": msg.get("role", "user"), "parts": parts})

                headers = {
                    "Authorization": _GEMINI_API_KEY if _GEMINI_API_KEY.startswith("Bearer ") else f"Bearer {_GEMINI_API_KEY}",
                    "Content-Type": "application/json",
                }
                payload = {"model": model, "contents": contents}
                resp = _requests.post(_GEMINI_BASE_URL, headers=headers, json=payload, timeout=300, verify=False)
                resp.raise_for_status()
                data = resp.json()
                raw = data["candidates"][0]["content"]["parts"][0]["text"]
            else:
                response = _CLIENT.messages.create(
                    model=model,
                    messages=messages,
                    max_tokens=1024,
                    temperature=0.0,
                )
                raw = response.content[0].text

            return _parse_vlm_text(raw)
        except Exception as e:
            if attempt < max_retries:
                delay = 2 ** (attempt + 1)  # 2s, 4s, 8s
                print(f"[VLM] {model} attempt {attempt+1} failed: {e}, retrying in {delay}s...",
                      file=sys.stderr)
                time.sleep(delay)
            else:
                raise


# ─── Message builders ────────────────────────────────────────────────────


def _make_full_report_message(images: list[dict], rubric: str,
                              extra_text: str = "") -> list:
    """Build message for full report evaluation (all pages + optional text)."""
    content = [{"type": "text", "text": rubric}]
    if extra_text:
        content.append({"type": "text", "text": extra_text})
    if len(images) > 1:
        content.append({"type": "text", "text": (
            f"Note: The following {len(images)} images are from the same report.\n"
            "- Image 1 is a FULL-PAGE OVERVIEW (scaled down) — use it to assess "
            "overall layout, chart type selection, and visual structure.\n"
            f"- Images 2-{len(images)} are DETAIL SHOTS at native resolution — use "
            "them to read chart labels, data values, annotations, and fine details.\n"
            "Elements may appear cut off at page boundaries — this is a screenshot "
            "artifact, not a problem with the report. Evaluate the report as a whole."
        )})
    for img in images:
        content.append(_img_block(img["base64"]))
    return [{"role": "user", "content": content}]


def _make_text_only_message(rubric: str) -> list:
    """Build message for text-only evaluation (no images)."""
    return [{"role": "user", "content": rubric}]


# ─── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VLM-as-Judge for report evaluation (enhanced)")
    parser.add_argument("report_path", help="Path to report file (HTML/PDF) or directory")
    parser.add_argument("task_input_path", help="Path to task_input.json")
    parser.add_argument("--eval-report", default=None,
                        help="Path to eval_report.json from Docker Agent (Data Accuracy & Fidelity)")
    parser.add_argument("--model", default="claude-opus-4-6", help="VLM model to use")
    parser.add_argument("--output", default=None, help="Output JSON path (default: stdout)")
    parser.add_argument("--max-workers", type=int, default=3, help="Max parallel API calls")
    parser.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"],
                        help="API provider (anthropic or openai)")
    parser.add_argument("--base-url", default=None, help="Override API base URL")
    parser.add_argument("--api-key", default=None, help="Override API key")
    parser.add_argument("--log-prefix", default="", help="Prefix for all stderr log lines")
    args = parser.parse_args()

    _pfx = f"{args.log_prefix} " if args.log_prefix else "  "

    # ── Init client ────────────────────────────────────────────────
    init_client(args.provider, args.base_url, args.api_key)

    report_arg = Path(args.report_path)
    task_input = json.loads(Path(args.task_input_path).read_text())
    output_path = Path(args.output) if args.output else None

    # ── Find report file ──────────────────────────────────────────
    report_path = find_report(report_arg)
    if report_path is None:
        print(f"Error: No report found at {report_arg}", file=sys.stderr)
        sys.exit(1)

    fmt = detect_format(report_path)

    # ── Extract text ──────────────────────────────────────────────
    if fmt == "html":
        report_text = extract_text_from_html(report_path)
    elif fmt == "pdf":
        report_text = extract_text_from_pdf(report_path)
    else:
        print(f"Error: Unknown format for {report_path}", file=sys.stderr)
        sys.exit(1)

    # ── Extract screenshots ───────────────────────────────────────
    tmp_dir = Path(tempfile.mkdtemp(prefix="report_eval_"))
    if fmt == "html":
        png_paths = screenshot_html(report_path, tmp_dir)
    else:
        png_paths = screenshot_pdf(report_path, tmp_dir)
    images = load_images_as_base64(png_paths)

    # ── Load agent eval_report (Data Accuracy & Fidelity) ─────────
    agent_eval = {}
    da_score = "N/A"
    fi_score = "N/A"
    if args.eval_report and Path(args.eval_report).exists():
        agent_eval = json.loads(Path(args.eval_report).read_text())
        da_score = agent_eval.get('data_accuracy', {}).get('score', 'N/A')
        fi_score = agent_eval.get('fidelity', {}).get('score', 'N/A')

    print(f"{_pfx}[{args.model}] {len(report_text)} chars, {len(images)} pages | eval: da={da_score} fi={fi_score}", file=sys.stderr)

    # ── Pre-build rubrics ─────────────────────────────────────────
    task_req = build_task_requirements(task_input)
    rubric_complete = RUBRIC_COMPLETENESS.format(task_requirements=task_req)

    # ── Results container ─────────────────────────────────────────
    results = {}
    done_count = 0
    total_vlm_calls = 3
    vlm_failures = 0

    def _flush_results():
        """Write current results to file after each dimension completes."""
        all_dims = ["content_quality", "visualization", "data_accuracy",
                    "completeness", "fidelity"]
        if all(d in results for d in all_dims):
            dim_scores = []
            for d in all_dims:
                s = results[d]
                dim_scores.append(s.get("score", 0) if isinstance(s, dict) else 0)
            results["overall"] = round(sum(dim_scores) / len(dim_scores), 2)

        text = json.dumps(results, indent=2, ensure_ascii=False)
        if output_path:
            output_path.write_text(text)
        return text

    # ── Submit 3 VLM dimensions concurrently ──────────────────────
    futures = {}

    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        # 1. Content Quality (text-only, full report)
        def _eval_content_quality():
            msg = _make_text_only_message(
                RUBRIC_CONTENT_QUALITY + f"\n\nReport text:\n{report_text}")
            return call_vlm(msg, args.model)
        futures[pool.submit(_eval_content_quality)] = "content_quality"

        # 2. Visualization (full report, all pages)
        if images:
            def _eval_visualization():
                msg = _make_full_report_message(images, RUBRIC_VISUALIZATION)
                return call_vlm(msg, args.model)
            futures[pool.submit(_eval_visualization)] = "visualization"
        else:
            futures[pool.submit(lambda: {"score": 0, "reason": "No screenshots available for visual evaluation"})] = "visualization"

        # 3. Completeness (text-only, checks sections/KPIs/dimensions coverage)
        def _eval_completeness():
            msg = _make_text_only_message(
                rubric_complete + f"\n\nReport text:\n{report_text}")
            return call_vlm(msg, args.model)
        futures[pool.submit(_eval_completeness)] = "completeness"

        # ── Collect VLM results ───────────────────────────────────
        for future in as_completed(futures):
            dim = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = {"score": 0, "reason": f"VLM API error: {e}"}
                vlm_failures += 1
                print(f"{_pfx}[{done_count+1}/{total_vlm_calls}] {dim}: FAILED — {e}", file=sys.stderr)
            done_count += 1
            results[dim] = result
            print(f"{_pfx}[{done_count}/{total_vlm_calls}] {dim}: score={result.get('score', '?')}", file=sys.stderr)

    # ── 4. Data Accuracy (from agent eval_report.json) ────────────
    if "data_accuracy" in agent_eval and isinstance(agent_eval["data_accuracy"], dict):
        da = agent_eval["data_accuracy"]
        results["data_accuracy"] = {
            "score": da.get("score", 0),
            "source": "agent_code",
            "accuracy_rate": da.get("accuracy_rate"),
            "total_checked": da.get("total_checked", 0),
            "correct": da.get("correct", 0),
            "reason": da.get("reason", ""),
            "details": da.get("details", []),
        }
        print(f"{_pfx}[agent] data_accuracy={da.get('score', 0)}", file=sys.stderr)
    else:
        results["data_accuracy"] = {
            "score": 0,
            "source": "missing",
            "reason": "No agent eval_report.json provided.",
        }

    # ── 5. Fidelity (from agent eval_report.json) ─────────────────
    if "fidelity" in agent_eval and isinstance(agent_eval["fidelity"], dict):
        fi = agent_eval["fidelity"]
        results["fidelity"] = {
            "score": fi.get("score", 0),
            "source": "agent_code",
            "support_rate": fi.get("support_rate"),
            "total_claims": fi.get("total_claims", 0),
            "supported": fi.get("supported", 0),
            "reason": fi.get("reason", ""),
            "details": fi.get("details", []),
        }
        print(f"{_pfx}[agent] fidelity={fi.get('score', 0)}", file=sys.stderr)
    else:
        results["fidelity"] = {
            "score": 0,
            "source": "missing",
            "reason": "No agent eval_report.json provided.",
        }

    # ── Final output ──────────────────────────────────────────────
    if vlm_failures > 0:
        print(f"{_pfx}[ERROR] {vlm_failures}/{total_vlm_calls} VLM calls failed, skipping result output",
              file=sys.stderr)
        if output_path and output_path.exists():
            output_path.unlink()
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(1)

    final = _flush_results()
    if not output_path:
        print(final)
    else:
        pass  # caller (judge_single.sh) prints done message

    # ── Cleanup temp dir ──────────────────────────────────────────
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
