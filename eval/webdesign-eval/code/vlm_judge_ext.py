#!/usr/bin/env python3
"""Enhanced VLM-as-Judge for web-design evaluation.

Based on tasks/web-design/scripts/vlm_judge.py, extended with:
  - Multi-provider support (anthropic / openai)
  - --base-url and --api-key overrides
  - --max-workers concurrent API calls
  - Incremental result flushing
  - Regex fallback for JSON parsing

VLM-scored dimensions (1-5 scale):
  - Visual Design:   per-page aesthetics + layout (fullpage + detail shots)
  - Responsiveness:  batched mobile + tablet adaptation quality

Pass-rate dimensions (from eval_report.json, no VLM needed):
  - Navigation:      page-to-page link correctness
  - Interactions:    toggle/switch/modal functionality
  - Data Display:    data rendering correctness

Usage:
    python vlm_judge_ext.py <eval_output_dir> <task_input_path> <source_brief_path> \
        [--model MODEL] [--provider anthropic|openai] [--base-url URL] [--api-key KEY] \
        [--max-workers N] [--output PATH]

Outputs JSON with per-dimension scores and overall.
"""

import argparse
import base64
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import time

import anthropic
import requests as _requests
import urllib3
from PIL import Image
from io import BytesIO

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Maximum number of images per VLM submit; excess images are truncated (to avoid overly long context / API rejection)
MAX_IMAGES_PER_CALL = 20


# ─── Provider helpers ────────────────────────────────────────────────────

_CLIENT = None
_PROVIDER = "anthropic"
_GEMINI_BASE_URL = ""
_GEMINI_API_KEY = ""


def init_client(provider: str, base_url: str, api_key: str):
    """Initialise the module-level VLM client. base_url / api_key are
    forwarded from judge_single.sh --base-url / --api-key, originating from
    agent_configs/snippets/judge.snippet."""
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


# ─── Rubric prompts ─────────────────────────────────────────────────────

RUBRIC_VISUAL_DESIGN = """\
Evaluate the **visual design execution quality** of this web page.

Criteria:
- Color & typography: harmonious palette, readable fonts, clear heading hierarchy (h1 > h2 > body), consistent font sizing
- Layout & structure: well-organized sections, clear information hierarchy, consistent grid alignment, no misaligned or jagged elements
- Spacing & polish: appropriate white space between sections, no elements overlapping or clipped, no exposed HTML tags or rendering artifacts
- Placeholder handling: the task does NOT provide image/video assets, so placeholder elements are expected. Do NOT penalize for the absence of real images. Instead, evaluate whether placeholders are well-designed — appropriate size and position, consistent styling with the overall theme (e.g., labeled gray boxes are better than empty black voids)

Note: the first image is a fullpage overview, subsequent images are detail crops scrolled top-to-bottom. Boundary cuts between detail images are screenshot artifacts.

Scoring (1-5):
- 5: Harmonious color palette with intentional accent choices; typography has clear h1/h2/body hierarchy with consistent sizing; sections are well-separated with balanced spacing; grid alignment is pixel-consistent; placeholders are styled to match the theme (e.g., colored boxes with labels, icon placeholders)
- 4: Color palette is coordinated, typography hierarchy is clear, layout is well-organized; has 1-2 minor issues such as: one section has slightly uneven spacing, a placeholder is slightly oversized, a font weight is inconsistent in one area
- 3: Has a color scheme and basic section structure, but execution is noticeably rough: spacing is uneven across sections, heading sizes are inconsistent, placeholders are unstyled empty blocks, or the page feels like a wireframe with colors applied
- 2: Multiple visible problems: elements overlap or get clipped, sections are misaligned, exposed HTML tags or broken CSS visible, color choices clash, or large portions of the page are visually broken
- 1: Page renders as unstyled HTML (no CSS applied), or the page fails to load / displays a blank screen

Return JSON: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""


RUBRIC_RESPONSIVE = """\
Evaluate the **responsiveness** of this website on a {device_name}.

You are shown screenshots captured with {device_description}.
Each screenshot is from a different page of the same website. Evaluate the overall responsive quality across all pages shown.

Criteria:
- No horizontal scrollbar / content overflow
- Navigation is accessible (hamburger menu or adapted nav)
- Touch targets are appropriately sized (>=44px)
- Text is readable without zooming
- Content adapts to the viewport width (no desktop layout forced into a smaller screen)

Note: the task does NOT provide image/video assets. Pages may contain elements explicitly labeled or styled as placeholders (e.g., boxes with "placeholder" text, icons indicating missing media). Do NOT penalize these as a responsive issue — only evaluate how the layout, navigation, and content elements adapt to this viewport.

Scoring (1-5):
- 5: No overflow, navigation properly adapted (e.g., hamburger menu on mobile), touch targets well-sized, content reflows cleanly to viewport width
- 4: Good adaptation overall; 1-2 minor issues such as: nav items slightly tight, one button partially clipped at edge, or slight spacing inconsistency
- 3: Has viewport meta and attempts responsive layout, but noticeable problems: nav not collapsed, some text requires horizontal scroll, or several elements are cramped
- 2: Layout significantly broken: content overflows viewport, navigation is unusable, or desktop layout is forced into the smaller screen
- 1: No responsive handling at all — page renders at desktop width requiring zoom and scroll

Return JSON: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""



# ─── Helpers ─────────────────────────────────────────────────────────────

# def load_image_b64(path: Path) -> str:
#     with open(path, "rb") as f:
#         return base64.b64encode(f.read()).decode()

MAX_IMAGE_DIMENSION = 8000
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Claude API 5MB limit


def load_image_b64(path: Path) -> str:
    """Load image file as base64 string, downscaling if dimension or file size exceeds limits."""
    img = Image.open(path)
    w, h = img.size

    # Step 1: dimension cap
    if w > MAX_IMAGE_DIMENSION or h > MAX_IMAGE_DIMENSION:
        scale = MAX_IMAGE_DIMENSION / max(w, h)
        new_w, new_h = int(w * scale), int(h * scale)
        print(f"[VLM] Resizing {path.name} from {w}x{h} to {new_w}x{new_h} (max {MAX_IMAGE_DIMENSION}px)", file=sys.stderr)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
    else:
        with open(path, "rb") as f:
            raw = f.read()

    # Step 2: file size cap — shrink until base64-encoded payload is under 5MB
    # Bedrock API validates the base64 string byte count (~33% bloat vs raw),
    # so check the b64 length directly to avoid the case where raw < 5MB but b64 > 5MB gets rejected
    scale = 0.8
    while len(base64.b64encode(raw)) > MAX_IMAGE_BYTES:
        cur_w, cur_h = img.size
        new_w, new_h = int(cur_w * scale), int(cur_h * scale)
        print(f"[VLM] {path.name} b64={len(base64.b64encode(raw))/1024/1024:.1f}MB "
              f"(>{MAX_IMAGE_BYTES/1024/1024:.0f}MB), shrinking to {new_w}x{new_h}", file=sys.stderr)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()

    return base64.b64encode(raw).decode()




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


def judge_single_image(image_path: Path, rubric: str, model: str) -> dict:
    """Score a single screenshot."""
    b64 = load_image_b64(image_path)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": rubric},
        _img_block(b64),
    ]}]
    return call_vlm(messages, model)


def judge_multi_image(image_paths: list[Path], rubric: str, model: str) -> dict:
    """Score multiple screenshots together. If more than MAX_IMAGES_PER_CALL, keep the first N
    (in the visual_design path, fullpage is at index 0, so truncation preserves the overview + top details)."""
    if len(image_paths) > MAX_IMAGES_PER_CALL:
        print(f"[VLM] {len(image_paths)} images > {MAX_IMAGES_PER_CALL}, truncating to first {MAX_IMAGES_PER_CALL}",
              file=sys.stderr)
        image_paths = image_paths[:MAX_IMAGES_PER_CALL]
    content = [{"type": "text", "text": rubric}]
    for p in image_paths:
        b64 = load_image_b64(p)
        content.append(_img_block(b64))
    messages = [{"role": "user", "content": content}]
    return call_vlm(messages, model)


# ─── Dimension evaluators ────────────────────────────────────────────────

def eval_visual_design(screenshots_dir: Path, pages_spec: list, model: str,
                       pool: ThreadPoolExecutor) -> dict:
    """Visual Design (aesthetics + layout): per-page fullpage + detail shots."""
    futures = {}
    for ps in pages_spec:
        pid = ps.get("page_id", "home")
        images = []
        # Fullpage screenshot first (overall layout)
        fullpage = screenshots_dir / f"{pid}_fullpage.png"
        if fullpage.exists():
            images.append(fullpage)
        # Detail shots: {pid}_full_01.png, {pid}_full_02.png, ...
        idx = 1
        while True:
            detail = screenshots_dir / f"{pid}_full_{idx:02d}.png"
            if not detail.exists():
                break
            images.append(detail)
            idx += 1
        # Fallback: use desktop screenshot if no fullpage/details
        if not images:
            desktop = screenshots_dir / f"{pid}_desktop.png"
            if desktop.exists():
                images = [desktop]
        if images:
            note = (f"Page: {pid}. The first image is the fullpage screenshot (overall layout). "
                    f"Subsequent images are detail shots (viewport crops, top-to-bottom) for fine inspection.")
            rubric = RUBRIC_VISUAL_DESIGN + "\n\n" + note
            futures[pool.submit(judge_multi_image, images, rubric, model)] = pid

    scores = []
    for future in as_completed(futures):
        pid = futures[future]
        result = future.result()
        result["page_id"] = pid
        scores.append(result)
        print(f"  [visual_design] {pid}: {result.get('score', 0)}", file=sys.stderr)

    avg = sum(s.get("score", 0) for s in scores) / len(scores) if scores else 0
    return {"score": round(avg, 2), "per_page": scores}



def eval_responsiveness(screenshots_dir: Path, pages_spec: list, model: str,
                        pool: ThreadPoolExecutor) -> dict:
    """Responsiveness: batch all mobile screenshots into one VLM call, all tablet into another."""
    mobile_imgs = []
    tablet_imgs = []
    mobile_page_ids = []
    tablet_page_ids = []

    for ps in pages_spec:
        pid = ps.get("page_id", "home")
        img_mobile = screenshots_dir / f"{pid}_mobile.png"
        if img_mobile.exists():
            mobile_imgs.append(img_mobile)
            mobile_page_ids.append(pid)
        img_tablet = screenshots_dir / f"{pid}_tablet.png"
        if img_tablet.exists():
            tablet_imgs.append(img_tablet)
            tablet_page_ids.append(pid)

    futures = {}
    if mobile_imgs:
        rubric = RUBRIC_RESPONSIVE.format(
            device_name="mobile phone",
            device_description="iPhone 13 emulation (DPR=3, touch, mobile UA)",
        )
        page_note = "Pages in order: " + ", ".join(mobile_page_ids)
        futures[pool.submit(judge_multi_image, mobile_imgs, rubric + "\n\n" + page_note, model)] = "mobile"

    if tablet_imgs:
        rubric = RUBRIC_RESPONSIVE.format(
            device_name="tablet",
            device_description="iPad (gen 7) emulation (DPR=2, touch)",
        )
        page_note = "Pages in order: " + ", ".join(tablet_page_ids)
        futures[pool.submit(judge_multi_image, tablet_imgs, rubric + "\n\n" + page_note, model)] = "tablet"

    mobile_result = None
    tablet_result = None
    for future in as_completed(futures):
        device = futures[future]
        result = future.result()
        result["device"] = device
        if device == "mobile":
            mobile_result = result
            result["page_ids"] = mobile_page_ids
        else:
            tablet_result = result
            result["page_ids"] = tablet_page_ids
        print(f"  [responsive:{device}] {result.get('score', 0)}", file=sys.stderr)

    scores = [r for r in [mobile_result, tablet_result] if r is not None]
    if scores:
        avg = sum(s.get("score", 0) for s in scores) / len(scores)
        return {"score": round(avg, 2), "mobile": mobile_result, "tablet": tablet_result}
    return {"score": 0, "reason": "No mobile/tablet screenshots found"}


def eval_data_display(eval_report: dict) -> dict | None:
    """Data Display pass_rate from eval_report.json (code-verified, no VLM)."""
    dd_list = eval_report.get("data_display", [])
    if not dd_list:
        return None
    dd_pass_rate = eval_report.get("data_display_pass_rate")
    if dd_pass_rate is None:
        total_expected = 0
        total_found = 0
        for dd in dd_list:
            found = dd.get("found_items", [])
            missing = dd.get("missing_items", [])
            total_found += len(found)
            total_expected += len(found) + len(missing)
        dd_pass_rate = total_found / total_expected if total_expected > 0 else 0
    result = {
        "pass_rate": round(dd_pass_rate, 4),
        "details": dd_list,
    }
    print(f"  [data_display] pass_rate: {dd_pass_rate}", file=sys.stderr)
    return result


def eval_navigation(eval_report: dict) -> dict | None:
    """Navigation pass_rate from eval_report.json."""
    nav_list = eval_report.get("navigation", [])
    if not nav_list:
        return None
    nav_pass_rate = eval_report.get("navigation_pass_rate")
    if nav_pass_rate is None:
        passed = sum(1 for n in nav_list if n.get("result") == "pass")
        nav_pass_rate = passed / len(nav_list)
    result = {
        "pass_rate": round(nav_pass_rate, 4),
        "passed": sum(1 for n in nav_list if n.get("result") == "pass"),
        "total": len(nav_list),
        "details": nav_list,
    }
    print(f"  [navigation] pass_rate: {nav_pass_rate}", file=sys.stderr)
    return result


def eval_interactions(eval_report: dict) -> dict | None:
    """Interactions pass_rate from eval_report.json."""
    inter_list = eval_report.get("interactions", [])
    if not inter_list:
        return None
    inter_pass_rate = eval_report.get("interaction_pass_rate")
    if inter_pass_rate is None:
        passed = sum(1 for i in inter_list if i.get("result") == "pass")
        inter_pass_rate = passed / len(inter_list)
    result = {
        "pass_rate": round(inter_pass_rate, 4),
        "passed": sum(1 for i in inter_list if i.get("result") == "pass"),
        "total": len(inter_list),
        "details": inter_list,
    }
    print(f"  [interactions] pass_rate: {inter_pass_rate}", file=sys.stderr)
    return result


# ─── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VLM-as-Judge for web-design evaluation (enhanced)")
    parser.add_argument("eval_output_dir", help="Eval Agent output dir (screenshots/, eval_report.json)")
    parser.add_argument("task_input_path", help="Path to task_input.json")
    parser.add_argument("source_brief_path", help="Path to source_brief.md")
    parser.add_argument("--model", default="claude-opus-4-6", help="VLM model to use")
    parser.add_argument("--output", default=None, help="Output JSON path (default: stdout)")
    parser.add_argument("--max-workers", type=int, default=4, help="Max parallel API calls")
    parser.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"],
                        help="API provider (anthropic or openai)")
    parser.add_argument("--base-url", default=None, help="Override API base URL")
    parser.add_argument("--api-key", default=None, help="Override API key")
    args = parser.parse_args()

    # ── Init client ────────────────────────────────────────────────
    init_client(args.provider, args.base_url, args.api_key)

    eval_dir = Path(args.eval_output_dir)
    task_input = json.loads(Path(args.task_input_path).read_text())
    output_path = Path(args.output) if args.output else None

    screenshots_dir = eval_dir / "screenshots"
    pages_spec = task_input.get("pages", [{"page_id": "home"}])
    site = task_input.get("site", {})
    data_display_spec = task_input.get("data_display", [])

    # Load eval_report.json from Eval Agent
    eval_report = {}
    report_path = eval_dir / "eval_report.json"
    if report_path.exists():
        eval_report = json.loads(report_path.read_text())
    else:
        print("[WARN] eval_report.json not found, navigation/interaction/data_display scores will be 0", file=sys.stderr)

    print(f"Judging web-design: {eval_dir} with model={args.model}, "
          f"provider={args.provider}, max_workers={args.max_workers}", file=sys.stderr)

    results = {}
    vlm_failures = 0
    total_vlm_calls = 0

    def _flush_results():
        text = json.dumps(results, indent=2, ensure_ascii=False)
        if output_path:
            output_path.write_text(text)
        return text

    # ── Evaluate VLM dimensions ────────────────────────────────────
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        # 1. Visual Design (aesthetics + layout, merged)
        try:
            results["visual_design"] = eval_visual_design(screenshots_dir, pages_spec, args.model, pool)
            total_vlm_calls += 1
        except Exception as e:
            print(f"  [visual_design] FAILED — {e}", file=sys.stderr)
            results["visual_design"] = {"score": 0, "reason": str(e)}
            vlm_failures += 1
            total_vlm_calls += 1

    # 2. Responsiveness (if site.responsive)
    if site.get("responsive", True):
        try:
            with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
                results["responsiveness"] = eval_responsiveness(screenshots_dir, pages_spec, args.model, pool)
            total_vlm_calls += 1
        except Exception as e:
            print(f"  [responsiveness] FAILED — {e}", file=sys.stderr)
            results["responsiveness"] = {"score": 0, "reason": str(e)}
            vlm_failures += 1
            total_vlm_calls += 1

    # ── Evaluate pass-rate dimensions (from eval_report, no VLM) ──
    # 4. Navigation
    nav_result = eval_navigation(eval_report)
    if nav_result:
        results["navigation"] = nav_result

    # 5. Interactions
    inter_result = eval_interactions(eval_report)
    if inter_result:
        results["interactions"] = inter_result

    # 6. Data Display
    if data_display_spec:
        dd_result = eval_data_display(eval_report)
        if dd_result:
            results["data_display"] = dd_result

    # ── Check VLM failures before writing output ──────────────────
    if vlm_failures > 0:
        print(f"[ERROR] {vlm_failures}/{total_vlm_calls} VLM calls failed, skipping result output",
              file=sys.stderr)
        if output_path and output_path.exists():
            output_path.unlink()
        sys.exit(1)

    # ── Overall score (simple average of all dimensions) ────────────
    all_dims = ["visual_design", "navigation", "interactions",
                "responsiveness", "data_display"]
    dim_scores = []
    for dim in all_dims:
        if dim not in results:
            continue
        dim_data = results[dim]
        if "score" in dim_data:
            dim_scores.append(dim_data["score"])
        elif "pass_rate" in dim_data:
            # min-max rescale: pass_rate ∈ [0,1] → [1,5] to align with the VLM scale
            dim_scores.append(dim_data["pass_rate"] * 4 + 1)

    results["overall"] = round(sum(dim_scores) / len(dim_scores), 2) if dim_scores else 0

    # ── Final output ──────────────────────────────────────────────
    final = _flush_results()
    if not output_path:
        print(final)
    else:
        print(f"Results saved to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
