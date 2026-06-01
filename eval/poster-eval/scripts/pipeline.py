#!/usr/bin/env python3
"""pipeline.py — Poster end-to-end evaluation (Python version, replaces pipeline.sh).

Structure aligned with eval/dataviz-eval/scripts/pipeline.py:
- asyncio.Semaphore for concurrency, no xargs
- start_new_session=True + os.killpg for clean child process group kills
- SIGINT graceful cleanup: SIGINT(30s) -> SIGTERM(20s) -> SIGKILL
- judge.snippet is the source of truth for credentials, @VAR placeholders expanded in Python
- Defaults to a single judge (claude-opus-4-6); use --judge multiple times for multiple judges

CLI:
    python pipeline.py <JOBS_ROOT> [PARALLEL] [WORKERS_PER_MODEL] \
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
import signal
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).absolute().parent
PROJECT_DIR = SCRIPT_DIR.parent              # eval/poster-eval/
REPO_ROOT = PROJECT_DIR.parent.parent        # OpenSkillEval/
JUDGE_SCRIPT = SCRIPT_DIR / "judge_poster.sh"
JUDGE_SNIPPET = REPO_ROOT / "agent_configs" / "snippets" / "judge.snippet"

# Within the same artifacts directory, PNG takes priority over PDF
POSTER_FILENAMES = ("final_poster.png", "final_poster.pdf")

DEFAULT_JUDGE = "claude-opus-4-6|anthropic|@ANTHROPIC_BASE_URL|@ANTHROPIC_API_KEY"

TASK_INT_TIMEOUT = int(os.environ.get("TASK_INT_TIMEOUT", "30"))
TASK_TERM_TIMEOUT = int(os.environ.get("TASK_TERM_TIMEOUT", "20"))


# ---------------------------------------------------------------------------
# Subprocess env whitelist
# ---------------------------------------------------------------------------
# Only the keys below are forwarded from the operator's shell into the judge
# subprocess. Judge credentials are passed via positional argv (resolved from
# judge.snippet @VAR_NAME placeholders); the shell should not leak its
# unrelated env into the subprocess.
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
# Global state (asyncio single-threaded, no locks)
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
    logger = logging.getLogger("pipeline_poster")
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
            log.warning(">>> Received signal %s again, SIGKILL all judge process groups immediately", sig)
            for p in list(_live_procs):
                _killpg(p, signal.SIGKILL)
            return
        _force_kill = True
        if _shutdown_event is not None:
            _shutdown_event.set()
        log.warning(">>> Received signal %s, starting graceful cleanup; press Ctrl+C again to exit immediately", sig)
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
# Snippet ENV parsing + @VAR expansion (same as tools/runner/run_variants.py)
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
    """@VAR_NAME -> look up value from snippet ENV / os.environ."""
    if not v.startswith("@"):
        return v
    name = v[1:]
    val = _SNIPPET_ENV.get(name) or os.environ.get(name, "")
    if not val:
        raise SystemExit(
            f"judge spec referenced @{name} but it is not defined in snippet/env "
            f"(check {JUDGE_SNIPPET})"
        )
    return val


# ---------------------------------------------------------------------------
# Judge list (CLI --judge, defaults to a single Opus)
# ---------------------------------------------------------------------------

def _resolve_judges(specs: list[str]) -> list[tuple[str, str, str, str]]:
    """Return list of (model, provider, base_url, api_key), with @VAR expanded."""
    judges = specs or [DEFAULT_JUDGE]
    out: list[tuple[str, str, str, str]] = []
    for s in judges:
        parts = s.split("|")
        if len(parts) != 4:
            raise SystemExit(
                f"--judge format should be 'MODEL|PROVIDER|BASE_URL|API_KEY', got: {s!r}"
            )
        model, provider, base_url, api_key = (_expand_at(p) for p in parts)
        out.append((model, provider, base_url, api_key))
    return out


# ---------------------------------------------------------------------------
# Poster discovery + dedup (PNG takes priority over PDF)
# ---------------------------------------------------------------------------

def _discover_posters(jobs_root: Path) -> list[Path]:
    """find $JOBS_ROOT -name final_poster.png/pdf; PNG takes priority within the same artifacts directory."""
    raw: list[Path] = []
    for dirpath, _dirs, filenames in os.walk(str(jobs_root), followlinks=True):
        for fn in filenames:
            if fn in POSTER_FILENAMES:
                raw.append(Path(dirpath) / fn)
    raw.sort()

    # Dedup by dirname, PNG takes priority
    seen: dict[Path, Path] = {}
    for p in raw:
        d = p.parent
        if d not in seen or p.name.endswith(".png"):
            seen[d] = p
    return sorted(seen.values())


def _safe_model(name: str) -> str:
    return re.sub(r"[ /:]", "-", name)


def _meta_from_poster(poster: Path) -> tuple[str, str, str, str, str, str, str]:
    """.../<tf>/<ca>/<va>/<mo>/<ru>/<rn>/<tr>/artifacts/final_poster.{png,pdf}
    Derived from the last 9 path segments."""
    parts = poster.parts
    return (parts[-9], parts[-8], parts[-7], parts[-6],
            parts[-5], parts[-4], parts[-3])


def _short_id(poster: Path) -> str:
    parts = poster.parts
    if len(parts) < 9:
        return str(poster)
    return f"{parts[-8]}/{parts[-6]}/{parts[-5]}/{parts[-4]}/{parts[-3]}"


def _is_done(poster: Path, output_dir: Path,
             judge_models: list[str]) -> bool:
    """All judge_result_*.json exist + run-level result.json has no error -> skip."""
    parts = poster.parts
    if len(parts) < 9:
        return False
    tf, ca, va, mo, ru, rn, tr = _meta_from_poster(poster)

    # Check whether the harbor job itself completed (result.json exists)
    run_dir = poster.parent.parent.parent
    if not (run_dir / "result.json").is_file():
        return False

    base = output_dir / tf / ca / va / mo / ru / rn / tr
    for jm in judge_models:
        if not (base / f"judge_result_{_safe_model(jm)}.json").is_file():
            return False
    return True


def _summarize_judge(jf: Path) -> str:
    try:
        with open(jf) as f:
            d = json.load(f)
    except Exception:
        return ""
    parts = []
    for k in ("visual_design", "content", "completeness"):
        v = d.get(k, {})
        s = v.get("score", 0) if isinstance(v, dict) else 0
        parts.append(f"{k[:4]}={s}")
    ov = d.get("overall", 0)
    return f"{d.get('judge_model','?')}: " + " ".join(parts) + f" ov={ov}"


# ---------------------------------------------------------------------------
# Single poster x single judge execution
# ---------------------------------------------------------------------------

async def _run_one_judge(
    poster: Path,
    judge: tuple[str, str, str, str],
    result_dir: Path,
    workers_per_model: int,
    log: logging.Logger,
    short: str,
) -> tuple[str, int]:
    """Return (model, returncode). 0 = success, !=0 = fail."""
    model, provider, base_url, api_key = judge
    result_file = result_dir / f"judge_result_{_safe_model(model)}.json"
    if result_file.is_file():
        return model, 0

    if _shutdown_event and _shutdown_event.is_set():
        return model, -1

    proc = await asyncio.create_subprocess_exec(
        "bash", str(JUDGE_SCRIPT),
        str(poster), str(result_file), model, str(workers_per_model),
        provider, base_url, api_key,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        env=_inherit_env(_PIPELINE_EXTRA_ENV_KEYS),
    )
    _live_procs.add(proc)
    try:
        assert proc.stdout is not None
        async for line in proc.stdout:
            text = line.decode(errors="replace").rstrip()
            if text:
                log.debug("[%s | %s] %s", short, model, text)
        await proc.wait()
        rc = proc.returncode if proc.returncode is not None else -1
    finally:
        _live_procs.discard(proc)
    return model, rc


async def _run_one_poster(
    poster: Path,
    judges: list[tuple[str, str, str, str]],
    output_dir: Path,
    workers_per_model: int,
    log: logging.Logger,
    sem: asyncio.Semaphore,
) -> None:
    global _progress_done, _fail_count
    async with sem:
        if _shutdown_event and _shutdown_event.is_set():
            return

        short = _short_id(poster)
        try:
            tf, ca, va, mo, ru, rn, tr = _meta_from_poster(poster)
        except IndexError:
            log.error("[pipeline] path depth does not match expectations, skipping: %s", poster)
            _fail_count += 1
            return

        result_dir = output_dir / tf / ca / va / mo / ru / rn / tr
        result_dir.mkdir(parents=True, exist_ok=True)

        _progress_done += 1
        cnt = _progress_done
        log.info("[pipeline] [%d/%d] %s ...", cnt, _progress_total, short)
        start = time.monotonic()

        # Run all judges concurrently
        results = await asyncio.gather(
            *[_run_one_judge(poster, j, result_dir, workers_per_model, log, short)
              for j in judges],
            return_exceptions=False,
        )
        elapsed = int(time.monotonic() - start)
        m, s = divmod(elapsed, 60)

        fails = [(name, rc) for name, rc in results if rc != 0]
        if not fails:
            log.info("[pipeline] [%d/%d] %s -> OK  %dm%02ds",
                     cnt, _progress_total, short, m, s)
            for jf in sorted(result_dir.glob("judge_result_*.json")):
                summary = _summarize_judge(jf)
                if summary:
                    log.info("           %s", summary)
        else:
            _fail_count += 1
            if _shutdown_event and _shutdown_event.is_set():
                log.warning("[pipeline] [%d/%d] %s -> ABORTED  %dm%02ds",
                            cnt, _progress_total, short, m, s)
            else:
                bad = ", ".join(f"{n}(rc={rc})" for n, rc in fails)
                log.error("[pipeline] [%d/%d] %s -> FAIL  %dm%02ds  %s",
                          cnt, _progress_total, short, m, s, bad)
                log.error("           see %s/", result_dir)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _async_main(
    args: argparse.Namespace,
    log: logging.Logger,
    output_dir: Path,
    jobs_root: Path,
    jobs_basename: str,
) -> int:
    global _shutdown_event, _progress_total, _SNIPPET_ENV
    _shutdown_event = asyncio.Event()
    _SNIPPET_ENV = _parse_snippet_env(JUDGE_SNIPPET)

    judges = _resolve_judges(args.judge)
    judge_models = [m for m, _, _, _ in judges]

    posters = _discover_posters(jobs_root)
    total = len(posters)
    if total == 0:
        log.error("[error] under %s, did not find %s", jobs_root, " / ".join(POSTER_FILENAMES))
        return 1

    todo: list[Path] = []
    skip = 0
    for p in posters:
        if _is_done(p, output_dir, judge_models):
            skip += 1
        else:
            todo.append(p)

    todo_n = len(todo)
    log.info("[pipeline] %d posters | skip %d | todo %d | parallel %d | judges: %s",
             total, skip, todo_n, args.parallel, " ".join(judge_models))

    if todo_n > 0:
        _progress_total = todo_n
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, log)

        sem = asyncio.Semaphore(args.parallel)
        try:
            async with asyncio.TaskGroup() as tg:
                for p in todo:
                    tg.create_task(_run_one_poster(
                        p, judges, output_dir, args.workers_per_model, log, sem
                    ))
        except* asyncio.CancelledError:
            log.warning(">>> TaskGroup cancelled")
        except* Exception as eg:
            for exc in eg.exceptions:
                log.error(">>> TaskGroup exception: %s: %s", type(exc).__name__, exc)
    else:
        log.info("[pipeline] all done, nothing to do.")

    # ── Aggregate ────────────────────────────────────────────
    jsonl_path = output_dir / "pipeline_poster.jsonl"
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
                log.warning("[pipeline] skipping corrupt judge_result: %s (%s)", jf, e)
    log.info("[pipeline] done: %d/%d results in %s", count, total, jsonl_path)

    return 1 if _fail_count else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Poster end-to-end evaluation (VLM Judge, batch parallel, safe Ctrl+C cleanup)",
    )
    p.add_argument("jobs_root", help="harbor jobs root directory (e.g. harbor/smoke_jobs)")
    p.add_argument("parallel", nargs="?", type=int, default=3,
                   help="number of posters evaluated concurrently (default 3)")
    p.add_argument("workers_per_model", nargs="?", type=int, default=4,
                   help="VLM API concurrency within each model (default 4)")
    p.add_argument("--judge", action="append", default=[],
                   metavar="MODEL|PROVIDER|BASE_URL|API_KEY",
                   help=("Judge spec, can be specified multiple times; any field can use @VAR_NAME "
                         "to reference ENV from agent_configs/snippets/judge.snippet. "
                         f"Default: {DEFAULT_JUDGE}"))
    p.add_argument("--run-id", default="eval_result",
                   help="output subdirectory name (default eval_result; reusing it enables incremental evaluation)")
    args = p.parse_args(argv)

    jobs_root = Path(args.jobs_root).absolute()
    if not jobs_root.is_dir():
        print(f"[error] JOBS_ROOT does not exist: {jobs_root}", file=sys.stderr)
        return 1

    run_id = args.run_id
    os.environ["RUN_ID"] = run_id
    output_dir = PROJECT_DIR / "output" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    jobs_basename = jobs_root.name
    log_file = output_dir / f"pipeline_{jobs_basename}.log"
    log = _setup_logging(log_file)
    log.info("[pipeline] log file: %s", log_file)

    try:
        return asyncio.run(_async_main(args, log, output_dir, jobs_root, jobs_basename))
    except KeyboardInterrupt:
        log.warning(">>> KeyboardInterrupt")
        return 130


if __name__ == "__main__":
    sys.exit(main())
