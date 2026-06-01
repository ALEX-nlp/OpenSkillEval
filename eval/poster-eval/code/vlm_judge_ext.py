#!/usr/bin/env python3
"""Enhanced VLM-as-Judge for poster evaluation.

Based on tasks/poster-generation/scripts/vlm_judge.py, extended with:
  - Multi-provider support (anthropic / openai)
  - --base-url and --api-key overrides
  - --max-workers concurrent API calls (3 dimensions in parallel)
  - Incremental result flushing

3 dimensions (1-5 scale each):
  - Design:         visual aesthetics, color, layout, consistency
  - Content:        data accuracy, traceability to source, no fabrication
  - Completeness:   task requirement fulfillment

Usage:
    python vlm_judge_ext.py <poster_png> <task_input_path> <source_brief_path> \
        [--model MODEL] [--provider anthropic|openai] [--base-url URL] [--api-key KEY] \
        [--max-workers N] [--output PATH]

Outputs JSON with per-dimension scores and reasoning.
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
import requests as _requests
import urllib3
from io import BytesIO
from PIL import Image

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─── Provider helpers ────────────────────────────────────────────────────

_CLIENT = None
_PROVIDER = "anthropic"
_GEMINI_BASE_URL = ""
_GEMINI_API_KEY = ""


def init_client(provider: str, base_url: str, api_key: str):
    """Initialise the module-level VLM client. base_url / api_key are
    forwarded from judge_poster.sh --base-url / --api-key, originally sourced
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


# ─── Rubric prompts ─────────────────────────────────────────────────────

RUBRIC_DESIGN = """\
Evaluate the **visual design quality** of this poster/infographic.

Criteria:
- Color scheme: harmonious palette, appropriate for the topic and tone
- Layout: clean alignment, proper spacing, clear visual hierarchy
- Typography: readable fonts, clear size hierarchy (title > heading > body)
- Consistency: unified style throughout (colors, fonts, spacing)
- Professional polish: attention to detail, no overlapping elements

Scoring (1-5):
- 5: Harmonious colors, polished layout, clear hierarchy, professional design quality
- 4: Good colors and layout, minor flaws (slight misalignment, spacing issues)
- 3: Basic color scheme, layout functional but lacks design sophistication
- 2: Monotonous or clashing colors, rough layout
- 1: Chaotic styles, overlapping elements, hard to read

Return JSON: {"score": <1-5>, "reason": "<brief explanation>"}
"""


RUBRIC_COMPLETENESS = """\
Evaluate the **task completeness** of this poster/infographic.

The task requirements are provided below. Check whether ALL requirements are met.

Task requirements:
{task_requirements}

Scoring (1-5):
- 5: All requirements fully satisfied
- 4: Core requirements met, minor omissions
- 3: Most requirements met, some notable gaps
- 2: Only partially satisfied
- 1: Key requirements not met

Return JSON: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""


# ─── Helpers ─────────────────────────────────────────────────────────────

MAX_IMAGE_DIMENSION = 8000
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Claude API 5MB limit


def load_image_as_base64(path: Path) -> str:
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
    # so check b64 length directly to avoid rejection when raw < 5MB but b64 > 5MB
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


def load_brief_assets(brief_path: Path) -> list[dict]:
    """Parse source_brief.md into interleaved text/image content blocks."""
    brief_dir = brief_path.parent
    text = brief_path.read_text(encoding="utf-8")

    pattern = r"!\[([^\]]*)\]\((\./[^)]+)\)"
    parts = re.split(pattern, text)

    blocks = []
    i = 0
    while i < len(parts):
        if i % 3 == 0:
            chunk = parts[i].strip()
            if chunk:
                blocks.append({"type": "text", "text": chunk})
            i += 1
        else:
            ref_path = parts[i + 1]
            img_path = (brief_dir / ref_path).resolve()
            if img_path.exists():
                mime, _ = mimetypes.guess_type(str(img_path))
                if mime and mime.startswith("image/"):
                    with open(img_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    blocks.append(_img_block(b64, mime))
            i += 2

    return blocks


def build_task_requirements(task_input: dict) -> str:
    """Extract human-readable requirements from task_input.json."""
    parts = []
    poster = task_input.get("poster", {})
    if "aspect_ratio" in poster:
        parts.append(f"- Aspect ratio: {poster['aspect_ratio']}")
    if "audience" in poster:
        parts.append(f"- Target audience: {poster['audience']}")
    if "tone" in poster:
        parts.append(f"- Tone/style: {poster['tone']}")
    if "venue" in poster:
        parts.append(f"- Venue: {poster['venue']}")

    brief = task_input.get("brief", {})
    if "title" in brief:
        parts.append(f"- Title: {brief['title']}")
    if "goal" in brief:
        parts.append(f"- Goal: {brief['goal']}")

    sections = task_input.get("sections", [])
    if sections:
        parts.append(f"- Must include these {len(sections)} sections:")
        for s in sections:
            parts.append(f"  - \"{s.get('title', '?')}\": {s.get('objective', '')}")

    metrics = task_input.get("metrics", [])
    if metrics:
        parts.append(f"- Must display these metrics:")
        for m in metrics:
            if isinstance(m, dict):
                parts.append(f"  - {m.get('name', '?')}: {m.get('current', '?')} → {m.get('target', '?')}")
            else:
                parts.append(f"  - {m}")

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
        print(f"[VLM] Failed to parse JSON: {raw}", file=sys.stderr)
        return {"score": 0, "reason": f"JSON parse error: {raw[:200]}"}


def call_vlm(messages: list, model: str, max_retries: int = 5) -> dict:
    """Call the VLM API with exponential backoff retry on rate limit / transient errors."""
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
            err_str = str(e).lower()
            is_retryable = any(k in err_str for k in [
                "rate_limit", "rate limit", "429", "overloaded",
                "529", "timeout", "timed out", "connection",
                "server error", "500", "502", "503", "504",
            ])
            if is_retryable and attempt < max_retries:
                wait = min(2 ** attempt * 2, 60)
                print(f"[VLM] retry {attempt+1}/{max_retries} after {wait}s: {e}", file=sys.stderr)
                time.sleep(wait)
            else:
                raise


# ─── Message builders ────────────────────────────────────────────────────

def _make_single_image_message(poster_b64: str, rubric: str) -> list:
    """Build message for single-image rubric evaluation."""
    return [{"role": "user", "content": [
        {"type": "text", "text": rubric},
        _img_block(poster_b64),
    ]}]


def _make_content_message(poster_b64: str, brief_blocks: list[dict]) -> list:
    """Build message for content evaluation (poster + source brief)."""
    content = [
        {"type": "text", "text": (
            "Evaluate the **content accuracy** of this poster.\n\n"
            "Compare every data point, statistic, and factual claim on the poster against "
            "the source material below. Check:\n"
            "- Are all numbers, percentages, and rankings correct and traceable to the source?\n"
            "- Are there any fabricated data points not present in the source?\n"
            "- Are key findings from the source faithfully represented (not distorted or misattributed)?\n\n"
            "Important:\n"
            "- Judge ONLY whether the data and facts are correct. "
            "Do NOT penalize for visual rendering issues (overlapping text, truncated labels, "
            "unreadable sections) — those are scored under Design. "
            "If a data point is present and correct but hard to read due to rendering, "
            "it still counts as correct.\n"
            "- Minor rounding (e.g., $3.123T displayed as $3.12T) is acceptable and "
            "should NOT cause a deduction. Only penalize for meaningful numerical errors "
            "that would mislead the reader.\n\n"
            "--- SOURCE MATERIAL START ---"
        )},
    ]
    content.extend(brief_blocks)
    content.append({"type": "text", "text": (
        "--- SOURCE MATERIAL END ---\n\n"
        "Now here is the poster to evaluate:"
    )})
    content.append(_img_block(poster_b64))
    content.append({"type": "text", "text": (
        "Scoring (1-5):\n"
        "- 5: All data points accurate and traceable to source, no fabrication\n"
        "- 4: Core data correct, minor omissions or rounding differences\n"
        "- 3: Most data correct, but some numbers are wrong or untraceable to source\n"
        "- 2: Multiple inaccuracies, misattributed data, or notable deviations from source\n"
        "- 1: Extensive fabrication or data unrelated to the source material\n\n"
        'Return JSON: {"score": <1-5>, "reason": "<brief explanation>"}'
    )})
    return [{"role": "user", "content": content}]


# ─── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VLM-as-Judge for poster evaluation (enhanced)")
    parser.add_argument("poster_png", help="Path to poster PNG file")
    parser.add_argument("task_input_path", help="Path to task_input.json")
    parser.add_argument("source_brief_path", help="Path to source_brief.md")
    parser.add_argument("--model", default="claude-opus-4-6", help="VLM model to use")
    parser.add_argument("--output", default=None, help="Output JSON path (default: stdout)")
    parser.add_argument("--max-workers", type=int, default=3, help="Max parallel API calls")
    parser.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"],
                        help="API provider (anthropic or openai)")
    parser.add_argument("--base-url", default=None, help="Override API base URL")
    parser.add_argument("--api-key", default=None, help="Override API key")
    args = parser.parse_args()

    # ── Init client ────────────────────────────────────────────────
    init_client(args.provider, args.base_url, args.api_key)

    poster_path = Path(args.poster_png)
    task_input = json.loads(Path(args.task_input_path).read_text())
    source_brief_path = Path(args.source_brief_path)

    if not poster_path.exists():
        print(f"Error: {poster_path} not found", file=sys.stderr)
        sys.exit(1)

    poster_b64 = load_image_as_base64(poster_path)
    output_path = Path(args.output) if args.output else None
    print(f"Judging poster: {poster_path} with model={args.model}, provider={args.provider}, max_workers={args.max_workers}", file=sys.stderr)

    # ── Pre-build all messages ─────────────────────────────────────
    task_req = build_task_requirements(task_input)
    rubric_complete = RUBRIC_COMPLETENESS.format(task_requirements=task_req)

    brief_blocks = load_brief_assets(source_brief_path)
    img_count = sum(1 for b in brief_blocks if b.get("type") == "image")
    print(f"Loaded source_brief with {img_count} inline images", file=sys.stderr)

    # ── Results container ──────────────────────────────────────────
    results = {}
    vlm_failures = 0
    total_vlm_calls = 0

    # ── Submit all 3 dimensions concurrently ───────────────────────
    futures = {}

    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        # 1. Design (single image)
        msg = _make_single_image_message(poster_b64, RUBRIC_DESIGN)
        futures[pool.submit(call_vlm, msg, args.model)] = "visual_design"

        # 2. Content (poster + source brief with inline images)
        msg = _make_content_message(poster_b64, brief_blocks)
        futures[pool.submit(call_vlm, msg, args.model)] = "content"

        # 3. Completeness (poster + task requirements)
        msg = _make_single_image_message(poster_b64, rubric_complete)
        futures[pool.submit(call_vlm, msg, args.model)] = "completeness"

        # ── Collect results ───────────────────────────────────────
        for future in as_completed(futures):
            dim = futures[future]
            total_vlm_calls += 1
            try:
                result = future.result()
                results[dim] = result
                print(f"  [{total_vlm_calls}/3] {dim}: score={result.get('score', '?')}", file=sys.stderr)
            except Exception as e:
                print(f"  [{total_vlm_calls}/3] {dim}: FAILED — {e}", file=sys.stderr)
                results[dim] = {"score": 0, "reason": str(e)}
                vlm_failures += 1

    # ── Check VLM failures before writing output ──────────────────
    if vlm_failures > 0:
        print(f"[ERROR] {vlm_failures}/{total_vlm_calls} VLM calls failed, skipping result output",
              file=sys.stderr)
        if output_path and output_path.exists():
            output_path.unlink()
        sys.exit(1)

    # ── Final output ───────────────────────────────────────────────
    dim_scores = [
        results["visual_design"].get("score", 0),
        results["content"].get("score", 0),
        results["completeness"].get("score", 0),
    ]
    results["overall"] = round(sum(dim_scores) / len(dim_scores), 2)

    final = json.dumps(results, indent=2, ensure_ascii=False)
    if output_path:
        output_path.write_text(final)
        print(f"Results saved to {args.output}", file=sys.stderr)
    else:
        print(final)


if __name__ == "__main__":
    main()
