#!/usr/bin/env python3
"""pipeline.py — Web Design end-to-end evaluation (Python version, replaces pipeline.sh).

Design reference: eval/scripts/pipeline.py (report-eval version, same signal/docker cleanup skeleton).

Improvements over the bash version:
- Single asyncio signal handler replaces nested traps
- start_new_session=True + os.killpg cleanly kills the entire stage1 process group
- Graceful cleanup on Ctrl+C: SIGINT(30s) → docker compose stragglers → SIGTERM(20s) → SIGKILL
- Pending tasks skipped early via _shutdown_event
- Sweep stage1 leftover docker compose containers/volumes (/tmp/webdesign_eval_task_* working_dir)
- No longer routed to the wrong REPO_ROOT via symlink/bind-mount (REPO_ROOT_OVERRIDE feeds the
  jobs_root-side tasks/ into stage1 / judge_single.sh)

CLI compatible with the bash version:
    python pipeline.py <JOBS_ROOT> [PARALLEL_N] [MAX_WORKERS]
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

SCRIPT_DIR = Path(__file__).absolute().parent
PROJECT_DIR = SCRIPT_DIR.parent  # eval/webdesign-eval/
REPO_ROOT = PROJECT_DIR.parent.parent          # OpenSkillEval/
STAGE1_SCRIPT = SCRIPT_DIR / "stage1_eval_agent.sh"
JUDGE_SNIPPET = REPO_ROOT / "agent_configs" / "snippets" / "judge.snippet"
DEFAULT_JUDGE = "claude-opus-4-6|anthropic|@ANTHROPIC_BASE_URL|@ANTHROPIC_API_KEY"
# stage1_eval_agent.sh creates working dirs via mktemp -d /tmp/webdesign_eval_task_XXXXXX.
STAGE1_TMP_PREFIX = "/tmp/webdesign_eval_task_"

TASK_INT_TIMEOUT = int(os.environ.get("TASK_INT_TIMEOUT", "30"))
TASK_TERM_TIMEOUT = int(os.environ.get("TASK_TERM_TIMEOUT", "20"))


# ---------------------------------------------------------------------------
# Subprocess env whitelist
# ---------------------------------------------------------------------------
# Only the keys below are forwarded from the operator's shell into the
# stage1 / judge subprocess. Credentials must come from
# agent_configs/snippets/judge.snippet — never from the surrounding shell.
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
    logger = logging.getLogger("pipeline_webdesign")
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
# Docker compose sweep cleanup
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
        log.warning(">>> force removing %d stage1 leftover containers", len(container_ids))
        await _docker_run(["rm", "-f", *container_ids], timeout=20)

    for proj in sorted(project_names):
        vol_ids = await _docker_capture(
            ["volume", "ls", "--filter", f"label=com.docker.compose.project={proj}", "-q"],
            timeout=10,
        )
        if vol_ids:
            log.warning(">>> removing stage1 leftover volume: %s", proj)
            await _docker_run(["volume", "rm", "-f", *vol_ids], timeout=15)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _install_signal_handlers(loop: asyncio.AbstractEventLoop, log: logging.Logger) -> None:
    def _on_signal(sig: int) -> None:
        global _force_kill
        if _force_kill:
            log.warning(">>> received signal %s again, immediately SIGKILL all stage1 process groups", sig)
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

    if procs:
        log.warning(">>> Phase 1: SIGINT to %d stage1 process groups (wait %ds)",
                    len(procs), TASK_INT_TIMEOUT)
        for p in procs:
            _killpg(p, signal.SIGINT)
        survivors = await _wait_procs(procs, TASK_INT_TIMEOUT)
    else:
        survivors = []

    log.warning(">>> Phase 2: sweeping stage1 docker compose resources")
    await _docker_cleanup_stage1(log)

    if not survivors:
        return

    log.warning(">>> Phase 3: SIGTERM to %d process groups (wait %ds)",
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
# Judge list (passed via CLI --judge, defaults to single Opus)
# ---------------------------------------------------------------------------


def _resolve_judges(specs: list[str]) -> tuple[list[str], list[str]]:
    """Return (full_specs, model_names). Each spec = MODEL|PROVIDER|BASE_URL|API_KEY."""
    judges = specs or [DEFAULT_JUDGE]
    models: list[str] = []
    for s in judges:
        parts = s.split("|")
        if len(parts) != 4:
            raise SystemExit(
                f"--judge format must be 'MODEL|PROVIDER|BASE_URL|API_KEY', got: {s!r}"
            )
        models.append(parts[0])
    return judges, models


# ---------------------------------------------------------------------------
# REPO_ROOT inference and site discovery
# ---------------------------------------------------------------------------

def _find_repo_root_for(jobs_root: Path) -> Path | None:
    p = jobs_root
    while True:
        if (p / "tasks").is_dir():
            return p
        if p.parent == p:
            return None
        p = p.parent


def _discover_sites(jobs_root: Path) -> list[Path]:
    """Equivalent to bash: find <root> -path '*/artifacts/output/index.html' -type f.
    Returns each site's artifacts dir (two levels up from .../artifacts/output/index.html).
    """
    sites: list[Path] = []
    for dirpath, _dirs, filenames in os.walk(str(jobs_root), followlinks=True):
        if "index.html" not in filenames:
            continue
        p = Path(dirpath)
        # must be .../artifacts/output/
        if p.name != "output":
            continue
        if p.parent.name != "artifacts":
            continue
        sites.append(p.parent)  # artifacts dir
    sites.sort()
    return sites


def _is_done(artifacts_dir: Path, output_dir: Path, judge_models: list[str]) -> bool:
    """All judge_result_*.json generated + result.json (trial-level) has no error → skip."""
    parts = artifacts_dir.parts  # ends with 'artifacts'
    if len(parts) < 8:
        return False
    tf = parts[-8]; ca = parts[-7]; va = parts[-6]
    mo = parts[-5]; ru = parts[-4]; rn = parts[-3]; tr = parts[-2]

    # bash: _run_dir="$(dirname "$artifacts_dir")" → trial dir
    trial_dir = artifacts_dir.parent
    result_json = trial_dir / "result.json"
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

def _short_id(artifacts_dir: Path) -> str:
    parts = artifacts_dir.parts
    if len(parts) < 8:
        return str(artifacts_dir)
    # case/mode/runner/run/trial (drops variant, aligns with bash _short)
    return f"{parts[-7]}/{parts[-5]}/{parts[-4]}/{parts[-3]}/{parts[-2]}"


def _meta_from_artifacts(artifacts_dir: Path) -> tuple[str, str, str, str, str, str, str]:
    parts = artifacts_dir.parts
    return (parts[-8], parts[-7], parts[-6], parts[-5], parts[-4], parts[-3], parts[-2])


def _summarize_judge(jf: Path) -> str:
    try:
        with open(jf) as f:
            d = json.load(f)
    except Exception:
        return ""
    parts = []
    # VLM-scored 1-5 dimensions
    for k in ("visual_design", "responsiveness"):
        v = d.get(k, {})
        s = v.get("score", 0) if isinstance(v, dict) else 0
        parts.append(f"{k[:4]}={s}")
    # code-checked pass_rate (0-1, from stage1 eval_report.json)
    for k in ("navigation", "interactions", "data_display"):
        v = d.get(k, {})
        if not isinstance(v, dict):
            continue
        r = v.get("pass_rate")
        if r is None:
            continue
        parts.append(f"{k[:3]}={r}")
    ov = d.get("overall", 0)
    return f"{d.get('judge_model','?')}: " + " ".join(parts) + f" ov={ov}"


async def _run_one_site(
    artifacts_dir: Path,
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

        short = _short_id(artifacts_dir)
        try:
            tf, ca, va, mo, ru, rn, tr = _meta_from_artifacts(artifacts_dir)
        except IndexError:
            log.error("[pipeline] path depth unexpected, skipping: %s", artifacts_dir)
            _fail_count += 1
            return
        result_dir = output_dir / tf / ca / va / mo / ru / rn / tr

        _progress_done += 1
        cnt = _progress_done
        log.info("[pipeline] [%d/%d] %s ...", cnt, _progress_total, short)

        start = time.monotonic()

        env = _inherit_env(_PIPELINE_EXTRA_ENV_KEYS)
        env["REPO_ROOT_OVERRIDE"] = str(repo_root)
        # snippet is the source of truth for judge credentials; the @VAR placeholders
        # in --judge spec get the real values fed in here via stage1's indirect expansion.
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

    os.environ["MAX_WORKERS"] = str(args.max_workers)

    judges, judge_models = _resolve_judges(args.judge)
    # stage1 picks up the judge list via JUDGES_SPEC env var (newline-joined)
    os.environ["JUDGES_SPEC"] = "\n".join(judges)
    # parameterize stage1 harbor config
    os.environ["EVAL_AGENT"] = args.eval_agent
    # When --eval-model is not given, inherit the model name from the first --judge spec
    os.environ["EVAL_MODEL"] = args.eval_model or judge_models[0]
    os.environ["EVAL_TIMEOUT_MULT"] = str(args.eval_timeout_mult)

    repo_root = _find_repo_root_for(jobs_root)
    if repo_root is None:
        log.error("[error] no ancestor dir containing tasks/ found upward from %s, stage1 cannot locate case data", jobs_root)
        return 1
    log.info("[pipeline] repo_root: %s", repo_root)

    sites = _discover_sites(jobs_root)
    total = len(sites)
    if total == 0:
        log.error("[error] no web-design artifacts found under %s (artifacts/output/index.html)", jobs_root)
        return 1

    todo: list[Path] = []
    skip = 0
    for s in sites:
        if _is_done(s, output_dir, judge_models):
            skip += 1
        else:
            todo.append(s)

    todo_n = len(todo)
    log.info("[pipeline] %d sites | skip %d | todo %d | parallel %d | judges: %s",
             total, skip, todo_n, args.parallel, " ".join(judge_models))

    if todo_n > 0:
        _progress_total = todo_n
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, log)

        sem = asyncio.Semaphore(args.parallel)
        try:
            async with asyncio.TaskGroup() as tg:
                for s in todo:
                    tg.create_task(_run_one_site(s, output_dir, log, sem, repo_root))
        except* asyncio.CancelledError:
            log.warning(">>> TaskGroup cancelled")
        except* Exception as eg:
            for exc in eg.exceptions:
                log.error(">>> TaskGroup exception: %s: %s", type(exc).__name__, exc)
    else:
        log.info("[pipeline] all done, nothing to do.")

    # ── Merge all judge_result_*.json → JSONL ──────────────────
    jsonl_path = output_dir / "pipeline_webdesign.jsonl"
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
                log.warning("[pipeline] skipping corrupted judge_result: %s (%s)", jf, e)
    log.info("[pipeline] done: %d/%d results in %s", count, total, jsonl_path)

    return 1 if _fail_count else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Web Design end-to-end evaluation (Eval Agent + VLM Judge, batch parallel, Ctrl+C safely reaps docker)",
    )
    p.add_argument("jobs_root", help="harbor jobs root dir (e.g. harbor/smoke_jobs)")
    p.add_argument("parallel", nargs="?", type=int, default=3,
                   help="number of sites to run end-to-end concurrently (default 3)")
    p.add_argument("max_workers", nargs="?", type=int, default=3,
                   help="VLM API concurrency inside each Judge (default 3)")
    p.add_argument("--judge", action="append", default=[],
                   metavar="MODEL|PROVIDER|BASE_URL|API_KEY",
                   help=("Judge spec, repeatable; any field may use @VAR_NAME to reference "
                         "an ENV from agent_configs/snippets/judge.snippet. "
                         f"Default: {DEFAULT_JUDGE}"))
    p.add_argument("--run-id", default="eval_result",
                   help="output subdirectory name (default eval_result, reuse for incremental eval)")
    p.add_argument("--eval-agent", default="claude-code",
                   help="agent that runs stage1 code checks (default claude-code)")
    p.add_argument("--eval-model", default=None,
                   help="model used by stage1 eval agent (defaults to the model from the first --judge spec)")
    p.add_argument("--eval-timeout-mult", type=float, default=5.0,
                   help="stage1 harbor --timeout-multiplier (default 5.0)")
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
