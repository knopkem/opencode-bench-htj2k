#!/usr/bin/env python3
"""opencode-bench-htj2k: one-command local-model benchmark for OpenCode.

The task is to make a working pure-Rust HTJ2K codec faster at lossless encode
and decode while preserving standard conformance.  Everything that touches the
task runs in Docker:

  * the agent container sees only a fresh copy of the seed workdir;
  * the hidden suite, pristine seed, corpus generator and independent C++ OpenJPH
    oracle are mounted only after the agent exits;
  * the test container has no network and one pinned CPU;
  * the agent run is checkpointed, and the best eligible checkpoint is graded
    on a held-out final corpus.

Usage:
    ./bench.py build
    ./bench.py check
    ./bench.py models
    ./bench.py run --model provider/model          # 75/75/45 checkpointed rounds
    ./bench.py run --model provider/model --round-plan 90,90,30
    ./bench.py report
    ./bench.py leaderboard
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import html
import io
import json
import os
import re
import shlex
import shutil
import sqlite3
import statistics
import subprocess
import tarfile
import sys
import time
import urllib.request
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASK_DIR = ROOT / "tasks" / "htj2k"
SEED_DIR = TASK_DIR / "seed"
TESTS_DIR = TASK_DIR / "tests"
PROMPT_PATH = TASK_DIR / "PROMPT.md"
RUNS_DIR = ROOT / "runs"
RESULTS_PATH = ROOT / "results-opencode-bench-htj2k.csv"
REPORT_PATH = ROOT / "report-opencode-bench-htj2k.md"
LEADERBOARD_PATH = ROOT / "opencode-bench-htj2k-leaderboard.html"
SUBMISSIONS_DIR = ROOT / "submissions"
DOCS_DIR = ROOT / "docs"

DEFAULT_AGENT_IMAGE = os.environ.get(
    "OPENCODE_BENCH_AGENT_IMAGE", "opencode-bench-htj2k-agent:latest"
)
DEFAULT_TEST_IMAGE = os.environ.get(
    "OPENCODE_BENCH_TEST_IMAGE", "opencode-bench-htj2k-test:latest"
)
DEFAULT_ENGINE = os.environ.get("OPENCODE_BENCH_ENGINE", "docker")
DEFAULT_CPU_SET = os.environ.get("OPENCODE_BENCH_CPUSET", "0")
DEFAULT_NETWORK_POLICY = os.environ.get("OPENCODE_BENCH_NETWORK", "open")
OPENCODE = os.environ.get("OPENCODE_BENCH_OPENCODE", "opencode")

TEST_SCHEMA = "opencode-bench/htj2k-tests@1"
MANIFEST_SCHEMA = "opencode-bench/htj2k-manifest@1"
SUITE_VERSION = "htj2k-v1"
SUBMISSION_SCHEMA = "opencode-bench/htj2k-result@1"
PUBLIC_REPO = "knopkem/opencode-bench-htj2k"
BENCHMARK_LABEL = "benchmark-result"
HIDDEN_LABEL = "hidden"
VERIFIED_LABEL = "verified"
ATTENTION_LABEL = "needs-attention"
LEADERBOARD_INELIGIBLE_FACTOR = 0.75

REASONING_ORDER = {
    "xhigh": 6,
    "high": 5,
    "medium": 4,
    "low": 3,
    "minimal": 2,
    "none": 1,
    "off": 0,
}

SELF_TEST_RE = re.compile(
    r"(?:cargo\s+(?:build|test|bench|clippy|check)|scripts/bench\.py|python3?\s+scripts/bench\.py)",
    re.IGNORECASE,
)
BUILD_TOOL_RE = re.compile(r"cargo\s+(?:build|check|clippy)", re.IGNORECASE)

SOURCE_BAN_RE = re.compile(
    r"(?:"
    r"\bstd::process::Command\b"
    r"|\bCommand::new\b"
    r"|\blibloading\b"
    r"|\bdlopen\b"
    r"|\bextern\s+\"C\""
    r"|\binclude_bytes!\b"
    r"|\binclude_str!\b"
    r"|\bstd::net\b"
    r"|\bTcpStream\b"
    r"|\bUdpSocket\b"
    r")"
)
THREAD_RE = re.compile(
    r"(?:\bstd::thread\b|\bthread::scope\b|\bavailable_parallelism\b|\brayon\b)"
)
ALLOWED_LOCK_PACKAGES = {
    "htj2k",
    "htj2k-cli",
    "thiserror",
    "thiserror-impl",
    "proc-macro2",
    "quote",
    "syn",
    "unicode-ident",
}

EFFORT_FIELDS = [
    "span_s",
    "agent_s",
    "ttft_s",
    "think_s",
    "output_s",
    "model_time_s",
    "tool_s",
    "other_s",
    "tool_shell_s",
    "tool_write_s",
    "tool_read_s",
    "tool_other_s",
    "self_test_s",
    "self_test_runs",
    "build_tool_s",
    "wall_ex_self_test_s",
    "retries",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
    "max_request_tokens",
    "mean_request_tokens",
    "gen_tokens_per_s",
    "metrics_source",
    "metrics_version",
]
CSV_FIELDS = [
    "run_id",
    "timestamp",
    "model",
    "provider",
    "hardware",
    "rep",
    "variant",
    "thinking",
    "reasoning_effort",
    "suite_version",
    "fixture_seed",
    "image",
    "cpu_set",
    "cpu_count",
    "network_policy",
    "rounds",
    "round_plan",
    "best_round",
    "validation_seed",
    "final_seed",
    "confidence",
    "budget_wall_cap_s",
    "budget_idle_timeout_s",
    "build_ok",
    "seed_build_ok",
    "gate_ok",
    "result_valid",
    "perf_valid",
    "eligible",
    "score",
    "quality",
    "decode_speedup",
    "encode_speedup",
    "target_decode",
    "target_encode",
    "decode_seed_mp_s",
    "decode_candidate_mp_s",
    "encode_seed_mp_s",
    "encode_candidate_mp_s",
    "fatal",
    "candidate_rss_bytes",
    "wall_s",
    "compactions",
    "watchdog",
    "completion",
    "outcome",
    "steps",
    *EFFORT_FIELDS,
    "files_changed",
    "agent_ok",
    "agent_exit_code",
    "report",
]


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def slug(value: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return re.sub(r"-{2,}", "-", out).strip("-").lower() or "x"


def provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else model


def format_duration(seconds: float | str | None) -> str:
    if seconds is None:
        return "—"
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if value < 60:
        return f"{value:.1f}s"
    if value < 3600:
        return f"{value / 60:.1f}m"
    return f"{value / 3600:.2f}h"


def format_tokens(value: object) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "—"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1000:
        return f"{number / 1000:.1f}k"
    return str(number)


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def results_lock():
    lock_path = RESULTS_PATH.with_suffix(RESULTS_PATH.suffix + ".lock")
    with lock_path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_command(
    command: list[str],
    *,
    log_path: Path | None = None,
    dry_run: bool = False,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess | None:
    print("+ " + " ".join(shlex.quote(part) for part in command))
    if dry_run:
        return None
    if log_path is None:
        return subprocess.run(command, check=check, timeout=timeout)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write("+ " + " ".join(shlex.quote(part) for part in command) + "\n\n")
        handle.flush()
        proc = subprocess.run(
            command,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}); see {log_path}")
    return proc


def engine_available(engine: str, dry_run: bool = False) -> bool:
    if dry_run:
        return True
    try:
        return subprocess.run(
            [engine, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0
    except FileNotFoundError:
        return False


def image_exists(engine: str, image: str) -> bool:
    try:
        return subprocess.run(
            [engine, "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
    except FileNotFoundError:
        return False


def cpu_set_flags(args: argparse.Namespace) -> list[str]:
    """Docker flags that make one CPU visible to the process, not just a quota."""
    if getattr(args, "no_cpu_set", False):
        return []
    value = (getattr(args, "cpu_set", None) or "").strip()
    if not value:
        return []
    return ["--cpuset-cpus", value, "--cpus", "1"]


def effective_cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


# --------------------------------------------------------------------------- #
# OpenCode configuration and model selection
# --------------------------------------------------------------------------- #
def config_env(config_dir: Path | None) -> dict[str, str]:
    env = os.environ.copy()
    if config_dir is not None:
        if config_dir.name == "opencode":
            env["XDG_CONFIG_HOME"] = str(config_dir.parent)
        else:
            for name in ("opencode.json", "opencode.jsonc"):
                candidate = config_dir / name
                if candidate.exists():
                    env["OPENCODE_CONFIG"] = str(candidate)
                    break
    return env


def list_models(provider: str | None = None, config_dir: Path | None = None) -> list[str]:
    try:
        proc = subprocess.run(
            [OPENCODE, "models"],
            capture_output=True,
            text=True,
            timeout=60,
            env=config_env(config_dir),
            check=False,
        )
    except FileNotFoundError:
        sys.exit(f"error: {OPENCODE} not found on PATH")
    except subprocess.TimeoutExpired:
        sys.exit("error: `opencode models` timed out")
    if proc.returncode != 0:
        sys.exit(f"error: `opencode models` failed:\n{proc.stdout}{proc.stderr}")
    models: list[str] = []
    for line in proc.stdout.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        if not re.match(r"^[^\s/]+/\S+$", line):
            continue
        if provider and not line.startswith(f"{provider}/"):
            continue
        if line not in models:
            models.append(line)
    return models


def resolve_config_dir(explicit: str | None, run_dir: Path) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file():
            target = run_dir / "opencode-config" / "opencode"
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target / path.name)
            return target
        if path.is_dir():
            return path
        sys.exit(f"error: OpenCode config not found: {path}")
    try:
        proc = subprocess.run(
            [OPENCODE, "debug", "paths"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        for line in proc.stdout.splitlines():
            match = re.match(r"^config\s+(.+)$", line.strip())
            if match:
                path = Path(match.group(1)).expanduser()
                if path.exists():
                    return path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    fallback = Path.home() / ".config" / "opencode"
    if fallback.exists():
        return fallback
    sys.exit("error: could not find the OpenCode config directory; pass --config")


def config_env_names(config_dir: Path) -> set[str]:
    names: set[str] = set()
    if config_dir.is_file():
        files = [config_dir]
    else:
        files = list(config_dir.glob("*.json")) + list(config_dir.glob("*.jsonc"))
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in re.findall(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", text):
            if name in os.environ:
                names.add(name)
    return names


def load_model_entry(model: str) -> dict | None:
    try:
        proc = subprocess.run(
            [OPENCODE, "debug", "config", "--standalone"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        sources = json.loads(proc.stdout)
        info: dict = {}
        for source in sources:
            if isinstance(source.get("info"), dict):
                info.update(source["info"])
        providers = info.get("providers") or info.get("provider") or {}
        provider, _, model_id = model.partition("/")
        return (providers.get(provider) or {}).get("models", {}).get(model_id)
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
        return None


def describe_variant(entry: dict, variant: str | None) -> tuple[str, str]:
    body = dict(entry.get("body") or {})
    settings = dict(entry.get("settings") or {})
    if variant:
        for candidate in entry.get("variants") or []:
            if candidate.get("id") == variant:
                body.update(candidate.get("body") or {})
                settings.update(candidate.get("settings") or {})
                break
    thinking = body.get("thinking")
    effort = settings.get("reasoningEffort") or body.get("reasoningEffort") or ""
    if thinking is True:
        return "on", str(effort)
    if thinking is False:
        return "off", str(effort)
    if effort == "none":
        return "off", "none"
    if effort:
        return "on", str(effort)
    return "unknown", ""


def model_thinking(model: str, variant: str | None) -> tuple[str, str]:
    entry = load_model_entry(model)
    return describe_variant(entry, variant) if entry else ("unknown", "")


def model_variants(model: str) -> list[tuple[str, str, str]]:
    entry = load_model_entry(model)
    if not entry:
        return []
    out: list[tuple[str, str, str]] = []
    for candidate in entry.get("variants") or []:
        variant_id = str(candidate.get("id", ""))
        if not variant_id:
            continue
        thinking, effort = describe_variant(entry, variant_id)
        out.append((variant_id, thinking, effort))
    return out


def variant_rank(variant_id: str, thinking: str, effort: str) -> int:
    key = (effort or variant_id or "").strip().lower()
    if key in REASONING_ORDER:
        return REASONING_ORDER[key]
    if thinking == "off":
        return 0
    if thinking == "on":
        return 2
    return -1


def ordered_variants(model: str) -> list[tuple[str, str, str]]:
    return sorted(
        model_variants(model),
        key=lambda item: (-variant_rank(item[0], item[1], item[2]), item[0]),
    )


# --------------------------------------------------------------------------- #
# Workdir and agent
# --------------------------------------------------------------------------- #
def copy_seed(destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        SEED_DIR,
        destination,
        ignore=shutil.ignore_patterns(
            "target", ".git", "__pycache__", "*.pyc", "runs", "results-*"
        ),
    )
    shutil.copy2(PROMPT_PATH, destination / "PROMPT.md")


def agent_command(
    args: argparse.Namespace,
    *,
    round_number: int = 1,
    prompt_file: Path | None = None,
    session_id: str | None = None,
    continue_session: bool = False,
) -> str:
    prompt_name = f"ROUND-{round_number}.md" if round_number > 1 else "PROMPT.md"
    if prompt_file is not None:
        prompt_name = prompt_file.name
    if getattr(args, "agent_cmd", None):
        template = args.agent_cmd
        return (
            template.replace("{workdir}", "/work")
            .replace("{prompt_file}", f"/work/{prompt_name}")
            .replace("{model}", args.model or "")
            .replace("{variant}", args.variant or "")
        )
    model_ref = '"$OPENCODE_BENCH_MODEL"'
    if args.variant:
        model_ref = '"${OPENCODE_BENCH_MODEL}#${OPENCODE_BENCH_VARIANT}"'
    parts = ["exec opencode run --format json --auto"]
    if session_id:
        parts.append(f"--session {shlex.quote(session_id)}")
    elif continue_session:
        parts.append("--continue")
    parts.append(f"--model {model_ref}")
    if args.agent:
        parts.append(f"--agent {shlex.quote(args.agent)}")
    if args.pure:
        parts.append("--pure")
    parts.append(f'"$(cat /work/{prompt_name})"')
    return " ".join(parts)


def start_agent(
    model: str,
    config_dir: Path,
    workdir: Path,
    data_dir: Path,
    run_dir: Path,
    *,
    env_names: set[str],
    args: argparse.Namespace,
    round_number: int = 1,
    prompt_file: Path | None = None,
    session_id: str | None = None,
    continue_session: bool = False,
    timeout_s: float | None = None,
    idle_timeout_s: float | None = None,
) -> dict:
    name = f"opencode-bench-htj2k-{slug(provider_of(model))}-{uuid.uuid4().hex[:8]}"
    log_path = run_dir / f"agent-round-{round_number}.log"
    command = [
        args.engine,
        "run",
        "--rm",
        "--name",
        name,
        "--shm-size=1g",
        *cpu_set_flags(args),
        "-v",
        f"{workdir}:/work",
        "-v",
        f"{config_dir}:/config/opencode:ro",
        "-v",
        f"{data_dir}:/home/agent/.local/share/opencode",
        "-e",
        "HOME=/home/agent",
        "-e",
        "XDG_CONFIG_HOME=/home/agent/.config",
        "-e",
        f"OPENCODE_BENCH_MODEL={model}",
    ]
    if args.variant:
        command += ["-e", f"OPENCODE_BENCH_VARIANT={args.variant}"]
    for key in sorted(env_names):
        command += ["-e", key]
    command += [
        "-w",
        "/work",
        args.agent_image,
        "bash",
        "-c",
        agent_command(
            args,
            round_number=round_number,
            prompt_file=prompt_file,
            session_id=session_id,
            continue_session=continue_session,
        ),
    ]
    if args.dry_run:
        print("+ " + " ".join(shlex.quote(part) for part in command))
        return {
            "wall_s": 0.0,
            "watchdog": "dry-run",
            "completion": False,
            "agent_ok": True,
            "agent_exit_code": None,
            "self_test_runs": 0,
        }

    start = time.time()
    round_timeout = float(timeout_s if timeout_s is not None else args.timeout)
    round_idle = float(idle_timeout_s if idle_timeout_s is not None else args.idle_timeout)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write("+ " + " ".join(shlex.quote(part) for part in command) + "\n\n")
        handle.flush()
        proc = subprocess.Popen(
            command,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        completion_path = workdir / "COMPLETION.md"
        last_size = 0
        last_change = start
        completion_seen_at: float | None = None
        watchdog = "exit"
        while proc.poll() is None:
            now = time.time()
            try:
                size = log_path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size != last_size:
                last_size = size
                last_change = now
            if completion_path.exists() and completion_seen_at is None:
                completion_seen_at = now
                print("  COMPLETION.md seen; stopping in 60s unless the agent exits")
            if completion_seen_at is not None and now - completion_seen_at > 60:
                watchdog = "completion"
                break
            if now - start > round_timeout:
                watchdog = "hard"
                break
            if now - last_change > round_idle:
                watchdog = "idle"
                break
            time.sleep(2)
        if watchdog != "exit":
            subprocess.run(
                [args.engine, "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    wall_s = time.time() - start
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    self_tests = len(SELF_TEST_RE.findall(log_text))
    session_match = re.search(r'"sessionID"\s*:\s*"([^"]+)"', log_text)
    return {
        "wall_s": wall_s,
        "watchdog": watchdog,
        "completion": (workdir / "COMPLETION.md").exists(),
        "agent_ok": proc.returncode == 0,
        "agent_exit_code": proc.returncode,
        "self_test_runs": self_tests,
        "session_id": session_match.group(1) if session_match else None,
        "log_path": str(log_path),
    }


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def count_compactions(data_dir: Path) -> int:
    database = data_dir / "opencode.db"
    if not database.exists():
        return 0
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "select count(*) from session_message where type = 'compaction'"
            ).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def read_agent_usage(log_path: Path, wall_s: float) -> dict:
    metrics = {
        "span_s": wall_s,
        "agent_s": wall_s,
        "ttft_s": 0.0,
        "think_s": 0.0,
        "output_s": 0.0,
        "model_time_s": 0.0,
        "tool_s": 0.0,
        "other_s": 0.0,
        "tool_shell_s": 0.0,
        "tool_write_s": 0.0,
        "tool_read_s": 0.0,
        "tool_other_s": 0.0,
        "self_test_s": 0.0,
        "self_test_runs": 0,
        "build_tool_s": 0.0,
        "wall_ex_self_test_s": wall_s,
        "retries": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "max_request_tokens": 0,
        "mean_request_tokens": 0,
        "gen_tokens_per_s": None,
        "metrics_source": "log",
        "metrics_version": 1,
    }
    if not log_path.exists():
        return metrics
    step_starts: dict[str, int] = {}
    steps = 0
    request_tokens: list[int] = []
    self_test_s = 0.0
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            part = event.get("part") or {}
            timestamp = event.get("timestamp")
            if kind == "step_start" and timestamp:
                step_starts[
                    str(part.get("messageID") or part.get("id") or event.get("messageID") or steps)
                ] = int(timestamp)
            elif kind == "step_finish":
                steps += 1
                key = str(part.get("messageID") or part.get("id") or "")
                start_ts = step_starts.pop(key, None)
                if start_ts is None and timestamp:
                    start_ts = int(timestamp)
                if timestamp and start_ts is not None:
                    duration = max(0.0, (int(timestamp) - start_ts) / 1000.0)
                    metrics["model_time_s"] += duration
                tokens = part.get("tokens") or {}
                input_tokens = int(tokens.get("input") or 0)
                output_tokens = int(tokens.get("output") or 0)
                reasoning = int(tokens.get("reasoning") or 0)
                cache = tokens.get("cache") or {}
                read = int(cache.get("read") or 0)
                write = int(cache.get("write") or 0)
                metrics["input_tokens"] += input_tokens
                metrics["output_tokens"] += output_tokens
                metrics["reasoning_tokens"] += reasoning
                metrics["cache_read_tokens"] += read
                metrics["cache_write_tokens"] += write
                total = input_tokens + output_tokens + reasoning + read + write
                metrics["total_tokens"] += total
                request_tokens.append(input_tokens + output_tokens + reasoning)
            elif kind == "tool_use":
                command = json.dumps(part.get("state") or {})
                match = SELF_TEST_RE.search(command)
                if match:
                    metrics["self_test_runs"] += 1
                if BUILD_TOOL_RE.search(command):
                    metrics["build_tool_s"] += 0.0
    metrics["self_test_s"] = self_test_s
    metrics["wall_ex_self_test_s"] = max(0.0, wall_s - self_test_s)
    if request_tokens:
        metrics["max_request_tokens"] = max(request_tokens)
        metrics["mean_request_tokens"] = sum(request_tokens) / len(request_tokens)
    generation = metrics["output_tokens"] + metrics["reasoning_tokens"]
    if metrics["model_time_s"] > 0:
        metrics["gen_tokens_per_s"] = generation / metrics["model_time_s"]
    metrics["other_s"] = max(
        0.0, wall_s - metrics["model_time_s"] - metrics["tool_s"] - metrics["self_test_s"]
    )
    return metrics


def workdir_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(
            part in {"target", ".git", "__pycache__", "bin"}
            for part in relative.parts
        ):
            continue
        if path.name == "PROMPT.md":
            continue
        if str(relative).startswith("corpus/generated/"):
            continue
        try:
            manifest[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            pass
    return manifest


def count_changed_files(workdir: Path) -> int:
    seed = workdir_manifest(SEED_DIR)
    current = workdir_manifest(workdir)
    changed = sum(1 for path, digest in current.items() if seed.get(path) != digest)
    changed += sum(1 for path in seed if path not in current)
    return changed


def classify_outcome(*, watchdog: str, completion: bool, files_changed: int, agent_ok: bool) -> str:
    if watchdog == "idle":
        return "idle-timeout"
    if watchdog == "hard":
        return "wall-timeout"
    if watchdog == "completion":
        return "completion-timeout"
    if completion and agent_ok:
        return "completed"
    if files_changed and agent_ok:
        return "agent-exit-with-changes"
    if agent_ok:
        return "agent-exit-no-changes"
    return "agent-error"


# --------------------------------------------------------------------------- #
# Integrity and build/test orchestration
# --------------------------------------------------------------------------- #
def integrity_scan(workdir: Path) -> dict:
    problems: list[str] = []
    warnings: list[str] = []
    for path in workdir.rglob("build.rs"):
        if "vendor" in path.parts or "target" in path.parts:
            continue
        problems.append(f"build scripts are not allowed: {path.relative_to(workdir)}")
    for path in workdir.rglob("*.rs"):
        relative = path.relative_to(workdir)
        if "vendor" in relative.parts or "target" in relative.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("//"):
                continue
            if SOURCE_BAN_RE.search(line):
                problems.append(f"{relative}:{line_number}: forbidden construct: {line.strip()[:160]}")
            elif THREAD_RE.search(line):
                warnings.append(
                    f"{relative}:{line_number}: thread-based code is ineffective on the pinned one-CPU grader"
                )
    lock_path = workdir / "Cargo.lock"
    if lock_path.exists():
        text = lock_path.read_text(encoding="utf-8", errors="replace")
        for name in re.findall(r'^name = "([^"]+)"$', text, re.MULTILINE):
            if name not in ALLOWED_LOCK_PACKAGES:
                problems.append(f"Cargo.lock contains a non-vendored package: {name}")
    cargo = workdir / "Cargo.toml"
    if cargo.exists():
        text = cargo.read_text(encoding="utf-8", errors="replace")
        if "[patch" in text:
            warnings.append("Cargo.toml contains a [patch] section")
        if "git =" in text:
            problems.append("git dependencies are not allowed")
    for path in workdir.rglob("*"):
        if path.is_file() and path.stat().st_size > 8 * 1024 * 1024:
            warnings.append(f"large file in workdir: {path.relative_to(workdir)}")
    return {"problems": problems, "warnings": warnings, "ok": not problems}


def build_app(
    run_dir: Path,
    workdir: Path,
    *,
    engine: str,
    image: str,
    dry_run: bool,
    artifacts_dir: Path | None = None,
) -> dict:
    artifacts = artifacts_dir or (run_dir / "artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    command = [
        engine,
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{workdir}:/work:ro",
        "-v",
        f"{SEED_DIR}:/seed:ro",
        "-v",
        f"{artifacts}:/out",
        "-w",
        "/",
        image,
        "bash",
        "-c",
        "set +e; "
        "rm -rf /tmp/candidate /tmp/seed; mkdir -p /tmp/candidate /tmp/seed /out/bin; "
        "tar --exclude=target --exclude=.git --exclude=corpus/generated -C /work -cf - . | tar -C /tmp/candidate -xf -; "
        "tar --exclude=target --exclude=.git --exclude=corpus/generated -C /seed -cf - . | tar -C /tmp/seed -xf -; "
        "(cd /tmp/candidate && cargo build --release --offline --locked) > /out/build-candidate.log 2>&1; "
        "echo $? > /out/build-candidate.code; "
        "(cd /tmp/seed && cargo build --release --offline --locked) > /out/build-seed.log 2>&1; "
        "echo $? > /out/build-seed.code; "
        "cp /tmp/candidate/target/release/htj2k /out/bin/candidate 2>/dev/null; "
        "cp /tmp/seed/target/release/htj2k /out/bin/seed 2>/dev/null; "
        "test -x /out/bin/candidate && test -x /out/bin/seed",
    ]
    proc = run_command(
        command,
        log_path=artifacts.parent / "build-run.log",
        dry_run=dry_run,
        timeout=900,
    )
    if dry_run:
        return {"build_ok": True, "seed_build_ok": True, "typecheck_ok": True, "binary": True}
    candidate_code = _read_code(artifacts / "build-candidate.code")
    seed_code = _read_code(artifacts / "build-seed.code")
    return {
        "build_ok": candidate_code == 0 and (artifacts / "bin" / "candidate").exists(),
        "seed_build_ok": seed_code == 0 and (artifacts / "bin" / "seed").exists(),
        "typecheck_ok": candidate_code == 0,
        "binary": (artifacts / "bin" / "candidate").exists(),
        "proc_ok": proc.returncode == 0 if proc else True,
    }


def _read_code(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def run_tests(
    run_dir: Path,
    *,
    engine: str,
    image: str,
    dry_run: bool,
    manifest: dict,
    fixture_seed: int,
    artifacts_dir: Path | None = None,
    require_single_cpu: bool = False,
    args: argparse.Namespace | None = None,
) -> dict:
    artifacts = artifacts_dir or (run_dir / "artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    result_file = artifacts / "results.json"
    if result_file.exists():
        result_file.unlink()
    bundle = artifacts.parent / "tests-bundle.tar"
    if not dry_run:
        with tarfile.open(bundle, "w") as archive:
            for name in ("runner.py", "corpus.py", "manifest.json"):
                archive.add(TESTS_DIR / name, arcname=name)
            archive.add(TESTS_DIR / "fixtures", arcname="fixtures")
        os.chmod(bundle, 0o600)
    command = [
        engine,
        "run",
        "--rm",
        "--network",
        "none",
        *(cpu_set_flags(args) if args is not None else ["--cpus", "1"]),
        "--memory",
        "4g",
        "--tmpfs",
        "/perf:rw,size=6g,exec",
        "-v",
        f"{artifacts / 'bin'}:/bins:ro",
        "-v",
        f"{bundle}:/bundle.tar:ro",
        "-v",
        f"{artifacts}:/out",
        "-w",
        "/",
        "-e",
        "HOME=/tmp",
        "-e",
        "TMPDIR=/tmp",
        image,
        "bash",
        "-c",
        "set -e; mkdir -p /opt/private; "
        "tar -C /opt/private -xf /bundle.tar; "
        "chmod -R 700 /opt/private; "
        "exec python3 -B /opt/private/runner.py "
        "--candidate-bin /bins/candidate --seed-bin /bins/seed "
        "--oracle /opt/oracle/bin/oracle "
        "--manifest /opt/private/manifest.json "
        "--fixtures /opt/private/fixtures "
        "--out /out --scratch /perf "
        f"--run-seed {fixture_seed}"
        + (" --require-single-cpu" if require_single_cpu else ""),
    ]
    timeout = float(manifest["limits"].get("perf_deadline_s", 900)) + 300
    try:
        proc = run_command(
            command,
            log_path=artifacts / "test.log",
            dry_run=dry_run,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "result_valid": False,
            "test_ok": False,
            "result": None,
            "error": f"hidden suite exceeded {timeout:.0f}s",
            "exit_code": None,
        }
    if dry_run:
        return {"result_valid": True, "test_ok": True, "result": None, "error": None, "exit_code": 0}
    if not result_file.exists():
        return {
            "result_valid": False,
            "test_ok": False,
            "result": None,
            "error": f"results.json missing (suite exit {proc.returncode if proc else '?'})",
            "exit_code": proc.returncode if proc else None,
        }
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "result_valid": False,
            "test_ok": False,
            "result": None,
            "error": f"invalid results.json: {exc}",
            "exit_code": proc.returncode if proc else None,
        }
    error = validate_result(data, manifest)
    if error:
        return {
            "result_valid": False,
            "test_ok": False,
            "result": data,
            "error": error,
            "exit_code": proc.returncode if proc else None,
        }
    gates_ok = bool(data.get("all_gates"))
    perf_ok = bool(data.get("perf_valid"))
    all_ok = gates_ok and perf_ok and proc and proc.returncode == 0
    return {
        "result_valid": True,
        "test_ok": all_ok,
        "result": data,
        "error": None if all_ok else _result_error_summary(data),
        "exit_code": proc.returncode if proc else None,
    }


def _result_error_summary(data: dict) -> str:
    if data.get("fatal_error"):
        return str(data["fatal_error"])
    failed: list[str] = []
    for family, cases in (data.get("correctness", {}).get("families", {}) or {}).items():
        for case, ok in cases.items():
            if not ok:
                failed.append(f"{family}:{case}")
    if not data.get("perf_valid"):
        failed.append("perf-invalid")
    return ", ".join(failed[:12]) or "suite failed"


def validate_result(data: object, manifest: dict) -> str | None:
    if not isinstance(data, dict):
        return "results.json must be an object"
    if data.get("schema") != TEST_SCHEMA:
        return f"results.json schema must be {TEST_SCHEMA!r}"
    if data.get("suite_version") != manifest.get("suite_version"):
        return "results.json suite_version does not match the manifest"
    correctness = data.get("correctness")
    if not isinstance(correctness, dict) or not isinstance(correctness.get("families"), dict):
        return "results.json is missing correctness families"
    families = correctness["families"]
    expected = {
        "p0.decode-lossless": [c["id"] for c in manifest["correctness"]]
        + [c["id"] for c in manifest.get("decode_extra", [])],
        "p0.encode-lossless": [c["id"] for c in manifest["correctness"]],
        "p0.dicom-201": ["ABDOM_1"],
        "p0.robustness": ["truncated", "garbage", "empty", "bad-magic", "wrong-raw-size"],
        "p0.determinism": ["same-input"],
    }
    for family, cases in expected.items():
        provided = families.get(family)
        if not isinstance(provided, dict):
            return f"results.json is missing family {family}"
        missing = [case for case in cases if case not in provided]
        if missing:
            return f"results.json is missing {family} cases: {', '.join(missing[:5])}"
    perf = data.get("perf")
    if not isinstance(perf, dict):
        return "results.json is missing perf data"
    for kind in ("decode", "encode"):
        if not isinstance(perf.get(kind), list):
            return f"perf.{kind} must be a list"
    return None


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def score_run(
    tests: dict,
    build: dict,
    *,
    manifest: dict,
    result_valid: bool,
) -> dict:
    data = tests.get("result") or {}
    families = (data.get("correctness") or {}).get("families") or {}
    expected = {
        "p0.decode-lossless": [c["id"] for c in manifest["correctness"]]
        + [c["id"] for c in manifest.get("decode_extra", [])],
        "p0.encode-lossless": [c["id"] for c in manifest["correctness"]],
        "p0.dicom-201": ["ABDOM_1"],
        "p0.robustness": ["truncated", "garbage", "empty", "bad-magic", "wrong-raw-size"],
        "p0.determinism": ["same-input"],
    }
    gate_rows = []
    failures = 0
    passed = 0
    total = 0
    for family, cases in expected.items():
        provided = families.get(family) or {}
        case_results = {case: bool(provided.get(case, False)) for case in cases}
        ok = all(case_results.values())
        failures += sum(1 for value in case_results.values() if not value)
        passed += sum(1 for value in case_results.values() if value)
        total += len(cases)
        gate_rows.append(
            {
                "id": family,
                "ok": ok,
                "passed": sum(1 for value in case_results.values() if value),
                "total": len(cases),
                "cases": case_results,
            }
        )
    gates_ok = all(row["ok"] for row in gate_rows)
    perf = data.get("perf") or {}
    decode_items = perf.get("decode") or []
    encode_items = perf.get("encode") or []
    valid_decode = [item["speedup"] for item in decode_items if item.get("valid")]
    valid_encode = [item["speedup"] for item in encode_items if item.get("valid")]
    decode_speedup = _geomean([clamp(float(v), 0.25, 5.0) for v in valid_decode])
    encode_speedup = _geomean([clamp(float(v), 0.25, 5.0) for v in valid_encode])
    expected_decode = len(manifest["perf"]["decode"])
    expected_encode = len(manifest["perf"]["encode"])
    perf_valid = bool(
        perf.get("decode_valid")
        and perf.get("encode_valid")
        and len(decode_items) == expected_decode
        and len(encode_items) == expected_encode
        and len(valid_decode) == expected_decode
        and len(valid_encode) == expected_encode
    )
    target_decode = float(manifest["targets"]["decode"])
    target_encode = float(manifest["targets"]["encode"])
    noise = float(manifest.get("limits", {}).get("noise_floor", 0.0))
    credit_decode = clamp(
        (decode_speedup - 1.0 - noise) / max(target_decode - 1.0 - noise, 1e-6),
        0.0,
        2.0,
    )
    credit_encode = clamp(
        (encode_speedup - 1.0 - noise) / max(target_encode - 1.0 - noise, 1e-6),
        0.0,
        2.0,
    )
    score = 100.0 * (0.5 * credit_decode + 0.5 * credit_encode)
    eligible = (
        bool(build.get("build_ok"))
        and bool(build.get("seed_build_ok"))
        and bool(result_valid)
        and gates_ok
        and perf_valid
    )
    if not eligible:
        score = 0.0
    if not gates_ok or not perf_valid:
        score = 0.0
    return {
        "gates": gate_rows,
        "gate_ok": gates_ok,
        "gate_failures": failures,
        "gate_pass": passed,
        "gate_total": total,
        "perf_valid": perf_valid,
        "decode_speedup": decode_speedup,
        "encode_speedup": encode_speedup,
        "target_decode": target_decode,
        "target_encode": target_encode,
        "credit_decode": credit_decode,
        "credit_encode": credit_encode,
        "quality": min(1.0, score / 100.0),
        "score": score,
        "eligible": eligible,
        "fatal": failures,
        "major": 0 if perf_valid else 1,
        "minor": 0,
        "regressions": 0,
        "result_valid": bool(result_valid),
        "decode": decode_items,
        "encode": encode_items,
        "subcases": {"pass": passed, "total": total},
        "headroom": {"pass": passed, "total": total},
        "counts": {
            "gates": {"pass": sum(1 for row in gate_rows if row["ok"]), "total": len(gate_rows)},
            "perf": {"pass": 2 if perf_valid else 0, "total": 2},
        },
    }


def _geomean(values: list[float]) -> float:
    if not values:
        return 0.0
    import math

    return math.exp(sum(math.log(max(value, 1e-9)) for value in values) / len(values))


# --------------------------------------------------------------------------- #
# Reports and leaderboard
# --------------------------------------------------------------------------- #
def write_run_report(run_dir: Path, meta: dict, score: dict) -> None:
    lines = [
        f"# {meta['run_id']}",
        "",
        f"- model: `{meta['model']}`",
        f"- provider: {meta['provider']}",
        *([f"- hardware: {meta['hardware']}"] if meta.get("hardware") else []),
        f"- cpu: {meta.get('cpu_set')} (effective {meta.get('cpu_count')})",
        f"- network: {meta.get('network_policy')} (grader is always offline)",
        f"- rounds: {meta.get('rounds_count')} ({meta.get('round_plan')}); best {meta.get('best_round')}",
        f"- variant: {meta.get('variant', 'default')}",
        f"- thinking: {meta.get('thinking', 'unknown')}"
        + (f" ({meta['reasoning_effort']})" if meta.get("reasoning_effort") else ""),
        f"- suite: {meta.get('suite_version')} (fixture {meta.get('fixture_seed')})",
        f"- wall: {format_duration(meta['wall_s'])}",
        "- budget: "
        + (
            f"{format_duration(meta['budget_wall_cap_s'])} wall / "
            f"{format_duration(meta['budget_idle_timeout_s'])} idle"
            if meta.get("budget_wall_cap_s") and meta.get("budget_idle_timeout_s")
            else "unknown"
        ),
        f"- compactions: {meta.get('compactions', 0)}",
        f"- watchdog: {meta['watchdog']}",
        f"- completion file: {meta['completion']}",
        f"- outcome: {meta.get('outcome', 'unknown')}",
        f"- steps: {meta.get('steps', 0)}",
        f"- files changed: {meta.get('files_changed', 0)}",
        f"- build: {'ok' if meta['build_ok'] else 'failed'}"
        f" (seed {'ok' if meta.get('seed_build_ok') else 'FAILED'})",
        f"- gates: {'pass' if score['gate_ok'] else 'fail'}"
        f" ({score['gate_pass']}/{score['gate_total']} cases)",
        f"- perf valid: {score['perf_valid']}",
        f"- eligible: {score['eligible']}",
        f"- score: {score['score']:.1f}",
        f"- decode speedup: x{score['decode_speedup']:.3f} (target x{score['target_decode']:.2f})",
        f"- encode speedup: x{score['encode_speedup']:.3f} (target x{score['target_encode']:.2f})",
        "",
        "## Correctness gates",
        "",
        "| family | passed | failed cases |",
        "|---|---:|---|",
    ]
    for row in score["gates"]:
        failed = ", ".join(case for case, ok in row["cases"].items() if not ok) or "-"
        lines.append(f"| {row['id']} | {row['passed']}/{row['total']} | {failed} |")
    lines += [
        "",
        "## Rounds",
        "",
        "| round | budget | wall | watchdog | gates | validation score | final score |",
        "|---|---:|---:|---|---:|---:|---:|",
    ]
    for row in meta.get("rounds") or []:
        validation = row.get("validation_score")
        final = row.get("final_score")
        lines.append(
            f"| {row.get('round')} | {format_duration(row.get('budget_s'))} | "
            f"{format_duration(row.get('wall_s'))} | {row.get('watchdog', '—')} | "
            f"{'pass' if row.get('gate_ok') else 'FAIL'} "
            f"({row.get('gate_pass')}/{row.get('gate_total')}) | "
            f"{validation:.1f} | "
            f"{final:.1f} |"
            if validation is not None and final is not None
            else f"| {row.get('round')} | {format_duration(row.get('budget_s'))} | "
            f"{format_duration(row.get('wall_s'))} | {row.get('watchdog', '—')} | "
            f"{'pass' if row.get('gate_ok') else 'FAIL'} "
            f"({row.get('gate_pass')}/{row.get('gate_total')}) | - | - |"
        )
    lines += [
        "",
        "Checkpoints use a validation corpus; the official score is the best eligible",
        "checkpoint rescored on a held-out final corpus.",
        "",
        "## Performance",
        "",
        "| kind | workload | seed MP/s | candidate MP/s | reference MP/s | speedup | cv |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for item in score["decode"] + score["encode"]:
        if not item.get("valid"):
            lines.append(
                f"| {item.get('kind', '?')} | {item.get('id', '?')} | - | - | - | invalid | - |"
            )
            continue
        ref = item.get("reference_mp_s")
        cv = item.get("cv")
        cv_text = f"{cv * 100:.1f}%" if isinstance(cv, (int, float)) else "—"
        lines.append(
            f"| {item['kind']} | {item['id']} | {item['seed_mp_s']:.1f} | "
            f"{item['candidate_mp_s']:.1f} | "
            f"{ref:.1f} | x{item['speedup']:.3f} | {cv_text} |"
            if ref
            else f"| {item['kind']} | {item['id']} | {item['seed_mp_s']:.1f} | "
            f"{item['candidate_mp_s']:.1f} | - | x{item['speedup']:.3f} | {cv_text} |"
        )
    lines += [
        "",
        "The score is seed-normalised. The untouched seed scores 0; matching the",
        "frozen target in both dimensions scores 100. Correctness gates must all",
        "pass for a result to be eligible.",
        "",
        "## Artifacts",
        "",
        "- agent round logs: " + ", ".join(f"`{Path(path).name}`" for path in meta.get("agent_logs") or []),
        "- per-round `build-run.log`, `test.log`, `results.json` and `source.tar`",
        "- `rounds/<best>/final/results.json`",
        "",
    ]
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def ensure_results_schema() -> None:
    if not RESULTS_PATH.exists() or RESULTS_PATH.stat().st_size == 0:
        with RESULTS_PATH.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=CSV_FIELDS).writeheader()


def read_result_rows() -> list[dict]:
    ensure_results_schema()
    with RESULTS_PATH.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_result_rows(rows: list[dict]) -> None:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    atomic_write_text(RESULTS_PATH, buffer.getvalue())


def append_result(row: dict) -> None:
    row = dict(row)
    if isinstance(row.get("rounds"), list):
        row["rounds"] = len(row["rounds"])
    with results_lock():
        rows = read_result_rows()
        rows = [existing for existing in rows if existing.get("run_id") != row.get("run_id")]
        rows.append(row)
        write_result_rows(rows)


def score_row(row: dict) -> float:
    try:
        return float(row.get("score") or 0.0)
    except ValueError:
        return 0.0


def leaderboard_entries(rows: list[dict]) -> list[dict]:
    entries = []
    for row in rows:
        if not row.get("run_id"):
            continue
        score = score_row(row)
        eligible = str(row.get("eligible", "")).lower() == "true"
        if not eligible:
            score *= LEADERBOARD_INELIGIBLE_FACTOR
        entries.append(
            {
                **row,
                "_score": score,
                "_eligible": eligible,
                "_model_time": _float(row.get("model_time_s")),
            }
        )
    entries.sort(
        key=lambda entry: (
            -entry["_score"],
            entry["_model_time"] if entry["_model_time"] is not None else float("inf"),
            entry.get("timestamp", ""),
        )
    )
    return entries


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _fmt(value: object, digits: int = 3) -> str:
    try:
        return f"{float(value):.{digits}f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def aggregate_entries(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if not row.get("run_id"):
            continue
        key = (str(row.get("model") or "?"), str(row.get("variant") or "default"))
        groups.setdefault(key, []).append(row)
    entries: list[dict] = []
    for (model, variant), group in groups.items():
        all_scores = [score_row(row) for row in group]
        eligible_rows = [
            row for row in group if str(row.get("eligible", "")).lower() == "true"
        ]
        scored_rows = eligible_rows or group
        scores = [score_row(row) for row in scored_rows]
        gate_values = [
            1.0 if str(row.get("gate_ok", "")).lower() == "true" else 0.0
            for row in group
        ]
        decode = [
            float(row.get("decode_speedup"))
            for row in scored_rows
            if _float(row.get("decode_speedup")) is not None
        ]
        encode = [
            float(row.get("encode_speedup"))
            for row in scored_rows
            if _float(row.get("encode_speedup")) is not None
        ]
        model_time = [
            float(row.get("model_time_s"))
            for row in scored_rows
            if _float(row.get("model_time_s")) is not None
        ]
        entries.append(
            {
                "model": model,
                "variant": variant,
                "runs": len(group),
                "eligible_runs": len(eligible_rows),
                "median_score": _median(scores) or 0.0,
                "best_score": max(all_scores) if all_scores else 0.0,
                "gate_rate": sum(gate_values) / len(gate_values) if gate_values else 0.0,
                "median_decode": _median(decode),
                "median_encode": _median(encode),
                "median_model_time": _median(model_time),
                "confidence": "multi-run" if len(group) >= 3 else "single-run",
                "network": sorted({str(row.get("network_policy") or "unknown") for row in group}),
                "cpu": sorted({str(row.get("cpu_set") or "unknown") for row in group}),
            }
        )
    entries.sort(
        key=lambda entry: (
            -entry["median_score"],
            entry["median_model_time"] if entry["median_model_time"] is not None else float("inf"),
            entry["model"],
            entry["variant"],
        )
    )
    return entries


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def render_leaderboard(rows: list[dict]) -> str:
    entries = leaderboard_entries(rows)
    summaries = aggregate_entries(rows)
    summary_body = []
    for index, entry in enumerate(summaries, 1):
        summary_body.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><code>{_esc(entry['model'])}</code></td>"
            f"<td>{_esc(entry['variant'])}</td>"
            f"<td>{entry['runs']}</td>"
            f"<td>{entry['median_score']:.1f}</td>"
            f"<td>{entry['best_score']:.1f}</td>"
            f"<td>{entry['gate_rate'] * 100:.0f}%</td>"
            f"<td>{_esc(_fmt(entry['median_decode']))}</td>"
            f"<td>{_esc(_fmt(entry['median_encode']))}</td>"
            f"<td>{_esc(entry['confidence'])}</td>"
            f"<td>{_esc(', '.join(entry['network']))}</td>"
            "</tr>"
        )
    body = []
    for index, entry in enumerate(entries, 1):
        status = "eligible" if entry["_eligible"] else "ineligible"
        body.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><code>{_esc(entry.get('model', ''))}</code></td>"
            f"<td>{_esc(entry.get('variant', 'default'))}</td>"
            f"<td>{entry['_score']:.1f}</td>"
            f"<td>{_esc(entry.get('decode_speedup', '—'))}</td>"
            f"<td>{_esc(entry.get('encode_speedup', '—'))}</td>"
            f"<td>{status}</td>"
            f"<td>{_esc(entry.get('cpu_set', '—'))}</td>"
            f"<td>{_esc(entry.get('network_policy', '—'))}</td>"
            f"<td>{_esc(entry.get('hardware', ''))}</td>"
            f"<td>{format_duration(entry.get('wall_s'))}</td>"
            f"<td>{format_tokens(entry.get('total_tokens'))}</td>"
            f"<td>{_esc(entry.get('timestamp', ''))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>opencode-bench-htj2k leaderboard</title>
<style>
body {{ font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; color: #111; }}
h1 {{ font-size: 1.5rem; }}
h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
th, td {{ border-bottom: 1px solid #ddd; padding: .5rem .6rem; text-align: left; vertical-align: top; }}
th {{ background: #f5f5f5; font-weight: 600; }}
code {{ font-size: .9em; }}
.note {{ color: #555; max-width: 70rem; }}
</style>
</head>
<body>
<h1>opencode-bench-htj2k</h1>
<p class="note">Lossless HTJ2K encode/decode optimisation. Score is seed-normalised:
the untouched seed is 0, matching the frozen target in both dimensions is 100.
Correctness gates must pass for eligibility. One run is accepted, but single-run
entries are low-confidence; three runs with different seeds are recommended.
The grader runs on one pinned CPU; the agent network is labelled per run.
Rows are local artifacts unless a community submission says otherwise.</p>
<h2>Models</h2>
<table>
<thead><tr><th>#</th><th>model</th><th>variant</th><th>runs</th><th>median score</th><th>best</th><th>gate rate</th><th>decode</th><th>encode</th><th>confidence</th><th>network</th></tr></thead>
<tbody>
{''.join(summary_body) or '<tr><td colspan="11">no runs yet</td></tr>'}
</tbody>
</table>
<h2>Runs</h2>
<table>
<thead><tr><th>#</th><th>model</th><th>variant</th><th>score</th><th>decode</th><th>encode</th><th>status</th><th>cpu</th><th>network</th><th>hardware</th><th>wall</th><th>tokens</th><th>run</th></tr></thead>
<tbody>
{''.join(body) or '<tr><td colspan="13">no runs yet</td></tr>'}
</tbody>
</table>
</body>
</html>
"""


def write_leaderboard() -> Path:
    atomic_write_text(LEADERBOARD_PATH, render_leaderboard(read_result_rows()))
    return LEADERBOARD_PATH


def print_summary(meta: dict, score: dict) -> None:
    print("")
    print(f"{meta['model']}  rep {meta['rep']}  variant {meta.get('variant', 'default')}")
    print(
        f"  build {'ok' if meta['build_ok'] else 'FAIL'}"
        f"   seed build {'ok' if meta.get('seed_build_ok') else 'FAIL'}"
        f"   gates {'pass' if score['gate_ok'] else 'FAIL'}"
        f" ({score['gate_pass']}/{score['gate_total']})"
        f"   perf {'ok' if score['perf_valid'] else 'FAIL'}"
    )
    print(
        f"  score {score['score']:.1f}"
        f"   decode x{score['decode_speedup']:.3f}"
        f"   encode x{score['encode_speedup']:.3f}"
        f"   {'ELIGIBLE' if score['eligible'] else 'not eligible'}"
    )
    rounds = meta.get("rounds") or []
    if rounds:
        print(
            f"  rounds {len(rounds)}"
            f"   best {meta.get('best_round')}"
            f"   cpu {meta.get('cpu_set')}"
            f"   network {meta.get('network_policy')}"
        )
        for row in rounds:
            final = row.get("final_score")
            print(
                f"    round {row.get('round')}: "
                f"gates {'pass' if row.get('gate_ok') else 'FAIL'}"
                f"  validation {float(row.get('validation_score') or 0):.1f}"
                + (f"  final {float(final):.1f}" if final is not None else "")
                + f"  watchdog {row.get('watchdog')}"
            )
    print(
        f"  wall {format_duration(meta['wall_s'])}"
        f"   model {format_duration(meta.get('model_time_s'))}"
        f"   self-test {meta.get('self_test_runs') or 0}"
        f"   compactions {meta.get('compactions', 0)}"
        f"   watchdog {meta['watchdog']}"
    )
    print(
        f"  tokens {format_tokens(meta.get('input_tokens'))} in"
        f" · {format_tokens(meta.get('output_tokens'))} out"
        f" · {format_tokens(meta.get('reasoning_tokens'))} reason"
        f" · peak {format_tokens(meta.get('max_request_tokens'))} req"
    )
    if meta.get("report"):
        print(f"  report {meta['report']}")


# --------------------------------------------------------------------------- #
# Run orchestration
# --------------------------------------------------------------------------- #
def claim_run_dir(base_id: str) -> tuple[str, Path]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = base_id
    suffix = 1
    while (RUNS_DIR / run_id).exists():
        suffix += 1
        run_id = f"{base_id}-{suffix}"
    path = RUNS_DIR / run_id
    path.mkdir(parents=True)
    return run_id, path


def parse_round_plan(args: argparse.Namespace) -> list[float]:
    if getattr(args, "no_rounds", False):
        return [max(1.0, float(args.timeout) / 60.0)]
    raw = str(getattr(args, "round_plan", "75,75,45"))
    values: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            raise SystemExit(f"error: invalid --round-plan value {part!r}")
        if value <= 0:
            raise SystemExit("error: round budgets must be positive")
        values.append(value)
    if not values:
        raise SystemExit("error: --round-plan is empty")
    return values


def _round_prompt_header(
    number: int,
    total: int,
    budget_minutes: float,
    remaining_minutes: float,
) -> str:
    return (
        f"# Round {number} of {total}\n\n"
        f"This round has **{budget_minutes:g} minutes**; the total agent budget is "
        f"**{remaining_minutes:g} minutes** remaining.\n\n"
    )


def write_round_one_prompt(workdir: Path, plan: list[float], args: argparse.Namespace) -> Path:
    total = len(plan)
    schedule = " / ".join(f"{value:g}m" for value in plan)
    final_note = (
        "This is the only round: optimize, then finish with **COMPLETION.md**."
        if total == 1
        else (
            "Do **not** write `COMPLETION.md` until the final round. "
            "After each round the harness builds your tree and reports gate status "
            "and speedups; the best eligible checkpoint is graded on a held-out corpus."
        )
    )
    text = (
        _round_prompt_header(1, total, plan[0], sum(plan))
        + f"Schedule: **{schedule}**.\n\n"
        + "Read `/work/PROMPT.md` first; it contains the task contract, limits and scoring.\n\n"
        + "The grader exposes **exactly one CPU** (`available_parallelism()` is 1). "
        "Thread-based parallelism will not help.\n\n"
        + final_note
        + "\n\nWork only in `/work`. Good luck.\n"
    )
    path = workdir / "ROUND-1.md"
    path.write_text(text, encoding="utf-8")
    return path


def checkpoint_prompt_text(checkpoint: dict) -> str:
    score = checkpoint.get("score") or {}
    tests = checkpoint.get("tests") or {}
    lines = [
        f"## Checkpoint after round {checkpoint.get('round')}",
        "",
        f"- build: {'ok' if checkpoint.get('build', {}).get('build_ok') else 'FAILED'}",
        f"- gates: {'pass' if score.get('gate_ok') else 'FAIL'} "
        f"({score.get('gate_pass', 0)}/{score.get('gate_total', 0)})",
        f"- decode speedup: x{float(score.get('decode_speedup') or 0):.3f} "
        f"(target x{float(score.get('target_decode') or 0):.2f})",
        f"- encode speedup: x{float(score.get('encode_speedup') or 0):.3f} "
        f"(target x{float(score.get('target_encode') or 0):.2f})",
        f"- validation score: {float(score.get('score') or 0):.1f}",
    ]
    if tests.get("error"):
        lines.append(f"- suite note: {tests['error']}")
    failed: list[str] = []
    for gate in score.get("gates") or []:
        for case, ok in (gate.get("cases") or {}).items():
            if not ok:
                failed.append(f"{gate.get('id')}:{case}")
    if failed:
        lines += ["", "Failed gates:", *[f"- `{item}`" for item in failed[:30]]]
    return "\n".join(lines)


def write_next_round_prompt(
    workdir: Path,
    number: int,
    total: int,
    plan: list[float],
    checkpoint: dict,
    remaining_s: float,
) -> Path:
    is_final = number == total
    text = (
        _round_prompt_header(number, total, plan[number - 1], max(1.0, remaining_s / 60.0))
        + checkpoint_prompt_text(checkpoint)
        + "\n\n"
        + "Keep the CLI contract and correctness gates intact. "
        + (
            "This is the **final round**: do not start new optimizations. "
            "Remove profiling/timing instrumentation, run `cargo build --release "
            "--offline --locked`, `cargo test --workspace --offline` and "
            "`python3 scripts/bench.py --reps 1`, then write `COMPLETION.md`. "
            "The best eligible checkpoint is graded."
            if is_final
            else "Do **not** write `COMPLETION.md` yet. Continue optimizing; the "
            "next checkpoint will build and score your tree."
        )
        + "\n"
    )
    path = workdir / f"ROUND-{number}.md"
    path.write_text(text, encoding="utf-8")
    return path


def snapshot_source(workdir: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w") as archive:
        for path in sorted(workdir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(workdir)
            if any(part in {"target", ".git", "__pycache__"} for part in relative.parts):
                continue
            if str(relative).startswith("corpus/generated/"):
                continue
            archive.add(path, arcname=str(relative))


def count_log_steps(path: Path) -> int:
    try:
        return len(re.findall(r'"type":"step_finish"', path.read_text(errors="replace")))
    except OSError:
        return 0


def aggregate_agent_metrics(rounds: list[dict]) -> dict:
    metrics = {
        "span_s": 0.0,
        "agent_s": 0.0,
        "ttft_s": 0.0,
        "think_s": 0.0,
        "output_s": 0.0,
        "model_time_s": 0.0,
        "tool_s": 0.0,
        "other_s": 0.0,
        "tool_shell_s": 0.0,
        "tool_write_s": 0.0,
        "tool_read_s": 0.0,
        "tool_other_s": 0.0,
        "self_test_s": 0.0,
        "self_test_runs": 0,
        "build_tool_s": 0.0,
        "wall_ex_self_test_s": 0.0,
        "retries": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "max_request_tokens": 0,
        "mean_request_tokens": 0,
        "gen_tokens_per_s": None,
        "metrics_source": "round-logs",
        "metrics_version": 1,
    }
    for row in rounds:
        source = row.get("metrics") or {}
        for key in metrics:
            if key in {"max_request_tokens", "mean_request_tokens", "gen_tokens_per_s", "metrics_source", "metrics_version"}:
                continue
            value = source.get(key)
            if isinstance(value, (int, float)):
                metrics[key] += value
        metrics["max_request_tokens"] = max(
            metrics["max_request_tokens"], int(source.get("max_request_tokens") or 0)
        )
    if rounds:
        means = [
            float(row["metrics"].get("mean_request_tokens") or 0)
            for row in rounds
            if row.get("metrics", {}).get("mean_request_tokens") is not None
        ]
        metrics["mean_request_tokens"] = sum(means) / len(means) if means else 0
        generation = metrics["output_tokens"] + metrics["reasoning_tokens"]
        if metrics["model_time_s"] > 0:
            metrics["gen_tokens_per_s"] = generation / metrics["model_time_s"]
    return metrics


def round_summary(row: dict) -> dict:
    validation = row.get("score") or {}
    final = (row.get("final") or {}).get("score") or {}
    return {
        "round": row.get("round"),
        "budget_s": row.get("budget_s"),
        "wall_s": row.get("wall_s"),
        "watchdog": row.get("watchdog"),
        "completion": row.get("completion"),
        "build_ok": bool((row.get("build") or {}).get("build_ok")),
        "gate_ok": bool(validation.get("gate_ok")),
        "gate_pass": validation.get("gate_pass", 0),
        "gate_total": validation.get("gate_total", 0),
        "validation_score": validation.get("score", 0.0),
        "validation_decode": validation.get("decode_speedup", 0.0),
        "validation_encode": validation.get("encode_speedup", 0.0),
        "validation_eligible": bool(validation.get("eligible")),
        "final_score": final.get("score") if final else None,
        "final_decode": final.get("decode_speedup") if final else None,
        "final_encode": final.get("encode_speedup") if final else None,
        "final_eligible": bool(final.get("eligible")) if final else False,
        "artifacts": row.get("artifacts"),
        "source": row.get("source"),
    }


def run_one(model: str, *, rep: int, args: argparse.Namespace, variant: str | None) -> dict:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    variant_slug = slug(variant) if variant else "default"
    base_id = f"{timestamp}-htj2k-{slug(model)}-{variant_slug}-r{rep}"
    run_id, run_dir = claim_run_dir(base_id)
    workdir = run_dir / "workdir"
    data_dir = run_dir / "opencode-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir = resolve_config_dir(args.config, run_dir)
    copy_seed(workdir)

    env_names = config_env_names(config_dir) | set(args.env or [])
    if not args.no_preflight:
        available = list_models(provider_of(model), config_dir)
        if model not in available:
            raise SystemExit(
                f"error: '{model}' is not in `opencode models` for {config_dir}; "
                "run ./bench.py models or pass --no-preflight"
            )

    thinking, effort = model_thinking(model, variant)
    variant_label = variant or "default"
    manifest = load_manifest()
    plan = parse_round_plan(args)
    base_seed = int(args.fixture_seed or manifest.get("seed", 1))
    run_hash = int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16)
    validation_seed = base_seed ^ run_hash
    final_seed = base_seed ^ (run_hash ^ 0x5EED5EED)
    print(f"run {run_id}")
    print(f"  workdir {workdir}")
    print(f"  model   {model}")
    print(f"  config  {config_dir}")
    print(f"  thinking {thinking}" + (f" ({effort})" if effort else "") + f"  variant={variant_label}")
    print(f"  cpu     {args.cpu_set if not args.no_cpu_set else 'unpinned'}  network={args.network_policy}")
    print(f"  rounds  {' / '.join(f'{value:g}m' for value in plan)}")

    rounds: list[dict] = []
    session_id: str | None = None
    run_started = time.time()
    total_rounds = len(plan)

    for index, minutes in enumerate(plan):
        round_number = index + 1
        final_round = round_number == total_rounds
        round_dir = run_dir / "rounds" / f"round-{round_number}"
        round_dir.mkdir(parents=True, exist_ok=True)
        elapsed = time.time() - run_started
        remaining = max(0.0, float(args.timeout) - elapsed)
        if index > 0 and remaining < 30:
            print("  no time left for another round; stopping")
            break
        budget_s = max(10.0, min(float(minutes) * 60.0, remaining or float(minutes) * 60.0))
        idle_s = min(float(args.idle_timeout), budget_s)

        completion = workdir / "COMPLETION.md"
        if completion.exists() and not final_round:
            archived = round_dir / "COMPLETION.md"
            if archived.exists():
                archived.unlink()
            completion.replace(archived)

        if round_number == 1:
            prompt = write_round_one_prompt(workdir, plan, args)
        else:
            previous = rounds[-1] if rounds else {}
            prompt = write_next_round_prompt(
                workdir,
                round_number,
                total_rounds,
                plan,
                previous,
                max(0.0, float(args.timeout) - (time.time() - run_started)),
            )

        print(f"\nround {round_number}/{total_rounds}  budget {format_duration(budget_s)}")
        agent = start_agent(
            model,
            config_dir,
            workdir,
            data_dir,
            run_dir,
            env_names=env_names,
            args=args,
            round_number=round_number,
            prompt_file=prompt,
            session_id=session_id,
            continue_session=bool(index > 0 and not session_id),
            timeout_s=budget_s,
            idle_timeout_s=idle_s,
        )
        session_id = session_id or agent.get("session_id")
        log_path = Path(agent.get("log_path") or (run_dir / f"agent-round-{round_number}.log"))
        metrics = read_agent_usage(log_path, float(agent["wall_s"]))
        steps = count_log_steps(log_path)
        integrity = integrity_scan(workdir)
        if integrity["problems"]:
            print("  integrity problems:")
            for problem in integrity["problems"]:
                print(f"    - {problem}")

        build = build_app(
            run_dir,
            workdir,
            engine=args.engine,
            image=args.test_image,
            dry_run=args.dry_run,
            artifacts_dir=round_dir / "artifacts",
        )
        if integrity["problems"]:
            build["build_ok"] = False
        if build["build_ok"]:
            tests = run_tests(
                run_dir,
                engine=args.engine,
                image=args.test_image,
                dry_run=args.dry_run,
                manifest=manifest,
                fixture_seed=validation_seed,
                artifacts_dir=round_dir / "artifacts",
                require_single_cpu=not args.no_cpu_set,
                args=args,
            )
        else:
            tests = {
                "result_valid": False,
                "test_ok": False,
                "result": None,
                "error": "candidate build failed; suite not run",
                "exit_code": None,
            }
        score = score_run(
            tests,
            build,
            manifest=manifest,
            result_valid=tests.get("result_valid", False),
        )
        if integrity["problems"]:
            score["eligible"] = False
            score["score"] = 0.0
            score["gate_ok"] = False
            score["perf_valid"] = False
        snapshot_source(workdir, round_dir / "source.tar")
        rounds.append(
            {
                "round": round_number,
                "budget_s": budget_s,
                "wall_s": float(agent["wall_s"]),
                "watchdog": agent["watchdog"],
                "completion": bool(agent["completion"]),
                "agent_ok": bool(agent["agent_ok"]),
                "agent_exit_code": agent["agent_exit_code"],
                "session_id": session_id,
                "metrics": metrics,
                "steps": steps,
                "integrity": integrity,
                "build": build,
                "tests": tests,
                "score": score,
                "dir": str(round_dir),
                "artifacts": str(round_dir / "artifacts"),
                "source": str(round_dir / "source.tar"),
                "agent_log": str(log_path),
                "final": None,
            }
        )
        print(
            f"  checkpoint {round_number}: gates "
            f"{'pass' if score['gate_ok'] else 'FAIL'} "
            f"({score['gate_pass']}/{score['gate_total']}), "
            f"decode x{score['decode_speedup']:.3f}, encode x{score['encode_speedup']:.3f}, "
            f"validation score {score['score']:.1f}"
        )
        if completion.exists() and not final_round:
            archived = round_dir / "COMPLETION.md"
            if archived.exists():
                archived.unlink()
            completion.replace(archived)

    if args.dry_run:
        print("dry run complete; no score or report written")
        shutil.rmtree(run_dir, ignore_errors=True)
        return {}

    if not rounds:
        raise SystemExit("error: no rounds were executed")

    # Grade every eligible checkpoint on a held-out final corpus and keep the best.
    best: dict | None = None
    for row in rounds:
        artifacts = Path(row["artifacts"])
        candidate = artifacts / "bin" / "candidate"
        seed_bin = artifacts / "bin" / "seed"
        if not candidate.exists() or not seed_bin.exists():
            continue
        final_dir = Path(row["dir"]) / "final"
        (final_dir / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, final_dir / "bin" / "candidate")
        shutil.copy2(seed_bin, final_dir / "bin" / "seed")
        final_tests = run_tests(
            run_dir,
            engine=args.engine,
            image=args.test_image,
            dry_run=False,
            manifest=manifest,
            fixture_seed=final_seed,
            artifacts_dir=final_dir,
            require_single_cpu=not args.no_cpu_set,
            args=args,
        )
        final_score = score_run(
            final_tests,
            row["build"],
            manifest=manifest,
            result_valid=final_tests.get("result_valid", False),
        )
        if row.get("integrity", {}).get("problems"):
            final_score["eligible"] = False
            final_score["score"] = 0.0
            final_score["gate_ok"] = False
            final_score["perf_valid"] = False
        row["final"] = {"tests": final_tests, "score": final_score}
        print(
            f"  final grade round {row['round']}: "
            f"score {final_score['score']:.1f}, "
            f"gates {'pass' if final_score['gate_ok'] else 'FAIL'}, "
            f"decode x{final_score['decode_speedup']:.3f}, "
            f"encode x{final_score['encode_speedup']:.3f}"
        )
        if final_score["eligible"] and (
            best is None or final_score["score"] > best["final"]["score"]["score"]
        ):
            best = row

    if best is None:
        best = max(
            rounds,
            key=lambda row: float((row.get("final") or {}).get("score", {}).get("score") or row["score"]["score"]),
        )
    chosen_final = (best.get("final") or {}).get("score") or best.get("score") or {}
    chosen_tests = (best.get("final") or {}).get("tests") or best.get("tests") or {}
    chosen_build = best.get("build") or {}
    chosen_dir = Path(best["dir"])
    chosen_metrics = best.get("metrics") or {}

    metrics = aggregate_agent_metrics(rounds)
    metrics["compactions"] = count_compactions(data_dir)
    metrics["self_test_runs"] = sum(int(row.get("metrics", {}).get("self_test_runs") or 0) for row in rounds)
    metrics["max_request_tokens"] = max(
        int(row.get("metrics", {}).get("max_request_tokens") or 0) for row in rounds
    )
    metrics["mean_request_tokens"] = sum(
        float(row.get("metrics", {}).get("mean_request_tokens") or 0) for row in rounds
    ) / len(rounds)
    metrics["gen_tokens_per_s"] = (
        (metrics["output_tokens"] + metrics["reasoning_tokens"]) / metrics["model_time_s"]
        if metrics["model_time_s"] > 0
        else None
    )
    metrics["metrics_source"] = "round-logs"

    final_environment = (chosen_tests.get("result") or {}).get("environment") or {}
    final_score = chosen_final
    final_build = {
        "build_ok": bool(chosen_build.get("build_ok")),
        "seed_build_ok": bool(chosen_build.get("seed_build_ok")),
        "typecheck_ok": bool(chosen_build.get("typecheck_ok")),
        "binary": bool(chosen_build.get("binary")),
    }
    summaries = [round_summary(row) for row in rounds]
    meta = {
        "run_id": run_id,
        "timestamp": timestamp,
        "model": model,
        "provider": provider_of(model),
        "hardware": (getattr(args, "hardware", None) or "").strip()[:300],
        "rep": rep,
        "variant": variant_label,
        "thinking": thinking,
        "reasoning_effort": effort,
        "suite_version": manifest.get("suite_version"),
        "fixture_seed": final_seed,
        "validation_seed": validation_seed,
        "final_seed": final_seed,
        "image": args.test_image,
        "cpu_set": "none" if args.no_cpu_set else str(args.cpu_set),
        "cpu_count": int(final_environment.get("affinity") or effective_cpu_count()),
        "network_policy": args.network_policy,
        "rounds": summaries,
        "rounds_count": len(rounds),
        "round_plan": args.round_plan if not args.no_rounds else "single",
        "best_round": best.get("round"),
        "confidence": "single-run",
        "budget_wall_cap_s": args.timeout,
        "budget_idle_timeout_s": args.idle_timeout,
        "wall_s": metrics["span_s"],
        "compactions": metrics["compactions"],
        "watchdog": rounds[-1].get("watchdog"),
        "round_watchdogs": [row.get("watchdog") for row in rounds],
        "completion": rounds[-1].get("completion", False),
        "outcome": classify_outcome(
            watchdog=str(rounds[-1].get("watchdog") or "exit"),
            completion=bool(rounds[-1].get("completion")),
            files_changed=count_changed_files(workdir),
            agent_ok=bool(rounds[-1].get("agent_ok")),
        ),
        "steps": sum(int(row.get("steps") or 0) for row in rounds),
        **metrics,
        "files_changed": count_changed_files(workdir),
        "agent_ok": bool(rounds[-1].get("agent_ok")),
        "agent_exit_code": rounds[-1].get("agent_exit_code"),
        "build_ok": bool(final_build.get("build_ok")),
        "seed_build_ok": bool(final_build.get("seed_build_ok")),
        "typecheck_ok": bool(final_build.get("typecheck_ok")),
        "test_ok": bool(chosen_tests.get("test_ok")),
        "test_error": chosen_tests.get("error"),
        "result_valid": bool(final_score.get("result_valid")),
        "gate_ok": bool(final_score.get("gate_ok")),
        "perf_valid": bool(final_score.get("perf_valid")),
        "eligible": bool(final_score.get("eligible")),
        "score": float(final_score.get("score") or 0.0),
        "quality": float(final_score.get("quality") or 0.0),
        "decode_speedup": float(final_score.get("decode_speedup") or 0.0),
        "encode_speedup": float(final_score.get("encode_speedup") or 0.0),
        "target_decode": float(final_score.get("target_decode") or 0.0),
        "target_encode": float(final_score.get("target_encode") or 0.0),
        "fatal": int(final_score.get("fatal") or 0),
        "major": int(final_score.get("major") or 0),
        "minor": int(final_score.get("minor") or 0),
        "regressions": int(final_score.get("regressions") or 0),
        "subcases": final_score.get("subcases"),
        "gates": final_score.get("gates"),
        "decode": final_score.get("decode"),
        "encode": final_score.get("encode"),
        "integrity": rounds[-1].get("integrity"),
        "candidate_rss_bytes": manifest["limits"].get("candidate_rss_bytes"),
        "report": str(run_dir / "report.md"),
        "agent_log": str(rounds[0].get("agent_log") or (run_dir / "agent-round-1.log")),
        "agent_logs": [str(row.get("agent_log") or (run_dir / f"agent-round-{row['round']}.log")) for row in rounds],
        "build_log": str(chosen_dir / "artifacts" / ".." / "build-run.log"),
        "test_log": str(chosen_dir / "final" / "test.log"),
    }
    meta["agent_log"] = str(run_dir / "agent-round-1.log")
    meta["build_log"] = str(chosen_dir / "build-run.log")
    meta["test_log"] = str(chosen_dir / "final" / "test.log")
    meta["decode_seed_mp_s"] = _mean_metric(final_score.get("decode") or [], "seed_mp_s")
    meta["decode_candidate_mp_s"] = _mean_metric(final_score.get("decode") or [], "candidate_mp_s")
    meta["encode_seed_mp_s"] = _mean_metric(final_score.get("encode") or [], "seed_mp_s")
    meta["encode_candidate_mp_s"] = _mean_metric(final_score.get("encode") or [], "candidate_mp_s")

    write_run_report(run_dir, meta, final_score)
    (run_dir / "score.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    append_result(meta)
    try:
        write_leaderboard()
    except Exception as exc:  # noqa: BLE001
        eprint(f"warning: could not update leaderboard: {exc}")
    print_summary(meta, final_score)
    return meta


def _mean_metric(items: list[dict], key: str) -> float | None:
    values = [float(item[key]) for item in items if item.get("valid") and item.get(key) is not None]
    return sum(values) / len(values) if values else None


def load_manifest() -> dict:
    path = TESTS_DIR / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: cannot read {path}: {exc}")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise SystemExit(f"error: {path} schema must be {MANIFEST_SCHEMA!r}")
    if manifest.get("suite_version") != SUITE_VERSION:
        raise SystemExit(f"error: {path} suite_version must be {SUITE_VERSION!r}")
    return manifest


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_build(args: argparse.Namespace) -> int:
    if not engine_available(args.engine, args.dry_run):
        eprint(f"error: {args.engine} is not available; start Docker/OrbStack or pass --engine")
        return 2
    jobs = [
        (
            ROOT / "docker" / "agent.Dockerfile",
            args.agent_image,
            [
                "--build-arg",
                f"RUST_VERSION={args.rust_version}",
                "--build-arg",
                f"OPENCODE_VERSION={args.opencode_version}",
            ],
        ),
        (
            ROOT / "docker" / "test.Dockerfile",
            args.test_image,
            ["--build-arg", f"RUST_VERSION={args.rust_version}"],
        ),
    ]
    for dockerfile, image, extra in jobs:
        command = [
            args.engine,
            "build",
            "-f",
            str(dockerfile),
            "-t",
            image,
            *extra,
            str(ROOT),
        ]
        print("+ " + " ".join(shlex.quote(part) for part in command))
        if not args.dry_run:
            result = subprocess.run(command, check=False)
            if result.returncode != 0:
                return result.returncode
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    config_dir = resolve_config_dir(args.config, RUNS_DIR / f"_models-{uuid.uuid4().hex[:8]}")
    models = list_models(args.provider, config_dir)
    if not models:
        eprint("error: no models found")
        return 1
    counts: dict[str, int] = {}
    for model in models:
        provider = provider_of(model)
        counts[provider] = counts.get(provider, 0) + 1
    for provider, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"{provider}  ({count})")
        for model in models:
            if provider_of(model) == provider:
                print(f"  {model}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    problems: list[str] = []
    if not PROMPT_PATH.exists():
        problems.append(f"missing {PROMPT_PATH}")
    if not (ROOT / "docker" / "agent.Dockerfile").exists():
        problems.append("missing docker/agent.Dockerfile")
    if not (ROOT / "docker" / "test.Dockerfile").exists():
        problems.append("missing docker/test.Dockerfile")
    if not (SEED_DIR / "Cargo.lock").exists():
        problems.append("missing seed Cargo.lock")
    if not (SEED_DIR / "vendor").exists():
        problems.append("missing seed vendor/")
    if not (SEED_DIR / "crates" / "htj2k-cli" / "src" / "main.rs").exists():
        problems.append("missing seed CLI")
    if not (SEED_DIR / "corpus" / "public" / "manifest.json").exists():
        problems.append("missing public fixtures")
    if not (TESTS_DIR / "runner.py").exists():
        problems.append("missing hidden runner")
    if not (TESTS_DIR / "manifest.json").exists():
        problems.append("missing hidden manifest")
    if not (TESTS_DIR / "fixtures" / "ABDOM_1_htj2k.j2c").exists():
        problems.append("missing DICOM fixture")
    if not (TESTS_DIR / "oracle" / "OpenJPH" / "CMakeLists.txt").exists():
        problems.append("missing vendored OpenJPH oracle source")
    try:
        manifest = load_manifest()
    except SystemExit as exc:
        problems.append(str(exc))
        manifest = {}
    if manifest:
        if not manifest.get("correctness"):
            problems.append("manifest has no correctness cases")
        if not manifest.get("perf", {}).get("decode"):
            problems.append("manifest has no decode perf workloads")
        if not manifest.get("perf", {}).get("encode"):
            problems.append("manifest has no encode perf workloads")
        for name, target in (manifest.get("targets") or {}).items():
            try:
                value = float(target)
            except (TypeError, ValueError):
                problems.append(f"manifest target {name} is not numeric")
            else:
                if value < 1.05:
                    problems.append(f"manifest target {name} must be >= 1.05")
        if int(manifest["limits"].get("perf_repetitions", 0)) < 3:
            problems.append("manifest perf_repetitions must be >= 3")
    for path in SEED_DIR.rglob("build.rs"):
        if "vendor" in path.parts:
            continue
        problems.append(f"seed contains a build script: {path.relative_to(SEED_DIR)}")
    seed_scan = integrity_scan(SEED_DIR)
    problems.extend(seed_scan["problems"])
    prompt = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else ""
    for needle in (
        "4 hours",
        "single-component",
        "lossless",
        "OpenJPH",
        "COMPLETION.md",
        "one CPU",
        "checkpoint",
        "ROUND",
    ):
        if needle.lower() not in prompt.lower():
            problems.append(f"PROMPT.md is missing {needle!r}")
    if not args.no_docker:
        if not engine_available(args.engine, args.dry_run):
            problems.append(f"{args.engine} is not available")
        else:
            if not image_exists(args.engine, args.agent_image):
                problems.append(f"missing image {args.agent_image}; run ./bench.py build")
            if not image_exists(args.engine, args.test_image):
                problems.append(f"missing image {args.test_image}; run ./bench.py build")
            else:
                if not args.no_cpu_set:
                    for image, label in (
                        (args.test_image, "test"),
                        (args.agent_image, "agent"),
                    ):
                        probe = subprocess.run(
                            [
                                args.engine,
                                "run",
                                "--rm",
                                *cpu_set_flags(args),
                                image,
                                "bash",
                                "-c",
                                "test \"$(nproc)\" -eq 1 && "
                                "python3 -c 'import os,sys; sys.exit(0 if len(os.sched_getaffinity(0)) == 1 else 1)'",
                            ],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        if probe.returncode != 0:
                            problems.append(
                                f"{label} image is not pinned to one CPU with --cpuset-cpus {args.cpu_set}; "
                                "the engine may not support cpuset"
                            )
                else:
                    print("warning: --no-cpu-set is for debugging only; official runs require one pinned CPU")
                probe = subprocess.run(
                    [
                        args.engine,
                        "run",
                        "--rm",
                        *cpu_set_flags(args),
                        args.test_image,
                        "bash",
                        "-c",
                        "test -x /opt/oracle/bin/oracle && /opt/oracle/bin/oracle >/dev/null 2>&1; "
                        "test $? -eq 2",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if probe.returncode != 0:
                    problems.append("test image oracle is missing or does not start")
                probe = subprocess.run(
                    [
                        args.engine,
                        "run",
                        "--rm",
                        *cpu_set_flags(args),
                        args.agent_image,
                        "bash",
                        "-c",
                        "command -v cargo >/dev/null && command -v python3 >/dev/null "
                        "&& command -v opencode >/dev/null && test ! -e /opt/oracle",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if probe.returncode != 0:
                    problems.append("agent image is missing cargo/python3/opencode or contains /opt/oracle")
    if problems:
        print("check: FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("check: ok")
    if manifest:
        gates = (
            len(manifest["correctness"]) * 2
            + len(manifest.get("decode_extra", []))
            + 1
            + 5
            + 1
        )
        print(
            f"  suite {manifest['suite_version']} · seed {manifest['seed']} · "
            f"{gates} gate subcases · targets "
            f"decode x{manifest['targets']['decode']} / encode x{manifest['targets']['encode']}"
        )
        print(
            f"  cpu-set {args.cpu_set if not args.no_cpu_set else 'unpinned'} · "
            f"agent network {args.network_policy} · grader network none"
        )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not args.model:
        eprint("error: --model provider/model is required")
        return 2
    if args.variant == "all":
        variants: list[str | None] = [item[0] for item in ordered_variants(args.model)] or [None]
    else:
        variants = [args.variant]
    failed = 0
    for variant in variants:
        for rep in range(1, args.reps + 1):
            try:
                run_one(args.model, rep=rep, args=args, variant=variant)
            except (Exception, SystemExit) as exc:
                failed += 1
                eprint(f"error: run failed for {args.model} variant={variant or 'default'}: {exc}")
                if not args.keep_going:
                    return 1
    if failed:
        eprint(f"{failed} run(s) failed")
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    rows = read_result_rows()
    if not rows:
        print("no runs")
        return 0
    for row in rows[-args.limit :]:
        print(
            f"{row.get('run_id', '?'):50s} "
            f"score {score_row(row):6.1f}  "
            f"decode x{_fmt(row.get('decode_speedup'), 3):>7}  "
            f"encode x{_fmt(row.get('encode_speedup'), 3):>7}  "
            f"{'eligible' if str(row.get('eligible')).lower() == 'true' else 'ineligible'}"
        )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    rows = read_result_rows()
    if not rows:
        print("no runs")
        return 0
    if not args.aggregate:
        print(f"{'model':40s} {'score':>6s} {'decode':>8s} {'encode':>8s} {'wall':>7s} {'tokens':>8s}")
        for row in rows[-args.limit :]:
            print(
                f"{row.get('model', '?'):40s} "
                f"{score_row(row):6.1f} "
                f"{_fmt(row.get('decode_speedup'), 3):>8} "
                f"{_fmt(row.get('encode_speedup'), 3):>8} "
                f"{format_duration(row.get('wall_s')):>7s} "
                f"{format_tokens(row.get('total_tokens')):>8s}"
            )
        print("")
    print("models:")
    print(
        f"{'model':40s} {'variant':10s} {'runs':>4s} {'median':>7s} "
        f"{'best':>6s} {'gate':>5s} {'decode':>7s} {'encode':>7s} {'conf':>11s}"
    )
    for entry in aggregate_entries(rows):
        print(
            f"{entry['model']:40s} {entry['variant']:10s} {entry['runs']:4d} "
            f"{entry['median_score']:7.1f} {entry['best_score']:6.1f} "
            f"{entry['gate_rate'] * 100:4.0f}% "
            f"{_fmt(entry['median_decode'], 3):>7s} {_fmt(entry['median_encode'], 3):>7s} "
            f"{entry['confidence']:>11s}"
        )
    return 0


def cmd_leaderboard(args: argparse.Namespace) -> int:
    path = write_leaderboard()
    print(path)
    if args.write:
        return 0
    url = f"http://127.0.0.1:{args.port}/{path.name}"
    handler = lambda *handler_args, **kwargs: _StaticHandler(  # noqa: E731
        *handler_args, directory=str(ROOT), **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"serving {url}")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


class _StaticHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, directory: str, **kwargs):
        self.directory = Path(directory)
        super().__init__(*args, **kwargs)

    def do_GET(self):  # noqa: N802
        name = self.path.split("?", 1)[0].lstrip("/") or "index.html"
        path = (self.directory / name).resolve()
        if not str(path).startswith(str(self.directory.resolve())) or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):  # noqa: D401
        pass


def cmd_rescore(args: argparse.Namespace) -> int:
    run_id = getattr(args, "run_id", None) or args.run
    if not run_id:
        eprint("error: rescore needs RUN_ID")
        return 2
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        eprint(f"error: run not found: {run_dir}")
        return 2
    meta_path = run_dir / "score.json"
    if not meta_path.exists():
        eprint(f"error: {meta_path} missing")
        return 2
    meta = json.loads(meta_path.read_text())
    workdir = run_dir / "workdir"
    manifest = load_manifest()
    rescore_dir = run_dir / "rescore"
    build = build_app(
        run_dir,
        workdir,
        engine=args.engine,
        image=args.test_image,
        dry_run=args.dry_run,
        artifacts_dir=rescore_dir,
    )
    tests = run_tests(
        run_dir,
        engine=args.engine,
        image=args.test_image,
        dry_run=args.dry_run,
        manifest=manifest,
        fixture_seed=int(meta.get("final_seed") or meta.get("fixture_seed") or manifest["seed"]),
        artifacts_dir=rescore_dir,
        require_single_cpu=not args.no_cpu_set,
        args=args,
    )
    score = score_run(
        tests,
        build,
        manifest=manifest,
        result_valid=tests.get("result_valid", False),
    )
    meta.update(
        {
            "build_ok": build["build_ok"],
            "seed_build_ok": build.get("seed_build_ok", False),
            "result_valid": score["result_valid"],
            "gate_ok": score["gate_ok"],
            "perf_valid": score["perf_valid"],
            "eligible": score["eligible"],
            "score": score["score"],
            "quality": score["quality"],
            "decode_speedup": score["decode_speedup"],
            "encode_speedup": score["encode_speedup"],
            "fatal": score["fatal"],
            "gates": score["gates"],
            "decode": score["decode"],
            "encode": score["encode"],
            "test_ok": tests["test_ok"],
            "test_error": tests["error"],
            "cpu_set": "none" if args.no_cpu_set else str(args.cpu_set),
            "network_policy": args.network_policy,
        }
    )
    meta["test_log"] = str(rescore_dir / "test.log")
    meta["build_log"] = str(rescore_dir / "build-run.log")
    write_run_report(run_dir, meta, score)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    append_result(meta)
    write_leaderboard()
    print_summary(meta, score)
    return 0 if score["eligible"] else 1


def cmd_preview(args: argparse.Namespace) -> int:
    run_id = getattr(args, "run_id", None) or args.run
    if not run_id:
        eprint("error: preview needs RUN_ID")
        return 2
    run_dir = RUNS_DIR / run_id
    meta_path = run_dir / "score.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    candidates = []
    best_round = meta.get("best_round")
    if best_round:
        candidates.append(run_dir / "rounds" / f"round-{best_round}" / "artifacts" / "bin" / "candidate")
    candidates.append(run_dir / "artifacts" / "bin" / "candidate")
    candidates.append(run_dir / "rescore" / "bin" / "candidate")
    binary = next((path for path in candidates if path.exists()), None)
    if binary is None:
        eprint(f"error: no built candidate in {run_dir}")
        return 2
    print(f"candidate binary: {binary}")
    print(f"report: {run_dir / 'report.md'}")
    if best_round:
        print(f"results: {run_dir / 'rounds' / f'round-{best_round}' / 'final' / 'results.json'}")
        print(f"rounds: {run_dir / 'rounds'}")
    else:
        print(f"results: {run_dir / 'artifacts' / 'results.json'}")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    rows = read_result_rows()
    changed = 0
    for row in rows:
        run_dir = RUNS_DIR / str(row.get("run_id"))
        if not run_dir.exists():
            continue
        logs = sorted(run_dir.glob("agent-round-*.log"))
        if not logs:
            legacy = run_dir / "agent.log"
            logs = [legacy] if legacy.exists() else []
        if not logs:
            continue
        total_wall = _float(row.get("wall_s")) or 0.0
        per_log = total_wall / len(logs) if total_wall else 0.0
        merged: dict[str, float] = {}
        max_request = 0
        for log in logs:
            metrics = read_agent_usage(log, per_log)
            max_request = max(max_request, int(metrics.get("max_request_tokens") or 0))
            for key, value in metrics.items():
                if isinstance(value, (int, float)) and key not in {
                    "max_request_tokens",
                    "mean_request_tokens",
                    "gen_tokens_per_s",
                    "metrics_version",
                }:
                    merged[key] = merged.get(key, 0.0) + float(value)
        merged["max_request_tokens"] = max_request
        if logs:
            means = [
                float((read_agent_usage(log, per_log)).get("mean_request_tokens") or 0)
                for log in logs
            ]
            merged["mean_request_tokens"] = sum(means) / len(means)
        if merged.get("model_time_s"):
            merged["gen_tokens_per_s"] = (
                merged.get("output_tokens", 0.0) + merged.get("reasoning_tokens", 0.0)
            ) / merged["model_time_s"]
        for key, value in merged.items():
            if key in CSV_FIELDS and row.get(key) in (None, ""):
                row[key] = value
                changed += 1
    if changed:
        write_result_rows(rows)
    print(f"backfilled {changed} fields across {len(rows)} runs")
    return 0


def submission_from_score(meta: dict) -> dict:
    return {
        "schema": SUBMISSION_SCHEMA,
        "run_id": meta.get("run_id"),
        "timestamp": meta.get("timestamp"),
        "model": meta.get("model"),
        "provider": meta.get("provider"),
        "hardware": meta.get("hardware"),
        "variant": meta.get("variant", "default"),
        "thinking": meta.get("thinking"),
        "reasoning_effort": meta.get("reasoning_effort"),
        "suite_version": meta.get("suite_version"),
        "fixture_seed": meta.get("fixture_seed"),
        "validation_seed": meta.get("validation_seed"),
        "final_seed": meta.get("final_seed") or meta.get("fixture_seed"),
        "cpu_set": meta.get("cpu_set"),
        "cpu_count": meta.get("cpu_count"),
        "network_policy": meta.get("network_policy"),
        "rounds": [
            {key: value for key, value in row.items() if key not in {"artifacts", "source"}}
            for row in (meta.get("rounds") or [])
        ],
        "rounds_count": meta.get("rounds_count"),
        "best_round": meta.get("best_round"),
        "confidence": meta.get("confidence"),
        "score": round(float(meta.get("score") or 0.0), 2),
        "eligible": bool(meta.get("eligible")),
        "gate_ok": bool(meta.get("gate_ok")),
        "perf_valid": bool(meta.get("perf_valid")),
        "decode_speedup": meta.get("decode_speedup"),
        "encode_speedup": meta.get("encode_speedup"),
        "target_decode": meta.get("target_decode"),
        "target_encode": meta.get("target_encode"),
        "decode_seed_mp_s": meta.get("decode_seed_mp_s"),
        "decode_candidate_mp_s": meta.get("decode_candidate_mp_s"),
        "encode_seed_mp_s": meta.get("encode_seed_mp_s"),
        "encode_candidate_mp_s": meta.get("encode_candidate_mp_s"),
        "gates": meta.get("gates"),
        "wall_s": meta.get("wall_s"),
        "model_time_s": meta.get("model_time_s"),
        "input_tokens": meta.get("input_tokens"),
        "output_tokens": meta.get("output_tokens"),
        "reasoning_tokens": meta.get("reasoning_tokens"),
        "total_tokens": meta.get("total_tokens"),
        "watchdog": meta.get("watchdog"),
        "outcome": meta.get("outcome"),
        "verified": False,
    }


def validate_submission(data: object) -> list[str]:
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["submission must be an object"]
    if data.get("schema") != SUBMISSION_SCHEMA:
        problems.append(f"schema must be {SUBMISSION_SCHEMA!r}")
    for key in ("run_id", "model", "suite_version", "score", "eligible"):
        if data.get(key) in (None, ""):
            problems.append(f"missing {key}")
    if data.get("suite_version") != SUITE_VERSION:
        problems.append(f"suite_version must be {SUITE_VERSION!r}")
    if data.get("network_policy") not in (None, "open", "offline"):
        problems.append("network_policy must be 'open' or 'offline'")
    try:
        score = float(data.get("score") or 0)
    except (TypeError, ValueError):
        problems.append("score must be numeric")
    else:
        if not 0 <= score <= 200:
            problems.append("score must be in 0..200")
    for key in ("decode_speedup", "encode_speedup"):
        try:
            value = float(data.get(key) or 0)
        except (TypeError, ValueError):
            problems.append(f"{key} must be numeric")
        else:
            if value <= 0:
                problems.append(f"{key} must be positive")
    return problems


def cmd_submit(args: argparse.Namespace) -> int:
    if args.check:
        path = Path(args.check)
        data = json.loads(path.read_text())
        problems = validate_submission(data)
        if problems:
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print("submission: ok")
        return 0
    run_id = getattr(args, "run_id", None) or args.run
    if not run_id:
        eprint("error: submit needs RUN_ID (or --check FILE)")
        return 2
    run_dir = RUNS_DIR / run_id
    score_path = run_dir / "score.json"
    if not score_path.exists():
        eprint(f"error: {score_path} missing; run or rescore the run first")
        return 2
    meta = json.loads(score_path.read_text())
    submission = submission_from_score(meta)
    problems = validate_submission(submission)
    if problems:
        eprint("submission invalid:")
        for problem in problems:
            eprint(f"  - {problem}")
        return 1
    block = "```json\n" + json.dumps(submission, indent=2) + "\n```"
    if args.print or not args.gh:
        print(block)
    if args.gh:
        body = (
            "## opencode-bench-htj2k result\n\n"
            + block
            + "\n\nSelf-reported unless a maintainer adds `verified`.\n"
        )
        command = [
            "gh",
            "issue",
            "create",
            "--repo",
            args.repo,
            "--label",
            BENCHMARK_LABEL,
            "--title",
            f"[result] {submission['model']} · score {submission['score']} · {submission['run_id']}",
            "--body",
            body,
        ]
        print("+ " + " ".join(shlex.quote(part) for part in command))
        return subprocess.run(command, check=False).returncode
    return 0


def cmd_site(args: argparse.Namespace) -> int:
    path = Path(args.issues)
    issues = json.loads(path.read_text())
    rows = []
    for issue in issues:
        labels = {label.get("name") for label in issue.get("labels", [])}
        if BENCHMARK_LABEL not in labels or HIDDEN_LABEL in labels:
            continue
        body = issue.get("body") or ""
        match = re.search(r"```json\s*(\{.*?\})\s*```", body, re.DOTALL)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if data.get("schema") != SUBMISSION_SCHEMA:
            continue
        data["_issue"] = issue.get("number")
        if VERIFIED_LABEL in labels:
            data["verified"] = True
        rows.append(data)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(
        render_leaderboard(
            [
                {
                    "run_id": row.get("run_id"),
                    "model": row.get("model"),
                    "variant": row.get("variant"),
                    "score": row.get("score"),
                    "decode_speedup": row.get("decode_speedup"),
                    "encode_speedup": row.get("encode_speedup"),
                    "eligible": row.get("eligible"),
                    "gate_ok": row.get("gate_ok"),
                    "perf_valid": row.get("perf_valid"),
                    "network_policy": row.get("network_policy"),
                    "cpu_set": row.get("cpu_set"),
                    "confidence": row.get("confidence"),
                    "hardware": row.get("hardware"),
                    "wall_s": row.get("wall_s"),
                    "total_tokens": row.get("total_tokens"),
                    "timestamp": row.get("timestamp"),
                }
                for row in rows
            ]
        ),
        encoding="utf-8",
    )
    print(f"wrote {output / 'index.html'} ({len(rows)} submissions)")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default=DEFAULT_ENGINE)
    parser.add_argument("--agent-image", default=DEFAULT_AGENT_IMAGE)
    parser.add_argument("--test-image", default=DEFAULT_TEST_IMAGE)
    parser.add_argument("--config", help="OpenCode config directory or file")
    parser.add_argument("--cpu-set", default=DEFAULT_CPU_SET,
                        help="CPU affinity set for agent and grader (default: 0)")
    parser.add_argument("--no-cpu-set", action="store_true",
                        help="disable CPU pinning (debugging only)")
    parser.add_argument("--network-policy", default=DEFAULT_NETWORK_POLICY,
                        choices=("open", "offline"),
                        help="agent network policy label; grading is always offline")
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="build agent and test images")
    p.add_argument("--opencode-version", default="2.2.5")
    p.add_argument("--rust-version", default="1.83")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("check", help="validate prompt, seed, manifest and images")
    p.add_argument("--no-docker", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("models", help="list models from the local OpenCode config")
    p.add_argument("--provider")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("run", help="run the agent and score it")
    p.add_argument("--model", required=True)
    p.add_argument("--variant")
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--timeout", type=int, default=4 * 3600)
    p.add_argument("--idle-timeout", type=int, default=3600)
    p.add_argument("--round-plan", default="75,75,45",
                   help="comma-separated per-round minutes; last round is finalize")
    p.add_argument("--no-rounds", action="store_true",
                   help="single agent run using --timeout instead of checkpoints")
    p.add_argument("--fixture-seed", type=int)
    p.add_argument("--agent")
    p.add_argument("--agent-cmd", help="override the agent command (test/debug)")
    p.add_argument("--pure", action="store_true")
    p.add_argument("--no-preflight", action="store_true")
    p.add_argument("--keep-going", action="store_true")
    p.add_argument("--hardware", default="")
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="show recent runs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="print a score table")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--aggregate", action="store_true", help="show model summary only")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("leaderboard", help="write and serve the leaderboard")
    p.add_argument("--write", action="store_true")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_leaderboard)

    p = sub.add_parser("rescore", help="rebuild and rerun a saved workdir")
    p.add_argument("run", nargs="?")
    p.add_argument("--run", dest="run_id")
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("preview", help="show a run's artifacts")
    p.add_argument("run", nargs="?")
    p.add_argument("--run", dest="run_id")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("backfill", help="recompute effort metrics from logs")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("submit", help="build a community submission")
    p.add_argument("run", nargs="?")
    p.add_argument("--run", dest="run_id")
    p.add_argument("--print", action="store_true")
    p.add_argument("--gh", action="store_true")
    p.add_argument("--repo", default=PUBLIC_REPO)
    p.add_argument("--check")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("site", help="render a leaderboard from issue JSON")
    p.add_argument("--issues", required=True)
    p.add_argument("--output", default=str(DOCS_DIR))
    p.set_defaults(func=cmd_site)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
