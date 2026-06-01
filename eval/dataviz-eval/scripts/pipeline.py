#!/usr/bin/env python3
"""pipeline.py — DataViz end-to-end eval (Python version, replaces pipeline.sh).

Design reference: eval/scripts/pipeline.py (report-eval version, same signal/docker cleanup skeleton).

Improvements over the bash version:
- Single asyncio signal handler replaces nested trap
- start_new_session=True + os.killpg cleanly kills the whole stage1 process group
- Graceful cleanup on Ctrl+C: SIGINT(30s) → docker compose residual fallback → SIGTERM(20s) → SIGKILL
- Pending tasks skipped early via _shutdown_event
- Fallback cleanup of stage1 residual docker compose containers/volumes (/tmp/dataviz_eval_task_* working_dir)
- No longer routed to the wrong REPO_ROOT by symlink/bind-mount (via REPO_ROOT_OVERRIDE pass
  the tasks/ on the jobs_root side into stage1 / judge_dataviz.sh)

CLI compatible with bash version:
    python pipeline.py <JOBS_ROOT> [PARALLEL_VIZ] [WORKERS_PER_MODEL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

# Why .absolute() not .resolve(): this repo may be a symlink/bind-mount of another real path.
# See same-named note in eval/scripts/pipeline.py.
SCRIPT_DIR = Path(__file__).absolute().parent
PROJECT_DIR = SCRIPT_DIR.parent  # eval/data_viz/
REPO_ROOT = PROJECT_DIR.parent.parent  # OpenSkillEval/
STAGE1_SCRIPT = SCRIPT_DIR / "stage1_eval_agent.sh"
JUDGE_SNIPPET = REPO_ROOT / "agent_configs" / "snippets" / "judge.snippet"
DEFAULT_JUDGE = "claude-opus-4-6|anthropic|@ANTHROPIC_BASE_URL|@ANTHROPIC_API_KEY"

# stage1_eval_agent.sh uses mktemp -d /tmp/dataviz_eval_task_XXXXXX to build the working dir.
STAGE1_TMP_PREFIX = "/tmp/dataviz_eval_task_"

# Only look at result.png under the data-visualization task family (matches bash's -path '*/data-visualization/*')
VIZ_FILENAMES = ("result.png",)
TASK_FAMILY_FILTER = "data-visualization"

TASK_INT_TIMEOUT = int(os.environ.get("TASK_INT_TIMEOUT", "30"))
TASK_TERM_TIMEOUT = int(os.environ.get("TASK_TERM_TIMEOUT", "20"))


# ---------------------------------------------------------------------------
# Subprocess env whitelist
# ---------------------------------------------------------------------------
# Only the keys below are forwarded from the operator's shell into the
# stage1 / judge subprocess. Credentials must come from agent_configs/snippets/
# judge.snippet — never from the surrounding shell.
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
# Pipeline-specific keys that main() writes to os.environ to communicate with
# the stage1 subprocess (see e.g. os.environ["RUN_ID"] = run_id in _async_main).
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
# Global state (asyncio single-threaded, no locks needed)
# ---------------------------------------------------------------------------

_shutdown_event: asyncio.Event | None = None
_force_kill: bool = False
_live_procs: set[asyncio.subprocess.Process] = set()
_progress_done: int = 0
_progress_total: int = 0
_skip_count: int = 0
_fail_count: int = 0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pipeline_dataviz")
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
# Docker compose fallback cleanup
# ---------------------------------------------------------------------------

async def _docker_capture(args: list[str], timeout: float = 10.0) -> list[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return []
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return []
    return [line for line in stdout.decode(errors="replace").splitlines() if line.strip()]


async def _docker_run(args: list[str], timeout: float = 15.0) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()


async def _docker_cleanup_stage1(log: logging.Logger) -> None:
    """Remove all compose containers + associated volumes whose working_dir is under /tmp/dataviz_eval_task_*."""
    rows = await _docker_capture(
        ["ps", "-a", "--no-trunc",
         "--format", '{{.ID}}\t{{.Label "com.docker.compose.project.working_dir"}}\t{{.Label "com.docker.compose.project"}}'],
        timeout=10,
    )
    container_ids: list[str] = []
    project_names: set[str] = set()
    for row in rows:
        parts = row.split("\t")
        if len(parts) < 3:
            continue
        cid, work_dir, proj = parts[0], parts[1], parts[2]
        if work_dir.startswith(STAGE1_TMP_PREFIX):
            container_ids.append(cid)
            if proj:
                project_names.add(proj)

    if container_ids:
        log.warning(">>> Force-removing %d stage1 residual containers", len(container_ids))
        await _docker_run(["rm", "-f", *container_ids], timeout=20)

    for proj in sorted(project_names):
        vol_ids = await _docker_capture(
            ["volume", "ls", "--filter", f"label=com.docker.compose.project={proj}", "-q"],
            timeout=10,
        )
        if vol_ids:
            log.warning(">>> Removing stage1 residual volume: %s", proj)
            await _docker_run(["volume", "rm", "-f", *vol_ids], timeout=15)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _install_signal_handlers(loop: asyncio.AbstractEventLoop, log: logging.Logger) -> None:
    def _on_signal(sig: int) -> None:
        global _force_kill
        if _force_kill:
            log.warning(">>> Received signal %s again, immediately SIGKILL all stage1 process groups", sig)
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

    if procs:
        log.warning(">>> Phase 1: SIGINT to %d stage1 process groups (waiting %ds)",
                    len(procs), TASK_INT_TIMEOUT)
        for p in procs:
            _killpg(p, signal.SIGINT)
        survivors = await _wait_procs(procs, TASK_INT_TIMEOUT)
    else:
        survivors = []

    log.warning(">>> Phase 2: Fallback cleanup of stage1 docker compose resources")
    await _docker_cleanup_stage1(log)

    if not survivors:
        return

    log.warning(">>> Phase 3: SIGTERM to %d process groups (waiting %ds)",
                len(survivors), TASK_TERM_TIMEOUT)
    for p in survivors:
        _killpg(p, signal.SIGTERM)
    survivors = await _wait_procs(survivors, TASK_TERM_TIMEOUT)
    if not survivors:
        return

    log.warning(">>> Phase 4: SIGKILL fallback for %d process groups", len(survivors))
    for p in survivors:
        _killpg(p, signal.SIGKILL)
    await _wait_procs(survivors, 5)


# ---------------------------------------------------------------------------
# snippet ENV parsing (same as tools/runner/run_variants.py)
#
# judge.snippet looks like:
#   ENV ANTHROPIC_API_KEY="sk-ant-..." \
#       ANOTHER_KEY="..."
# Parsed into {"ANTHROPIC_API_KEY": "sk-ant-...", "ANOTHER_KEY": "..."},
# merged into env by _run_one_viz before spawning the stage1 subprocess; stage1 then does @VAR indirect expansion internally.
# ---------------------------------------------------------------------------

def _parse_snippet_env(snippet_path: Path) -> dict[str, str]:
    if not snippet_path.is_file():
        return {}
    import shlex
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


# ---------------------------------------------------------------------------
# Judge list (passed via CLI --judge, defaults to a single Opus)
# ---------------------------------------------------------------------------


def _resolve_judges(specs: list[str]) -> tuple[list[str], list[str]]:
    """Return (full_specs, model_names). Each spec = MODEL|PROVIDER|BASE_URL|API_KEY."""
    judges = specs or [DEFAULT_JUDGE]
    models: list[str] = []
    for s in judges:
        parts = s.split("|")
        if len(parts) != 4:
            raise SystemExit(
                f"--judge format should be 'MODEL|PROVIDER|BASE_URL|API_KEY', got: {s!r}"
            )
        models.append(parts[0])
    return judges, models


# ---------------------------------------------------------------------------
# REPO_ROOT inference and viz discovery
# ---------------------------------------------------------------------------

def _find_repo_root_for(jobs_root: Path) -> Path | None:
    """Walk up from jobs_root to find the ancestor containing a tasks/ subdir = the REPO_ROOT stage1 should use."""
    p = jobs_root
    while True:
        if (p / "tasks").is_dir():
            return p
        if p.parent == p:
            return None
        p = p.parent


def _discover_viz(jobs_root: Path) -> list[Path]:
    """Equivalent of bash: find <root> -path '*/data-visualization/*' -name result.png -type f"""
    pattern = f"/{TASK_FAMILY_FILTER}/"
    found: list[Path] = []
    for dirpath, _dirs, filenames in os.walk(str(jobs_root), followlinks=True):
        # path must fall under the data-visualization task family
        if pattern not in dirpath + "/":
            continue
        for fn in filenames:
            if fn in VIZ_FILENAMES:
                found.append(Path(dirpath) / fn)
    found.sort()
    return found


def _is_done(viz: Path, output_dir: Path, judge_models: list[str]) -> bool:
    """All judge_result_*.json generated + run-level result.json has no error → skip.

    Note: unlike report-eval, the bash dataviz version's result.json check is at the run level
    (3 dirnames), not trial level. Replicated here verbatim.
    """
    parts = viz.parts
    if len(parts) < 9:
        return False
    tf = parts[-9]; ca = parts[-8]; va = parts[-7]
    mo = parts[-6]; ru = parts[-5]; rn = parts[-4]; tr = parts[-3]

    # 3 dirnames: result.png → artifacts → trial → run
    run_dir = viz.parent.parent.parent
    result_json = run_dir / "result.json"
    if not result_json.is_file():
        return False
    base = output_dir / tf / ca / va / mo / ru / rn / tr
    for jm in judge_models:
        safe = re.sub(r"[ /:]", "-", jm)
        if not (base / f"judge_result_{safe}.json").is_file():
            return False
    return True


# ---------------------------------------------------------------------------
# Stage1 execution
# ---------------------------------------------------------------------------

def _short_id(viz: Path) -> str:
    """case/mode/runner/run/trial (drops variant, matches bash _short)."""
    parts = viz.parts
    if len(parts) < 9:
        return str(viz)
    return f"{parts[-8]}/{parts[-6]}/{parts[-5]}/{parts[-4]}/{parts[-3]}"


def _meta_from_viz(viz: Path) -> tuple[str, str, str, str, str, str, str]:
    parts = viz.parts
    return (parts[-9], parts[-8], parts[-7], parts[-6], parts[-5], parts[-4], parts[-3])


def _summarize_judge(jf: Path) -> str:
    try:
        with open(jf) as f:
            d = json.load(f)
    except Exception:
        return ""
    parts = []
    for k in ("insight_expression", "data_accuracy", "visual_quality", "completeness"):
        v = d.get(k, {})
        s = v.get("score", 0) if isinstance(v, dict) else 0
        parts.append(f"{k[:4]}={s}")
    ov = d.get("overall", 0)
    return f"{d.get('judge_model','?')}: " + " ".join(parts) + f" ov={ov}"


async def _run_one_viz(
    viz: Path,
    output_dir: Path,
    log: logging.Logger,
    sem: asyncio.Semaphore,
    repo_root: Path,
) -> None:
    global _progress_done, _skip_count, _fail_count

    async with sem:
        if _shutdown_event and _shutdown_event.is_set():
            _skip_count += 1
            return

        short = _short_id(viz)
        try:
            tf, ca, va, mo, ru, rn, tr = _meta_from_viz(viz)
        except IndexError:
            log.error("[pipeline] Path depth does not match expectation, skipping: %s", viz)
            _fail_count += 1
            return
        result_dir = output_dir / tf / ca / va / mo / ru / rn / tr
        result_dir.mkdir(parents=True, exist_ok=True)

        artifacts_dir = viz.parent

        _progress_done += 1
        cnt = _progress_done
        log.info("[pipeline] [%d/%d] %s ...", cnt, _progress_total, short)

        start = time.monotonic()

        env = _inherit_env(_PIPELINE_EXTRA_ENV_KEYS)
        env["REPO_ROOT_OVERRIDE"] = str(repo_root)
        # snippet is the source of truth for judge credentials; the 4th segment of the --judge spec uses @VAR placeholders,
        # which stage1_eval_agent.sh indirectly expands to grab the real value injected here.
        env.update(_parse_snippet_env(JUDGE_SNIPPET))
        proc = await asyncio.create_subprocess_exec(
            "bash", str(STAGE1_SCRIPT), str(artifacts_dir),
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
                if "ERROR" in upper or "FAILED" in upper:
                    err_lines.append(text)
            await proc.wait()
            rc = proc.returncode if proc.returncode is not None else -1
        finally:
            _live_procs.discard(proc)

        elapsed = int(time.monotonic() - start)
        m, s = divmod(elapsed, 60)

        if rc == 0:
            log.info("[pipeline] [%d/%d] %s -> OK  %dm%02ds", cnt, _progress_total, short, m, s)
            for jf in sorted(result_dir.glob("judge_result_*.json")):
                summary = _summarize_judge(jf)
                if summary:
                    log.info("           %s", summary)
        else:
            _fail_count += 1
            if _shutdown_event and _shutdown_event.is_set():
                log.warning("[pipeline] [%d/%d] %s -> ABORTED  %dm%02ds (rc=%d)",
                            cnt, _progress_total, short, m, s, rc)
            else:
                log.error("[pipeline] [%d/%d] %s -> FAIL  %dm%02ds (rc=%d)",
                          cnt, _progress_total, short, m, s, rc)
                for ln in err_lines[:3]:
                    log.error("           %s", ln)
                stage1_log = result_dir / "stage1.log"
                if stage1_log.exists():
                    log.error("           see %s", stage1_log)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def _async_main(
    args: argparse.Namespace,
    log: logging.Logger,
    output_dir: Path,
    jobs_root: Path,
    jobs_basename: str,
) -> int:
    global _shutdown_event, _progress_total
    _shutdown_event = asyncio.Event()

    # Forward to stage1 → judge_dataviz.sh (corresponds to bash's ${MAX_WORKERS:-4})
    os.environ["MAX_WORKERS"] = str(args.workers_per_model)

    judges, judge_models = _resolve_judges(args.judge)
    # stage1 picks up the judge list via JUDGES_SPEC env var (newline-joined)
    os.environ["JUDGES_SPEC"] = "\n".join(judges)
    # stage1 harbor config parameterized (passed via env, shell side uses ${VAR:-default})
    os.environ["EVAL_AGENT"] = args.eval_agent
    # When --eval-model is not passed, inherit the model name from the first segment of --judge (step 1 and step 2 use the same model by default)
    os.environ["EVAL_MODEL"] = args.eval_model or judge_models[0]
    os.environ["EVAL_TIMEOUT_MULT"] = str(args.eval_timeout_mult)

    repo_root = _find_repo_root_for(jobs_root)
    if repo_root is None:
        log.error("[error] No ancestor containing tasks/ found above %s, stage1 cannot locate case data", jobs_root)
        return 1
    log.info("[pipeline] repo_root: %s", repo_root)

    viz_paths = _discover_viz(jobs_root)
    total = len(viz_paths)
    if total == 0:
        log.error("[error] No data-visualization %s found under %s",
                  " / ".join(VIZ_FILENAMES), jobs_root)
        return 1

    todo: list[Path] = []
    skip = 0
    for v in viz_paths:
        if _is_done(v, output_dir, judge_models):
            skip += 1
        else:
            todo.append(v)

    todo_n = len(todo)
    log.info("[pipeline] %d viz | skip %d | todo %d | parallel %d | judges: %s",
             total, skip, todo_n, args.parallel, " ".join(judge_models))

    if todo_n > 0:
        _progress_total = todo_n
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, log)

        sem = asyncio.Semaphore(args.parallel)
        try:
            async with asyncio.TaskGroup() as tg:
                for v in todo:
                    tg.create_task(_run_one_viz(v, output_dir, log, sem, repo_root))
        except* asyncio.CancelledError:
            log.warning(">>> TaskGroup cancelled")
        except* Exception as eg:
            for exc in eg.exceptions:
                log.error(">>> TaskGroup exception: %s: %s", type(exc).__name__, exc)
    else:
        log.info("[pipeline] all done, nothing to do.")

    # ── Aggregate all judge_result_*.json → JSONL ───────────────────
    jsonl_path = output_dir / "pipeline_dataviz.jsonl"
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
                log.warning("[pipeline] Skipping corrupt judge_result: %s (%s)", jf, e)
    log.info("[pipeline] done: %d/%d results in %s", count, total, jsonl_path)

    return 1 if _fail_count else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pipeline.py",
        description="DataViz end-to-end eval (VLM Judge, parallel batch, Ctrl+C safely reclaims docker)",
    )
    p.add_argument("jobs_root", help="harbor jobs root dir (e.g. harbor/smoke_jobs)")
    p.add_argument("parallel", nargs="?", type=int, default=3,
                   help="how many viz to eval concurrently (default 3)")
    p.add_argument("workers_per_model", nargs="?", type=int, default=4,
                   help="VLM API concurrency per model (default 4)")
    p.add_argument("--judge", action="append", default=[],
                   metavar="MODEL|PROVIDER|BASE_URL|API_KEY",
                   help=("Judge spec, may be specified multiple times; API_KEY uses @VAR_NAME to reference "
                         "ENV in agent_configs/snippets/judge.snippet. "
                         f"Default: {DEFAULT_JUDGE}"))
    p.add_argument("--run-id", default="eval_result",
                   help="output subdir name (default eval_result; reuse = incremental eval)")
    p.add_argument("--eval-agent", default="claude-code",
                   help="agent stage1 uses to run data checking (default claude-code)")
    p.add_argument("--eval-model", default=None,
                   help="model for stage1 eval agent (default inherits the model from the first segment of --judge)")
    p.add_argument("--eval-timeout-mult", type=float, default=3.0,
                   help="stage1 harbor --timeout-multiplier (default 3.0)")
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
