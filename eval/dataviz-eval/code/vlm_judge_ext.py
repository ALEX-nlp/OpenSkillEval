#!/usr/bin/env python3
"""Enhanced VLM-as-Judge for data-visualization evaluation.

Based on tasks/data-visualization/scripts/vlm_judge.py, extended with:
  - Multi-provider support (anthropic / openai)
  - --base-url and --api-key overrides
  - --max-workers concurrent API calls (4 dimensions in parallel)
  - Incremental result flushing
  - Regex fallback for JSON parsing

4 dimensions (1-5 scale each):
  - Insight Expression:  does the visualization effectively convey the goal's insight?
  - Data Accuracy:       is the data consistent with source_data.json, no fabrication?
  - Visual Quality:      aesthetics, color, layout, labels, professional finish
  - Completeness:        does it fulfill the goal and instruction requirements?

Usage:
    python vlm_judge_ext.py <viz_png> <task_input_path> <source_brief_path> <source_data_path> \
        [--model MODEL] [--provider anthropic|openai] [--base-url URL] [--api-key KEY] \
        [--max-workers N] [--output PATH]

Outputs JSON with per-dimension scores and reasoning.
"""

import argparse
import base64
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
import requests as _requests
import time
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
    forwarded from judge_dataviz.sh --base-url / --api-key, originating
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

RUBRIC_INSIGHT_EXPRESSION = """\
Evaluate the **insight expression** of this data visualization.

The visualization was created to convey a specific insight:

**Goal insight**: {insight}

Criteria:
- Does the chosen visualization type effectively communicate this insight?
- Can the reader **actually** understand the key message at a glance, as rendered?
- Are there effective annotations, highlights, or emphasis that draw attention to the insight?
- Is the presentation approach well-suited to the data characteristics?
- Does the visualization tell a clear story, or does the reader have to work to extract meaning?

Important: Judge the **end result as the reader would experience it**. A structurally sound approach that is undermined by severe rendering problems (e.g., overlapping labels hiding key annotations, illegible text in critical areas) should be scored lower — the insight is only "expressed" if the reader can actually perceive it. Conversely, do NOT double-penalize minor cosmetic issues already covered by Visual Quality; only penalize rendering problems that **directly obstruct** the target insight.

Note: The creator was free to choose ANY chart type or visual approach. Do NOT penalize for unconventional choices — judge ONLY whether the insight is effectively conveyed.

Scoring (1-5):
- 5: Reader grasps the key message immediately as rendered, clever use of annotation/emphasis/contrast, all insight components clearly visible
- 4: Insight clearly expressed, reasonable approach, minor gaps in emphasis or supporting elements
- 3: Insight basically understandable but presentation is generic or partially obscured, reader must interpret on their own
- 2: Presentation approach mismatches the insight goal, or key information is buried/illegible
- 1: Cannot read the target insight from the visualization

Return JSON: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""

RUBRIC_DATA_ACCURACY = """\
Evaluate the **data accuracy and factual fidelity** of this data visualization.

You are given the visualization, the original data, and background context. Check:
- Are the data points, proportions, and trends consistent with the source data?
- Are axis scales, labels, and values correct?
- Are any annotations or callouts traceable to the source material?
- Is there any fabricated data or misleading representation?

--- SOURCE DATA ---
{source_data}
--- END SOURCE DATA ---

--- SOURCE CONTEXT ---
{source_brief}
--- END SOURCE CONTEXT ---

Scoring (1-5):
- 5: All data points, proportions, and trends match source exactly; annotations traceable to source; no fabrication
- 4: Core data correct, minor deviations in secondary data points or slight extrapolation
- 3: Mostly correct, but notable numerical errors, proportion distortion, or untraceable annotations
- 2: Multiple data inconsistencies with source, or obvious fabricated content
- 1: Extensive data errors or fabrication, visualization is not trustworthy

Return JSON: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""

RUBRIC_VISUAL_QUALITY = """\
Evaluate the **visual quality** of this data visualization.

Criteria:
- Color scheme: harmonious palette, appropriate for the topic, colorblind-friendly if applicable
- Layout: clean arrangement, proper spacing, nothing overlapping
- Labels and annotations: title, axis labels, legend, units — all present and readable
- Typography: readable fonts, clear size hierarchy
- Professional finish: publication-ready quality, attention to detail

Scoring (1-5):
- 5: Harmonious colors, polished layout, complete labels (title/axes/legend/units), publication-grade professional quality
- 4: Good colors and layout, labels mostly complete, minor flaws (uneven spacing, label overlap)
- 3: Basic colors, functional layout but lacks design sophistication, some labels missing
- 2: Monotonous or clashing colors, rough layout, labels severely lacking
- 1: Chaotic styles, overlapping elements, visually unacceptable

Return JSON: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""

RUBRIC_COMPLETENESS = """\
Evaluate the **task completeness** of this data visualization.

The task requirements are provided below. Check whether the goal's insight is expressed and all requirements are met.

Task requirements:
{task_requirements}

Scoring (1-5):
- 5: Goal insight fully expressed, all requirements completely satisfied
- 4: Core requirements met, minor omissions (e.g., missing unit or a specific annotation)
- 3: Most requirements met, but notable gaps
- 2: Only partially satisfied
- 1: Key requirements not met, output unusable

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
    # so check the b64 length directly to avoid raw < 5MB but b64 > 5MB rejection
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


def build_task_requirements(task_input: dict) -> str:
    """Extract human-readable requirements from task_input.json."""
    parts = []

    goal = task_input.get("goal", [])
    if isinstance(goal, list) and goal:
        parts.append("**Insight checkpoints** (each must be conveyed):")
        for i, item in enumerate(goal):
            gid = item.get("id", f"item-{i}")
            insight = item.get("insight", "")
            parts.append(f"  {i+1}. [{gid}] {insight}")
    elif isinstance(goal, dict):
        if "insight" in goal:
            parts.append(f"- Insight to convey: {goal['insight']}")

    style = task_input.get("style", {})
    if "theme" in style:
        parts.append(f"- Theme: {style['theme']}")
    if "audience" in style:
        parts.append(f"- Target audience: {style['audience']}")
    if "tone" in style:
        parts.append(f"- Tone/style: {style['tone']}")

    return "\n".join(parts) if parts else "No specific requirements provided."


def build_insight_text(task_input: dict) -> str:
    """Extract insight text from goal array/dict."""
    goal = task_input.get("goal", [])
    if isinstance(goal, list) and goal:
        lines = []
        for item in goal:
            gid = item.get("id", "")
            ins = item.get("insight", "")
            lines.append(f"[{gid}] {ins}" if gid else ins)
        return "\n".join(lines)
    elif isinstance(goal, dict):
        return goal.get("insight", "No insight specified")
    return "No insight specified"


def truncate_data(source_data: str, max_chars: int = 8000) -> str:
    """Truncate source data to fit in context, keeping structure visible."""
    #if len(source_data) <= max_chars:
    return source_data
    #return source_data[:max_chars] + "\n... [truncated for length]"


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
        # Regex fallback: try to extract {"score": N, "reason": "..."}
        match = re.search(r'\{[^}]*"score"\s*:\s*\d[^}]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
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
                # Extract text from Gemini response
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

def _make_single_image_message(viz_b64: str, rubric: str) -> list:
    """Build message for single-image rubric evaluation."""
    return [{"role": "user", "content": [
        {"type": "text", "text": rubric},
        _img_block(viz_b64),
    ]}]


# ─── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VLM-as-Judge for data-visualization evaluation (enhanced)")
    parser.add_argument("viz_png", help="Path to visualization PNG file")
    parser.add_argument("task_input_path", help="Path to task_input.json")
    parser.add_argument("source_brief_path", help="Path to source_brief.md")
    parser.add_argument("source_data_path", help="Path to source_data.json")
    parser.add_argument("--model", default="claude-opus-4-6", help="VLM model to use")
    parser.add_argument("--output", default=None, help="Output JSON path (default: stdout)")
    parser.add_argument("--max-workers", type=int, default=4, help="Max parallel API calls")
    parser.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"],
                        help="API provider (anthropic or openai)")
    parser.add_argument("--base-url", default=None, help="Override API base URL")
    parser.add_argument("--api-key", default=None, help="Override API key")
    parser.add_argument("--agent-eval-report", required=True,
                        help="Path to agent eval_report.json for data_accuracy dimension")
    args = parser.parse_args()

    # ── Init client ────────────────────────────────────────────────
    init_client(args.provider, args.base_url, args.api_key)

    viz_path = Path(args.viz_png)
    task_input = json.loads(Path(args.task_input_path).read_text())

    if not viz_path.exists():
        print(f"Error: {viz_path} not found", file=sys.stderr)
        sys.exit(1)

    viz_b64 = load_image_as_base64(viz_path)
    insight = build_insight_text(task_input)
    output_path = Path(args.output) if args.output else None

    print(f"Judging visualization: {viz_path} with model={args.model}, provider={args.provider}, max_workers={args.max_workers}", file=sys.stderr)
    print(f"  Goal insight: {insight[:80]}...", file=sys.stderr)

    # ── Load agent eval report for data_accuracy ────────────────────
    agent_eval_path = Path(args.agent_eval_report)
    if not agent_eval_path.exists():
        print(f"Error: agent eval report not found at {agent_eval_path}", file=sys.stderr)
        sys.exit(1)
    agent_report = json.loads(agent_eval_path.read_text())
    da = agent_report.get("data_accuracy", {})
    if not isinstance(da, dict) or "score" not in da:
        print(f"Error: agent eval report missing data_accuracy.score", file=sys.stderr)
        sys.exit(1)
    print(f"  data_accuracy from agent eval: score={da.get('score')}", file=sys.stderr)

    # ── Pre-build rubrics ──────────────────────────────────────────
    rubric_insight = RUBRIC_INSIGHT_EXPRESSION.format(insight=insight)
    task_req = build_task_requirements(task_input)
    rubric_complete = RUBRIC_COMPLETENESS.format(task_requirements=task_req)

    # ── Results container ──────────────────────────────────────────
    results = {"data_accuracy": da}

    # dimension name -> message-builder function, used for retries
    dim_tasks = {
        "insight_expression": lambda: _make_single_image_message(viz_b64, rubric_insight),
        "visual_quality":     lambda: _make_single_image_message(viz_b64, RUBRIC_VISUAL_QUALITY),
        "completeness":       lambda: _make_single_image_message(viz_b64, rubric_complete),
    }

    max_rounds = 3  # max retry rounds (including the first attempt)
    pending_dims = set(dim_tasks.keys())

    for round_idx in range(1, max_rounds + 1):
        if not pending_dims:
            break

        dims_this_round = sorted(pending_dims)
        print(f"  [round {round_idx}/{max_rounds}] evaluating: {', '.join(dims_this_round)}", file=sys.stderr)

        futures = {}
        with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            for dim in dims_this_round:
                msg = dim_tasks[dim]()
                futures[pool.submit(call_vlm, msg, args.model)] = dim

            for future in as_completed(futures):
                dim = futures[future]
                try:
                    result = future.result()
                    score = result.get("score", 0)
                    if score > 0:
                        results[dim] = result
                        pending_dims.discard(dim)
                        print(f"    {dim}: score={score} ✓", file=sys.stderr)
                    else:
                        print(f"    {dim}: score=0 (will retry)", file=sys.stderr)
                except Exception as e:
                    print(f"    {dim}: FAILED — {e} (will retry)", file=sys.stderr)

        if pending_dims and round_idx < max_rounds:
            wait = 5 * round_idx
            print(f"  [round {round_idx}] {len(pending_dims)} dim(s) still pending, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)

    # ── Check for unresolved dimensions ───────────────────────────
    if pending_dims:
        print(f"[ERROR] {len(pending_dims)} dim(s) failed after {max_rounds} rounds: {', '.join(sorted(pending_dims))}",
              file=sys.stderr)
        if output_path and output_path.exists():
            output_path.unlink()
        sys.exit(1)

    # ── Final output ───────────────────────────────────────────────
    dim_scores = [
        results["insight_expression"].get("score", 0),
        results["data_accuracy"].get("score", 0),
        results["visual_quality"].get("score", 0),
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
