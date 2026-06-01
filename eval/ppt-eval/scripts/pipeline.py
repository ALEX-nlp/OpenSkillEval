#!/usr/bin/env python3
"""pipeline.py — full PPT eval pipeline (Python version, replaces stage2_judge.sh).

PPT runs in three stages:
  1. stage1_pack.sh    collect *.pptx, pack into tar
  2. manual Mac screenshot  pptx -> tar of slide_*.png
  3. pipeline.py       this script: extract + run VLM judge

Aligned with other evals (dataviz/poster/report/webdesign):
- asyncio.Semaphore for concurrency control, no xargs
- start_new_session=True + os.killpg for clean child process group kill
- SIGINT graceful cleanup: SIGINT(30s) -> SIGTERM(20s) -> SIGKILL
- judge.snippet is the source of truth for credentials; @VAR placeholders expanded in-place in Python
- Single judge by default (claude-opus-4-6); for multiple judges pass --judge multiple times

CLI:
    python pipeline.py <SCREENSHOTS_TAR> [MANIFEST_JSONL] [PARALLEL] [WORKERS_PER_DECK] \
        [--judge "MODEL|PROVIDER|BASE_URL|API_KEY"] [--run-id eval_result]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).absolute().parent
PROJECT_DIR = SCRIPT_DIR.parent              # eval/ppt-eval/
REPO_ROOT = PROJECT_DIR.parent.parent        # OpenSkillEval/
VLM_JUDGE_SCRIPT = PROJECT_DIR / "code" / "vlm_judge_ext.py"
JUDGE_SNIPPET = REPO_ROOT / "agent_configs" / "snippets" / "judge.snippet"

DEFAULT_JUDGE = "claude-opus-4-6|anthropic|@ANTHROPIC_BASE_URL|@ANTHROPIC_API_KEY"

# Screenshot cache (avoids slow PFS IO)
SCREENSHOTS_CACHE_DEFAULT = REPO_ROOT / "eval.bak" / "ppt-eval" / "output"

TASK_INT_TIMEOUT = int(os.environ.get("TASK_INT_TIMEOUT", "30"))
TASK_TERM_TIMEOUT = int(os.environ.get("TASK_TERM_TIMEOUT", "20"))


# ---------------------------------------------------------------------------
# Subprocess env whitelist
# ---------------------------------------------------------------------------
# Only the keys below are forwarded from the operator's shell into the judge /
# uv-run subprocess. Judge credentials are injected explicitly per call.
# Set OPENSKILLEVAL_EXTRA_ENV="KEY1,KEY2" as escape hatch for corp env names.
_INHERITED_ENV_KEYS: frozenset[str] = frozenset({
    # shell / OS basics
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR",
    # locale / timezone
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ",
    # XDG dirs
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    # Docker / Compose (only forwarded if explicitly set)
    "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
    "DOCKER_CONFIG", "DOCKER_BUILDKIT",
    "COMPOSE_DOCKER_CLI_BUILD", "COMPOSE_HTTP_TIMEOUT",
    # uv / Python venv
    "VIRTUAL_ENV", "UV_CACHE_DIR", "UV_PYTHON", "UV_HTTP_TIMEOUT",
    # corp network
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # pipeline self-config (read above via os.environ.get)
    "TASK_INT_TIMEOUT", "TASK_TERM_TIMEOUT",
})
_PIPELINE_EXTRA_ENV_KEYS: frozenset[str] = frozenset({
    "RUN_ID", "MAX_WORKERS", "JUDGES_SPEC",
    "EVAL_AGENT", "EVAL_MODEL", "EVAL_TIMEOUT_MULT",
    "PLAYWRIGHT_BROWSERS_PATH",
})


def _inherit_env(extra_keys: frozenset[str] = frozenset()) -> dict[str, str]:
    """Return only whitelisted entries from os.environ."""
    user_extra = frozenset(
        k.strip()
        for k in os.environ.get("OPENSKILLEVAL_EXTRA_ENV", "").split(",")
        if k.strip()
    )
    allowed = _INHERITED_ENV_KEYS | extra_keys | user_extra
    return {k: v for k, v in os.environ.items() if k in allowed}


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_shutdown_event: asyncio.Event | None = None
_force_kill: bool = False
_live_procs: set[asyncio.subprocess.Process] = set()
_progress_done: int = 0
_progress_total: int = 0
_fail_count: int = 0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pipeline_ppt")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(sh)

    return logger


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _killpg(proc: asyncio.subprocess.Process, sig: int) -> None:
    if proc.returncode is not None:
        return
    pid = proc.pid
    if not pid or pid <= 1:
        return
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


async def _wait_procs(
    procs: list[asyncio.subprocess.Process], timeout: float
) -> list[asyncio.subprocess.Process]:
    deadline = asyncio.get_event_loop().time() + timeout
    for p in procs:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        if p.returncode is not None:
            continue
        try:
            await asyncio.wait_for(p.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass
    return [p for p in procs if p.returncode is None]


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _install_signal_handlers(loop: asyncio.AbstractEventLoop, log: logging.Logger) -> None:
    def _on_signal(sig: int) -> None:
        global _force_kill
        if _force_kill:
            log.warning(">>> received signal %s again, immediately SIGKILL all judge process groups", sig)
            for p in list(_live_procs):
                _killpg(p, signal.SIGKILL)
            return
        _force_kill = True
        if _shutdown_event is not None:
            _shutdown_event.set()
        log.warning(">>> received signal %s, starting graceful cleanup; press Ctrl+C again to exit immediately", sig)
        loop.create_task(_graceful_cascade(log))

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError):
            pass


async def _graceful_cascade(log: logging.Logger) -> None:
    procs = list(_live_procs)
    if not procs:
        return
    log.warning(">>> Phase 1: SIGINT to %d judge process groups (waiting %ds)",
                len(procs), TASK_INT_TIMEOUT)
    for p in procs:
        _killpg(p, signal.SIGINT)
    survivors = await _wait_procs(procs, TASK_INT_TIMEOUT)
    if not survivors:
        return
    log.warning(">>> Phase 2: SIGTERM to %d process groups (waiting %ds)",
                len(survivors), TASK_TERM_TIMEOUT)
    for p in survivors:
        _killpg(p, signal.SIGTERM)
    survivors = await _wait_procs(survivors, TASK_TERM_TIMEOUT)
    if not survivors:
        return
    log.warning(">>> Phase 3: SIGKILL fallback for %d process groups", len(survivors))
    for p in survivors:
        _killpg(p, signal.SIGKILL)
    await _wait_procs(survivors, 5)


# ---------------------------------------------------------------------------
# Snippet ENV parsing + @VAR expansion
# ---------------------------------------------------------------------------

def _parse_snippet_env(snippet_path: Path) -> dict[str, str]:
    if not snippet_path.is_file():
        return {}
    raw = snippet_path.read_text().replace("\\\n", " ")
    out: dict[str, str] = {}
    for line in raw.splitlines():
        s = line.strip()
        if not s.startswith("ENV "):
            continue
        try:
            tokens = shlex.split(s[4:])
        except ValueError:
            continue
        for tok in tokens:
            if "=" not in tok:
                continue
            k, _, v = tok.partition("=")
            k = k.strip()
            if k:
                out[k] = v
    return out


_SNIPPET_ENV: dict[str, str] = {}


def _expand_at(v: str) -> str:
    if not v.startswith("@"):
        return v
    name = v[1:]
    val = _SNIPPET_ENV.get(name) or os.environ.get(name, "")
    if not val:
        raise SystemExit(
            f"❌ judge spec references @{name} but it is not defined in snippet/env "
            f"(check {JUDGE_SNIPPET})"
        )
    return val


def _resolve_judges(specs: list[str]) -> list[tuple[str, str, str, str]]:
    judges = specs or [DEFAULT_JUDGE]
    out: list[tuple[str, str, str, str]] = []
    for s in judges:
        parts = s.split("|")
        if len(parts) != 4:
            raise SystemExit(
                f"❌ --judge format should be 'MODEL|PROVIDER|BASE_URL|API_KEY', got: {s!r}"
            )
        model, provider, base_url, api_key = (_expand_at(p) for p in parts)
        out.append((model, provider, base_url, api_key))
    return out


# ---------------------------------------------------------------------------
# Screenshot extraction + manifest parsing
# ---------------------------------------------------------------------------

def _extract_screenshots(tar_path: Path, cache_dir: Path, log: logging.Logger) -> Path:
    """Extract tar, return the actual root directory containing slide_*.png. Reuse existing extraction cache."""
    tar_stem = tar_path.name
    for suf in (".tar.gz", ".tgz", ".tar"):
        if tar_stem.endswith(suf):
            tar_stem = tar_stem[: -len(suf)]
            break
    work_dir = cache_dir / tar_stem

    # Existing cache containing slide_001.png -> reuse
    cached = next(work_dir.rglob("slide_001.png"), None) if work_dir.is_dir() else None
    if cached is not None:
        log.info("[pipeline] screenshots already extracted, reusing cache: %s", work_dir)
    else:
        work_dir.mkdir(parents=True, exist_ok=True)
        log.info("[pipeline] extracting screenshots to %s ...", work_dir)
        subprocess.run(["tar", "xf", str(tar_path), "-C", str(work_dir)], check=False)
        cached = next(work_dir.rglob("slide_001.png"), None)

    # Parent of parent of slide_001.png = screenshots root (contains multiple {pptx_stem}/ subdirs)
    if cached is not None:
        return cached.parent.parent
    return work_dir


def _load_manifest(manifest_path: Path | None, work_dir: Path) -> tuple[Path, list[dict]]:
    """Read manifest.jsonl; if not found, search the extraction directory."""
    if manifest_path is None or not manifest_path.is_file():
        fallback = work_dir.parent / "manifest.jsonl"
        candidates = [
            work_dir.parent / "manifest.jsonl",
            *list(work_dir.rglob("manifest.jsonl")),
        ]
        for c in candidates:
            if c.is_file():
                manifest_path = c
                break
        else:
            raise SystemExit(f"❌ manifest.jsonl not found (not at {manifest_path} nor {fallback})")
    entries: list[dict] = []
    with open(manifest_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return manifest_path, entries


# ---------------------------------------------------------------------------
# Skip / summary helpers
# ---------------------------------------------------------------------------

def _safe_model(name: str) -> str:
    return re.sub(r"[ /:]", "-", name)


def _result_path(entry: dict, model: str, output_dir: Path) -> Path:
    """7-level hierarchical path aligned with other evals:
    output/<RUN_ID>/<task_family>/<case>/<variant>/<mode>/<runner>/<run>/<trial>/judge_result_<safe_model>.json
    """
    return (
        output_dir
        / entry.get("task_family", "unknown")
        / entry.get("case", "unknown")
        / entry.get("variant", "unknown")
        / entry.get("mode", "unknown")
        / entry.get("runner", "unknown")
        / entry.get("run", "unknown")
        / entry.get("trial", "unknown")
        / f"judge_result_{_safe_model(model)}.json"
    )


def _summarize_judge(jf: Path) -> str:
    try:
        with open(jf) as f:
            d = json.load(f)
    except Exception:
        return ""
    parts = []
    for k in ("content", "design", "completeness", "fidelity"):
        v = d.get(k, {})
        s = v.get("score", 0) if isinstance(v, dict) else 0
        parts.append(f"{k[:4]}={s}")
    ov = d.get("overall", 0)
    return f"{d.get('judge_model','?')}: " + " ".join(parts) + f" ov={ov}"


# ---------------------------------------------------------------------------
# Single deck x single judge execution
# ---------------------------------------------------------------------------

async def _run_one_job(
    entry: dict,
    slide_dir: Path,
    judge: tuple[str, str, str, str],
    workers: int,
    output_dir: Path,
    log: logging.Logger,
) -> tuple[str, str, int]:
    """Run a single (deck x judge). Returns (pptx_stem, model, rc)."""
    global _progress_done, _fail_count
    model, provider, base_url, api_key = judge
    pptx_name = entry["pptx_name"]
    pptx_stem = pptx_name.rsplit(".pptx", 1)[0]
    case = entry["case"]

    result_file = _result_path(entry, model, output_dir)
    result_file.parent.mkdir(parents=True, exist_ok=True)
    short = f"{model}/{pptx_stem}"

    if result_file.is_file():
        return pptx_stem, model, 0

    if _shutdown_event and _shutdown_event.is_set():
        return pptx_stem, model, -1

    case_dir = REPO_ROOT / "tasks" / "ppt-generation" / "shared" / "cases" / case
    task_input = case_dir / "task_input.json"
    source_brief = case_dir / "source_brief.md"
    if not task_input.is_file() or not source_brief.is_file():
        log.warning("[pipeline] case=%s missing task_input.json/source_brief.md, skipping %s",
                    case, pptx_stem)
        _fail_count += 1
        return pptx_stem, model, 1

    _progress_done += 1
    cnt = _progress_done
    log.info("[pipeline] [%d/%d] %s ...", cnt, _progress_total, short)
    start = time.monotonic()

    raw_result = output_dir / f"raw_{pptx_stem}__{_safe_model(model)}.json"

    env = _inherit_env(_PIPELINE_EXTRA_ENV_KEYS)
    # Compatible with vlm_judge_ext.py's old env fallback (after cleanup, --base-url/--api-key alone works too)
    if provider == "openai":
        env["OPENAI_BASE_URL"] = base_url
        env["OPENAI_API_KEY"] = api_key
    else:
        env["ANTHROPIC_BASE_URL"] = base_url
        env["ANTHROPIC_API_KEY"] = api_key

    cmd = [
        "uv", "--project", str(PROJECT_DIR), "run", "python", str(VLM_JUDGE_SCRIPT),
        str(slide_dir), str(task_input), str(source_brief),
        "--model", model,
        "--max-workers", str(workers),
        "--provider", provider,
        "--output", str(raw_result),
        "--base-url", base_url,
        "--api-key", api_key,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
    _live_procs.add(proc)
    err_lines: list[str] = []
    try:
        assert proc.stdout is not None
        async for line in proc.stdout:
            text = line.decode(errors="replace").rstrip()
            if not text:
                continue
            log.debug("[%s] %s", short, text)
            upper = text.upper()
            if "ERROR" in upper or "FAILED" in upper or "TRACEBACK" in upper:
                err_lines.append(text)
        await proc.wait()
        rc = proc.returncode if proc.returncode is not None else -1
    finally:
        _live_procs.discard(proc)

    elapsed = int(time.monotonic() - start)
    m, s = divmod(elapsed, 60)

    if rc == 0 and raw_result.is_file():
        # Inject manifest metadata into the final result_file
        try:
            with open(raw_result) as f:
                data = json.load(f)
            meta = {
                "pptx_name":   entry.get("pptx_name"),
                "task_family": entry.get("task_family"),
                "case":        entry.get("case"),
                "variant":     entry.get("variant"),
                "mode":        entry.get("mode"),
                "runner":      entry.get("runner"),
                "run":         entry.get("run"),
                "trial":       entry.get("trial"),
                "judge_model": model,
            }
            meta.update(data)
            with open(result_file, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        finally:
            raw_result.unlink(missing_ok=True)

        summary = _summarize_judge(result_file)
        log.info("[pipeline] [%d/%d] %s -> OK  %dm%02ds  %s",
                 cnt, _progress_total, short, m, s, summary)
    else:
        _fail_count += 1
        raw_result.unlink(missing_ok=True)
        if _shutdown_event and _shutdown_event.is_set():
            log.warning("[pipeline] [%d/%d] %s -> ABORTED  %dm%02ds",
                        cnt, _progress_total, short, m, s)
        else:
            log.error("[pipeline] [%d/%d] %s -> FAIL  %dm%02ds (rc=%d)",
                      cnt, _progress_total, short, m, s, rc)
            for ln in err_lines[:3]:
                log.error("           %s", ln)
    return pptx_stem, model, rc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _async_main(
    args: argparse.Namespace,
    log: logging.Logger,
    output_dir: Path,
) -> int:
    global _shutdown_event, _progress_total, _SNIPPET_ENV
    _shutdown_event = asyncio.Event()
    _SNIPPET_ENV = _parse_snippet_env(JUDGE_SNIPPET)

    judges = _resolve_judges(args.judge)
    judge_models = [m for m, _, _, _ in judges]

    # Extract screenshots
    tar_path = Path(args.screenshots_tar).absolute()
    if not tar_path.is_file():
        log.error("[error] SCREENSHOTS_TAR does not exist: %s", tar_path)
        return 1
    cache_dir = Path(args.cache_dir).absolute() if args.cache_dir else SCREENSHOTS_CACHE_DEFAULT
    screenshots_root = _extract_screenshots(tar_path, cache_dir, log)
    log.info("[pipeline] screenshots_root: %s", screenshots_root)

    # manifest
    manifest_arg = Path(args.manifest).absolute() if args.manifest else None
    manifest_path, manifest_entries = _load_manifest(manifest_arg, screenshots_root)
    log.info("[pipeline] manifest: %s (%d entries)", manifest_path, len(manifest_entries))

    # Pair deck x judge, filter out already-existing ones
    todo: list[tuple[dict, Path, tuple[str, str, str, str]]] = []
    total_jobs = 0
    skip = 0
    for entry in manifest_entries:
        pptx_stem = entry["pptx_name"].rsplit(".pptx", 1)[0]
        slide_dir = screenshots_root / pptx_stem
        if not slide_dir.is_dir():
            log.warning("[pipeline] slide_dir not found: %s, skipping", slide_dir)
            continue
        if not any(slide_dir.glob("slide_*.png")):
            log.warning("[pipeline] no slide_*.png in %s directory, skipping", pptx_stem)
            continue
        for j in judges:
            total_jobs += 1
            if _result_path(entry, j[0], output_dir).is_file():
                skip += 1
                continue
            todo.append((entry, slide_dir, j))

    todo_n = len(todo)
    log.info("[pipeline] %d jobs (decks × judges) | skip %d | todo %d | parallel %d | judges: %s",
             total_jobs, skip, todo_n, args.parallel, " ".join(judge_models))

    if todo_n > 0:
        _progress_total = todo_n
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, log)

        sem = asyncio.Semaphore(args.parallel * max(1, len(judges)))
        async def _bounded(entry: dict, slide_dir: Path, j: tuple[str, str, str, str]) -> None:
            async with sem:
                if _shutdown_event and _shutdown_event.is_set():
                    return
                await _run_one_job(entry, slide_dir, j, args.workers_per_deck,
                                   output_dir, log)

        try:
            async with asyncio.TaskGroup() as tg:
                for entry, slide_dir, j in todo:
                    tg.create_task(_bounded(entry, slide_dir, j))
        except* asyncio.CancelledError:
            log.warning(">>> TaskGroup cancelled")
        except* Exception as eg:
            for exc in eg.exceptions:
                log.error(">>> TaskGroup exception: %s: %s", type(exc).__name__, exc)
    else:
        log.info("[pipeline] all done, nothing to do.")

    # Aggregate JSONL (fixed filename, reruns overwrite directly)
    jsonl_path = output_dir / "pipeline_ppt.jsonl"
    log.info("")
    log.info("[pipeline] merging results → %s", jsonl_path)
    count = 0
    with open(jsonl_path, "w", encoding="utf-8") as out:
        for jf in sorted(output_dir.rglob("judge_result_*.json")):
            try:
                with open(jf) as f:
                    d = json.load(f)
                out.write(json.dumps(d, ensure_ascii=False) + "\n")
                count += 1
            except Exception as e:
                log.warning("[pipeline] skipping corrupt result: %s (%s)", jf, e)
    log.info("[pipeline] done: %d/%d results in %s", count, total_jobs, jsonl_path)

    return 1 if _fail_count else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Full PPT eval pipeline (VLM Judge, batch parallel, Ctrl+C safe cleanup)",
    )
    p.add_argument("screenshots_tar",
                   help="screenshot tar(.gz) archive (stage1_pack + manual Mac screenshot output)")
    p.add_argument("manifest", nargs="?", default=None,
                   help="path to manifest.jsonl (defaults to looking in the tar extraction dir)")
    p.add_argument("parallel", nargs="?", type=int, default=3,
                   help="how many decks to evaluate concurrently (default 3)")
    p.add_argument("workers_per_deck", nargs="?", type=int, default=3,
                   help="VLM API concurrency within each deck (default 3)")
    p.add_argument("--judge", action="append", default=[],
                   metavar="MODEL|PROVIDER|BASE_URL|API_KEY",
                   help=("Judge spec, can be specified multiple times; any field can use @VAR_NAME "
                         "to reference ENV in agent_configs/snippets/judge.snippet. "
                         f"Default: {DEFAULT_JUDGE}"))
    p.add_argument("--run-id", default="eval_result",
                   help="output subdirectory name (default eval_result; reusing means incremental eval)")
    p.add_argument("--cache-dir", default=None,
                   help=f"screenshot extraction cache dir (default {SCREENSHOTS_CACHE_DEFAULT})")
    args = p.parse_args(argv)

    run_id = args.run_id
    os.environ["RUN_ID"] = run_id
    output_dir = PROJECT_DIR / "output" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    log_file = output_dir / "pipeline_ppt.log"
    log = _setup_logging(log_file)
    log.info("[pipeline] log file: %s", log_file)

    try:
        return asyncio.run(_async_main(args, log, output_dir))
    except KeyboardInterrupt:
        log.warning(">>> KeyboardInterrupt")
        return 130


if __name__ == "__main__":
    sys.exit(main())
