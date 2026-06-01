#!/usr/bin/env python3
"""Enhanced VLM-as-Judge for PPT evaluation.

Based on tasks/ppt-generation/scripts/vlm_judge.py, extended with:
  - Multi-provider support (anthropic / openai)
  - --base-url and --api-key overrides
  - --max-workers concurrent API calls
  - Incremental result flushing

4 dimensions (1-5 scale each):
  - Content:      per-slide text quality, info density, clarity
  - Design:       per-slide visual aesthetics, color, layout, consistency
  - Completeness: task requirement fulfillment (needs task_input.json)
  - Fidelity:     factual consistency with source (needs source_brief.md)

Usage:
    python vlm_judge_ext.py <judge_input_dir> <task_input_path> <source_brief_path> \
        [--model MODEL] [--provider anthropic|openai] [--base-url URL] [--api-key KEY] \
        [--max-workers N] [--output PATH]

Where <judge_input_dir> contains slide_*.png (and optionally slide_text.json).

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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─── Provider helpers ────────────────────────────────────────────────────

_CLIENT = None
_PROVIDER = "anthropic"
_GEMINI_BASE_URL = ""
_GEMINI_API_KEY = ""


def init_client(provider: str, base_url: str, api_key: str):
    """Initialise the module-level VLM client. base_url / api_key are forwarded from
    pipeline.py's --base-url / --api-key, originating in agent_configs/snippets/judge.snippet."""
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

RUBRIC_CONTENT = """\
Evaluate the **content quality** of this presentation slide.

Judge how effectively this slide delivers its key message to the reader.

Criteria:
- Key message: does the slide have a clear takeaway that the reader can grasp?
- Information density: appropriate amount of content (not too crowded, not too sparse)
- Clarity: text is well-written, grammatically correct, easy to understand
- Text-visual balance: charts/images complement the text and reinforce the message

If rendering issues (truncated text, blank charts) cause information to be LOST, deduct proportionally to how much of the slide's message is affected. But if the core message still comes through despite minor visual flaws, do not over-penalize — visual polish is scored under Design.

Scoring (1-5):
- 5: Clear key message, well-developed points, visuals and text complement each other
- 4: Key message clear, good content, but minor gaps (e.g., a chart lacks labels, text slightly sparse)
- 3: Core information present but message is diluted (e.g., missing title, no clear takeaway, or significant content lost to rendering)
- 2: Key message unclear, content poorly organized or mostly lost to rendering issues
- 1: No discernible message — slide is blank, empty, or content entirely unreadable

Return JSON: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""

RUBRIC_DESIGN = """\
Evaluate the **visual design** of this presentation slide.

Criteria:
- Color scheme: harmonious, appropriate for the tone
- Layout: clean alignment, proper spacing, no overlapping elements
- Typography: readable fonts, clear hierarchy (title vs body)
- Visual elements: backgrounds, icons, shapes that enhance the message
- Consistency: matches the overall deck style

Scoring (1-5):
- 5: Harmonious colors, engaging visual elements, professional and polished
- 4: Good colors with some visual elements, minor design flaws
- 3: Basic color scheme, with rough layout and no supplementary visual elements 
- 2: Monotonous black/white, readable but unappealing
- 1: Conflicting styles, content difficult to read

Return JSON: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""


RUBRIC_COMPLETENESS = """\
Evaluate the **task completeness** of this presentation.

Check whether the required CONTENT is present in the slides. For each requirement below, determine if the corresponding content exists in the presentation.

Task requirements:
{task_requirements}

Important:
- Judge whether the required content IS PRESENT, not how it looks visually.
- A chart that renders blank or empty counts as MISSING content (deduct).
- Text that is overlapping or hard to read but IS present counts as COMPLETE (do not deduct — visual issues are scored under Design).

Scoring (1-5):
- 5: All required content present — every section, data point, and chart accounted for
- 4: Core content present, minor omissions (e.g., one data point or detail missing)
- 3: Most content present, but some required sections or charts are missing
- 2: Only partially complete — multiple required elements missing
- 1: Key required content not present

Return JSON: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""


# ─── Helpers ─────────────────────────────────────────────────────────────

def load_images_as_base64(judge_dir: Path) -> list[dict]:
    """Load slide PNGs as base64 for API calls."""
    images = []
    for png in sorted(judge_dir.glob("slide_*.png")):
        with open(png, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        images.append({"path": str(png), "base64": b64})
    return images


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
                with open(img_path, "rb") as f:
                    header = f.read(16)
                    f.seek(0)
                    raw = f.read()
                if header[:8] == b'\x89PNG\r\n\x1a\n':
                    mime = "image/png"
                elif header[:3] == b'\xff\xd8\xff':
                    mime = "image/jpeg"
                elif header[:4] == b'GIF8':
                    mime = "image/gif"
                elif header[:4] == b'RIFF' and header[8:12] == b'WEBP':
                    mime = "image/webp"
                else:
                    mime, _ = mimetypes.guess_type(str(img_path))
                if mime and mime.startswith("image/"):
                    b64 = base64.b64encode(raw).decode()
                    blocks.append(_img_block(b64, mime))
            i += 2

    return blocks


def build_task_requirements(task_input: dict) -> str:
    """Extract human-readable requirements from task_input.json."""
    parts = []
    deck = task_input.get("deck", {})
    if "slide_count" in deck:
        parts.append(f"- Slide count: exactly {deck['slide_count']}")
    if "audience" in deck:
        parts.append(f"- Target audience: {deck['audience']}")
    if "tone" in deck:
        parts.append(f"- Tone: {deck['tone']}")
    if "aspect_ratio" in deck:
        parts.append(f"- Aspect ratio: {deck['aspect_ratio']}")

    brief = task_input.get("brief", {})
    if "goal" in brief:
        parts.append(f"- Goal: {brief['goal']}")

    slides = task_input.get("slides", [])
    if slides:
        parts.append(f"- Must have these {len(slides)} slides in order:")
        for s in slides:
            parts.append(f"  - \"{s.get('title', '?')}\": {s.get('objective', '')}")

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

def _make_per_slide_message(img: dict, rubric: str) -> list:
    return [{"role": "user", "content": [
        {"type": "text", "text": rubric},
        _img_block(img["base64"]),
    ]}]


def _make_full_deck_message(images: list[dict], rubric: str) -> list:
    content = [{"type": "text", "text": rubric}]
    for img in images:
        content.append(_img_block(img["base64"]))
    return [{"role": "user", "content": content}]


# ─── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VLM-as-Judge for PPT evaluation (enhanced)")
    parser.add_argument("judge_input_dir", help="Directory with slide_*.png files")
    parser.add_argument("task_input_path", help="Path to task_input.json")
    parser.add_argument("source_brief_path", help="Path to source_brief.md")
    parser.add_argument("--model", default="claude-opus-4-6", help="VLM model to use")
    parser.add_argument("--output", default=None, help="Output JSON path (default: stdout)")
    parser.add_argument("--max-workers", type=int, default=8, help="Max parallel API calls")
    parser.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"],
                        help="API provider (anthropic or openai)")
    parser.add_argument("--base-url", default=None, help="Override API base URL")
    parser.add_argument("--api-key", default=None, help="Override API key")
    args = parser.parse_args()

    # ── Init client ────────────────────────────────────────────────
    init_client(args.provider, args.base_url, args.api_key)

    judge_dir = Path(args.judge_input_dir)
    task_input = json.loads(Path(args.task_input_path).read_text())
    source_brief_path = Path(args.source_brief_path)

    images = load_images_as_base64(judge_dir)
    if not images:
        print(f"Error: No slide_*.png found in {judge_dir}", file=sys.stderr)
        sys.exit(1)

    n = len(images)
    total_calls = n * 2 + 2  # Content×N + Design×N + Completeness + Fidelity
    output_path = Path(args.output) if args.output else None
    print(f"Judging {n} slides with model={args.model}, provider={args.provider}, "
          f"{total_calls} API calls (max_workers={args.max_workers})", file=sys.stderr)

    # ── Pre-build messages ─────────────────────────────────────────
    task_req = build_task_requirements(task_input)
    rubric_complete = RUBRIC_COMPLETENESS.format(task_requirements=task_req)

    brief_blocks = load_brief_assets(source_brief_path)
    img_count = sum(1 for b in brief_blocks if b.get("type") in ("image", "image_url"))
    print(f"Loaded source_brief with {img_count} inline images", file=sys.stderr)

    # Fidelity message (full deck + source brief)
    fidelity_content = [
        {"type": "text", "text": (
            "Evaluate the **factual fidelity** of the presentation slides.\n\n"
            "Focus on the SUBSTANTIVE CONTENT of each slide (data points, statistics, "
            "charts, conclusions), not on titles, section headings, or phrasing choices.\n\n"
            "Check whether the data and claims can be derived from the source material below:\n"
            "- Are key numbers, rankings, and statistics traceable to the source?\n"
            "- Are chart/graph values consistent with the source data?\n"
            "- Are conclusions logically derivable from the source (even if synthesized)?\n\n"
            "Do NOT penalize for:\n"
            "- Minor rounding (e.g., $3.123T displayed as $3.12T)\n"
            "- Rephrased or shortened wording (as long as meaning is preserved)\n"
            "- Reasonable editorial synthesis (e.g., recommendations derived from the data)\n"
            "- Title/subtitle wording differences from the source\n\n"
            "Only penalize for: fabricated data not in the source, materially wrong numbers, "
            "claims that contradict the source, or invented names/dates.\n\n"
            "--- SOURCE MATERIAL START ---"
        )},
    ]
    fidelity_content.extend(brief_blocks)
    fidelity_content.append({"type": "text", "text": (
        "--- SOURCE MATERIAL END ---\n\n"
        "Now here are the presentation slides to evaluate:"
    )})
    for img in images:
        fidelity_content.append(_img_block(img["base64"]))
    fidelity_content.append({"type": "text", "text": (
        "Scoring (1-5):\n"
        "- 5: All content traceable to source, no fabrication\n"
        "- 4: Minor extrapolation that doesn't affect core conclusions\n"
        "- 3: Some untraceable information present\n"
        "- 2: Notable deviations from source material\n"
        "- 1: Extensive fabrication\n\n"
        'Return JSON: {"score": <1-5>, "reason": "<brief explanation>"}'
    )})

    # ── Results containers ─────────────────────────────────────────
    content_scores = [None] * n
    design_scores = [None] * n
    deck_results = {}

    # Build all tasks: (dim, idx) -> message-builder function
    all_tasks = {}
    for i, img in enumerate(images):
        all_tasks[("content", i)] = lambda _img=img: _make_per_slide_message(_img, RUBRIC_CONTENT)
        all_tasks[("design", i)] = lambda _img=img: _make_per_slide_message(_img, RUBRIC_DESIGN)
    all_tasks[("completeness", None)] = lambda: _make_full_deck_message(images, rubric_complete)
    all_tasks[("fidelity", None)] = lambda: [{"role": "user", "content": fidelity_content}]

    pending_tasks = set(all_tasks.keys())
    max_rounds = 3

    for round_idx in range(1, max_rounds + 1):
        if not pending_tasks:
            break

        print(f"  [round {round_idx}/{max_rounds}] {len(pending_tasks)} task(s) pending", file=sys.stderr)

        futures = {}
        with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            for key in sorted(pending_tasks):
                msg = all_tasks[key]()
                futures[pool.submit(call_vlm, msg, args.model)] = key

            for future in as_completed(futures):
                dim, idx = futures[future]
                label = f"{dim}[{idx}]" if idx is not None else dim
                try:
                    result = future.result()
                    score = result.get("score", 0)
                    if score > 0:
                        if dim == "content":
                            content_scores[idx] = result
                        elif dim == "design":
                            design_scores[idx] = result
                        else:
                            deck_results[dim] = result
                        pending_tasks.discard((dim, idx))
                        print(f"    {label}: score={score} ✓", file=sys.stderr)
                    else:
                        print(f"    {label}: score=0 (will retry)", file=sys.stderr)
                except Exception as e:
                    print(f"    {label}: FAILED — {e} (will retry)", file=sys.stderr)

        if pending_tasks and round_idx < max_rounds:
            wait = 5 * round_idx
            print(f"  [round {round_idx}] {len(pending_tasks)} task(s) still pending, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)

    # ── Check for unresolved tasks ────────────────────────────────
    if pending_tasks:
        labels = [f"{d}[{i}]" if i is not None else d for d, i in sorted(pending_tasks)]
        print(f"[ERROR] {len(pending_tasks)} task(s) failed after {max_rounds} rounds: {', '.join(labels)}",
              file=sys.stderr)
        if output_path and output_path.exists():
            output_path.unlink()
        sys.exit(1)

    # ── Final output ───────────────────────────────────────────────
    results = {}

    content_avg = sum(s.get("score", 0) for s in content_scores) / n
    results["content"] = {
        "score": round(content_avg, 2),
        "per_slide": content_scores,
    }

    design_avg = sum(s.get("score", 0) for s in design_scores) / n
    results["design"] = {
        "score": round(design_avg, 2),
        "per_slide": design_scores,
    }

    results["completeness"] = deck_results["completeness"]
    results["fidelity"] = deck_results["fidelity"]

    dim_scores = [
        results["content"]["score"],
        results["design"]["score"],
        results["completeness"].get("score", 0),
        results["fidelity"].get("score", 0),
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
