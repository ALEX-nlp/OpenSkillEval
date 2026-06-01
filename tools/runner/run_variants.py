#!/usr/bin/env python3
"""run_variants.py — Python batch variant runner.

See README.md in the same directory for usage.

CLI example:
    uv run python tools/runner/run_variants.py \\
      --runner "claude-code|claude-opus-4-6|10" \\
      --runner "codex|gpt-5.5|5" \\
      --parallel 10 --runs 3 --resume \\
      "data-visualization|data-viz-anthropics|case-ai-evolution-timeline|force-using"

Design notes:
- Single asyncio signal handler replaces three layers of nested traps
- start_new_session=True replaces setsid
- os.killpg replaces kill -SIG -PID
- asyncio single-threaded → progress counters and logs need no locks
- logging.LoggerAdapter replaces FIFO + sed -u
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Path anchor (this script lives at OpenSkillEval/tools/runner/run_variants.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# harbor CLI is installed in .venv via pip; no longer depends on a local harbor/ source dir
OUTPUT_ROOT = PROJECT_ROOT
TASKS_ROOT = PROJECT_ROOT / "tasks"
GLOBAL_SCRIPTS = TASKS_ROOT / "scripts"
LOG_DIR = OUTPUT_ROOT / "logs"
TMP_BASE_ROOT = PROJECT_ROOT / "tmp"

ARTIFACTS: dict[str, list[str]] = {
    "data-visualization": ["/app/output/result.png"],
    "ppt-generation": ["/app/output/final_deck.pptx"],
    "poster-generation": ["/app/output/final_poster.png", "/app/output/final_poster.pdf"],
    "report-generation": ["/app/output/final_report.html", "/app/output/final_report.pdf"],
    "web-design": ["/app/output"],
}

NO_SKILL_VARIANTS: frozenset[str] = frozenset({
    "data-viz-no-skills",
    "poster-generation-no-skills",
    "ppt-generation-no-skills",
    "report-generation-no-skills",
    "web-design-no-skills",
})

# Cleanup timeouts (seconds). Overridable via environment variables.
TASK_INT_TIMEOUT = int(os.environ.get("TASK_INT_TIMEOUT", "30"))
TASK_TERM_TIMEOUT = int(os.environ.get("TASK_TERM_TIMEOUT", "20"))


# ---------------------------------------------------------------------------
# Subprocess env whitelist
# ---------------------------------------------------------------------------
# Only the keys below are forwarded from the operator's shell into the harbor
# subprocess (and downstream into docker / containers). Credentials and
# provider routing must come from the agent snippet — never from the shell.
# Set OPENSKILLEVAL_EXTRA_ENV="KEY1,KEY2" to whitelist additional vars without
# editing source (useful for corp proxy / self-signed CA env names).
_INHERITED_ENV_KEYS: frozenset[str] = frozenset({
    # shell / OS basics (execve resolution, library ~ paths, TTY, tmp dir)
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR",
    # locale / timezone (avoid UnicodeEncodeError in subprocess, correct timestamps)
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ",
    # XDG dirs (rootless docker socket, some tools' config paths)
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    # Docker / Compose (only forwarded if user explicitly set them)
    "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
    "DOCKER_CONFIG", "DOCKER_BUILDKIT",
    "COMPOSE_DOCKER_CLI_BUILD", "COMPOSE_HTTP_TIMEOUT",
    # uv / Python venv
    "VIRTUAL_ENV", "UV_CACHE_DIR", "UV_PYTHON", "UV_HTTP_TIMEOUT",
    # corp network (HTTP proxy, self-signed CA bundles)
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # runner self-config (read above via os.environ.get)
    "TASK_INT_TIMEOUT", "TASK_TERM_TIMEOUT",
})


def _inherit_env(extra_keys: frozenset[str] = frozenset()) -> dict[str, str]:
    """Return only whitelisted entries from os.environ.

    `extra_keys` lets callers add file-specific keys (e.g. pipeline EVAL_*).
    `OPENSKILLEVAL_EXTRA_ENV` is parsed as a comma-separated escape hatch so
    operators can add corp-specific vars without editing source.
    """
    user_extra = frozenset(
        k.strip()
        for k in os.environ.get("OPENSKILLEVAL_EXTRA_ENV", "").split(",")
        if k.strip()
    )
    allowed = _INHERITED_ENV_KEYS | extra_keys | user_extra
    return {k: v for k, v in os.environ.items() if k in allowed}


# Custom-agent registry: agent CLI name → import path (used for --agent-import-path)
_CUSTOM_AGENTS: dict[str, str] = {
    "claude-code-minimax": "agents.claude_code_minimax:ClaudeCodeMinimax",
    "claude-code-ds": "agents.claude_code_ds:ClaudeCodeDS",
    "claude-code-glm": "agents.claude_code_glm:ClaudeCodeGLM",
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Runner:
    agent: str
    model: str
    parallel: int

    @property
    def safe_model(self) -> str:
        s = self.model
        for ch in (" ", "/", ":"):
            s = s.replace(ch, "-")
        return s

    @property
    def name(self) -> str:
        return f"{self.agent}-{self.safe_model}"

    @classmethod
    def parse(cls, spec: str, default_parallel: int) -> Runner:
        parts = spec.split("|")
        if len(parts) < 2:
            raise ValueError(f"invalid runner format (expected 'agent|model[|parallel]'): {spec!r}")
        agent, model = parts[0], parts[1]
        parallel = default_parallel
        if len(parts) >= 3 and parts[2].strip():
            try:
                parallel = int(parts[2])
            except ValueError:
                pass
        return cls(agent=agent, model=model, parallel=parallel)


@dataclass(frozen=True)
class VariantEntry:
    family: str
    variant: str
    case: str
    mode: str

    @classmethod
    def parse(cls, spec: str) -> VariantEntry:
        parts = spec.split("|")
        if len(parts) < 3:
            raise ValueError(f"invalid entry format (expected 'family|variant|case[|mode]'): {spec!r}")
        family, variant, case = parts[0], parts[1], parts[2]
        mode = parts[3] if len(parts) >= 4 and parts[3] else "force-using"
        return cls(family=family, variant=variant, case=case, mode=mode)

    def validate_no_skill(self) -> None:
        if self.mode == "no-skill" and self.variant not in NO_SKILL_VARIANTS:
            raise ValueError(
                f"mode=no-skill can only be paired with a *-no-skills variant, but {self.variant!r} is not"
            )
        if self.variant in NO_SKILL_VARIANTS and self.mode != "no-skill":
            raise ValueError(
                f"no-skills variant {self.variant!r} can only use mode=no-skill, got mode={self.mode!r}"
            )


@dataclass
class TaskItem:
    entry: VariantEntry
    runner: Runner
    run_id: str
    tmp_dir: Path
    jobs_dir: Path  # JOBS_BASE/family/case/variant/mode/runner.name

    @property
    def artifacts(self) -> list[str]:
        return ARTIFACTS.get(self.entry.family, [])

    @property
    def family_root(self) -> Path:
        return TASKS_ROOT / self.entry.family

    @property
    def variant_src(self) -> Path:
        return self.family_root / "variants" / self.entry.variant

    @property
    def tag(self) -> str:
        return f"{self.entry.variant}|{self.runner.name}"

    @property
    def run_jobs_dir(self) -> Path:
        return self.jobs_dir / self.run_id


# ---------------------------------------------------------------------------
# Global state (asyncio single-threaded, no locks needed)
# ---------------------------------------------------------------------------

_shutdown_event: asyncio.Event | None = None
_force_kill: bool = False
_live_procs: set[asyncio.subprocess.Process] = set()
# proc → the task's environment dir (used for docker label filter)
_live_task_dirs: dict[asyncio.subprocess.Process, Path] = {}

_progress_done: int = 0
_progress_total: int = 0
_skip_count: int = 0
_fail_records: list[str] = []


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("run_variants")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if logger.handlers:
        # Reentrant cleanup (test scenarios)
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


class TaggedAdapter(logging.LoggerAdapter):
    """Prepend a [tag] prefix to every message."""
    def process(self, msg, kwargs):
        return f"[{self.extra['tag']}] {msg}", kwargs


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _killpg(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Safely send a signal to the process group."""
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
        # Fall back to single PID when the PGID assumption breaks
        try:
            os.kill(pid, sig)
        except OSError:
            pass


async def _wait_procs(
    procs: list[asyncio.subprocess.Process], timeout: float
) -> list[asyncio.subprocess.Process]:
    """Wait for all processes to exit, up to `timeout` seconds. Returns the still-running ones."""
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


async def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path | None,
    logger: logging.LoggerAdapter,
    new_session: bool = False,
    env: dict[str, str] | None = None,
    track_dir: Path | None = None,
) -> int:
    """Start a subprocess, merge stdout/stderr, forward lines to logger. Returns returncode."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=new_session,
        env=env,
    )
    _live_procs.add(proc)
    if track_dir is not None:
        _live_task_dirs[proc] = track_dir
    try:
        assert proc.stdout is not None
        async for line in proc.stdout:
            text = line.decode(errors="replace").rstrip()
            if text:
                logger.info(text)
        await proc.wait()
        return proc.returncode if proc.returncode is not None else -1
    finally:
        _live_procs.discard(proc)
        _live_task_dirs.pop(proc, None)


# ---------------------------------------------------------------------------
# Docker fallback cleanup
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


async def _docker_cleanup_for_dir(project_dir: Path, log: logging.LoggerAdapter | logging.Logger) -> None:
    """Clean up one task's compose containers and volumes by working_dir label."""
    flt = f"label=com.docker.compose.project.working_dir={project_dir}"

    project_names = await _docker_capture(
        ["ps", "-a", "--filter", flt,
         "--format", '{{.Label "com.docker.compose.project"}}'],
        timeout=10,
    )
    container_ids = await _docker_capture(
        ["ps", "-a", "--filter", flt, "-q"],
        timeout=10,
    )

    if container_ids:
        log.warning("force-removing %d leftover compose containers: %s", len(container_ids), project_dir)
        await _docker_run(["rm", "-f", *container_ids], timeout=15)

    for proj in sorted({n for n in project_names if n}):
        vol_ids = await _docker_capture(
            ["volume", "ls", "--filter", f"label=com.docker.compose.project={proj}", "-q"],
            timeout=10,
        )
        if vol_ids:
            log.warning("removing compose volumes: %s", proj)
            await _docker_run(["volume", "rm", "-f", *vol_ids], timeout=15)

        img_ids = await _docker_capture(
            ["images", "--filter", f"label=com.docker.compose.project={proj}", "-q"],
            timeout=10,
        )
        if img_ids:
            log.warning("removing compose images: %s", proj)
            await _docker_run(["rmi", "-f", *img_ids], timeout=60)


async def _docker_cleanup_all(log: logging.Logger) -> None:
    """On shutdown, do a fallback docker cleanup for every live task."""
    items = list(_live_task_dirs.items())
    if not items:
        return
    log.warning(">>> force-cleaning compose resources for %d live tasks...", len(items))
    try:
        async with asyncio.TaskGroup() as tg:
            for _, d in items:
                tg.create_task(_docker_cleanup_for_dir(d, log))
    except* Exception as eg:
        for exc in eg.exceptions:
            log.warning("docker cleanup error: %s", exc)


def _image_name_prefix(tmp_dir_name: str) -> str:
    """Derive the Docker image-name prefix from a tmp_dir directory name.

    harbor builds trial_name = f"{task_name[:32].rstrip('_-')}__{ShortUUID(7)}",
    then _sanitize_docker_compose_project_name lowercases it and replaces illegal chars.
    Image name = f"{sanitized_project_name}-main".
    This function returns the prefix portion (up to __), used for matching with
    `docker images --filter reference=`.
    """
    prefix = tmp_dir_name[:32].rstrip("_-")
    prefix = prefix.lower()
    prefix = re.sub(r"[^a-z0-9_-]", "-", prefix)
    return f"{prefix}__"


# ---------------------------------------------------------------------------
# Signal handling (replaces three layers of traps)
# ---------------------------------------------------------------------------

def _install_signal_handlers(loop: asyncio.AbstractEventLoop, log: logging.Logger) -> None:
    def _on_signal(sig: int) -> None:
        global _force_kill
        if _force_kill:
            log.warning(">>> received signal %s again, immediately SIGKILLing all process groups and exiting", sig)
            for p in list(_live_procs):
                _killpg(p, signal.SIGKILL)
            # Can't use loop.stop() — we want graceful_cascade to finish for a clean exit.
            # Raising SystemExit inside a signal handler isn't viable on the second hit;
            # just flip the flag so cascade skips its waits.
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
            # Not supported on Windows or some event loops; ignore
            pass


async def _graceful_cascade(log: logging.Logger) -> None:
    """INT(30s) → docker cleanup → TERM(20s) → KILL cascade."""
    procs = list(_live_procs)
    if not procs:
        return

    # Phase 1: SIGINT to all process groups
    log.warning(">>> Phase 1: SIGINT to %d harbor process groups (waiting %ds)", len(procs), TASK_INT_TIMEOUT)
    for p in procs:
        _killpg(p, signal.SIGINT)
    survivors = await _wait_procs(procs, TASK_INT_TIMEOUT)
    if not survivors:
        return

    # Phase 2: forced docker cleanup
    log.warning(">>> Phase 2: %d harbor processes did not respond to INT, force-cleaning compose", len(survivors))
    await _docker_cleanup_all(log)

    # Phase 3: SIGTERM
    log.warning(">>> Phase 3: SIGTERM to %d process groups (waiting %ds)", len(survivors), TASK_TERM_TIMEOUT)
    for p in survivors:
        _killpg(p, signal.SIGTERM)
    survivors = await _wait_procs(survivors, TASK_TERM_TIMEOUT)
    if not survivors:
        return

    # Phase 4: SIGKILL
    log.warning(">>> Phase 4: SIGKILL fallback to %d process groups", len(survivors))
    for p in survivors:
        _killpg(p, signal.SIGKILL)
    await _wait_procs(survivors, 5)


# ---------------------------------------------------------------------------
# Resume scan
# ---------------------------------------------------------------------------

def _scan_done_dirs(jobs_base: Path) -> frozenset[Path]:
    """Walk jobs_base in parallel and collect every directory that contains result.json."""
    if not jobs_base.exists():
        return frozenset()

    # Expand two levels deep, then walk each subtree in parallel
    subtrees: list[Path] = []
    for d1 in jobs_base.iterdir():
        if not d1.is_dir():
            continue
        children = [c for c in d1.iterdir() if c.is_dir()]
        if children:
            subtrees.extend(children)
        else:
            subtrees.append(d1)

    if not subtrees:
        return frozenset()

    def _walk(root: Path) -> list[Path]:
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            if "result.json" in filenames:
                found.append(Path(dirpath))
                dirnames.clear()  # prune
        return found

    n_workers = min(len(subtrees), 64)
    results: list[Path] = []
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for batch in pool.map(_walk, subtrees):
            results.extend(batch)
    return frozenset(results)


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

def _parse_snippet_env(snippet_path: Path) -> dict[str, str]:
    """Read agent snippet, extract ENV var assignments into a flat dict.

    Supports:
      - single-line:  ENV KEY=VALUE
      - multi-line:   ENV KEY1=VAL1 \\\n    KEY2=VAL2
      - quoted vals:  ENV KEY="some value"
    """
    if not snippet_path.is_file():
        return {}
    raw = snippet_path.read_text()
    # Merge `\` line continuations
    joined = raw.replace("\\\n", " ")
    out: dict[str, str] = {}
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped.startswith("ENV "):
            continue
        body = stripped[4:]
        # body may be "KEY=VAL KEY=VAL ..." or "KEY=VAL"
        # Simple parse: shlex-split on whitespace, each token is KEY=VAL or KEY="VAL WITH SPACES"
        import shlex
        try:
            tokens = shlex.split(body)
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


def _agent_snippet_env(agent_name: str) -> dict[str, str]:
    snippet = PROJECT_ROOT / "agent_configs" / "snippets" / f"{agent_name}.snippet"
    return _parse_snippet_env(snippet)



def _build_harbor_cmd(task: TaskItem) -> list[str]:
    cmd = [
        "harbor", "run",
        "-p", str(task.tmp_dir),
        "--model", task.runner.model,
    ]
    if task.runner.agent in _CUSTOM_AGENTS:
        cmd += ["--agent-import-path", _CUSTOM_AGENTS[task.runner.agent]]
    else:
        cmd += ["--agent", task.runner.agent]
    cmd += [
        "--timeout-multiplier", "5.0",
        "--jobs-dir", str(task.jobs_dir),
        "--job-name", task.run_id,
        "--agent-kwarg", "max_turns=200",
    ]
    for ap in task.artifacts:
        cmd += ["--artifact", ap]
    return cmd


def _inc_progress() -> tuple[int, int]:
    global _progress_done
    _progress_done += 1
    return _progress_done, _progress_total


def _record_skip() -> tuple[int, int]:
    global _skip_count
    _skip_count += 1
    return _inc_progress()


async def _run_one_task(task: TaskItem, root_logger: logging.Logger) -> None:
    """A single (variant × runner × run_id) task."""
    log = TaggedAdapter(root_logger, {"tag": task.tag})

    if _shutdown_event and _shutdown_event.is_set():
        done, total = _record_skip()
        log.info(">>> SKIP (shutdown) %s [%d/%d]", task.run_id, done, total)
        return
    if not task.variant_src.is_dir():
        log.error(">>> FAILED / variant source dir not found: %s", task.variant_src)
        _fail_records.append(
            f"{task.entry.family} | {task.entry.variant} | {task.runner.agent} | "
            f"{task.runner.model} | variant_src_missing"
        )
        done, total = _inc_progress()
        log.info(">>> FAILED / %s [%d/%d]", task.run_id, done, total)
        return
    if task.tmp_dir.exists():
        shutil.rmtree(task.tmp_dir, ignore_errors=True)
    try:
        shutil.copytree(task.variant_src, task.tmp_dir, symlinks=True)
    except Exception as e:
        log.error(">>> FAILED / copytree: %s", e)
        _fail_records.append(
            f"{task.entry.family} | {task.entry.variant} | {task.runner.agent} | "
            f"{task.runner.model} | copytree"
        )
        done, total = _inc_progress()
        log.info(">>> FAILED / %s [%d/%d]", task.run_id, done, total)
        return

    try:
        log.info("------------------------------------------------------------")
        log.info("  %s / %s  |  %s (%s)  |  %s",
                 task.entry.family, task.entry.variant,
                 task.runner.agent, task.runner.model, task.run_id)
        log.info("  Jobs dir : %s", task.jobs_dir)
        log.info("  Tmp dir  : %s", task.tmp_dir)
        log.info("  Mode     : %s", task.entry.mode)
        log.info("------------------------------------------------------------")

        # 2) load case
        if task.entry.case:
            if _shutdown_event and _shutdown_event.is_set():
                done, total = _record_skip()
                log.info(">>> SKIP (shutdown) %s [%d/%d]", task.run_id, done, total)
                return
            rc = await _run_subprocess(
                ["bash", str(GLOBAL_SCRIPTS / "load_case_to_dir.sh"),
                 str(task.family_root), task.entry.case, task.entry.variant,
                 task.entry.mode, str(task.tmp_dir)],
                cwd=None, logger=log,
            )
            if rc != 0:
                log.error(">>> FAILED / load_case (rc=%d)", rc)
                _fail_records.append(
                    f"{task.entry.family} | {task.entry.variant} | {task.runner.agent} | "
                    f"{task.runner.model} | load_case"
                )
                done, total = _inc_progress()
                log.info(">>> %s / %s [%d/%d]", "FAILED", task.run_id, done, total)
                return

        # 3) Clear out any prior run_jobs_dir
        if task.run_jobs_dir.exists():
            log.info("removing stale dir: %s", task.run_jobs_dir)
            shutil.rmtree(task.run_jobs_dir, ignore_errors=True)

        # 4) harbor run (the only command that needs setsid)
        if _shutdown_event and _shutdown_event.is_set():
            done, total = _record_skip()
            log.info(">>> SKIP (shutdown) %s [%d/%d]", task.run_id, done, total)
            return
        run_start = time.monotonic()
        # Extract ENV from the snippet and inject into the harbor subprocess.
        # The snippet is the source of truth; the Dockerfile no longer carries agent
        # credentials, so all API keys / base URLs are injected here, and harbor
        # forwards them into the container via docker exec -e.
        harbor_env = _inherit_env()
        harbor_env["PYTHONPATH"] = str(PROJECT_ROOT)
        harbor_env.update(_agent_snippet_env(task.runner.agent))
        rc = await _run_subprocess(
            _build_harbor_cmd(task),
            cwd=PROJECT_ROOT,
            logger=log,
            new_session=True,
            env=harbor_env,
            track_dir=task.tmp_dir / "environment",
        )
        elapsed = int(time.monotonic() - run_start)
        done, total = _inc_progress()
        if rc == 0:
            log.info(">>> DONE %s / %s (%ds) [%d/%d]",
                     task.tag, task.run_id, elapsed, done, total)
        else:
            log.error(">>> FAILED %s / %s (%ds) [%d/%d] rc=%d",
                      task.tag, task.run_id, elapsed, done, total, rc)
            _fail_records.append(
                f"{task.entry.family} | {task.entry.variant} | {task.runner.agent} | "
                f"{task.runner.model} | {task.run_id}"
            )
    finally:
        # Fallback cleanup: harbor may leave compose containers behind on crash.
        # Try both the tmp path and the original variant path as docker labels.
        for cleanup_dir in (
            task.tmp_dir / "environment",
            task.variant_src / "environment",
        ):
            try:
                await _docker_cleanup_for_dir(cleanup_dir, log)
            except Exception:
                pass
        # Fallback: clean up leftover images by image-name prefix.
        # When harbor has removed the container but not the image, the label filter
        # misses it; fall back to matching on image-name prefix.
        try:
            prefix = _image_name_prefix(task.tmp_dir.name)
            img_ids = await _docker_capture(
                ["images", "--filter", f"reference={prefix}*", "-q"],
                timeout=10,
            )
            if img_ids:
                log.warning("fallback: removing %d leftover images (prefix=%s)", len(img_ids), prefix)
                await _docker_run(["rmi", "-f", *img_ids], timeout=120)
        except Exception:
            pass
        # Remove tmp dir (regardless of rc)
        shutil.rmtree(task.tmp_dir, ignore_errors=True)


async def _run_runner(
    runner: Runner, tasks: list[TaskItem], root_logger: logging.Logger
) -> None:
    """Per-runner scheduler: semaphore controls concurrency."""
    sem = asyncio.Semaphore(runner.parallel)
    root_logger.info("  [%s] scheduler started (parallel=%d, %d tasks)",
                     runner.name, runner.parallel, len(tasks))

    async def _bounded(t: TaskItem) -> None:
        async with sem:
            if _shutdown_event and _shutdown_event.is_set():
                done, total = _record_skip()
                root_logger.info("[%s] >>> SKIP (shutdown) %s [%d/%d]",
                                 t.tag, t.run_id, done, total)
                return
            await _run_one_task(t, root_logger)

    async with asyncio.TaskGroup() as tg:
        for t in tasks:
            tg.create_task(_bounded(t))

    root_logger.info("  [%s] scheduler done", runner.name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_variants.py",
        description="Batch-run (variant × runner × run_id) combinations.",
    )
    p.add_argument("--runner", action="append", default=[], metavar="SPEC",
                   help='Runner spec "agent|model[|parallel]"; may be passed multiple times')
    p.add_argument("--jobs-name", default="smoke_jobs/",
                   help="Output subdirectory under the project root (default: smoke_jobs/)")
    p.add_argument("--runs", type=int, default=3,
                   help="Number of rounds per variant × runner (default: 3)")
    p.add_argument("--start-run", type=int, default=1,
                   help="Round number to start from (default: 1)")
    p.add_argument("--parallel", type=int, default=3,
                   help="Default per-runner concurrency when a runner doesn't specify one")
    p.add_argument("--resume", action="store_true",
                   help="Skip tasks that already have result.json")
    p.add_argument("--entries-file", type=Path, default=None,
                   help="Read entries from a file, one per line")
    p.add_argument("entries", nargs="*",
                   help='Entry list "family|variant|case[|mode]"')
    return p.parse_args(argv)


def _load_entries(args: argparse.Namespace) -> list[VariantEntry]:
    raw: list[str] = []
    if args.entries_file:
        for line in args.entries_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.append(line)
    raw.extend(args.entries)
    if not raw:
        raise SystemExit("❌ no entries specified. Use positional args or --entries-file.")
    out = [VariantEntry.parse(s) for s in raw]
    for e in out:
        e.validate_no_skill()
    return out


def _load_runners(args: argparse.Namespace) -> list[Runner]:
    if not args.runner:
        raise SystemExit('❌ no runner specified. Use --runner "agent|model[|parallel]".')
    return [Runner.parse(s, args.parallel) for s in args.runner]


def _build_tasks(
    entries: list[VariantEntry],
    runners: list[Runner],
    start_run: int,
    runs: int,
    jobs_base: Path,
    tmp_base: Path,
) -> dict[Runner, list[TaskItem]]:
    out: dict[Runner, list[TaskItem]] = {}
    for runner in runners:
        items: list[TaskItem] = []
        for entry in entries:
            for n in range(start_run, runs + 1):
                run_id = f"run-{n:02d}"
                jobs_dir = (
                    jobs_base / entry.family / (entry.case or "no-case")
                    / entry.variant / entry.mode / runner.name
                )
                tmp_dir = (
                    tmp_base
                    / f"{entry.variant}-{entry.case or 'no-case'}-{runner.name}-{run_id}"
                )
                items.append(TaskItem(
                    entry=entry, runner=runner, run_id=run_id,
                    tmp_dir=tmp_dir, jobs_dir=jobs_dir,
                ))
        out[runner] = items
    return out


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def _async_main(args: argparse.Namespace, log: logging.Logger,
                      log_ts: str) -> int:
    global _shutdown_event, _progress_total
    _shutdown_event = asyncio.Event()

    runners = _load_runners(args)
    entries = _load_entries(args)

    if args.start_run > args.runs:
        raise SystemExit(
            f"❌ --start-run ({args.start_run}) > --runs ({args.runs}); no tasks would be produced"
        )

    jobs_base = OUTPUT_ROOT / args.jobs_name
    tmp_base = TMP_BASE_ROOT / log_ts
    tmp_base.mkdir(parents=True, exist_ok=True)

    # Startup banner
    total_par = sum(r.parallel for r in runners)
    log.info("############################################################")
    log.info("#  run_variants.py  |  %s", log_ts)
    log.info("#  Started: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("############################################################")
    log.info("")
    log.info("  Runners (%d):", len(runners))
    for r in runners:
        log.info("    %s / %s  (parallel=%d)", r.agent, r.model, r.parallel)
    log.info("")
    log.info("  Parallel : per-runner (total concurrency=%d)", total_par)
    log.info("  Default  : %d", args.parallel)
    log.info("  Resume   : %s", "YES" if args.resume else "NO")
    log.info("  Runs     : %d..%d", args.start_run, args.runs)
    log.info("  Jobs base: %s", jobs_base)
    log.info("  Tmp base : %s", tmp_base)
    log.info("")
    log.info("  Entries (%d):", len(entries))
    for e in entries:
        log.info("    [%s] %s | %s | %s", e.family, e.variant, e.case, e.mode)
    log.info("")

    # Resume scan
    done_dirs: frozenset[Path] = frozenset()
    if args.resume:
        log.info("  [resume] scanning %s ...", jobs_base)
        t0 = time.monotonic()
        done_dirs = _scan_done_dirs(jobs_base)
        log.info("  [resume] found %d completed jobs (%.1fs)",
                 len(done_dirs), time.monotonic() - t0)

    # Build the task matrix
    by_runner = _build_tasks(entries, runners, args.start_run, args.runs,
                              jobs_base, tmp_base)
    raw_total = sum(len(v) for v in by_runner.values())

    # Filter via resume
    if args.resume and done_dirs:
        for r, items in by_runner.items():
            by_runner[r] = [t for t in items if t.run_jobs_dir not in done_dirs]
    skipped = raw_total - sum(len(v) for v in by_runner.values())
    _progress_total = sum(len(v) for v in by_runner.values())
    log.info("  Total    : %d tasks (raw=%d, skipped=%d)",
             _progress_total, raw_total, skipped)
    log.info("")

    if _progress_total == 0:
        log.info(">>> nothing to run; everything already completed.")
        return 0

    # Signal handler
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, log)

    # Launch all runners in parallel
    log.info(">>> Phase 2: parallel execution (%d runners, total concurrency=%d)", len(runners), total_par)
    log.info("")

    try:
        async with asyncio.TaskGroup() as tg:
            for runner, items in by_runner.items():
                if items:
                    tg.create_task(_run_runner(runner, items, log))
    except* asyncio.CancelledError:
        log.warning(">>> TaskGroup cancelled")
    except* Exception as eg:
        for exc in eg.exceptions:
            log.error(">>> TaskGroup error: %s: %s", type(exc).__name__, exc)

    # Summary
    failed = len(_fail_records)
    passed = _progress_total - failed - _skip_count
    log.info("")
    log.info("############################################################")
    log.info("#  All done")
    log.info("#  Total: %d  |  Passed: %d  |  Failed: %d  |  Skipped: %d",
             _progress_total, passed, failed, _skip_count)
    if failed:
        log.info("#  Failure records:")
        for rec in _fail_records:
            log.info("#    %s", rec)
    log.info("#  Finished: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("############################################################")

    # Clean up tmp_base
    shutil.rmtree(tmp_base, ignore_errors=True)

    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    log_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = LOG_DIR / f"run-variants-{log_ts}.log"
    log = _setup_logging(log_file)
    log.info("Log: %s", log_file)

    try:
        return asyncio.run(_async_main(args, log, log_ts))
    except KeyboardInterrupt:
        log.warning(">>> KeyboardInterrupt")
        return 130


if __name__ == "__main__":
    sys.exit(main())
