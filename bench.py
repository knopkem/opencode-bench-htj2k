#!/usr/bin/env python3
"""opencode-bench-htj2k: one-command local-model benchmark for OpenCode.

The task is to make a working pure-Rust HTJ2K codec faster at lossless encode
and decode while preserving standard conformance.  Everything that touches the
task runs in Docker:

  * the agent container sees only a fresh copy of the seed workdir;
  * the hidden suite, corpus generator and independent C++ OpenJPH oracle are
    baked into the separate evaluator image and run only after the agent exits;
  * the evaluator container has no network and one pinned CPU;
  * the agent run is checkpointed, and the best eligible checkpoint is graded
    on a held-out final corpus.

Usage:
    ./bench.py build                              # public agent image
    ./bench.py build-eval --tests-dir <private>   # evaluator image (maintainers)
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
import ipaddress
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
import urllib.parse
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
PROMPT_PATH = TASK_DIR / "PROMPT.md"
RUNS_DIR = ROOT / "runs"
RESULTS_PATH = ROOT / "results-opencode-bench-htj2k.csv"
REPORT_PATH = ROOT / "report-opencode-bench-htj2k.md"
LEADERBOARD_PATH = ROOT / "opencode-bench-htj2k-leaderboard.html"
SUBMISSIONS_DIR = ROOT / "submissions"
DOCS_DIR = ROOT / "docs"

PUBLIC_AGENT_IMAGE = "ghcr.io/knopkem/opencode-bench-htj2k-agent:edge"
PUBLIC_EVAL_IMAGE = "ghcr.io/knopkem/opencode-bench-htj2k-eval:edge"
DEFAULT_AGENT_IMAGE = os.environ.get("OPENCODE_BENCH_AGENT_IMAGE", PUBLIC_AGENT_IMAGE)
DEFAULT_TEST_IMAGE = os.environ.get("OPENCODE_BENCH_EVAL_IMAGE") or os.environ.get(
    "OPENCODE_BENCH_TEST_IMAGE", PUBLIC_EVAL_IMAGE
)
DEFAULT_ENGINE = os.environ.get("OPENCODE_BENCH_ENGINE", "docker")
DEFAULT_CPU_SET = os.environ.get("OPENCODE_BENCH_CPUSET", "0")
DEFAULT_NETWORK_POLICY = os.environ.get("OPENCODE_BENCH_NETWORK", "open")
OPENCODE = os.environ.get("OPENCODE_BENCH_OPENCODE", "opencode")
EGRESS_PROXY_PATH = ROOT / "docker" / "egress_proxy.py"
OPENCODE_INFRA_ENDPOINTS = {
    "models.dev": [443],
    "models.opencode.ai": [443],
    "registry.npmjs.org": [443],
}
SDK_ENDPOINTS = {
    "@ai-sdk/anthropic": "https://api.anthropic.com",
    "@ai-sdk/cerebras": "https://api.cerebras.ai",
    "@ai-sdk/cohere": "https://api.cohere.com",
    "@ai-sdk/deepinfra": "https://api.deepinfra.com",
    "@ai-sdk/google": "https://generativelanguage.googleapis.com",
    "@ai-sdk/groq": "https://api.groq.com",
    "@ai-sdk/mistral": "https://api.mistral.ai",
    "@ai-sdk/openai": "https://api.openai.com",
    "@ai-sdk/xai": "https://api.x.ai",
    "@openrouter/ai-sdk-provider": "https://openrouter.ai",
}
EVALUATION_SOURCE_SUFFIXES = (
    "github.com",
    "githubusercontent.com",
    "ghcr.io",
    "docker.com",
    "docker.io",
)
PROTECTED_AGENT_ENV = {
    "ALL_PROXY",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "OPENCODE_BENCH_MODEL",
    "OPENCODE_BENCH_VARIANT",
    "OPENCODE_CONFIG_CONTENT",
    "XDG_CONFIG_HOME",
}

TEST_SCHEMA = "opencode-bench/htj2k-tests@1"
MANIFEST_SCHEMA = "opencode-bench/htj2k-manifest@1"
SUITE_VERSION = "htj2k-v1"
SUBMISSION_SCHEMA = "opencode-bench/htj2k-result@1"
PUBLIC_REPO = "knopkem/opencode-bench-htj2k"
PRIVATE_EVAL_REPO = "knopkem/opencode-bench-htj2k-eval"
EVALUATOR_MANIFEST = "/opt/private/manifest.json"
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
    r"(?:cargo\s+(?:test|bench)|scripts/bench\.py|python3?\s+scripts/bench\.py)",
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
    "self_test_failures",
    "build_tool_s",
    "wall_ex_self_test_s",
    "self_test_share",
    "retries",
    "retry_s",
    "step_median_s",
    "step_p90_s",
    "step_max_s",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "compaction_tokens",
    "total_tokens",
    "max_request_tokens",
    "mean_request_tokens",
    "cost_usd",
    "cost_known",
    "think_share",
    "output_share",
    "tool_share",
    "ttft_share",
    "other_share",
    "gen_tokens_per_s",
    "gen_tokens_per_s_median",
    "tokens_per_step",
    "cache_hit_rate",
    "metrics_source",
    "metrics_version",
]
METRICS_VERSION = 2
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
    "final_reps",
    "best_round",
    "validation_seed",
    "final_seed",
    "confidence",
    "agent_error",
    "provider_error_count",
    "provider_recovered",
    "provider_retries",
    "provider_retry_after_s",
    "budget_wall_cap_s",
    "budget_idle_timeout_s",
    "build_ok",
    "seed_build_ok",
    "gate_ok",
    "gate_pass",
    "gate_total",
    "result_valid",
    "perf_valid",
    "eligible",
    "integrity_violation",
    "network_denied",
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


def format_optional_duration(seconds: object) -> str:
    if seconds is None or seconds == "":
        return "—"
    try:
        return format_duration(float(seconds))
    except (TypeError, ValueError):
        return "—"


def format_percent(value: object) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


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


def image_inspect(engine: str, image: str) -> dict | None:
    try:
        proc = subprocess.run(
            [engine, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data[0] if isinstance(data, list) and data else None


def image_identifier(engine: str, image: str) -> str | None:
    """Return a repository digest when present, falling back to the local image ID."""
    data = image_inspect(engine, image)
    if not data:
        return None
    digests = data.get("RepoDigests")
    if isinstance(digests, list):
        for digest in digests:
            if isinstance(digest, str) and digest.strip():
                return digest.strip()
    image_id = data.get("Id")
    return image_id if isinstance(image_id, str) and image_id else None


def image_labels(engine: str, image: str) -> dict:
    data = image_inspect(engine, image)
    config = data.get("Config") if data else None
    labels = config.get("Labels") if isinstance(config, dict) else None
    return labels if isinstance(labels, dict) else {}


def ensure_image(engine: str, image: str, *, dry_run: bool = False) -> bool:
    """Return True when the image is available, pulling public GHCR images once."""
    if image_exists(engine, image):
        return True
    if not image.startswith("ghcr.io/"):
        return False
    print(f"pulling {image}")
    run_command([engine, "pull", image], dry_run=dry_run)
    return dry_run or image_exists(engine, image)


def collect_image_metadata(
    engine: str, agent_image: str, test_image: str
) -> dict:
    """Capture image references and immutable identifiers in run metadata."""
    return {
        "agent_image": agent_image,
        "agent_image_id": image_identifier(engine, agent_image),
        "test_image": test_image,
        "test_image_id": image_identifier(engine, test_image),
        "test_suite_version": image_labels(engine, test_image).get(
            "org.opencode-bench.evaluator-suite-version"
        ),
    }


def read_image_manifest(engine: str, image: str) -> tuple[dict | None, str | None]:
    """Read the frozen suite manifest baked into the evaluator image."""
    proc = subprocess.run(
        [
            engine,
            "run",
            "--rm",
            "--network",
            "none",
            image,
            "cat",
            EVALUATOR_MANIFEST,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip() or "evaluator manifest could not be read"
    try:
        manifest = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, f"invalid evaluator manifest: {exc}"
    if not isinstance(manifest, dict):
        return None, "invalid evaluator manifest: expected an object"
    return manifest, None


def image_help(engine: str, image: str, *, agent: bool) -> str:
    if image.startswith("ghcr.io/"):
        return f"{engine} pull {image}"
    if agent:
        return f"./bench.py build --agent-image {image}"
    return (
        f"./bench.py build-eval --tests-dir <private-suite> "
        f"--agent-image <agent-image> --test-image {image}"
    )


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


def parse_endpoint(value: str) -> tuple[str, int]:
    text = value.strip()
    if not text or "*" in text:
        raise ValueError(f"invalid network endpoint {value!r}")
    candidate = text if "://" in text else f"https://{text}"
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"invalid network endpoint {value!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("network endpoints must not contain credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError(f"invalid network endpoint {value!r}") from exc
    return parsed.hostname.rstrip(".").lower(), port


def loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost" or host.endswith(".localhost")


def evaluation_source_host(host: str) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in EVALUATION_SOURCE_SUFFIXES)


def add_endpoint(allowlist: dict[str, list[int]], value: str) -> None:
    host, port = parse_endpoint(value)
    ports = allowlist.setdefault(host, [])
    if port not in ports:
        ports.append(port)
        ports.sort()


def configured_base_urls(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in ("baseurl", "api") and isinstance(item, str):
                if item.startswith(("http://", "https://")):
                    found.append(item)
            elif isinstance(item, (dict, list)):
                found.extend(configured_base_urls(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(configured_base_urls(item))
    return found


def load_models_dev(config_dir: Path) -> dict:
    cache_dir: Path | None = None
    try:
        proc = subprocess.run(
            [OPENCODE, "debug", "paths"],
            capture_output=True,
            text=True,
            timeout=15,
            env=config_env(config_dir),
            check=False,
        )
        for line in proc.stdout.splitlines():
            match = re.match(r"^cache\s+(.+)$", line.strip())
            if match:
                cache_dir = Path(match.group(1)).expanduser()
                break
        if cache_dir:
            cached = cache_dir / "models.json"
            if cached.exists():
                data = json.loads(cached.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        with urllib.request.urlopen("https://models.dev/api.json", timeout=10) as response:
            data = json.load(response)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def resolve_agent_allowlist(
    model: str, config_dir: Path, extra_endpoints: list[str]
) -> dict[str, list[int]]:
    """Build a per-run, fail-closed allowlist for the selected model provider."""
    allowlist = {host: list(ports) for host, ports in OPENCODE_INFRA_ENDPOINTS.items()}
    discovered: list[str] = []
    info = load_config_info(config_dir)
    providers = info.get("providers") or info.get("provider") or {}
    provider_name, _, model_id = model.partition("/")
    provider = providers.get(provider_name) or {}
    if isinstance(provider, dict):
        discovered.extend(configured_base_urls(provider))
        selected = (provider.get("models") or {}).get(model_id) or {}
        discovered.extend(configured_base_urls(selected))
    if not discovered:
        metadata = load_models_dev(config_dir).get(provider_name) or {}
        if isinstance(metadata, dict):
            api = metadata.get("api")
            if isinstance(api, str) and api.startswith(("http://", "https://")):
                discovered.append(api)
            sdk_endpoint = SDK_ENDPOINTS.get(str(metadata.get("npm") or ""))
            if sdk_endpoint:
                discovered.append(sdk_endpoint)
    for value in discovered:
        try:
            add_endpoint(allowlist, value)
        except ValueError:
            continue
    for value in extra_endpoints:
        try:
            add_endpoint(allowlist, value)
        except ValueError as exc:
            raise SystemExit(f"error: --allow-host: {exc}") from exc
    for host in allowlist:
        if evaluation_source_host(host):
            raise SystemExit(
                f"error: network endpoint {host!r} is an evaluation source and "
                "cannot be approved as a model endpoint"
            )
        if loopback_host(host):
            raise SystemExit(
                f"error: model endpoint {host!r} resolves inside the agent container. "
                "Use a LAN address or host.docker.internal for a model server on the host."
            )
    approved = {
        (host, port)
        for host, ports in allowlist.items()
        for port in ports
        if host not in OPENCODE_INFRA_ENDPOINTS
    }
    if not approved:
        raise SystemExit(
            f"error: could not determine the API endpoint for {provider_name!r}; "
            "pass --allow-host HOST[:PORT] or --allow-host http://192.168.1.10:8080/v1. "
            "The agent network remains restricted; model authentication is unchanged."
        )
    return allowlist


def format_allowlist(allowlist: dict[str, list[int]]) -> list[str]:
    return [
        f"{host}:{port}"
        for host, ports in sorted(allowlist.items())
        for port in sorted(ports)
    ]


def strip_jsonc(text: str) -> str:
    """Turn JSONC into JSON: drop comments and trailing commas."""
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
        elif char == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
        elif char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
        else:
            out.append(char)
            index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def prepare_agent_config(config_dir: Path, run_dir: Path) -> Path:
    """Stage a per-run config copy with interactive tools disabled.

    Benchmark sessions are non-interactive: `question` only ever wastes a step.
    The user's config is copied (never modified), converted to plain JSON if it
    was JSONC, and given `tools.question = false`.
    """
    target = run_dir / "agent-config" / "opencode"
    try:
        if target.exists():
            shutil.rmtree(target)
        if config_dir.is_file():
            target.mkdir(parents=True)
            data = json.loads(strip_jsonc(config_dir.read_text(encoding="utf-8")))
            data.setdefault("tools", {})["question"] = False
            (target / "opencode.json").write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8"
            )
            return target
        shutil.copytree(
            config_dir,
            target,
            ignore=shutil.ignore_patterns("*.bak-*", "__pycache__", "*.pyc"),
        )
        candidates = [target / "opencode.jsonc", target / "opencode.json"]
        for path in candidates:
            if not path.exists():
                continue
            data = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
            data.setdefault("tools", {})["question"] = False
            for other in candidates:
                if other.exists():
                    other.unlink()
            (target / "opencode.json").write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8"
            )
            break
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"  warning: could not stage the agent config override ({exc})")
        return config_dir
    return target


def load_config_info(config_dir: Path | None = None) -> dict:
    """Return effective OpenCode config without printing its secret values."""
    try:
        proc = subprocess.run(
            [OPENCODE, "debug", "config", "--standalone"],
            capture_output=True,
            text=True,
            timeout=15,
            env=config_env(config_dir),
            check=False,
        )
        sources = json.loads(proc.stdout)
        info: dict = {}
        for source in sources:
            if isinstance(source, dict) and isinstance(source.get("info"), dict):
                info.update(source["info"])
        return info
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
        return {}


def load_model_entry(model: str, config_dir: Path | None = None) -> dict | None:
    info = load_config_info(config_dir)
    providers = info.get("providers") or info.get("provider") or {}
    provider, _, model_id = model.partition("/")
    return (providers.get(provider) or {}).get("models", {}).get(model_id)


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


def model_thinking(
    model: str, variant: str | None, config_dir: Path | None = None
) -> tuple[str, str]:
    entry = load_model_entry(model, config_dir)
    return describe_variant(entry, variant) if entry else ("unknown", "")


def model_variants(
    model: str, config_dir: Path | None = None
) -> list[tuple[str, str, str]]:
    entry = load_model_entry(model, config_dir)
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


def ordered_variants(
    model: str, config_dir: Path | None = None
) -> list[tuple[str, str, str]]:
    return sorted(
        model_variants(model, config_dir),
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


PROVIDER_TERMINAL_KINDS = {
    "provider.auth",
    "provider.permission",
    "provider.quota",
    "provider.free-limit",
    "provider.rate-limit",
    "rate_limit",
    "free_limit",
}
PROVIDER_TERMINAL_STATUSES = {401, 403, 429}
PROVIDER_FAILURE_STATUSES = {401, 403, 429, 500, 502, 503, 504}
PROVIDER_PROGRESS_EVENTS = {"step_start", "step_finish", "text", "tool_use"}
PROVIDER_MAX_EVENTS = 20


def _is_provider_failure(kind: str, status: object, message: str) -> bool:
    return (
        kind.startswith("provider.")
        or kind in {"rate_limit", "free_limit", "provider"}
        or status in PROVIDER_FAILURE_STATUSES
        or "usage limit" in message.lower()
    )


def _is_terminal_provider_failure(entry: dict) -> bool:
    """A failure the engine cannot wait out: auth, quota or rate limit."""
    kind = str(entry.get("type") or "").lower()
    status = entry.get("status")
    message = str(entry.get("message") or "").lower()
    if status in PROVIDER_TERMINAL_STATUSES:
        return True
    if kind in PROVIDER_TERMINAL_KINDS:
        return True
    return any(
        marker in message
        for marker in ("usage limit", "quota", "insufficient", "unauthorized", "invalid api key")
    )


def parse_provider_events(log_path: Path) -> list[dict]:
    """Return provider failures in an OpenCode JSON log, in order.

    Each entry records whether the session kept making progress after the
    failure. OpenCode retries a request in place when the engine dies
    mid-generation ("the next request restarts it"), so a transient failure
    followed by more steps is a recovered blip, not a provider outage.
    """
    if not log_path.exists():
        return []
    failures: list[tuple[int, dict]] = []
    progress: set[int] = set()
    text = log_path.read_text(encoding="utf-8", errors="replace")
    for index, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "error":
            error = event.get("error") or {}
            if not isinstance(error, dict):
                error = {"message": str(error)}
            error_type = str(error.get("type") or "")
            status = error.get("status")
            message = str(error.get("message") or "")
            if _is_provider_failure(error_type, status, message):
                failures.append(
                    (
                        index,
                        {
                            "type": error_type or "provider",
                            "status": status,
                            "message": message[:400],
                            "retry_after_ms": event.get("retryAfterMs"),
                        },
                    )
                )
        elif kind in PROVIDER_PROGRESS_EVENTS:
            progress.add(index)
    events: list[dict] = []
    for index, entry in failures:
        entry["recovered"] = any(position > index for position in progress)
        events.append(entry)
    return events[-PROVIDER_MAX_EVENTS:]


def analyze_provider_errors(log_path: Path) -> dict:
    """Classify provider failures: recovered blip, retryable, or terminal.

    A run is only a provider error when a failure is left unrecovered. Quota
    and auth failures are terminal; other engine failures may be retried by
    resuming the session.
    """
    events = parse_provider_events(log_path)
    unrecovered = [event for event in events if not event.get("recovered")]
    terminal = any(_is_terminal_provider_failure(event) for event in unrecovered)
    return {
        "events": events,
        "count": len(events),
        "recovered": bool(events) and not unrecovered,
        "terminal": terminal,
        "retryable": bool(unrecovered) and not terminal,
        "last_error": unrecovered[-1] if unrecovered else None,
    }


def parse_provider_error(log_path: Path) -> dict | None:
    """Return a provider failure that actually ended the run, or None.

    A failure the engine recovered from (more steps in the same log) is not an
    agent error: the model finished the work it was doing.
    """
    return analyze_provider_errors(log_path)["last_error"]


def provider_retry_delay(agent: dict, attempts: list[dict], args: argparse.Namespace) -> float:
    """Backoff before resuming a round after a transient provider failure."""
    base = max(0.0, float(getattr(args, "provider_retry_delay", 30.0)))
    error = agent.get("provider_error") or {}
    retry_after = error.get("retry_after_ms")
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        base = max(base, float(retry_after) / 1000.0)
    exponent = max(0, len(attempts) - 1)
    return min(base * (2**exponent), 15 * 60.0)


def start_egress_proxy(
    engine: str,
    agent_image: str,
    run_dir: Path,
    allowlist: dict[str, list[int]],
    dry_run: bool,
) -> dict:
    suffix = uuid.uuid4().hex[:8]
    proxy = f"opencode-bench-htj2k-egress-{suffix}"
    network = f"opencode-bench-htj2k-internal-{suffix}"
    policy_path = run_dir / f"egress-allowlist-{suffix}.json"
    policy_path.write_text(json.dumps(allowlist, indent=2) + "\n", encoding="utf-8")
    info = {
        "proxy": proxy,
        "network": network,
        "policy_path": policy_path,
        "log_path": run_dir / f"egress-{suffix}.log",
        "dry_run": dry_run,
    }
    create = [engine, "network", "create", "--internal", network]
    launch = [
        engine,
        "run",
        "-d",
        "--rm",
        "--name",
        proxy,
        "--add-host",
        "host.docker.internal:host-gateway",
        "-v",
        f"{policy_path}:/run/egress-allowlist.json:ro",
        "-v",
        f"{EGRESS_PROXY_PATH}:/opt/opencode-bench/egress_proxy.py:ro",
        "--entrypoint",
        "python3",
        agent_image,
        "/opt/opencode-bench/egress_proxy.py",
        "--port",
        "8080",
        "--allowlist",
        "/run/egress-allowlist.json",
    ]
    connect = [engine, "network", "connect", "--alias", "egress-proxy", network, proxy]
    if dry_run:
        for command in (create, launch, connect):
            print("+ " + " ".join(shlex.quote(part) for part in command))
        return info
    try:
        for command in (create, launch, connect):
            proc = subprocess.run(command, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"command failed: {command[1]}")
        ready = False
        for _ in range(50):
            probe = subprocess.run(
                [
                    engine,
                    "exec",
                    proxy,
                    "python3",
                    "-c",
                    "import socket; socket.create_connection(('127.0.0.1', 8080), 1).close()",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(0.1)
        if not ready:
            raise RuntimeError("egress proxy did not become ready")
        return info
    except Exception:
        stop_egress_proxy(engine, info)
        raise


def read_egress_audit(path: Path) -> dict:
    denied: set[str] = set()
    integrity_reasons: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != "deny":
            continue
        host = str(event.get("host") or "").lower()
        port = event.get("port")
        destination = f"{host}:{port}" if host else "unknown"
        denied.add(destination)
        if evaluation_source_host(host):
            integrity_reasons.add(f"blocked evaluation-source access to {destination}")
    return {
        "network_denied": len(denied),
        "network_denied_hosts": sorted(denied),
        "integrity_violation": bool(integrity_reasons),
        "integrity_reasons": sorted(integrity_reasons),
    }


def stop_egress_proxy(engine: str, info: dict) -> dict:
    if info.get("dry_run"):
        for command in (
            [engine, "rm", "-f", info["proxy"]],
            [engine, "network", "rm", info["network"]],
        ):
            print("+ " + " ".join(shlex.quote(part) for part in command))
        return read_egress_audit(info["log_path"])
    logs = subprocess.run(
        [engine, "logs", info["proxy"]], capture_output=True, text=True, check=False
    )
    info["log_path"].write_text(logs.stdout + logs.stderr, encoding="utf-8")
    subprocess.run(
        [engine, "rm", "-f", info["proxy"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        [engine, "network", "rm", info["network"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return read_egress_audit(info["log_path"])


def combine_network_audits(items: list[dict]) -> dict:
    """Aggregate per-attempt/per-round egress audits without losing evidence."""
    denied = 0
    hosts: set[str] = set()
    reasons: set[str] = set()
    for item in items:
        audit = item.get("network_integrity", item) if isinstance(item, dict) else {}
        denied += int(audit.get("network_denied") or 0)
        hosts.update(str(host) for host in audit.get("network_denied_hosts") or [])
        reasons.update(str(reason) for reason in audit.get("integrity_reasons") or [])
    return {
        "network_denied": denied,
        "network_denied_hosts": sorted(hosts),
        "integrity_violation": bool(reasons),
        "integrity_reasons": sorted(reasons),
    }


def start_agent(
    model: str,
    config_dir: Path,
    workdir: Path,
    data_dir: Path,
    run_dir: Path,
    *,
    env_names: set[str],
    allowlist: dict[str, list[int]],
    args: argparse.Namespace,
    round_number: int = 1,
    prompt_file: Path | None = None,
    session_id: str | None = None,
    continue_session: bool = False,
    timeout_s: float | None = None,
    idle_timeout_s: float | None = None,
    log_suffix: str = "",
    watch_completion: bool = True,
) -> dict:
    egress = start_egress_proxy(args.engine, args.agent_image, run_dir, allowlist, args.dry_run)
    name = f"opencode-bench-htj2k-{slug(provider_of(model))}-{uuid.uuid4().hex[:8]}"
    log_path = run_dir / f"agent-round-{round_number}{log_suffix}.log"
    command = [
        args.engine,
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        egress["network"],
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
        "-e",
        "HTTP_PROXY=http://egress-proxy:8080",
        "-e",
        "HTTPS_PROXY=http://egress-proxy:8080",
        "-e",
        "ALL_PROXY=http://egress-proxy:8080",
        "-e",
        "http_proxy=http://egress-proxy:8080",
        "-e",
        "https_proxy=http://egress-proxy:8080",
        "-e",
        "NO_PROXY=localhost,127.0.0.1,::1",
        "-e",
        "no_proxy=localhost,127.0.0.1,::1",
        "-e",
        'OPENCODE_CONFIG_CONTENT={"permission":{"webfetch":"deny","websearch":"deny"},"websearch":false}',
    ]
    if args.variant:
        command += ["-e", f"OPENCODE_BENCH_VARIANT={args.variant}"]
    for key in sorted(env_names):
        if key.upper() in PROTECTED_AGENT_ENV:
            continue
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
        audit = stop_egress_proxy(args.engine, egress)
        return {
            "wall_s": 0.0,
            "watchdog": "dry-run",
            "completion": False,
            "agent_ok": True,
            "agent_exit_code": None,
            "self_test_runs": 0,
            "provider_error": None,
            "provider_errors": [],
            "provider_recovered": False,
            "provider_terminal": False,
            "provider_retryable": False,
            **audit,
        }

    try:
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
                if watch_completion and completion_path.exists() and completion_seen_at is None:
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
        provider = analyze_provider_errors(log_path)
        provider_error = provider["last_error"]
        # OpenCode exits non-zero when the provider errored, even if it retried in
        # place and finished the step. Progress after the failure is the signal
        # that matters, not the process exit code.
        agent_ok = provider_error is None and (proc.returncode == 0 or provider["recovered"])
        result = {
            "wall_s": wall_s,
            "watchdog": watchdog,
            "completion": (workdir / "COMPLETION.md").exists(),
            "agent_ok": agent_ok,
            "agent_exit_code": proc.returncode,
            "self_test_runs": self_tests,
            "session_id": session_match.group(1) if session_match else None,
            "log_path": str(log_path),
            "provider_error": provider_error,
            "provider_errors": provider["events"],
            "provider_recovered": provider["recovered"],
            "provider_terminal": provider["terminal"],
            "provider_retryable": provider["retryable"],
        }
    finally:
        audit = stop_egress_proxy(args.engine, egress)
    result.update(audit)
    return result


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
    """Token usage and step timings from an `opencode run --format json` log.

    The log carries token totals and per-step durations. It has no per-block
    timings, so the effort split comes from :func:`read_session_metrics`.
    """
    metrics = {
        "available": False,
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
        "self_test_failures": 0,
        "build_tool_s": 0.0,
        "wall_ex_self_test_s": wall_s,
        "retries": 0,
        "retry_s": 0.0,
        "steps": 0,
        "step_median_s": None,
        "step_p90_s": None,
        "step_max_s": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "compaction_tokens": 0,
        "total_tokens": 0,
        "max_request_tokens": 0,
        "mean_request_tokens": 0,
        "cost_usd": 0.0,
        "cost_known": False,
        "gen_tokens_per_s": None,
        "gen_tokens_per_s_median": None,
        "tokens_per_step": None,
        "cache_hit_rate": None,
        "metrics_source": "log",
        "metrics_version": METRICS_VERSION,
    }
    if not log_path.is_file():
        return metrics
    metrics["available"] = True
    step_starts: dict[str, int] = {}
    steps = 0
    durations: list[float] = []
    step_rates: list[float] = []
    request_tokens: list[int] = []
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
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
                duration = None
                if timestamp and start_ts is not None:
                    duration = max(0.0, (int(timestamp) - start_ts) / 1000.0)
                    metrics["model_time_s"] += duration
                    durations.append(duration)
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
                metrics["total_tokens"] += (
                    input_tokens + output_tokens + reasoning + read + write
                )
                request_tokens.append(input_tokens + read + write)
                generated = output_tokens + reasoning
                if duration and duration > 0 and generated > 0:
                    step_rates.append(generated / duration)
                if "cost" in part:
                    metrics["cost_known"] = True
                try:
                    metrics["cost_usd"] += float(part.get("cost") or 0)
                except (TypeError, ValueError):
                    pass
            elif kind == "tool_use":
                tool = str(part.get("tool") or "")
                state = part.get("state") or {}
                command = str((state.get("input") or {}).get("command") or "")
                if tool == "shell" and SELF_TEST_RE.search(command):
                    metrics["self_test_runs"] += 1
    metrics["steps"] = steps
    metrics["wall_ex_self_test_s"] = max(0.0, wall_s - metrics["self_test_s"])
    if request_tokens:
        metrics["max_request_tokens"] = max(request_tokens)
        metrics["mean_request_tokens"] = sum(request_tokens) / len(request_tokens)
    if durations:
        metrics["step_median_s"] = statistics.median(durations)
        metrics["step_p90_s"] = percentile(durations, 0.9)
        metrics["step_max_s"] = max(durations)
    if step_rates:
        metrics["gen_tokens_per_s_median"] = statistics.median(step_rates)
    generation = metrics["output_tokens"] + metrics["reasoning_tokens"]
    if metrics["model_time_s"] > 0:
        metrics["gen_tokens_per_s"] = generation / metrics["model_time_s"]
    metrics["other_s"] = max(
        0.0, wall_s - metrics["model_time_s"] - metrics["tool_s"] - metrics["self_test_s"]
    )
    return metrics


def _json_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_session_metrics(data_dir: Path, wall_s: float) -> dict:
    """Time-split metrics from the OpenCode SQLite session database.

    The database records each assistant message with `created`, `streamed` and
    `completed` timestamps, plus per-content-block timings. Reasoning and tool
    blocks are children of the same message, so this separates thinking from
    tool execution without a model proxy.
    """
    result = {
        "available": False,
        "span_s": None,
        "agent_s": None,
        "ttft_s": None,
        "think_s": None,
        "output_s": None,
        "tool_s": None,
        "tool_shell_s": None,
        "tool_write_s": None,
        "tool_read_s": None,
        "tool_other_s": None,
        "self_test_s": 0.0,
        "self_test_runs": 0,
        "self_test_failures": 0,
        "build_tool_s": 0.0,
        "retries": 0,
        "retry_s": 0.0,
        "compaction_tokens": 0,
        "compaction_cost_usd": 0.0,
        "db_cost_usd": 0.0,
        "db_input_tokens": 0,
        "db_output_tokens": 0,
        "db_reasoning_tokens": 0,
        "db_cache_read_tokens": 0,
        "db_cache_write_tokens": 0,
        "assistant_steps": 0,
        "max_request_tokens": None,
        "mean_request_tokens": None,
        "step_median_s": None,
        "step_p90_s": None,
        "step_max_s": None,
        "gen_tokens_per_s_median": None,
    }
    database = data_dir / "opencode.db"
    if not database.exists():
        return result

    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:
        return result

    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        if "session_message" not in tables:
            connection.close()
            return result
        rows = connection.execute(
            "select type, seq, time_created, data from session_message "
            "order by time_created, seq"
        ).fetchall()
    except sqlite3.Error:
        connection.close()
        return result

    try:
        first_user: int | None = None
        last_assistant: int | None = None
        total_agent = 0.0
        total_ttft = 0.0
        total_think = 0.0
        total_output = 0.0
        tool_totals = {"shell": 0.0, "write": 0.0, "read": 0.0, "other": 0.0}
        request_tokens: list[int] = []
        durations: list[float] = []
        step_rates: list[float] = []

        for message_type, _seq, row_created, raw in rows:
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            times = data.get("time") or {}
            created = int(times.get("created") or row_created or 0)
            completed = int(times.get("completed") or 0)
            streamed = int(times.get("streamed") or completed or 0)

            if message_type == "user" and created:
                first_user = created if first_user is None else min(first_user, created)
                continue
            if message_type == "compaction":
                tokens = data.get("tokens") or {}
                cache = tokens.get("cache") or {}
                result["compaction_tokens"] += sum(
                    _json_int(tokens.get(key))
                    for key in ("input", "output", "reasoning")
                )
                result["compaction_tokens"] += _json_int(cache.get("read"))
                result["compaction_tokens"] += _json_int(cache.get("write"))
                try:
                    result["compaction_cost_usd"] += float(data.get("cost") or 0)
                except (TypeError, ValueError):
                    pass
                continue
            if message_type != "assistant":
                continue

            # Provider errors/retries are not model generation. They stay in
            # the wall clock (and are reported as retry_s) but must not inflate
            # think/output time or depress tok/s with zero-token attempts.
            errored = bool(data.get("error")) or str(
                data.get("finish") or ""
            ).lower() == "error"
            if errored:
                if created and (completed or streamed):
                    finished = completed or streamed
                    result["retries"] += 1
                    result["retry_s"] += max(0, finished - created) / 1000.0
                    last_assistant = (
                        finished
                        if last_assistant is None
                        else max(last_assistant, finished)
                    )
                continue

            result["assistant_steps"] += 1
            tokens = data.get("tokens") or {}
            cache = tokens.get("cache") or {}
            result["db_input_tokens"] += _json_int(tokens.get("input"))
            result["db_output_tokens"] += _json_int(tokens.get("output"))
            result["db_reasoning_tokens"] += _json_int(tokens.get("reasoning"))
            result["db_cache_read_tokens"] += _json_int(cache.get("read"))
            result["db_cache_write_tokens"] += _json_int(cache.get("write"))
            request_tokens.append(
                _json_int(tokens.get("input"))
                + _json_int(cache.get("read"))
                + _json_int(cache.get("write"))
            )
            try:
                result["db_cost_usd"] += float(data.get("cost") or 0)
            except (TypeError, ValueError):
                pass

            if not created or not completed:
                continue
            last_assistant = completed if last_assistant is None else max(
                last_assistant, completed
            )
            message_duration = max(0, completed - created) / 1000.0
            total_agent += message_duration
            durations.append(message_duration)
            generated = _json_int(tokens.get("output")) + _json_int(
                tokens.get("reasoning")
            )
            if message_duration > 0 and generated > 0:
                step_rates.append(generated / message_duration)

            timed: list[tuple[int, dict, dict]] = []
            for block in data.get("content") or []:
                if not isinstance(block, dict):
                    continue
                block_time = block.get("time") or {}
                start = int(block_time.get("created") or 0)
                if start:
                    timed.append((start, block_time, block))
            if not timed:
                continue
            timed.sort(key=lambda item: item[0])

            first_block = timed[0][0]
            total_ttft += max(0, first_block - created) / 1000.0

            reasoning = [item for item in timed if item[2].get("type") == "reasoning"]
            if reasoning:
                reasoning_start = min(item[0] for item in reasoning)
                boundary = min(
                    (
                        item[0]
                        for item in timed
                        if item[2].get("type") != "reasoning"
                        and item[0] >= reasoning_start
                    ),
                    default=streamed or reasoning_start,
                )
                boundary = max(reasoning_start, min(boundary, streamed or boundary))
                total_think += max(0, boundary - reasoning_start) / 1000.0
                if streamed:
                    total_output += max(0, streamed - boundary) / 1000.0
            elif streamed:
                total_output += max(0, streamed - first_block) / 1000.0

            for _start, block_time, block in timed:
                if block.get("type") != "tool":
                    continue
                ran = int(block_time.get("ran") or 0)
                finished = int(block_time.get("completed") or ran or 0)
                if not ran or not finished or finished < ran:
                    continue
                duration = (finished - ran) / 1000.0
                name = str(block.get("name") or "other")
                state = block.get("state") or {}
                command = str((state.get("input") or {}).get("command") or "")
                output = " ".join(
                    str(part.get("text") or "")
                    for part in (state.get("content") or [])
                    if isinstance(part, dict)
                )
                if name == "shell" and SELF_TEST_RE.search(command):
                    result["self_test_s"] += duration
                    result["self_test_runs"] += 1
                    exit_code = (state.get("metadata") or {}).get("exit")
                    failed = bool(
                        re.search(r"\b[1-9]\d*\s+failed\b", output, re.IGNORECASE)
                        or re.search(r"\bFAILED\b", output)
                    )
                    if failed or (exit_code not in (None, 0, "0")):
                        result["self_test_failures"] += 1
                elif name == "shell" and BUILD_TOOL_RE.search(command):
                    result["build_tool_s"] += duration
                if name == "shell":
                    tool_totals["shell"] += duration
                elif name in ("write", "edit", "patch"):
                    tool_totals["write"] += duration
                elif name in ("read", "glob", "grep", "list"):
                    tool_totals["read"] += duration
                else:
                    tool_totals["other"] += duration

        if first_user and last_assistant and last_assistant >= first_user:
            result["span_s"] = (last_assistant - first_user) / 1000.0
        result["agent_s"] = total_agent
        result["ttft_s"] = total_ttft
        result["think_s"] = total_think
        result["output_s"] = total_output
        result["tool_s"] = sum(tool_totals.values())
        result["tool_shell_s"] = tool_totals["shell"]
        result["tool_write_s"] = tool_totals["write"]
        result["tool_read_s"] = tool_totals["read"]
        result["tool_other_s"] = tool_totals["other"]
        if request_tokens:
            result["max_request_tokens"] = max(request_tokens)
            result["mean_request_tokens"] = sum(request_tokens) / len(request_tokens)
        if durations:
            result["step_median_s"] = statistics.median(durations)
            result["step_p90_s"] = percentile(durations, 0.9)
            result["step_max_s"] = max(durations)
        if step_rates:
            result["gen_tokens_per_s_median"] = statistics.median(step_rates)
        result["available"] = True
        return result
    finally:
        connection.close()


def compute_metrics(
    log_path: Path | None,
    data_dir: Path | None,
    wall_s: float,
    *,
    usage: dict | None = None,
) -> dict:
    """Merge log token usage and session timings into persisted metric fields.

    The database is authoritative for the effort split; the log is the fallback
    when a run kept no `opencode-data` tree (for example the mock agent).
    """
    if usage is None:
        usage = read_agent_usage(log_path or Path("/nonexistent"), wall_s)
    session = (
        read_session_metrics(data_dir, wall_s)
        if data_dir is not None
        else {"available": False}
    )
    log_available = bool(usage.get("available", True))
    db_available = bool(session.get("available"))

    if log_available:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        reasoning_tokens = int(usage.get("reasoning_tokens") or 0)
        cache_read_tokens = int(usage.get("cache_read_tokens") or 0)
        cache_write_tokens = int(usage.get("cache_write_tokens") or 0)
        cost_usd = float(usage.get("cost_usd") or 0)
        cost_known = bool(usage.get("cost_known"))
        steps = int(usage.get("steps") or 0)
    elif db_available:
        input_tokens = int(session["db_input_tokens"])
        output_tokens = int(session["db_output_tokens"])
        reasoning_tokens = int(session["db_reasoning_tokens"])
        cache_read_tokens = int(session["db_cache_read_tokens"])
        cache_write_tokens = int(session["db_cache_write_tokens"])
        cost_usd = float(session.get("db_cost_usd") or 0)
        cost_known = bool(session.get("db_cost_usd"))
        steps = int(session["assistant_steps"])
    else:
        input_tokens = output_tokens = reasoning_tokens = 0
        cache_read_tokens = cache_write_tokens = 0
        cost_usd = 0.0
        cost_known = False
        steps = 0

    max_request_tokens = usage.get("max_request_tokens")
    mean_request_tokens = usage.get("mean_request_tokens")
    if not max_request_tokens and db_available:
        max_request_tokens = session.get("max_request_tokens")
        mean_request_tokens = session.get("mean_request_tokens")

    compaction_tokens = int(session.get("compaction_tokens") or 0) if db_available else 0
    if db_available:
        compaction_cost = float(session.get("compaction_cost_usd") or 0)
        if log_available:
            cost_usd += compaction_cost
        else:
            cost_usd = float(session.get("db_cost_usd") or 0) + compaction_cost
        cost_known = cost_known or cost_usd > 0
    total_tokens = (
        input_tokens
        + output_tokens
        + reasoning_tokens
        + cache_read_tokens
        + cache_write_tokens
        + compaction_tokens
    )

    metrics: dict[str, object] = {
        "available": log_available or db_available,
        "span_s": wall_s,
        "agent_s": float(session["agent_s"]) if db_available else wall_s,
        "steps": steps,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "compaction_tokens": compaction_tokens,
        "total_tokens": total_tokens,
        "max_request_tokens": max_request_tokens,
        "mean_request_tokens": mean_request_tokens,
        "cost_usd": round(cost_usd, 6),
        "cost_known": cost_known,
        "self_test_runs": int(
            session.get("self_test_runs")
            if db_available
            else (usage.get("self_test_runs") or 0)
        ),
        "self_test_failures": int(session.get("self_test_failures") or 0),
        "retries": int(
            session.get("retries") if db_available else (usage.get("retries") or 0)
        ),
        "metrics_source": "db+log"
        if db_available and log_available
        else ("db" if db_available else ("log" if log_available else "none")),
        "metrics_version": METRICS_VERSION,
    }

    if db_available:
        for key in (
            "ttft_s",
            "think_s",
            "output_s",
            "tool_s",
            "tool_shell_s",
            "tool_write_s",
            "tool_read_s",
            "tool_other_s",
            "self_test_s",
            "build_tool_s",
            "retry_s",
            "step_median_s",
            "step_p90_s",
            "step_max_s",
            "gen_tokens_per_s_median",
        ):
            metrics[key] = session.get(key)
        buckets = [
            metrics.get("ttft_s"),
            metrics.get("think_s"),
            metrics.get("output_s"),
            metrics.get("tool_s"),
        ]
        known = [float(value) for value in buckets if value is not None]
        accounted = sum(known)
        # Message timestamps can overlap slightly when turns are queued; the
        # harness wall time remains the hard total. Scale the buckets so they
        # cannot exceed it.
        if wall_s and accounted > float(wall_s):
            factor = float(wall_s) / accounted
            for key in ("ttft_s", "think_s", "output_s", "tool_s"):
                value = metrics.get(key)
                if value is not None:
                    metrics[key] = float(value) * factor
            tool_total = float(metrics.get("tool_s") or 0)
            for key in ("self_test_s", "build_tool_s"):
                value = metrics.get(key)
                if value is not None:
                    metrics[key] = min(float(value) * factor, tool_total)
            accounted = float(wall_s)
        other = max(0.0, float(wall_s) - accounted) if wall_s else None
        metrics["other_s"] = other
        if other is not None and metrics.get("retry_s") is not None:
            metrics["retry_s"] = min(float(metrics["retry_s"]), float(other))

        def share(value: object) -> float | None:
            if value is None or not wall_s:
                return None
            return max(0.0, min(1.0, float(value) / float(wall_s)))

        metrics["ttft_share"] = share(metrics.get("ttft_s"))
        metrics["think_share"] = share(metrics.get("think_s"))
        metrics["output_share"] = share(metrics.get("output_s"))
        metrics["model_time_s"] = sum(
            float(metrics.get(key) or 0.0)
            for key in ("ttft_s", "think_s", "output_s")
        )
        metrics["tool_share"] = share(metrics.get("tool_s"))
        metrics["other_share"] = share(other)
        metrics["self_test_share"] = share(metrics.get("self_test_s"))
        metrics["wall_ex_self_test_s"] = (
            max(0.0, float(wall_s) - float(metrics.get("self_test_s") or 0))
            if wall_s
            else None
        )
        generation_s = float(metrics.get("think_s") or 0) + float(
            metrics.get("output_s") or 0
        )
        metrics["gen_tokens_per_s"] = (
            (output_tokens + reasoning_tokens) / generation_s
            if generation_s > 0
            else None
        )
    else:
        # Per-block timings are unavailable without the session database. Keep
        # the log-derived model time for ranking, but report the effort split
        # as unknown rather than as a misleading zero.
        for key in (
            "ttft_s",
            "think_s",
            "output_s",
            "tool_s",
            "tool_shell_s",
            "tool_write_s",
            "tool_read_s",
            "tool_other_s",
            "other_s",
            "self_test_s",
            "build_tool_s",
            "retry_s",
            "ttft_share",
            "think_share",
            "output_share",
            "tool_share",
            "other_share",
            "self_test_share",
        ):
            metrics[key] = None
        metrics["model_time_s"] = (
            float(usage["model_time_s"]) if usage.get("model_time_s") else None
        )
        metrics["wall_ex_self_test_s"] = float(wall_s) if wall_s else None
        metrics["gen_tokens_per_s"] = usage.get("gen_tokens_per_s")
        metrics["step_median_s"] = usage.get("step_median_s")
        metrics["step_p90_s"] = usage.get("step_p90_s")
        metrics["step_max_s"] = usage.get("step_max_s")
        metrics["gen_tokens_per_s_median"] = usage.get("gen_tokens_per_s_median")

    metrics["tokens_per_step"] = (
        (input_tokens + output_tokens + reasoning_tokens) / steps if steps else None
    )
    cache_total = input_tokens + cache_read_tokens
    metrics["cache_hit_rate"] = (
        cache_read_tokens / cache_total if cache_total else None
    )
    return metrics


def combine_attempt_metrics(attempts: list[dict]) -> dict:
    """Merge the usage metrics of every attempt of one round."""
    total_wall = sum(float(item.get("wall_s") or 0.0) for item in attempts)
    usages = [
        read_agent_usage(
            Path(item.get("log_path") or ""), float(item.get("wall_s") or 0.0)
        )
        for item in attempts
    ]
    if not usages:
        return read_agent_usage(Path("/nonexistent"), total_wall)
    merged = read_agent_usage(Path("/nonexistent"), total_wall)
    merged["available"] = any(usage.get("available") for usage in usages)
    additive_skip = {
        "available",
        "span_s",
        "agent_s",
        "wall_ex_self_test_s",
        "max_request_tokens",
        "mean_request_tokens",
        "gen_tokens_per_s",
        "gen_tokens_per_s_median",
        "step_median_s",
        "step_p90_s",
        "step_max_s",
        "other_s",
        "metrics_source",
        "metrics_version",
        "cost_known",
    }
    for key in merged:
        if key in additive_skip:
            continue
        values = [
            float(usage[key])
            for usage in usages
            if isinstance(usage.get(key), (int, float))
        ]
        merged[key] = sum(values) if values else None
    merged["cost_known"] = any(usage.get("cost_known") for usage in usages)
    request_max = max(int(usage.get("max_request_tokens") or 0) for usage in usages)
    means = [
        float(usage["mean_request_tokens"])
        for usage in usages
        if usage.get("mean_request_tokens") is not None
    ]
    merged["span_s"] = total_wall
    merged["agent_s"] = total_wall
    merged["wall_ex_self_test_s"] = max(
        0.0, total_wall - float(merged.get("self_test_s") or 0.0)
    )
    merged["max_request_tokens"] = request_max
    merged["mean_request_tokens"] = sum(means) / len(means) if means else 0.0
    merged["other_s"] = max(
        0.0,
        total_wall
        - float(merged.get("model_time_s") or 0.0)
        - float(merged.get("tool_s") or 0.0)
        - float(merged.get("self_test_s") or 0.0),
    )
    for key in ("step_median_s", "step_p90_s", "step_max_s", "gen_tokens_per_s_median"):
        values = [
            float(usage[key])
            for usage in usages
            if usage.get(key) is not None
        ]
        merged[key] = statistics.median(values) if values else None
    generation = float(merged.get("output_tokens") or 0.0) + float(
        merged.get("reasoning_tokens") or 0.0
    )
    merged["gen_tokens_per_s"] = (
        generation / float(merged["model_time_s"]) if merged.get("model_time_s") else None
    )
    merged["metrics_source"] = "round-logs"
    return merged


def refresh_run_metrics(meta: dict, run_dir: Path) -> bool:
    """Recompute effort/token fields for a saved run from its artifacts.

    Returns True when any persisted metric changed. Informational only: the
    score, quality and eligibility fields are untouched.
    """
    candidates = list(meta.get("agent_logs") or [])
    if not candidates and meta.get("agent_log"):
        candidates = [meta["agent_log"]]
    logs = [Path(path) for path in candidates if Path(path).is_file()]
    if not logs:
        legacy = run_dir / "agent.log"
        logs = [legacy] if legacy.exists() else []
    wall = float(meta.get("wall_s") or 0.0)
    per_log = wall / len(logs) if logs and wall else 0.0
    usage = combine_attempt_metrics(
        [{"log_path": str(log), "wall_s": per_log} for log in logs]
    )
    metrics = compute_metrics(
        None, run_dir / "opencode-data", wall, usage=usage
    )
    metrics["compactions"] = count_compactions(run_dir / "opencode-data")
    if not metrics.get("steps") and meta.get("steps") is not None:
        metrics["steps"] = int(meta.get("steps") or 0)
    if meta.get("gate_pass") in (None, ""):
        rounds = meta.get("rounds") or []
        best = next(
            (row for row in rounds if row.get("round") == meta.get("best_round")),
            None,
        )
        for candidate in ([best] if best else []) + rounds:
            if candidate and candidate.get("gate_total") not in (None, ""):
                metrics["gate_pass"] = candidate.get("gate_pass")
                metrics["gate_total"] = candidate.get("gate_total")
                break
    before = {key: meta.get(key) for key in metrics}
    for key, value in metrics.items():
        if value is not None:
            meta[key] = value
    return before != {key: meta.get(key) for key in metrics}


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
        if len(relative.parts) == 1 and (
            path.name == "COMPLETION.md" or re.fullmatch(r"ROUND-.*\.md", path.name)
        ):
            # Harness-owned round prompts and clocks are not candidate changes.
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
    bin_dir: Path | None = None,
    require_single_cpu: bool = False,
    args: argparse.Namespace | None = None,
) -> dict:
    artifacts = artifacts_dir or (run_dir / "artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    bins = bin_dir or (artifacts / "bin")
    result_file = artifacts / "results.json"
    if result_file.exists():
        result_file.unlink()
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
        f"{bins}:/bins:ro",
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
        "set -e; "
        "exec python3 -B /opt/private/runner.py "
        "--candidate-bin /bins/candidate --seed-bin /bins/seed "
        "--oracle /opt/oracle/bin/oracle "
        f"--manifest {EVALUATOR_MANIFEST} "
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


def combine_repeat_results(results: list[dict], manifest: dict) -> dict:
    """Combine repeated suite runs into one conservative result.

    Correctness must pass in *every* repeat.  Per-workload throughput is the
    median of the repeat speedups, which removes most of the effect of a single
    unlucky measurement of the chosen checkpoint.  The returned dict has the
    same shape as `run_tests` so callers and reports need no special case.
    """
    if not results:
        return {
            "result_valid": False,
            "test_ok": False,
            "result": None,
            "error": "no final repeats were run",
            "exit_code": None,
        }
    datas = [item.get("result") for item in results]
    if any(data is None for data in datas):
        errors = [str(item.get("error") or "missing results.json") for item in results]
        return {
            "result_valid": False,
            "test_ok": False,
            "result": next((data for data in datas if data is not None), None),
            "error": "final repeat failed: " + "; ".join(errors[:3]),
            "exit_code": results[0].get("exit_code"),
            "repeats": len(results),
        }
    base = json.loads(json.dumps(datas[0]))
    families: dict[str, dict[str, bool]] = {}
    for data in datas:
        for family, cases in ((data.get("correctness") or {}).get("families") or {}).items():
            target = families.setdefault(family, {})
            for case, ok in cases.items():
                target[case] = bool(target.get(case, True) and ok)
    base.setdefault("correctness", {})["families"] = families
    perf = base.setdefault("perf", {})
    for kind in ("decode", "encode"):
        by_id: dict[str, list[dict]] = {}
        for data in datas:
            for item in ((data.get("perf") or {}).get(kind) or []):
                by_id.setdefault(str(item.get("id")), []).append(item)
        combined: list[dict] = []
        for workload in by_id.values():
            item = json.loads(json.dumps(workload[0]))
            valid = all(bool(entry.get("valid")) for entry in workload) and len(workload) == len(datas)
            item["valid"] = valid
            item["validated"] = valid
            speedups = [float(entry["speedup"]) for entry in workload if entry.get("valid")]
            item["repeat_speedups"] = speedups
            if valid and speedups:
                item["speedup"] = statistics.median(speedups)
                spread = (
                    (max(speedups) - min(speedups)) / item["speedup"]
                    if item["speedup"] > 0
                    else 0.0
                )
                item["repeat_spread"] = spread
                item["cv"] = max(float(item.get("cv") or 0.0), spread)
            else:
                item["speedup"] = 0.0
                item["error"] = item.get("error") or "one or more final repeats were invalid"
            combined.append(item)
        perf[kind] = combined
        perf[f"{kind}_valid"] = bool(
            len(combined) == len(manifest["perf"][kind])
            and all(bool(item.get("valid")) for item in combined)
            and all(bool((data.get("perf") or {}).get(f"{kind}_valid")) for data in datas)
        )
        valid_values = [float(item["speedup"]) for item in combined if item.get("valid")]
        perf[f"{kind}_speedup"] = _geomean(valid_values) if valid_values else 0.0
    base["all_gates"] = all(bool(data.get("all_gates")) for data in datas)
    base["perf_valid"] = all(bool(data.get("perf_valid")) for data in datas) and bool(
        perf.get("decode_valid") and perf.get("encode_valid")
    )
    perf["stable"] = all(bool((data.get("perf") or {}).get("stable")) for data in datas)
    base["repeats"] = len(datas)
    perf["repeats"] = len(datas)
    perf["statistic"] = "median per workload across final repeats"
    base["repeat_scores"] = [
        {
            "decode_speedup": (data.get("perf") or {}).get("decode_speedup"),
            "encode_speedup": (data.get("perf") or {}).get("encode_speedup"),
            "duration_s": data.get("duration_s"),
        }
        for data in datas
    ]
    base["duration_s"] = round(sum(float(data.get("duration_s") or 0.0) for data in datas), 3)
    base["exit"] = 0 if base["all_gates"] and base["perf_valid"] else 1
    all_ok = all(bool(item.get("test_ok")) for item in results)
    return {
        "result_valid": True,
        "test_ok": all_ok and base["all_gates"] and base["perf_valid"],
        "result": base,
        "error": None if base["all_gates"] and base["perf_valid"] else _result_error_summary(base),
        "exit_code": 0 if base["all_gates"] and base["perf_valid"] else 1,
        "repeats": len(results),
    }


def run_tests_repeated(
    run_dir: Path,
    *,
    repeats: int,
    engine: str,
    image: str,
    dry_run: bool,
    manifest: dict,
    fixture_seed: int,
    artifacts_dir: Path,
    bin_dir: Path | None = None,
    require_single_cpu: bool = False,
    args: argparse.Namespace | None = None,
) -> dict:
    """Run the hidden suite `repeats` times and combine the measurements."""
    count = max(1, int(repeats))
    results: list[dict] = []
    try:
        (artifacts_dir / "results.json").unlink()
    except FileNotFoundError:
        pass
    for index in range(count):
        target = artifacts_dir if count == 1 else artifacts_dir / f"repeat-{index + 1}"
        target.mkdir(parents=True, exist_ok=True)
        results.append(
            run_tests(
                run_dir,
                engine=engine,
                image=image,
                dry_run=dry_run,
                manifest=manifest,
                fixture_seed=fixture_seed,
                artifacts_dir=target,
                bin_dir=bin_dir,
                require_single_cpu=require_single_cpu,
                args=args,
            )
        )
    if count == 1:
        return results[0]
    if dry_run:
        return results[0]
    combined = combine_repeat_results(results, manifest)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    if combined.get("result") is not None:
        (artifacts_dir / "results.json").write_text(
            json.dumps(combined["result"], indent=2) + "\n", encoding="utf-8"
        )
    logs: list[str] = []
    for index in range(count):
        log_path = artifacts_dir / f"repeat-{index + 1}" / "test.log"
        if log_path.exists():
            logs.append(f"===== repeat {index + 1}/{count} =====\n" + log_path.read_text())
    (artifacts_dir / "test.log").write_text("\n".join(logs), encoding="utf-8")
    return combined


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
    integrity_violation: bool = False,
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
        and not integrity_violation
    )
    if not eligible and not integrity_violation:
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
        "integrity_violation": bool(integrity_violation),
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
        f"- network: {meta.get('network_policy')} (provider-only egress; grader is always offline)",
        f"- integrity violation: {bool(meta.get('integrity_violation'))}",
        f"- blocked network destinations: {meta.get('network_denied', 0)}",
        *(
            ["", "### Integrity evidence", ""]
            + [f"- {reason}" for reason in meta.get("integrity_reasons", [])]
            if meta.get("integrity_violation")
            else []
        ),
        f"- rounds: {meta.get('rounds_count')} ({meta.get('round_plan')}); best {meta.get('best_round')}",
        f"- variant: {meta.get('variant', 'default')}",
        f"- thinking: {meta.get('thinking', 'unknown')}"
        + (f" ({meta['reasoning_effort']})" if meta.get("reasoning_effort") else ""),
        f"- suite: {meta.get('suite_version')} (fixture {meta.get('fixture_seed')})",
        f"- final grading: {meta.get('final_reps', 1)} repeat(s), median per workload",
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
        *(
            [
                f"- provider errors: {meta['provider_error_count']} "
                f"({meta.get('provider_recovered', 0)} recovered, "
                f"{meta.get('provider_retries', 0)} retries)"
            ]
            if meta.get("provider_error_count")
            else []
        ),
        *(
            [
                f"- agent error: {meta['agent_error'].get('type')} "
                f"({meta['agent_error'].get('status')}): "
                f"{meta['agent_error'].get('message', '')[:300]}"
                + (
                    f" (retry after {meta['provider_retry_after_s']:.0f}s)"
                    if meta.get("provider_retry_after_s")
                    else ""
                )
            ]
            if meta.get("agent_error")
            else []
        ),
        f"- steps: {meta.get('steps', 0)}",
        f"- files changed: {meta.get('files_changed', 0)}",
        *(
            [
                f"- model time: {format_duration(meta.get('model_time_s'))}"
                f" (rank tie-breaker; TTFT {format_duration(meta.get('ttft_s'))},"
                f" thinking {format_duration(meta.get('think_s'))},"
                f" output {format_duration(meta.get('output_s'))})",
                f"- effort: tools {format_duration(meta.get('tool_s'))},"
                f" self-test {format_duration(meta.get('self_test_s'))}"
                f" ({int(meta.get('self_test_runs') or 0)} runs),"
                f" other {format_duration(meta.get('other_s'))},"
                f" provider retry {format_duration(meta.get('retry_s'))}",
                f"- request tokens: peak {format_tokens(meta.get('max_request_tokens'))}"
                f" / mean {format_tokens(meta.get('mean_request_tokens'))}"
                f"; total {format_tokens(meta.get('total_tokens'))}"
                f" ({format_tokens(meta.get('reasoning_tokens'))} reasoning)",
                f"- generation: {_fmt(meta.get('gen_tokens_per_s'), 1)} tok/s"
                f" (median step {_fmt(meta.get('gen_tokens_per_s_median'), 1)})"
                f"; cache hit {format_percent(meta.get('cache_hit_rate'))}"
                f"; cost {_fmt(meta.get('cost_usd'), 4)} USD",
                f"- provider retries: {int(meta.get('retries') or 0)}"
                f"; metrics source: {meta.get('metrics_source')}",
            ]
            if str(meta.get("metrics_source") or "") in {"db", "db+log"}
            else []
        ),
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
        "| round | budget | wall | watchdog | nudges | gates | validation score | final score |",
        "|---|---:|---:|---|---:|---|---:|---:|",
    ]
    for row in meta.get("rounds") or []:
        validation = row.get("validation_score")
        final = row.get("final_score")
        lines.append(
            f"| {row.get('round')} | {format_duration(row.get('budget_s'))} | "
            f"{format_duration(row.get('wall_s'))} | {row.get('watchdog', '—')} | "
            f"{row.get('nudges', 0)} | "
            f"{'pass' if row.get('gate_ok') else 'FAIL'} "
            f"({row.get('gate_pass')}/{row.get('gate_total')}) | "
            f"{validation:.1f} | "
            f"{final:.1f} |"
            if validation is not None and final is not None
            else f"| {row.get('round')} | {format_duration(row.get('budget_s'))} | "
            f"{format_duration(row.get('wall_s'))} | {row.get('watchdog', '—')} | "
            f"{row.get('nudges', 0)} | "
            f"{'pass' if row.get('gate_ok') else 'FAIL'} "
            f"({row.get('gate_pass')}/{row.get('gate_total')}) | - | - |"
        )
    lines += [
        "",
        "Checkpoints use a validation corpus; the official score is the best eligible",
        "checkpoint rescored on a held-out final corpus.",
    ]
    if int(meta.get("final_reps") or 1) > 1:
        lines.append(
            f"Final grading is the median of {meta.get('final_reps')} rescore(s) "
            "per workload, which reduces single-measurement noise."
        )
    lines += [
        "The validation column is a single checkpoint measurement: it can move by",
        "more than the real code difference. Treat the official score as the result.",
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
        "Speedup is the median of paired seed/candidate ratios; min-of-N is a",
        "diagnostic only (per-process fast modes are not shared). Seed and candidate",
        "MP/s are medians. The score is seed-normalised. The untouched seed scores 0;",
        "matching the frozen target in both dimensions scores 100. Correctness gates",
        "must all pass for a result to be eligible.",
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
    if isinstance(row.get("agent_error"), dict):
        row["agent_error"] = json.dumps(row["agent_error"], sort_keys=True)
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
        integrity_violation = row.get("integrity_violation") is True or str(
            row.get("integrity_violation", "")
        ).lower() == "true"
        eligible = (
            str(row.get("eligible", "")).lower() == "true" and not integrity_violation
        )
        if not eligible:
            score *= LEADERBOARD_INELIGIBLE_FACTOR
        wall = _float(row.get("wall_s"))
        work = _float(row.get("wall_ex_self_test_s"))
        compactions = (
            int(_float(row.get("compactions")))
            if row.get("compactions") not in (None, "") and _float(row.get("compactions")) is not None
            else None
        )
        entries.append(
            {
                **row,
                "_score": score,
                "_eligible": eligible,
                "_model_time": _float(row.get("model_time_s")),
                "_wall": wall or 0.0,
                "_wall_work": work if work is not None else (wall or 0.0),
                "_compactions": compactions,
                "_decode": _float(row.get("decode_speedup")),
                "_encode": _float(row.get("encode_speedup")),
            }
        )
    entries.sort(
        key=lambda entry: (
            -entry["_score"],
            entry["_model_time"] if entry["_model_time"] is not None else float("inf"),
            entry["_compactions"] if entry["_compactions"] is not None else 0,
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


def thinking_label(row: dict) -> str:
    thinking = row.get("thinking") or "unknown"
    effort = row.get("reasoning_effort") or ""
    return f"{thinking} ({effort})" if effort else str(thinking)


def reasoning_key(row: dict) -> str:
    return (
        row.get("reasoning_effort") or row.get("thinking") or "unknown"
    ).strip().lower() or "unknown"


def reasoning_rank(row: dict) -> int:
    return REASONING_ORDER.get(reasoning_key(row), -1)


def provider_label(row: dict) -> str:
    return str(
        row.get("provider") or provider_of(str(row.get("model", ""))) or "unknown"
    )


def run_started(row: dict) -> datetime | None:
    """Parse the run's UTC start timestamp."""
    value = (row.get("timestamp") or "").strip()
    if not value:
        run_id = (row.get("run_id") or "").strip()
        if len(run_id) >= 16:
            value = run_id[:16]
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def started_label(row: dict) -> tuple[str, str]:
    """Return a short UTC start time and a full tooltip for the runner."""
    started = run_started(row)
    if started is None:
        return "—", ""
    return (
        started.strftime("%b %d %H:%M"),
        started.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


def _data_attr(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return ""


def _share_of(entry: dict, share_key: str, seconds_key: str) -> float | None:
    share = _float(entry.get(share_key))
    if share is not None:
        return max(0.0, min(1.0, share))
    seconds = _float(entry.get(seconds_key))
    wall = entry.get("_wall") or 0.0
    if seconds is not None and wall:
        return max(0.0, min(1.0, seconds / float(wall)))
    return None


def _render_effort_cell(entry: dict, escaped) -> str:
    shares = [
        ("ttft", _share_of(entry, "ttft_share", "ttft_s"), _float(entry.get("ttft_s"))),
        ("think", _share_of(entry, "think_share", "think_s"), _float(entry.get("think_s"))),
        ("out", _share_of(entry, "output_share", "output_s"), _float(entry.get("output_s"))),
        ("tool", _share_of(entry, "tool_share", "tool_s"), _float(entry.get("tool_s"))),
        ("other", _share_of(entry, "other_share", "other_s"), _float(entry.get("other_s"))),
    ]
    if all(share is None for _, share, _ in shares):
        return '<span class="muted">—</span>'
    values = [max(0.0, float(share or 0)) for _, share, _ in shares]
    total = sum(values)
    if total <= 0:
        return '<span class="muted">—</span>'
    segments = "".join(
        f'<span class="seg {name}" style="width:{value / total * 100:.2f}%"></span>'
        for (name, _, _), value in zip(shares, values)
    )
    labels = ["TTFT", "thinking", "output", "tools", "other"]
    parts = []
    for label, (_, share, seconds) in zip(labels, shares):
        if share is None:
            continue
        parts.append(
            f"{label} {format_optional_duration(seconds)} ({format_percent(share)})"
        )
    title = escaped(" · ".join(parts))
    percentages = "".join(
        f'<span class="pct {name}">{format_percent(share)}</span>'
        for label, (name, share, _) in zip(labels, shares)
        if share is not None
    )
    meta_parts = []
    if entry.get("_model_time") is not None:
        meta_parts.append(f"model {format_optional_duration(entry['_model_time'])}")
    peak = _float(entry.get("max_request_tokens"))
    if peak:
        meta_parts.append(f"peak {format_tokens(peak)} req tok")
    toks_median = _float(entry.get("gen_tokens_per_s_median"))
    toks = _float(entry.get("gen_tokens_per_s"))
    if toks_median is not None:
        meta_parts.append(f"{toks_median:.0f} tok/s med")
    elif toks is not None:
        meta_parts.append(f"{toks:.0f} tok/s")
    meta = (
        f'<span class="effort-meta">{" · ".join(meta_parts)}</span>'
        if meta_parts
        else ""
    )
    run_id = escaped(str(entry.get("run_id", "")))
    return (
        f'<button type="button" class="detail-btn" aria-label="Effort details for {run_id}">'
        f'<span class="split" role="img" aria-label="{title}">{segments}</span>'
        f'<span class="effort-pcts">{percentages}</span>'
        f"{meta}"
        "</button>"
    )


def _render_model_medians(entries: list[dict], escaped) -> str:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for entry in entries:
        key = (
            str(entry.get("model", "")),
            str(entry.get("variant") or "default"),
            thinking_label(entry),
        )
        groups.setdefault(key, []).append(entry)
    if not groups:
        return ""

    def values(items: list[dict], key: str) -> list[float]:
        out = []
        for item in items:
            number = _float(item.get(key))
            if number is not None:
                out.append(number)
        return out

    rows_html = []
    for (model, variant, thinking), items in sorted(groups.items()):
        runs = len(items)
        score = median_or_none([item["_score"] for item in items])
        decode = median_or_none(values(items, "_decode"))
        encode = median_or_none(values(items, "_encode"))
        wall = median_or_none(values(items, "_wall"))
        model_time = median_or_none(values(items, "_model_time"))
        work = median_or_none(values(items, "_wall_work"))
        think = median_or_none(values(items, "think_share"))
        output = median_or_none(values(items, "output_share"))
        tool = median_or_none(values(items, "tool_share"))
        self_test = median_or_none(values(items, "self_test_share"))
        tokens = median_or_none(values(items, "max_request_tokens"))
        reason = median_or_none(values(items, "reasoning_tokens"))
        toks = median_or_none(values(items, "gen_tokens_per_s_median"))
        if toks is None:
            toks = median_or_none(values(items, "gen_tokens_per_s"))
        retries = median_or_none(values(items, "retries"))
        retries_cell = (
            f'<td class="num">{retries:.0f}</td>'
            if retries is not None
            else '<td class="num">—</td>'
        )
        rows_html.append(
            "<tr>"
            f"<td>{escaped(model)}</td>"
            f"<td>{escaped(variant)}</td>"
            f"<td>{escaped(thinking)}</td>"
            f'<td class="num">{runs}</td>'
            f'<td class="num">{score:.1f}</td>'
            f'<td class="num">{f"x{decode:.3f}" if decode is not None else "—"}</td>'
            f'<td class="num">{f"x{encode:.3f}" if encode is not None else "—"}</td>'
            f'<td class="num">{format_optional_duration(wall)}</td>'
            f'<td class="num">{format_optional_duration(model_time)}</td>'
            f'<td class="num">{format_optional_duration(work)}</td>'
            f'<td class="num">{format_percent(think)}</td>'
            f'<td class="num">{format_percent(output)}</td>'
            f'<td class="num">{format_percent(tool)}</td>'
            f'<td class="num">{format_percent(self_test)}</td>'
            f'<td class="num">{format_tokens(tokens)}</td>'
            f'<td class="num">{format_tokens(reason)}</td>'
            f'<td class="num">{format_tokens(toks)}</td>'
            f"{retries_cell}"
            "</tr>"
        )
    return (
        '<details class="medians"><summary>Model medians (all runs)</summary>'
        '<div class="table-wrap"><table class="medians-table"><thead><tr>'
        "<th>Model</th><th>Variant</th><th>Thinking</th><th>Runs</th>"
        "<th>Score med</th><th>Decode med</th><th>Encode med</th>"
        "<th>Wall med</th><th>Model med</th><th>Work med</th><th>Think% med</th>"
        "<th>Out% med</th><th>Tool% med</th><th>Self-test% med</th>"
        "<th>Peak req tok med</th><th>Reason tok med</th><th>tok/s med</th>"
        "<th>Retries med</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table></div></details>"
    )


def aggregate_entries(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        if not row.get("run_id"):
            continue
        if row.get("agent_error") or str(row.get("outcome") or "") in {
            "agent-error",
            "provider-error",
        }:
            continue
        key = (
            str(row.get("model") or "?"),
            str(row.get("variant") or "default"),
            thinking_label(row),
        )
        groups.setdefault(key, []).append(row)
    entries: list[dict] = []
    for (model, variant, thinking), group in groups.items():
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

        def metric_values(key: str) -> list[float]:
            return [
                float(row[key])
                for row in scored_rows
                if _float(row.get(key)) is not None
            ]

        entries.append(
            {
                "model": model,
                "variant": variant,
                "thinking": thinking,
                "runs": len(group),
                "eligible_runs": len(eligible_rows),
                "median_score": _median(scores) or 0.0,
                "best_score": max(all_scores) if all_scores else 0.0,
                "gate_rate": sum(gate_values) / len(gate_values) if gate_values else 0.0,
                "median_decode": _median(metric_values("decode_speedup")),
                "median_encode": _median(metric_values("encode_speedup")),
                "median_wall": _median(metric_values("wall_s")),
                "median_model_time": _median(metric_values("model_time_s")),
                "median_work": _median(metric_values("wall_ex_self_test_s")),
                "median_think": _median(metric_values("think_share")),
                "median_output": _median(metric_values("output_share")),
                "median_tool": _median(metric_values("tool_share")),
                "median_self_test": _median(metric_values("self_test_share")),
                "median_request_tokens": _median(metric_values("max_request_tokens")),
                "median_reasoning_tokens": _median(metric_values("reasoning_tokens")),
                "median_toks": _median(
                    metric_values("gen_tokens_per_s_median")
                    or metric_values("gen_tokens_per_s")
                ),
                "median_retries": _median(metric_values("retries")),
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
            entry["thinking"],
        )
    )
    return entries


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def render_leaderboard(
    rows: list[dict],
    *,
    public: bool = False,
    title: str = "opencode-bench-htj2k leaderboard",
    subtitle: str | None = None,
) -> str:
    entries = leaderboard_entries(rows)
    escaped = html.escape

    providers = sorted({provider_label(entry) for entry in entries})
    reasoning_values = sorted(
        {reasoning_key(entry) for entry in entries},
        key=lambda value: (-REASONING_ORDER.get(value, -1), value),
    )
    provider_options = '<option value="">All providers</option>' + "".join(
        f'<option value="{escaped(provider)}">{escaped(provider)}</option>'
        for provider in providers
    )
    reasoning_options = '<option value="">All reasoning</option>' + "".join(
        f'<option value="{escaped(value)}">{escaped(value)}</option>'
        for value in reasoning_values
    )

    eligible_entries = [entry for entry in entries if entry["_eligible"]]
    decode_values = [
        entry["_decode"] for entry in eligible_entries if entry["_decode"] is not None
    ]
    encode_values = [
        entry["_encode"] for entry in eligible_entries if entry["_encode"] is not None
    ]
    toks_values = [
        float(entry["gen_tokens_per_s_median"])
        if _float(entry.get("gen_tokens_per_s_median")) is not None
        else float(entry["gen_tokens_per_s"])
        for entry in entries
        if _float(entry.get("gen_tokens_per_s_median")) is not None
        or _float(entry.get("gen_tokens_per_s")) is not None
    ]
    think_values = [
        float(entry["think_share"])
        for entry in entries
        if _float(entry.get("think_share")) is not None
    ]
    fastest_eligible = min(
        (
            entry["_model_time"]
            for entry in eligible_entries
            if entry["_model_time"]
        ),
        default=None,
    )
    retries_total = sum(int(_float(entry.get("retries")) or 0) for entry in entries)
    best_score = max(
        (entry["_score"] for entry in eligible_entries),
        default=None,
    )
    cards = [
        ("Runs", str(len(entries))),
        ("Eligible", str(len(eligible_entries))),
        ("Best score", f"{best_score:.1f}" if best_score is not None else "—"),
        (
            "Fastest model time",
            format_optional_duration(fastest_eligible)
            if fastest_eligible is not None
            else "—",
        ),
        (
            "Median decode",
            f"x{_median(decode_values):.3f}" if decode_values else "—",
        ),
        (
            "Median encode",
            f"x{_median(encode_values):.3f}" if encode_values else "—",
        ),
        (
            "Median tok/s",
            f"{median_or_none(toks_values):.0f}" if toks_values else "—",
        ),
        (
            "Median think",
            format_percent(median_or_none(think_values)) if think_values else "—",
        ),
        ("Retries", str(retries_total)),
    ]
    cards_html = "".join(
        f'<div class="card"><span>{escaped(label)}</span><b>{escaped(value)}</b></div>'
        for label, value in cards
    )

    def status_of(entry: dict) -> tuple[str, str]:
        if entry.get("agent_error") or str(entry.get("outcome") or "") in {
            "agent-error",
            "provider-error",
        }:
            return "agent-error", "warn"
        if entry["_eligible"]:
            return "eligible", "good"
        return "ineligible", "bad"

    body = []
    for index, entry in enumerate(entries, 1):
        status, status_kind = status_of(entry)
        model = escaped(str(entry.get("model", "")))
        variant = escaped(str(entry.get("variant") or "default"))
        thinking = escaped(thinking_label(entry))
        run_id = escaped(str(entry.get("run_id", "")))
        provider = provider_label(entry)
        reasoning = reasoning_key(entry)
        hardware = str(entry.get("hardware") or "").strip()
        hardware_line = (
            f'<div class="hardware">{escaped(hardware)}</div>' if hardware else ""
        )
        started, started_full = started_label(entry)
        started_epoch = 0
        started_dt = run_started(entry)
        if started_dt is not None:
            started_epoch = int(started_dt.timestamp())
        issue = entry.get("_issue")
        issue_url = str(entry.get("_issue_url") or "")
        submitter = str(entry.get("submitter") or "")
        if issue is not None and issue_url:
            source = f'<a href="{escaped(issue_url)}" rel="noreferrer">#{escaped(str(issue))}</a>'
        else:
            source = '<span class="muted">—</span>'
        if entry.get("verified"):
            evidence = '<span class="evidence verified">maintainer-checked</span>'
        elif issue is not None:
            evidence = '<span class="evidence reported">self-reported</span>'
        else:
            evidence = '<span class="evidence local">local</span>'
        source_cell = (
            f'<td class="source">{source}<div>{evidence}</div>'
            + (f'<div class="submitter">{escaped(submitter)}</div>' if submitter else "")
            + "</td>"
        )
        decode = entry["_decode"]
        encode = entry["_encode"]
        gate_pass = _float(entry.get("gate_pass"))
        gate_total = _float(entry.get("gate_total"))
        if gate_pass is not None and gate_total is not None:
            gates = f"{int(gate_pass)}/{int(gate_total)}"
            gates_rank = int(gate_pass)
        else:
            gates = "—"
            gates_rank = -1
        compactions = (
            "—" if entry["_compactions"] is None else str(entry["_compactions"])
        )
        wall_sub_parts = [f"model {format_optional_duration(entry.get('_model_time'))}"]
        wall_sub_parts.append(f"work {format_optional_duration(entry['_wall_work'])}")
        self_test_s = _float(entry.get("self_test_s"))
        self_test_runs = int(_float(entry.get("self_test_runs")) or 0)
        retries = int(_float(entry.get("retries")) or 0)
        if self_test_s is not None or self_test_runs:
            wall_sub_parts.append(
                f"test {format_optional_duration(self_test_s)} · {self_test_runs} runs"
            )
        if retries:
            wall_sub_parts.append(f"{retries} retr{'y' if retries == 1 else 'ies'}")
        wall_sub = " · ".join(wall_sub_parts)
        wall_title = (
            f"total {format_optional_duration(entry['_wall'])}; "
            f"model time {format_optional_duration(entry.get('_model_time'))}; "
            f"excluding self-test {format_optional_duration(entry['_wall_work'])}; "
            f"self-test {format_optional_duration(self_test_s)}"
            + (f"; provider retries {retries}" if retries else "")
        )
        score_note = (
            ""
            if entry["_eligible"]
            else (
                f' title="raw {entry["_score"] / LEADERBOARD_INELIGIBLE_FACTOR:.1f}; '
                f'ineligible score ×{LEADERBOARD_INELIGIBLE_FACTOR:g}"'
            )
        )
        search = escaped(
            " ".join(
                [
                    str(entry.get("model", "")),
                    str(entry.get("variant", "")),
                    thinking_label(entry),
                    started,
                    str(entry.get("run_id", "")),
                    str(entry.get("hardware", "")),
                    provider,
                    submitter,
                    str(entry.get("evidence_level", "")),
                ]
            ).lower()
        )
        body.append(
            f'<tr class="{"ineligible" if not entry["_eligible"] else ""}"'
            f' data-search="{search}" data-status="{escaped(status)}"'
            f' data-provider="{escaped(provider)}" data-reasoning="{escaped(reasoning)}"'
            f' data-model="{escaped(str(entry.get("model", "")))}"'
            f' data-variant="{variant}" data-thinking="{reasoning_rank(entry)}"'
            f' data-started="{started_epoch}"'
            f' data-score="{entry["_score"]:.6f}"'
            f' data-decode="{_data_attr(decode)}" data-encode="{_data_attr(encode)}"'
            f' data-gates="{gates_rank}" data-wall="{entry["_wall"]:.6f}"'
            f' data-wall-work="{entry["_wall_work"]:.6f}"'
            f' data-model-time="{_data_attr(entry.get("_model_time"))}"'
            f' data-compactions="{entry["_compactions"] if entry["_compactions"] is not None else 0}"'
            f' data-eligible="{1 if entry["_eligible"] else 0}"'
            f' data-tokens="{_data_attr(entry.get("total_tokens"))}"'
            f' data-tokens-out="{_data_attr(entry.get("output_tokens"))}"'
            f' data-tokens-max="{_data_attr(entry.get("max_request_tokens"))}"'
            f' data-tokens-mean="{_data_attr(entry.get("mean_request_tokens"))}"'
            f' data-tokens-reason="{_data_attr(entry.get("reasoning_tokens"))}"'
            f' data-toks="{_data_attr(entry.get("gen_tokens_per_s"))}"'
            f' data-toks-med="{_data_attr(entry.get("gen_tokens_per_s_median"))}"'
            f' data-build-tool="{_data_attr(entry.get("build_tool_s"))}"'
            f' data-ttft="{_data_attr(entry.get("ttft_s"))}"'
            f' data-think="{_data_attr(entry.get("think_s"))}"'
            f' data-output="{_data_attr(entry.get("output_s"))}"'
            f' data-tool="{_data_attr(entry.get("tool_s"))}"'
            f' data-other="{_data_attr(entry.get("other_s"))}"'
            f' data-self-test="{_data_attr(entry.get("self_test_s"))}"'
            f' data-self-test-runs="{_data_attr(entry.get("self_test_runs"))}"'
            f' data-self-test-failures="{_data_attr(entry.get("self_test_failures"))}"'
            f' data-retries="{_data_attr(entry.get("retries"))}"'
            f' data-retry-s="{_data_attr(entry.get("retry_s"))}"'
            f' data-steps="{_data_attr(entry.get("steps"))}"'
            f' data-files="{_data_attr(entry.get("files_changed"))}"'
            f' data-step-median="{_data_attr(entry.get("step_median_s"))}"'
            f' data-step-p90="{_data_attr(entry.get("step_p90_s"))}"'
            f' data-step-max="{_data_attr(entry.get("step_max_s"))}"'
            f' data-agent="{_data_attr(entry.get("agent_s"))}"'
            f' data-span="{_data_attr(entry.get("span_s"))}"'
            f' data-tokens-step="{_data_attr(entry.get("tokens_per_step"))}"'
            f' data-cache-rate="{_data_attr(entry.get("cache_hit_rate"))}"'
            f' data-cost="{_data_attr(entry.get("cost_usd"))}"'
            f' data-source="{escaped(str(entry.get("metrics_source") or ""))}"'
            f' data-run="{run_id}">'
            f'<td class="rank">{index}</td>'
            f'<td><div class="score"{score_note}><span class="bar" style="width:'
            f'{max(0.0, min(100.0, entry["_score"] / 2)):.1f}%"></span>'
            f'<b>{entry["_score"]:.1f}</b></div></td>'
            f'<td class="model"><div>{model}</div>{hardware_line}'
            f'<div class="runid">{run_id}</div></td>'
            f"{source_cell}"
            f'<td class="started" title="{escaped(started_full)}">{escaped(started)}</td>'
            f'<td>{variant}</td>'
            f'<td>{thinking}</td>'
            f'<td class="num">{f"x{decode:.3f}" if decode is not None else "—"}</td>'
            f'<td class="num">{f"x{encode:.3f}" if encode is not None else "—"}</td>'
            f'<td class="num gates">{gates}</td>'
            f'<td><span class="status {status_kind}">{escaped(status)}</span></td>'
            f'<td>{escaped(str(entry.get("confidence") or "—"))}</td>'
            f'<td class="num">{escaped(str(entry.get("cpu_set") or "—"))}</td>'
            f'<td>{escaped(str(entry.get("network_policy") or "—"))}</td>'
            f'<td class="wall" title="{escaped(wall_title)}">'
            f'{format_optional_duration(entry["_wall"])}'
            f'<div class="wall-sub">{escaped(wall_sub)}</div></td>'
            f'<td class="effort">{_render_effort_cell(entry, escaped)}</td>'
            f'<td class="num">{compactions}</td>'
            "</tr>"
        )

    if not entries:
        table = (
            '<p class="empty">No runs yet. '
            + (
                "Submit one with <code>./bench.py submit --run RUN_ID --gh</code>."
                if public
                else "Start with <code>./bench.py run --model ...</code>."
            )
            + "</p>"
        )
    else:
        table = (
            '<div class="table-wrap"><table id="runs">'
            "<thead><tr>"
            "<th>#</th>"
            '<th data-sort="score" data-type="number" data-default-dir="desc">Score</th>'
            '<th data-sort="model" data-type="text" data-default-dir="asc">Model / run</th>'
            "<th>Source</th>"
            '<th data-sort="started" data-type="number" data-default-dir="desc">Started (UTC)</th>'
            '<th data-sort="variant" data-type="text" data-default-dir="asc">Variant</th>'
            '<th data-sort="thinking" data-type="number" data-default-dir="desc">Thinking</th>'
            '<th data-sort="decode" data-type="number" data-default-dir="desc">Decode</th>'
            '<th data-sort="encode" data-type="number" data-default-dir="desc">Encode</th>'
            '<th data-sort="gates" data-type="number" data-default-dir="desc">Gates</th>'
            '<th data-sort="status" data-type="text" data-default-dir="asc">Status</th>'
            "<th>Confidence</th>"
            "<th>CPU</th>"
            "<th>Network</th>"
            '<th data-sort="wall" data-type="number" data-default-dir="asc">Wall / model / work</th>'
            "<th>Effort</th>"
            '<th data-sort="compactions" data-type="number" data-default-dir="asc">Compactions</th>'
            "</tr></thead><tbody>"
            + "".join(body)
            + "</tbody></table></div>"
        )
        table += _render_model_medians(entries, escaped)

    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    default_subtitle = (
        "Lossless HTJ2K encode/decode optimisation. Score is seed-normalised: the "
        "untouched seed is 0, matching the frozen target in both dimensions is 100 "
        "(cap 200). Correctness gates must pass for eligibility. One pinned CPU, "
        "agent network labelled per run. Effort splits come from the OpenCode "
        "session database and never affect the score."
    )
    subtitle_text = subtitle or default_subtitle
    footer_text = (
        'Community results are self-reported unless marked '
        '<span class="evidence verified">maintainer-checked</span>. Wall time and '
        'tok/s are hardware-dependent; compare only similar setups. '
        'Generated from labeled GitHub issues by <code>./bench.py site</code>. '
        f'<a href="https://github.com/knopkem/opencode-bench-htj2k/blob/main/submissions/TEMPLATE.md">'
        "Submission template</a>."
        if public
        else "Generated by <code>./bench.py leaderboard</code>. That command serves "
        "the page and opens it in the browser; use <code>--write</code> to write the "
        "file only. Raw data: <code>results-opencode-bench-htj2k.csv</code>. "
        "Effort/token fields can be rebuilt from existing run artifacts with "
        "<code>./bench.py backfill</code>."
    )

    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { color-scheme: dark; --bg:#0f1117; --panel:#171a23; --line:#272c3a;
        --text:#e7ebf5; --muted:#8b93a7; --accent:#4a90d9; --good:#3ecf8e;
        --warn:#f0b429; --bad:#ef5b5b; --ttft:#5b6478; --think:#8b7cf6;
        --out:#3ecf8e; --tool:#f0b429; --other:#3a4050; }
* { box-sizing: border-box; }
body { margin:0; padding:32px; background:var(--bg); color:var(--text);
       font:15px/1.5 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header { display:flex; gap:24px; align-items:flex-end; justify-content:space-between;
         flex-wrap:wrap; margin-bottom:20px; }
h1 { margin:0; font-size:28px; letter-spacing:-.02em; }
.sub { margin:6px 0 0; color:var(--muted); max-width:60rem; }
.cards { display:flex; gap:10px; flex-wrap:wrap; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
        padding:10px 14px; min-width:110px; }
.card span { display:block; color:var(--muted); font-size:12px; text-transform:uppercase;
             letter-spacing:.06em; }
.card b { font-size:20px; font-variant-numeric:tabular-nums; }
.formula { background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:12px 16px; color:var(--muted); margin-bottom:18px; }
.formula b { color:var(--text); }
.toolbar { display:flex; gap:12px; align-items:center; margin:0 0 14px; flex-wrap:wrap; }
input[type="search"] { background:var(--panel); border:1px solid var(--line); color:var(--text);
                       border-radius:8px; padding:9px 12px; min-width:280px; font:inherit; }
.toolbar label { color:var(--muted); }
select { background:var(--panel); border:1px solid var(--line); color:var(--text);
         border-radius:8px; padding:9px 12px; font:inherit; }
button { background:var(--accent); border:0; color:#fff; border-radius:8px; padding:9px 14px;
         font:inherit; cursor:pointer; }
.table-wrap { overflow-x:auto; border:1px solid var(--line); border-radius:12px; }
table { width:100%; border-collapse:collapse; min-width:1500px; }
thead th { position:sticky; top:0; background:#1b1f2b; color:var(--muted); font-size:12px;
           text-transform:uppercase; letter-spacing:.05em; text-align:left; padding:12px 14px;
           border-bottom:1px solid var(--line); white-space:nowrap; }
thead th[data-sort] { cursor:pointer; user-select:none; }
thead th[data-sort]:hover { color:var(--text); }
thead th[data-sort]::after { content:"↕"; opacity:.35; margin-left:6px; font-size:10px; }
thead th[data-sort][data-dir="asc"]::after { content:"▲"; opacity:1; }
thead th[data-sort][data-dir="desc"]::after { content:"▼"; opacity:1; }
tbody td { padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:middle; }
tbody tr:last-child td { border-bottom:0; }
tbody tr:hover { background:#1b1f2b; }
tr.ineligible { opacity:.55; }
.rank { font-size:18px; font-weight:700; color:var(--muted); }
tbody tr:nth-child(1) .rank { color:var(--warn); }
tbody tr:nth-child(2) .rank { color:#c9d1e3; }
tbody tr:nth-child(3) .rank { color:#b07d4f; }
.score { position:relative; min-width:120px; height:24px; display:flex; align-items:center; }
.score .bar { position:absolute; inset:0 auto 0 0; background:linear-gradient(90deg,#2f6db3,#4a90d9);
              border-radius:6px; opacity:.55; }
.score b { position:relative; padding-left:8px; font-variant-numeric:tabular-nums; }
.model div:first-child { font-weight:600; }
.runid { color:var(--muted); font:11px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
         word-break:break-all; max-width:380px; }
.started { color:var(--muted); white-space:nowrap; font-variant-numeric:tabular-nums; }
.wall, .num { font-variant-numeric:tabular-nums; white-space:nowrap; }
.wall-sub { color:var(--muted); font-size:11px; margin-top:2px; white-space:nowrap; }
.hardware { color:var(--muted); font-size:11px; margin-top:2px; max-width:380px;
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.source { white-space:nowrap; }
.source a { color:var(--accent); text-decoration:none; }
.source a:hover { text-decoration:underline; }
.status { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px;
          font-weight:700; text-transform:uppercase; letter-spacing:.04em; }
.status.good { background:rgba(62,207,142,.15); color:var(--good); }
.status.warn { background:rgba(240,180,41,.15); color:var(--warn); }
.status.bad { background:rgba(239,91,91,.15); color:var(--bad); }
.evidence { display:inline-block; margin-top:3px; padding:1px 7px; border-radius:999px;
            font-size:10px; text-transform:uppercase; letter-spacing:.05em; }
.evidence.reported { background:rgba(240,180,41,.15); color:var(--warn); }
.evidence.verified { background:rgba(62,207,142,.15); color:var(--good); }
.evidence.local { background:rgba(74,144,217,.15); color:var(--accent); }
.submitter { color:var(--muted); font-size:11px; margin-top:2px; }
.muted { color:var(--muted); }
.detail-btn { background:transparent; border:0; padding:0; color:inherit; text-align:left;
              cursor:pointer; }
.detail-btn:hover .split { filter:brightness(1.18); }
.split { display:flex; width:140px; height:10px; border-radius:999px; overflow:hidden;
         background:#222736; }
.seg { display:block; height:100%; }
.seg.ttft { background:var(--ttft); }
.seg.think { background:var(--think); }
.seg.out { background:var(--out); }
.seg.tool { background:var(--tool); }
.seg.other { background:var(--other); }
.effort { min-width:180px; }
.effort-pcts { display:flex; gap:7px; margin-top:4px; font-size:11px;
               font-variant-numeric:tabular-nums; }
.pct { cursor:help; }
.pct.ttft { color:#9aa4b8; }
.pct.think { color:#b8adff; }
.pct.out { color:#7ee2b5; }
.pct.tool { color:#ffd166; }
.pct.other { color:#8b93a7; }
.effort-meta { display:block; margin-top:2px; color:var(--muted); font-size:11px;
               white-space:nowrap; font-variant-numeric:tabular-nums; }
.medians { margin-top:18px; background:var(--panel); border:1px solid var(--line);
           border-radius:12px; padding:10px 14px; }
.medians summary { cursor:pointer; color:var(--muted); font-weight:600; }
.medians .table-wrap { margin-top:10px; border:0; }
.medians-table { min-width:1300px; }
.legend { display:flex; gap:14px; flex-wrap:wrap; margin-top:8px; font-size:12px;
          color:var(--muted); }
.legend i { display:inline-block; width:10px; height:10px; border-radius:3px;
            margin-right:5px; vertical-align:-1px; }
dialog.effort-dialog { background:var(--panel); color:var(--text); border:1px solid var(--line);
                       border-radius:14px; padding:0; max-width:660px; width:calc(100% - 32px); }
dialog.effort-dialog::backdrop { background:rgba(0,0,0,.55); }
.effort-dialog header { display:flex; justify-content:space-between; align-items:center;
                        padding:16px 20px; border-bottom:1px solid var(--line);
                        margin:0; gap:12px; flex-wrap:nowrap; }
.effort-dialog h2 { margin:0; font-size:18px; min-width:0; overflow-wrap:anywhere; }
.effort-dialog header form { margin:0; flex:0 0 auto; }
.effort-dialog .close { background:transparent; color:var(--muted); padding:2px 8px;
                        font-size:22px; line-height:1; }
.effort-dialog .body { padding:16px 20px 20px; display:grid;
                       grid-template-columns:1fr 1fr; gap:16px 24px; font-size:14px; }
.effort-dialog h3 { margin:0 0 6px; color:var(--muted); font-size:12px;
                    text-transform:uppercase; letter-spacing:.06em; }
.effort-dialog dl { margin:0; display:grid; grid-template-columns:auto 1fr; gap:4px 12px; }
.effort-dialog dt { color:var(--muted); }
.effort-dialog dd { margin:0; text-align:right; font-variant-numeric:tabular-nums; }
.effort-note { grid-column:1/-1; color:var(--warn); font-size:12px; }
.hover-tip { position:fixed; z-index:1000; max-width:300px; padding:7px 10px;
             background:#1b1f2b; color:var(--text); border:1px solid var(--line);
             border-radius:7px; box-shadow:0 8px 24px rgba(0,0,0,.35);
             font-size:12px; line-height:1.4; pointer-events:none; }
.hover-tip.effort-tip { max-width:460px; padding:10px 12px; }
.tip-title { font-weight:600; margin-bottom:6px; }
.tip-table { border-collapse:collapse; width:100%; min-width:0; font-size:12px; }
.tip-table td { padding:2px 7px 2px 0; white-space:nowrap; vertical-align:middle; }
.tip-table td.num { text-align:right; font-variant-numeric:tabular-nums; }
.tip-table .tip-tokens { color:var(--muted); }
.tip-table .dot { display:inline-block; width:8px; height:8px; border-radius:2px;
                  margin-right:6px; vertical-align:0; }
.dot.ttft { background:var(--ttft); }
.dot.think { background:var(--think); }
.dot.out { background:var(--out); }
.dot.tool { background:var(--tool); }
.dot.other { background:var(--other); }
.tip-footer { margin-top:6px; padding-top:6px; border-top:1px solid var(--line);
              color:var(--muted); font-variant-numeric:tabular-nums; }
.tip-note { margin-top:4px; color:var(--warn); }
footer { color:var(--muted); margin-top:18px; font-size:13px; }
code { background:#1b1f2b; border:1px solid var(--line); border-radius:5px; padding:1px 5px; }
.empty { color:var(--muted); }
@media (max-width:700px) { body { padding:18px; } h1 { font-size:22px; } }
</style>
</head>
<body>
<header>
  <div>
    <h1>__TITLE__</h1>
    <p class="sub">__SUBTITLE__ · updated __UPDATED__</p>
  </div>
  <div class="cards">__CARDS__</div>
</header>
<div class="formula">
  <b>score = 100 · (0.5 · decode-credit + 0.5 · encode-credit)</b>, credited per
  dimension as clamp((speedup − 1 − 0.08) / (target − 1 − 0.08), 0, 2) against
  frozen targets (decode x1.35, encode x1.25). Ineligible rows keep
  __INELIGIBLE__ of their score and are dimmed; hover the score for the raw value.
  Runs rank by score, then model time, then start time.
  <div class="legend">
    <span><i style="background:var(--ttft)"></i>TTFT (wait for first token)</span>
    <span><i style="background:var(--think)"></i>thinking</span>
    <span><i style="background:var(--out)"></i>model output / code</span>
    <span><i style="background:var(--tool)"></i>tool execution</span>
    <span><i style="background:var(--other)"></i>startup / gaps</span>
  </div>
  <p style="margin:8px 0 0">Effort is derived from <code>opencode.db</code> timings,
  not from a model proxy. Request tokens are the per-request prompt size (input +
  cache reads/writes); peak and mean are shown rather than cumulative run totals.
  Token counts are provider-reported; a thinking phase with <b>0 reasoning
  tokens</b> means the provider did not report them. Hover the effort cell for the
  breakdown, or click for step distribution, cache hit rate and cost. tok/s is
  hardware-dependent. Effort never changes the score.</p>
</div>
<div class="toolbar">
  <input id="filter" type="search" placeholder="Filter model, variant, thinking or run id">
  <label for="filter-status">Status</label>
  <select id="filter-status">
    <option value="">All statuses</option>
    <option value="eligible">eligible</option>
    <option value="ineligible">ineligible</option>
    <option value="agent-error">agent-error</option>
  </select>
  <label for="filter-provider">Provider</label>
  <select id="filter-provider">__PROVIDER_OPTIONS__</select>
  <label for="filter-reasoning">Reasoning</label>
  <select id="filter-reasoning">__REASONING_OPTIONS__</select>
  <button type="button" onclick="location.reload()">Refresh</button>
</div>
__TABLE__
<dialog id="effort-dialog" class="effort-dialog">
  <header>
    <h2 id="effort-title">Effort details</h2>
    <form method="dialog"><button class="close" aria-label="Close">×</button></form>
  </header>
  <div class="body" id="effort-body"></div>
</dialog>
<div id="hover-tip" class="hover-tip" hidden></div>
<footer>__FOOTER__</footer>
<script>
const search = document.getElementById('filter');
const statusFilter = document.getElementById('filter-status');
const providerFilter = document.getElementById('filter-provider');
const reasoningFilter = document.getElementById('filter-reasoning');
const tableBody = document.querySelector('#runs tbody');
const SORT_STATE_KEY = 'opencode-bench-htj2k-sort-state';
const FILTER_KEY = 'opencode-bench-htj2k-filters';
let sortKey = 'score';
let sortDir = 'desc';
try {
  const savedSort = JSON.parse(localStorage.getItem(SORT_STATE_KEY) || '{}');
  if (savedSort.key && document.querySelector('th[data-sort="' + savedSort.key + '"]')) {
    sortKey = savedSort.key;
    sortDir = savedSort.dir === 'asc' ? 'asc' : 'desc';
  }
  const savedFilters = JSON.parse(localStorage.getItem(FILTER_KEY) || '{}');
  if (savedFilters.status && statusFilter) statusFilter.value = savedFilters.status;
  if (savedFilters.provider && providerFilter) providerFilter.value = savedFilters.provider;
  if (savedFilters.reasoning && reasoningFilter) reasoningFilter.value = savedFilters.reasoning;
} catch (error) {}

function saveFilters() {
  try {
    localStorage.setItem(FILTER_KEY, JSON.stringify({
      status: statusFilter?.value || '',
      provider: providerFilter?.value || '',
      reasoning: reasoningFilter?.value || ''
    }));
  } catch (error) {}
}

function updateRanks() {
  if (!tableBody) return;
  let rank = 0;
  for (const row of tableBody.querySelectorAll('tr')) {
    const cell = row.querySelector('.rank');
    if (row.style.display === 'none') {
      if (cell) cell.textContent = '';
    } else {
      rank += 1;
      if (cell) cell.textContent = String(rank);
    }
  }
}

function applyView() {
  if (!tableBody) return;
  const needle = (search?.value || '').trim().toLowerCase();
  const status = statusFilter?.value || '';
  const provider = providerFilter?.value || '';
  const reasoning = reasoningFilter?.value || '';
  for (const row of tableBody.querySelectorAll('tr')) {
    const ok = (!needle || row.dataset.search.includes(needle))
      && (!status || row.dataset.status === status)
      && (!provider || row.dataset.provider === provider)
      && (!reasoning || row.dataset.reasoning === reasoning);
    row.style.display = ok ? '' : 'none';
  }
  updateRanks();
}

function sortRows() {
  if (!tableBody) return;
  const header = document.querySelector('th[data-sort="' + sortKey + '"]');
  const type = header?.dataset.type || 'text';
  const direction = sortDir === 'asc' ? 1 : -1;
  const rows = [...tableBody.querySelectorAll('tr')];
  rows.sort((a, b) => {
    let av;
    let bv;
    let aMissing = false;
    let bMissing = false;
    if (type === 'number') {
      const rawA = a.dataset[sortKey];
      const rawB = b.dataset[sortKey];
      aMissing = rawA === undefined || rawA === '';
      bMissing = rawB === undefined || rawB === '';
      av = aMissing ? 0 : Number(rawA);
      bv = bMissing ? 0 : Number(rawB);
    } else {
      av = (a.dataset[sortKey] || '').toLowerCase();
      bv = (b.dataset[sortKey] || '').toLowerCase();
    }
    if (type === 'number') {
      if (aMissing && !bMissing) return 1;
      if (bMissing && !aMissing) return -1;
    }
    if (av < bv) return -1 * direction;
    if (av > bv) return direction;
    const score = Number(b.dataset.score || 0) - Number(a.dataset.score || 0);
    if (score) return score;
    return (a.dataset.run || '').localeCompare(b.dataset.run || '');
  });
  rows.forEach((row) => tableBody.appendChild(row));
  for (const th of document.querySelectorAll('th[data-sort]')) {
    th.removeAttribute('data-dir');
    th.removeAttribute('aria-sort');
  }
  if (header) {
    header.dataset.dir = sortDir;
    header.setAttribute('aria-sort', sortDir === 'asc' ? 'ascending' : 'descending');
  }
  updateRanks();
}

for (const header of document.querySelectorAll('th[data-sort]')) {
  header.addEventListener('click', () => {
    const key = header.dataset.sort;
    if (key === sortKey) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortKey = key;
      sortDir = header.dataset.defaultDir || 'asc';
    }
    try {
      localStorage.setItem(SORT_STATE_KEY, JSON.stringify({key: sortKey, dir: sortDir}));
    } catch (error) {}
    sortRows();
    applyView();
  });
}

const effortDialog = document.getElementById('effort-dialog');
const effortBody = document.getElementById('effort-body');
const effortTitle = document.getElementById('effort-title');

function fmtSec(value) {
  const number = Number(value);
  if (value === undefined || value === '' || Number.isNaN(number)) return '—';
  const total = Math.max(0, Math.round(number));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return String(hours).padStart(2, '0') + ':' + String(minutes).padStart(2, '0')
    + ':' + String(seconds).padStart(2, '0');
}

function fmtPct(value) {
  const number = Number(value);
  if (value === undefined || value === '' || Number.isNaN(number)) return '—';
  return (number * 100).toFixed(0) + '%';
}

function fmtTok(value) {
  const number = Number(value);
  if (value === undefined || value === '' || Number.isNaN(number)) return '—';
  if (number >= 1e9) return (number / 1e9).toFixed(1).replace(/\\.0$/, '') + 'B';
  if (number >= 1e6) return (number / 1e6).toFixed(1).replace(/\\.0$/, '') + 'M';
  if (number >= 1e3) return (number / 1e3).toFixed(1).replace(/\\.0$/, '') + 'k';
  return String(Math.round(number));
}

function fmtNum(value, digits = 0) {
  const number = Number(value);
  if (value === undefined || value === '' || Number.isNaN(number)) return '—';
  return number.toFixed(digits);
}

function esc(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[character]));
}

function detailList(items) {
  return items.map(([label, value]) => '<dt>' + esc(label) + '</dt><dd>' + value + '</dd>').join('');
}

function effortBodyHtml(row) {
  const data = row.dataset;
  const note = Number(data.tokensReason || 0) === 0 && Number(data.think || 0) > 60
    ? '<p class="effort-note">Provider reported zero reasoning tokens; the time split still shows the thinking phase.</p>'
    : '';
  const outputNote = Number(data.output || 0) < 1 && Number(data.tokensOut || 0) > 1000
    ? '<p class="effort-note">No separate timestamped output block was recorded; output time is a lower bound.</p>'
    : '';
  const retryNote = Number(data.retries || 0) > 0
    ? '<p class="effort-note">Provider errors/retries are counted in wall time and other, but excluded from think/output and tok/s.</p>'
    : '';
  const sourceNote = data.source === 'log'
    ? '<p class="effort-note">No session database for this run; only log token totals and model time are available.</p>'
    : '';
  return ''
    + '<div><h3>Time</h3><dl>' + detailList([
      ['Wall', fmtSec(data.wall)],
      ['Model time (rank tie-breaker)', fmtSec(data.modelTime)],
      ['Wall excl. self-test', fmtSec(Number(data.wall || 0) - Number(data.selfTest || 0))],
      ['Agent turns', fmtSec(data.agent)],
      ['TTFT (wait for first token)', fmtSec(data.ttft)],
      ['TTFT / step', Number(data.steps || 0) > 0 ? fmtSec(Number(data.ttft || 0) / Number(data.steps)) : '—'],
      ['Thinking', fmtSec(data.think)],
      ['Model output', fmtSec(data.output)],
      ['Tool execution', fmtSec(data.tool)],
      ['Self-test', fmtSec(data.selfTest)],
      ['Build / typecheck', fmtSec(data.buildTool)],
      ['Provider retry', fmtSec(data.retryS)],
      ['Other', fmtSec(data.other)]
    ]) + '</dl></div>'
    + '<div><h3>Request tokens</h3><dl>' + detailList([
      ['Peak request', fmtTok(data.tokensMax)],
      ['Mean request', fmtTok(data.tokensMean)],
      ['Output', fmtTok(data.tokensOut)],
      ['Reasoning', fmtTok(data.tokensReason)],
      ['Total', fmtTok(data.tokens)]
    ]) + '</dl></div>'
    + '<div><h3>Steps</h3><dl>' + detailList([
      ['Steps', fmtNum(data.steps)],
      ['Files changed', fmtNum(data.files)],
      ['Median step', fmtSec(data.stepMedian)],
      ['p90 step', fmtSec(data.stepP90)],
      ['Max step', fmtSec(data.stepMax)]
    ]) + '</dl></div>'
    + '<div><h3>Rates</h3><dl>' + detailList([
      ['Generation tok/s', fmtNum(data.toks, 1)],
      ['Median step tok/s', fmtNum(data.toksMed, 1)],
      ['Self-test runs', fmtNum(data.selfTestRuns)],
      ['Self-test failures', fmtNum(data.selfTestFailures)],
      ['Retries', fmtNum(data.retries)],
      ['Tokens / step', fmtTok(data.tokensStep)],
      ['Cache hit rate', fmtPct(data.cacheRate)],
      ['Cost', fmtNum(data.cost, 4)]
    ]) + '</dl></div>'
    + note + outputNote + retryNote + sourceNote
    + '<p class="effort-note">Source: ' + esc(data.source || 'none') + '. tok/s excludes failed/retried attempts; aggregate is output+reasoning over think+output time, median is per step. Hardware-dependent.</p>';
}

function effortTipHtml(row) {
  const data = row.dataset;
  const wall = Number(data.wall || 0);
  const share = (value) => wall > 0 ? Math.round(Number(value || 0) / wall * 100) + '%' : '—';
  const reasoning = Number(data.tokensReason || 0);
  const thinkTokens = reasoning
    ? fmtTok(reasoning) + ' reasoning'
    : (Number(data.think || 0) > 0 ? 'not reported' : '—');
  const rows = [
    ['ttft', 'TTFT', data.ttft, '—'],
    ['think', 'Thinking', data.think, thinkTokens],
    ['out', 'Output', data.output, fmtTok(data.tokensOut) + ' output tok'],
    ['tool', 'Tools', data.tool, 'no model tokens'],
    ['other', 'Other', data.other, 'no model tokens']
  ];
  const rowsHtml = rows.map(([kind, label, seconds, tokens]) => (
    '<tr><td><i class="dot ' + kind + '"></i>' + esc(label) + '</td>'
    + '<td class="num">' + fmtSec(seconds) + '</td>'
    + '<td class="num">' + share(seconds) + '</td>'
    + '<td class="tip-tokens">' + tokens + '</td></tr>'
  )).join('');
  const caveat = reasoning === 0 && Number(data.think || 0) > 60 && Number(data.tokensOut || 0) > 1000
    ? '<div class="tip-note">Output tokens may include unreported reasoning.</div>'
    : '';
  const retries = Number(data.retries || 0);
  return '<div class="tip-title">Effort · ' + fmtSec(data.wall) + '</div>'
    + '<table class="tip-table">' + rowsHtml + '</table>'
    + '<div class="tip-footer">peak ' + fmtTok(data.tokensMax) + ' / mean ' + fmtTok(data.tokensMean) + ' req tok'
    + ' · ' + fmtNum(data.toks, 1) + ' tok/s aggregate / ' + fmtNum(data.toksMed, 1) + ' median'
    + ' · self-test ' + fmtSec(data.selfTest) + ' (' + fmtNum(data.selfTestRuns) + ' runs)'
    + (retries ? ' · ' + retries + ' retr' + (retries === 1 ? 'y' : 'ies') : '')
    + ' · ' + fmtNum(data.steps) + ' steps · ' + fmtPct(data.cacheRate) + ' cache</div>'
    + caveat;
}

function showEffort(row) {
  if (hoverTip) hoverTip.hidden = true;
  effortTitle.textContent = 'Effort · ' + (row.dataset.run || 'run');
  effortBody.innerHTML = effortBodyHtml(row);
  if (typeof effortDialog.showModal === 'function') {
    effortDialog.showModal();
  } else {
    effortDialog.setAttribute('open', '');
  }
}

for (const button of document.querySelectorAll('.detail-btn')) {
  button.addEventListener('click', () => {
    const row = button.closest('tr');
    if (row) showEffort(row);
  });
}

effortDialog?.addEventListener('click', (event) => {
  if (event.target === effortDialog) effortDialog.close();
});

const hoverTip = document.getElementById('hover-tip');
function placeHoverTip(event, target) {
  const tipRect = hoverTip.getBoundingClientRect();
  let clientX;
  let clientY;
  if (event && typeof event.clientX === 'number' && event.clientX) {
    clientX = event.clientX;
    clientY = event.clientY;
  } else {
    const rect = target.getBoundingClientRect();
    clientX = rect.left + rect.width / 2;
    clientY = rect.top;
  }
  let x = clientX + 12;
  let y = clientY + 16;
  if (x + tipRect.width > window.innerWidth - 8) x = clientX - tipRect.width - 12;
  if (y + tipRect.height > window.innerHeight - 8) y = clientY - tipRect.height - 16;
  hoverTip.style.left = Math.max(8, x) + 'px';
  hoverTip.style.top = Math.max(8, y) + 'px';
}
function showEffortTip(row, event, target) {
  hoverTip.className = 'hover-tip effort-tip';
  hoverTip.innerHTML = effortTipHtml(row);
  hoverTip.hidden = false;
  placeHoverTip(event, target);
}
for (const button of document.querySelectorAll('.detail-btn')) {
  button.addEventListener('mouseenter', (event) => {
    const row = button.closest('tr');
    if (row) showEffortTip(row, event, button);
  });
  button.addEventListener('mousemove', (event) => placeHoverTip(event, button));
  button.addEventListener('mouseleave', () => { hoverTip.hidden = true; });
}
window.addEventListener('scroll', () => { hoverTip.hidden = true; }, true);

search?.addEventListener('input', applyView);
statusFilter?.addEventListener('change', () => { saveFilters(); applyView(); });
providerFilter?.addEventListener('change', () => { saveFilters(); applyView(); });
reasoningFilter?.addEventListener('change', () => { saveFilters(); applyView(); });

sortRows();
applyView();

if (new URLSearchParams(location.search).get('live') === '1') {
  setTimeout(() => location.reload(), 30000);
}
</script>
</body>
</html>
"""
    return (
        template.replace("__TITLE__", escaped(title))
        .replace("__SUBTITLE__", escaped(subtitle_text))
        .replace("__UPDATED__", escaped(updated))
        .replace("__CARDS__", cards_html)
        .replace("__PROVIDER_OPTIONS__", provider_options)
        .replace("__REASONING_OPTIONS__", reasoning_options)
        .replace("__TABLE__", table)
        .replace("__FOOTER__", footer_text)
        .replace("__INELIGIBLE__", f"{LEADERBOARD_INELIGIBLE_FACTOR:.0%}")
    )


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
    if meta.get("agent_error"):
        print(
            f"  AGENT ERROR: {meta['agent_error'].get('type')} "
            f"({meta['agent_error'].get('status')}): "
            f"{meta['agent_error'].get('message', '')[:180]}"
            + (
                f" (retry after {meta['provider_retry_after_s']:.0f}s)"
                if meta.get("provider_retry_after_s")
                else ""
            )
        )
    elif meta.get("provider_error_count"):
        print(
            f"  provider errors: {meta['provider_error_count']} "
            f"({meta.get('provider_recovered', 0)} recovered, "
            f"{meta.get('provider_retries', 0)} retries) — not fatal"
        )
    print(
        f"  score {score['score']:.1f}"
        f"   decode x{score['decode_speedup']:.3f}"
        f"   encode x{score['encode_speedup']:.3f}"
        f"   {'ELIGIBLE' if score['eligible'] else 'not eligible'}"
    )
    if meta.get("integrity_violation"):
        print("  network INTEGRITY VIOLATION — run ineligible; score retained for diagnostics")
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


def _minutes(value: float) -> str:
    rounded = max(0.0, float(value))
    text = f"{rounded:g}"
    return f"{text} minute" if abs(rounded - 1.0) < 1e-9 else f"{text} minutes"


def _round_prompt_header(
    number: int,
    total: int,
    budget_minutes: float,
    remaining_minutes: float,
) -> str:
    return (
        f"# Round {number} of {total}\n\n"
        f"This round has **{_minutes(budget_minutes)}**; the total agent budget is "
        f"**{_minutes(remaining_minutes)}** remaining.\n\n"
        f"The live round clock is in `/work/ROUND-TIME.md`; check `date -u` against "
        f"its deadline.\n\n"
    )


def write_round_time(
    workdir: Path,
    round_number: int,
    budget_s: float,
    started_at: float | None = None,
) -> Path:
    """Write the round clock the agent can read instead of guessing."""
    start = time.time() if started_at is None else started_at
    deadline = start + max(1.0, budget_s)
    text = (
        f"# Round {round_number} clock\n\n"
        f"- started: {datetime.fromtimestamp(start, timezone.utc).isoformat(timespec='seconds')}\n"
        f"- deadline: {datetime.fromtimestamp(deadline, timezone.utc).isoformat(timespec='seconds')}\n"
        f"- budget: {budget_s / 60.0:.1f} minutes\n\n"
        "Check the current time with `date -u`. Keep working while there is "
        "useful, verified work to do; do not stop early. The interactive "
        "`question` tool is disabled in this benchmark.\n"
    )
    path = workdir / "ROUND-TIME.md"
    path.write_text(text, encoding="utf-8")
    return path


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


def write_continue_prompt(workdir: Path, round_number: int, attempt: int, remaining_s: float) -> Path:
    text = (
        f"# Round {round_number} continues ({attempt})\n\n"
        f"You still have about **{_minutes(remaining_s / 60.0)}** left in this "
        "round. Your previous turn ended early, but the round is not over.\n\n"
        "Keep working in `/work`: continue the highest-value optimisation you "
        "identified, verify it against a pristine seed, and keep the CLI contract "
        "and correctness gates intact. Do not write `COMPLETION.md` yet.\n\n"
        "Read `/work/ROUND-TIME.md` for the round deadline and check `date -u`.\n"
    )
    path = workdir / f"ROUND-{round_number}-CONTINUE-{attempt}.md"
    path.write_text(text, encoding="utf-8")
    return path


def write_retry_prompt(
    workdir: Path, round_number: int, attempt: int, error: dict | None
) -> Path:
    detail = ""
    if error:
        detail = (
            f"The failure was `{error.get('type')}` ({error.get('status')}): "
            f"{str(error.get('message') or '')[:300]}\n"
        )
    text = (
        f"# Round {round_number} retry {attempt}\n\n"
        "The provider failed at the transport level, not because of your work. "
        "`/work` is exactly as you left it and this is the same session.\n\n"
        + detail
        + "\nContinue the round from the current state. Re-run the build and tests "
        "before changing anything else, and do not redo work that already passes.\n"
    )
    path = workdir / f"ROUND-{round_number}-RETRY-{attempt}.md"
    path.write_text(text, encoding="utf-8")
    return path


def checkpoint_prompt_text(checkpoint: dict) -> str:
    score = checkpoint.get("score") or {}
    tests = checkpoint.get("tests") or {}
    perf = (tests.get("result") or {}).get("perf") or {}

    def band(kind: str) -> float:
        values = [
            float(item.get("ratio_ci95") or 0.0)
            for item in perf.get(kind) or []
            if item.get("valid")
        ]
        return max(values) if values else 0.0

    lines = [
        f"## Checkpoint after round {checkpoint.get('round')}",
        "",
        f"- build: {'ok' if checkpoint.get('build', {}).get('build_ok') else 'FAILED'}",
        f"- gates: {'pass' if score.get('gate_ok') else 'FAIL'} "
        f"({score.get('gate_pass', 0)}/{score.get('gate_total', 0)})",
        f"- decode speedup: x{float(score.get('decode_speedup') or 0):.3f} "
        f"(target x{float(score.get('target_decode') or 0):.2f}) "
        f"+/-{band('decode'):.3f}",
        f"- encode speedup: x{float(score.get('encode_speedup') or 0):.3f} "
        f"(target x{float(score.get('target_encode') or 0):.2f}) "
        f"+/-{band('encode'):.3f}",
        f"- validation score: {float(score.get('score') or 0):.1f}",
        "- this checkpoint is a single measurement; differences inside the band "
        "are noise, so keep changes that are clearly above it and re-measure "
        "before drawing conclusions.",
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
            "This is the **final round**: the best eligible checkpoint is graded, "
            "so optimizing is still allowed while time remains. **Freeze 10 "
            "minutes before the deadline** in `/work/ROUND-TIME.md`: remove all "
            "profiling/timing instrumentation, run `cargo build --release "
            "--offline --locked`, `cargo test --workspace --offline` and "
            "`python3 scripts/bench.py --reps 1`, then write `COMPLETION.md`. "
            "Do not leave instrumentation or an unverified change in the tree."
            if is_final
            else "Do **not** write `COMPLETION.md` yet. Continue optimizing; the "
            "next checkpoint will build and score your tree. If your turn ends "
            "with time left, the harness will resume the session."
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
        "self_test_failures": 0,
        "build_tool_s": 0.0,
        "retries": 0,
        "retry_s": 0.0,
        "steps": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "compaction_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "max_request_tokens": 0,
        "mean_request_tokens": 0,
        "metrics_source": "round-logs",
        "metrics_version": METRICS_VERSION,
    }
    for row in rounds:
        source = row.get("metrics") or {}
        for key in metrics:
            if key in {
                "steps",
                "max_request_tokens",
                "mean_request_tokens",
                "metrics_source",
                "metrics_version",
            }:
                continue
            value = source.get(key)
            if isinstance(value, (int, float)):
                metrics[key] += value
        metrics["max_request_tokens"] = max(
            metrics["max_request_tokens"], int(source.get("max_request_tokens") or 0)
        )
        metrics["steps"] += int(row.get("steps") or 0)
    if rounds:
        means = [
            float(row["metrics"].get("mean_request_tokens") or 0)
            for row in rounds
            if row.get("metrics", {}).get("mean_request_tokens") is not None
        ]
        metrics["mean_request_tokens"] = sum(means) / len(means) if means else 0
    return metrics


def round_summary(row: dict) -> dict:
    validation = row.get("score") or {}
    final = (row.get("final") or {}).get("score") or {}
    return {
        "round": row.get("round"),
        "budget_s": row.get("budget_s"),
        "wall_s": row.get("wall_s"),
        "watchdog": row.get("watchdog"),
        "nudges": int(row.get("nudges") or 0),
        "completion": row.get("completion"),
        "provider_error": bool(row.get("provider_error")),
        "provider_error_count": len(row.get("provider_errors") or []),
        "provider_recovered": len(
            [event for event in (row.get("provider_errors") or []) if event.get("recovered")]
        ),
        "provider_retries": int(row.get("provider_retries") or 0),
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
    config_dir = prepare_agent_config(config_dir, run_dir)
    copy_seed(workdir)

    env_names = config_env_names(config_dir) | set(args.env or [])
    if not args.no_preflight:
        available = list_models(provider_of(model), config_dir)
        if model not in available:
            raise SystemExit(
                f"error: '{model}' is not in `opencode models` for {config_dir}; "
                "run ./bench.py models or pass --no-preflight"
            )

    thinking, effort = model_thinking(model, variant, config_dir)
    allowlist = resolve_agent_allowlist(model, config_dir, args.allow_host)
    variant_label = variant or "default"
    for image, agent in ((args.agent_image, True), (args.test_image, False)):
        if not ensure_image(args.engine, image, dry_run=args.dry_run):
            raise SystemExit(
                f"error: image {image} unavailable; run: {image_help(args.engine, image, agent=agent)}"
            )
    manifest = load_manifest(args.engine, args.test_image)
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
    print(f"  egress  provider-only ({', '.join(format_allowlist(allowlist))})")
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

        round_started = time.time()
        write_round_time(workdir, round_number, budget_s, round_started)
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
        attempts: list[dict] = []
        max_attempts = 1 + max(0, int(getattr(args, "provider_retries", 0)))
        max_nudges = 0 if final_round else max(0, int(getattr(args, "max_nudges", 2)))
        nudge_min = max(0.0, float(getattr(args, "nudge_min", 10.0)) * 60.0)
        nudges = 0
        continue_prompt: Path | None = None
        while True:
            provider_attempt = 0
            run_attempts: list[dict] = []
            while True:
                attempt_budget = max(30.0, budget_s - (time.time() - round_started))
                retry = provider_attempt > 0
                if retry:
                    prompt = write_retry_prompt(
                        workdir,
                        round_number,
                        provider_attempt,
                        attempts[-1].get("provider_error"),
                    )
                    log_suffix = f"-retry{provider_attempt}"
                elif continue_prompt is not None:
                    prompt = continue_prompt
                    log_suffix = f"-continue{nudges}"
                else:
                    log_suffix = ""
                agent = start_agent(
                    model,
                    config_dir,
                    workdir,
                    data_dir,
                    run_dir,
                    env_names=env_names,
                    allowlist=allowlist,
                    args=args,
                    round_number=round_number,
                    prompt_file=prompt,
                    session_id=session_id,
                    continue_session=True
                    if (retry or continue_prompt is not None)
                    else bool(index > 0 and not session_id),
                    timeout_s=attempt_budget,
                    idle_timeout_s=min(idle_s, attempt_budget),
                    log_suffix=log_suffix,
                    watch_completion=not retry,
                )
                attempts.append(agent)
                run_attempts.append(agent)
                provider_attempt += 1
                provider_error = agent.get("provider_error")
                for event in agent.get("provider_errors") or []:
                    state = "recovered" if event.get("recovered") else "unrecovered"
                    print(
                        f"  provider error ({state}): {event.get('type')} "
                        f"({event.get('status')}): {event.get('message', '')[:160]}"
                    )
                if provider_error is None:
                    if agent.get("provider_errors"):
                        print("  engine resumed after the provider error; continuing")
                    break
                if agent.get("provider_terminal") or provider_attempt >= max_attempts:
                    break
                delay = provider_retry_delay(agent, run_attempts, args)
                left = budget_s - (time.time() - round_started)
                if left <= delay + 60:
                    print("  not enough round budget left to retry; keeping the checkpoint")
                    break
                print(
                    f"  transient provider failure; retrying the round in "
                    f"{format_duration(delay)} (attempt {provider_attempt + 1}/{max_attempts})"
                )
                time.sleep(delay)
                session_id = session_id or agent.get("session_id")
            session_id = session_id or attempts[-1].get("session_id")
            agent = attempts[-1]
            left = budget_s - (time.time() - round_started)
            if (
                not final_round
                and provider_error is None
                and not completion.exists()
                and nudges < max_nudges
                and left >= nudge_min
                and agent.get("watchdog") == "exit"
                and agent.get("agent_ok")
            ):
                nudges += 1
                continue_prompt = write_continue_prompt(workdir, round_number, nudges, left)
                print(
                    f"  agent ended its turn with {format_duration(left)} left; "
                    "resuming the session"
                )
                continue
            break
        session_id = session_id or attempts[-1].get("session_id")
        agent = attempts[-1]
        network_integrity = combine_network_audits(attempts)
        if network_integrity["integrity_violation"]:
            print("  network integrity violation — see egress logs; run will be ineligible")
        log_path = Path(agent.get("log_path") or (run_dir / f"agent-round-{round_number}.log"))
        metrics = combine_attempt_metrics(attempts)
        steps = sum(count_log_steps(Path(item.get("log_path") or log_path)) for item in attempts)
        round_wall = sum(float(item.get("wall_s") or 0.0) for item in attempts)
        integrity = integrity_scan(workdir)
        if integrity["problems"]:
            print("  integrity problems:")
            for problem in integrity["problems"]:
                print(f"    - {problem}")

        build = build_app(
            run_dir,
            workdir,
            engine=args.engine,
            image=args.agent_image,
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
            integrity_violation=network_integrity["integrity_violation"],
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
                "wall_s": round_wall,
                "watchdog": agent["watchdog"],
                "nudges": nudges,
                "completion": bool(agent["completion"]),
                "agent_ok": bool(agent["agent_ok"]),
                "agent_exit_code": agent["agent_exit_code"],
                "session_id": session_id,
                "provider_error": provider_error,
                "provider_errors": [
                    {**event, "attempt": position + 1}
                    for position, item in enumerate(attempts)
                    for event in (item.get("provider_errors") or [])
                ],
                "provider_retries": len(attempts) - 1,
                "metrics": metrics,
                "steps": steps,
                "integrity": integrity,
                "network_integrity": network_integrity,
                "build": build,
                "tests": tests,
                "score": score,
                "dir": str(round_dir),
                "artifacts": str(round_dir / "artifacts"),
                "source": str(round_dir / "source.tar"),
                "agent_log": str(log_path),
                "agent_logs": [str(item.get("log_path") or log_path) for item in attempts],
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
        if provider_error:
            # Quota/auth failures are terminal, and a transient failure that
            # survived every retry means the provider is not answering. Keep
            # the checkpoint, then stop instead of burning later rounds on a
            # dead provider.
            break

    if args.dry_run:
        print("dry run complete; no score or report written")
        shutil.rmtree(run_dir, ignore_errors=True)
        return {}

    if not rounds:
        raise SystemExit("error: no rounds were executed")

    # Grade every eligible checkpoint on a held-out final corpus and keep the best.
    final_reps = max(1, int(getattr(args, "final_reps", 2)))
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
        final_tests = run_tests_repeated(
            run_dir,
            repeats=final_reps,
            engine=args.engine,
            image=args.test_image,
            dry_run=False,
            manifest=manifest,
            fixture_seed=final_seed,
            artifacts_dir=final_dir,
            bin_dir=final_dir / "bin",
            require_single_cpu=not args.no_cpu_set,
            args=args,
        )
        final_score = score_run(
            final_tests,
            row["build"],
            manifest=manifest,
            result_valid=final_tests.get("result_valid", False),
            integrity_violation=row.get("network_integrity", {}).get("integrity_violation", False),
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

    log_metrics = aggregate_agent_metrics(rounds)
    run_wall = sum(float(row.get("wall_s") or 0.0) for row in rounds)
    metrics = compute_metrics(None, data_dir, run_wall, usage=log_metrics)
    metrics["compactions"] = count_compactions(data_dir)
    metrics["steps"] = sum(int(row.get("steps") or 0) for row in rounds)

    provider_errors = [row.get("provider_error") for row in rounds if row.get("provider_error")]
    agent_error = provider_errors[-1] if provider_errors else None
    provider_events = [
        {**event, "round": row.get("round")}
        for row in rounds
        for event in (row.get("provider_errors") or [])
    ]
    recovered_provider_errors = [event for event in provider_events if event.get("recovered")]
    provider_retries = sum(int(row.get("provider_retries") or 0) for row in rounds)
    final_environment = (chosen_tests.get("result") or {}).get("environment") or {}
    network_integrity = combine_network_audits(rounds)
    final_score = dict(chosen_final) if chosen_final else {}
    if agent_error:
        final_score["eligible"] = False
        final_score["score"] = 0.0
        final_score["quality"] = 0.0
        for row in rounds:
            if row.get("final"):
                row["final"]["score"]["eligible"] = False
                row["final"]["score"]["score"] = 0.0
    if network_integrity["integrity_violation"]:
        final_score["eligible"] = False
        final_score["integrity_violation"] = True
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
        "final_reps": final_reps,
        "confidence": "single-run",
        "agent_error": agent_error,
        "provider_errors": provider_events,
        "provider_error_count": len(provider_events),
        "provider_recovered": len(recovered_provider_errors),
        "provider_retries": provider_retries,
        "provider_retry_after_s": (
            float(agent_error.get("retry_after_ms")) / 1000.0
            if agent_error and agent_error.get("retry_after_ms")
            else None
        ),
        "budget_wall_cap_s": args.timeout,
        "budget_idle_timeout_s": args.idle_timeout,
        "wall_s": metrics["span_s"],
        "compactions": metrics["compactions"],
        "watchdog": rounds[-1].get("watchdog"),
        "round_watchdogs": [row.get("watchdog") for row in rounds],
        "completion": rounds[-1].get("completion", False),
        "outcome": "provider-error"
        if agent_error
        else classify_outcome(
            watchdog=str(rounds[-1].get("watchdog") or "exit"),
            completion=bool(rounds[-1].get("completion")),
            files_changed=count_changed_files(workdir),
            agent_ok=bool(rounds[-1].get("agent_ok")),
        ),
        "steps": sum(int(row.get("steps") or 0) for row in rounds),
        **metrics,
        "files_changed": count_changed_files(workdir),
        "agent_ok": bool(rounds[-1].get("agent_ok")) and agent_error is None,
        "agent_exit_code": rounds[-1].get("agent_exit_code"),
        "build_ok": bool(final_build.get("build_ok")),
        "seed_build_ok": bool(final_build.get("seed_build_ok")),
        "typecheck_ok": bool(final_build.get("typecheck_ok")),
        "test_ok": bool(chosen_tests.get("test_ok")),
        "test_error": chosen_tests.get("error"),
        "result_valid": bool(final_score.get("result_valid")),
        "gate_ok": bool(final_score.get("gate_ok")),
        "gate_pass": int(final_score.get("gate_pass") or 0),
        "gate_total": int(final_score.get("gate_total") or 0),
        "perf_valid": bool(final_score.get("perf_valid")),
        "eligible": bool(final_score.get("eligible")),
        "integrity_violation": bool(network_integrity["integrity_violation"]),
        "integrity_reasons": network_integrity["integrity_reasons"],
        "network_denied": network_integrity["network_denied"],
        "network_denied_hosts": network_integrity["network_denied_hosts"],
        "network_allowed_hosts": format_allowlist(allowlist),
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
        "agent_logs": [
            str(path)
            for row in rounds
            for path in (
                row.get("agent_logs")
                or [row.get("agent_log") or (run_dir / f"agent-round-{row['round']}.log")]
            )
        ],
        "build_log": str(chosen_dir / "artifacts" / ".." / "build-run.log"),
        "test_log": str(chosen_dir / "final" / "test.log"),
    }
    meta["agent_log"] = str(run_dir / "agent-round-1.log")
    meta["build_log"] = str(chosen_dir / "build-run.log")
    meta["test_log"] = str(chosen_dir / "final" / "test.log")
    meta.update(collect_image_metadata(args.engine, args.agent_image, args.test_image))
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


def validate_suite_manifest(manifest: dict) -> dict:
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise SystemExit(f"error: evaluator manifest schema must be {MANIFEST_SCHEMA!r}")
    if manifest.get("suite_version") != SUITE_VERSION:
        raise SystemExit(f"error: evaluator manifest suite_version must be {SUITE_VERSION!r}")
    if not manifest.get("correctness"):
        raise SystemExit("error: evaluator manifest has no correctness cases")
    if not manifest.get("perf", {}).get("decode"):
        raise SystemExit("error: evaluator manifest has no decode perf workloads")
    if not manifest.get("perf", {}).get("encode"):
        raise SystemExit("error: evaluator manifest has no encode perf workloads")
    for name, target in (manifest.get("targets") or {}).items():
        try:
            value = float(target)
        except (TypeError, ValueError):
            raise SystemExit(f"error: evaluator manifest target {name} is not numeric")
        if value < 1.05:
            raise SystemExit(f"error: evaluator manifest target {name} must be >= 1.05")
    if int(manifest.get("limits", {}).get("perf_repetitions", 0)) < 3:
        raise SystemExit("error: evaluator manifest perf_repetitions must be >= 3")
    return manifest


def load_manifest(engine: str | None = None, image: str | None = None) -> dict:
    if not (engine and image):
        raise SystemExit(
            "error: the frozen suite manifest lives in the evaluator image; "
            "run ./bench.py check or pass --engine/--test-image"
        )
    manifest, error = read_image_manifest(engine, image)
    if error:
        raise SystemExit(f"error: cannot read suite manifest from {image}: {error}")
    return validate_suite_manifest(manifest)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_build(args: argparse.Namespace) -> int:
    if not engine_available(args.engine, args.dry_run):
        eprint(f"error: {args.engine} is not available; start Docker/OrbStack or pass --engine")
        return 2
    command = [
        args.engine,
        "build",
        "-f",
        str(ROOT / "docker" / "agent.Dockerfile"),
        "-t",
        args.agent_image,
        "--build-arg",
        f"RUST_VERSION={args.rust_version}",
        "--build-arg",
        f"OPENCODE_VERSION={args.opencode_version}",
        str(ROOT),
    ]
    print("+ " + " ".join(shlex.quote(part) for part in command))
    if not args.dry_run:
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


def cmd_build_eval(args: argparse.Namespace) -> int:
    """Build the evaluator image from a private suite checkout."""
    tests_dir = Path(args.tests_dir).expanduser().resolve()
    required = (
        tests_dir / "runner.py",
        tests_dir / "corpus.py",
        tests_dir / "manifest.json",
        tests_dir / "fixtures",
        tests_dir / "oracle" / "OpenJPH" / "CMakeLists.txt",
        tests_dir / "oracle" / "oracle.cpp",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        eprint(f"error: evaluator tests directory is incomplete: {tests_dir}")
        for path in missing:
            eprint(f"  missing {path}")
        return 2
    try:
        manifest = json.loads((tests_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        eprint(f"error: cannot read {tests_dir / 'manifest.json'}: {exc}")
        return 2
    try:
        validate_suite_manifest(manifest)
    except SystemExit as exc:
        eprint(str(exc))
        return 2
    if not engine_available(args.engine, args.dry_run):
        eprint(f"error: {args.engine} is not available; start Docker/OrbStack or pass --engine")
        return 2
    suite_version = args.suite_version or os.environ.get(
        "OPENCODE_BENCH_EVALUATOR_SUITE_VERSION", "local"
    )
    source_revision = args.source_revision or os.environ.get(
        "OPENCODE_BENCH_EVALUATOR_SOURCE_REVISION", "local"
    )
    command = [
        args.engine,
        "build",
        "-f",
        str(ROOT / "docker" / "evaluator.Dockerfile"),
        "--build-context",
        f"tests={tests_dir}",
        "-t",
        args.test_image,
        "--build-arg",
        f"RUST_VERSION={args.rust_version}",
        "--build-arg",
        f"EVALUATOR_SUITE_VERSION={suite_version}",
        "--build-arg",
        f"EVALUATOR_SOURCE_REVISION={source_revision}",
        str(ROOT),
    ]
    print("+ " + " ".join(shlex.quote(part) for part in command))
    if args.dry_run:
        return 0
    return subprocess.run(command, check=False).returncode


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
    if not (ROOT / "docker" / "evaluator.Dockerfile").exists():
        problems.append("missing docker/evaluator.Dockerfile")
    if not EGRESS_PROXY_PATH.exists():
        problems.append("missing docker/egress_proxy.py")
    if not (SEED_DIR / "Cargo.lock").exists():
        problems.append("missing seed Cargo.lock")
    if not (SEED_DIR / "vendor").exists():
        problems.append("missing seed vendor/")
    if not (SEED_DIR / "crates" / "htj2k-cli" / "src" / "main.rs").exists():
        problems.append("missing seed CLI")
    if not (SEED_DIR / "corpus" / "public" / "manifest.json").exists():
        problems.append("missing public fixtures")
    manifest: dict = {}
    if args.no_docker:
        print("note: --no-docker skips image and evaluator-suite checks")
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
            agent_ok = ensure_image(args.engine, args.agent_image, dry_run=args.dry_run)
            if not agent_ok:
                problems.append(
                    f"agent image {args.agent_image} unavailable; run: "
                    f"{image_help(args.engine, args.agent_image, agent=True)}"
                )
            test_ok = ensure_image(args.engine, args.test_image, dry_run=args.dry_run)
            if not test_ok:
                problems.append(
                    f"evaluator image {args.test_image} unavailable; run: "
                    f"{image_help(args.engine, args.test_image, agent=False)}"
                )
            if test_ok:
                image_manifest, error = (None, "dry run") if args.dry_run else read_image_manifest(
                    args.engine, args.test_image
                )
                if error:
                    problems.append(f"evaluator manifest: {error}")
                elif isinstance(image_manifest, dict):
                    try:
                        manifest = validate_suite_manifest(image_manifest)
                    except SystemExit as exc:
                        problems.append(str(exc))
            if agent_ok and test_ok:
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
                        "test -x /opt/oracle/bin/oracle && test -f /opt/private/runner.py "
                        "&& test -f /opt/private/manifest.json && test -d /opt/private/fixtures "
                        "&& /opt/oracle/bin/oracle >/dev/null 2>&1; "
                        "test $? -eq 2",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if probe.returncode != 0:
                    problems.append("evaluator image oracle or hidden suite is missing or does not start")
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
                        "&& command -v opencode >/dev/null "
                        "&& test ! -e /opt/oracle && test ! -e /opt/private",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if probe.returncode != 0:
                    problems.append(
                        "agent image is missing cargo/python3/opencode or contains /opt/oracle or /opt/private"
                    )
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
        f"  agent image {args.agent_image}"
    )
    print(
        f"  evaluator image {args.test_image}"
    )
    print(
        f"  cpu-set {args.cpu_set if not args.no_cpu_set else 'unpinned'} · "
        f"agent network {args.network_policy} · provider-only egress · grader network none"
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
            f"{'agent-error' if str(row.get('outcome') or '') in {'agent-error', 'provider-error'} or row.get('agent_error') else ('eligible' if str(row.get('eligible')).lower() == 'true' else 'ineligible')}"
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
    url = f"http://127.0.0.1:{args.port}/"
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
        path_only = self.path.split("?", 1)[0]
        if path_only in ("/", "/index.html", "/leaderboard.html"):
            try:
                data = render_leaderboard(read_result_rows()).encode("utf-8")
            except OSError:
                self.send_error(500)
                return
        else:
            name = path_only.lstrip("/")
            path = (self.directory / name).resolve()
            if not str(path).startswith(str(self.directory.resolve())) or not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):  # noqa: D401
        pass


def refresh_provider_status(meta: dict, run_dir: Path) -> bool:
    """Recompute provider failures for a saved run from its agent logs.

    Classification used to treat every provider error as terminal, including
    engine blips that OpenCode retried in place. The logs are still on disk, so
    rescoring an older run should not keep the old verdict.
    """
    rounds = meta.get("rounds") or []
    before = json.dumps(meta.get("provider_errors")) + str(meta.get("agent_error"))
    events: list[dict] = []
    fatal: dict | None = None
    for row in rounds:
        number = row.get("round")
        logs = sorted(
            set(run_dir.glob(f"agent-round-{number}.log"))
            | set(run_dir.glob(f"agent-round-{number}-retry*.log"))
        )
        analyses = [(log, analyze_provider_errors(log)) for log in logs]
        row_events: list[dict] = []
        for log, analysis in analyses:
            for event in analysis["events"]:
                entry = {**event, "round": number, "log": log.name}
                row_events.append(entry)
                events.append(entry)
            if analysis["last_error"] and fatal is None:
                fatal = analysis["last_error"]
        if row_events:
            row["provider_error_count"] = len(row_events)
            row["provider_recovered"] = len(
                [event for event in row_events if event.get("recovered")]
            )
        if analyses:
            row["provider_error"] = any(
                analysis["last_error"] for _, analysis in analyses
            )
    recovered = [event for event in events if event.get("recovered")]
    if not events:
        return False
    meta["provider_errors"] = events
    meta["provider_error_count"] = len(events)
    meta["provider_recovered"] = len(recovered)
    meta.setdefault("provider_retries", 0)
    meta["agent_error"] = fatal
    meta["agent_ok"] = fatal is None
    if fatal is None and str(meta.get("outcome") or "") == "provider-error":
        meta["outcome"] = classify_outcome(
            watchdog=str(meta.get("watchdog") or "exit"),
            completion=bool(meta.get("completion")),
            files_changed=int(meta.get("files_changed") or 0),
            agent_ok=True,
        )
    after = json.dumps(meta.get("provider_errors")) + str(meta.get("agent_error"))
    return before != after


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
    for image, agent in ((args.agent_image, True), (args.test_image, False)):
        if not ensure_image(args.engine, image, dry_run=args.dry_run):
            raise SystemExit(
                f"error: image {image} unavailable; run: {image_help(args.engine, image, agent=agent)}"
            )
    manifest = load_manifest(args.engine, args.test_image)
    if refresh_provider_status(meta, run_dir):
        print("  reclassified provider failures from the saved agent logs")
    rescore_dir = run_dir / "rescore"
    build = build_app(
        run_dir,
        workdir,
        engine=args.engine,
        image=args.agent_image,
        dry_run=args.dry_run,
        artifacts_dir=rescore_dir,
    )
    tests = run_tests_repeated(
        run_dir,
        repeats=max(1, int(getattr(args, "final_reps", 2))),
        engine=args.engine,
        image=args.test_image,
        dry_run=args.dry_run,
        manifest=manifest,
        fixture_seed=int(meta.get("final_seed") or meta.get("fixture_seed") or manifest["seed"]),
        artifacts_dir=rescore_dir,
        bin_dir=rescore_dir / "bin",
        require_single_cpu=not args.no_cpu_set,
        args=args,
    )
    score = score_run(
        tests,
        build,
        manifest=manifest,
        result_valid=tests.get("result_valid", False),
        integrity_violation=bool(meta.get("integrity_violation")),
    )
    if meta.get("agent_error"):
        score["eligible"] = False
        score["score"] = 0.0
        score["quality"] = 0.0
    meta.update(
        {
            "build_ok": build["build_ok"],
            "seed_build_ok": build.get("seed_build_ok", False),
            "final_reps": max(1, int(getattr(args, "final_reps", 2))),
            "result_valid": score["result_valid"],
            "gate_ok": score["gate_ok"],
            "gate_pass": int(score.get("gate_pass") or 0),
            "gate_total": int(score.get("gate_total") or 0),
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
    refresh_run_metrics(meta, run_dir)
    meta.update(collect_image_metadata(args.engine, args.agent_image, args.test_image))
    for row in meta.get("rounds") or []:
        if row.get("round") == meta.get("best_round"):
            row["final_eligible"] = bool(score["eligible"])
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
        total_wall = _float(row.get("wall_s")) or 0.0
        per_log = total_wall / len(logs) if logs and total_wall else 0.0
        usage = combine_attempt_metrics(
            [{"log_path": str(log), "wall_s": per_log} for log in logs]
        )
        metrics = compute_metrics(
            None, run_dir / "opencode-data", total_wall, usage=usage
        )
        metrics["compactions"] = count_compactions(run_dir / "opencode-data")
        score_path = run_dir / "score.json"
        if score_path.exists():
            try:
                score = json.loads(score_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                score = {}
            for key in ("gate_pass", "gate_total"):
                if score.get(key) not in (None, ""):
                    metrics[key] = score[key]
            if metrics.get("gate_pass") in (None, ""):
                rounds = score.get("rounds") or []
                best = next(
                    (
                        row
                        for row in rounds
                        if row.get("round") == score.get("best_round")
                    ),
                    None,
                )
                candidates = [best] if best else rounds
                for candidate in candidates:
                    if candidate and candidate.get("gate_total") not in (None, ""):
                        metrics["gate_pass"] = candidate.get("gate_pass")
                        metrics["gate_total"] = candidate.get("gate_total")
                        break
        updatable = set(EFFORT_FIELDS) | {
            "steps",
            "compactions",
            "gate_pass",
            "gate_total",
        }
        for key, value in metrics.items():
            if key not in updatable or key not in CSV_FIELDS or value is None:
                continue
            current = row.get(key)
            if current in (None, "") or str(current) != str(value):
                row[key] = value
                changed += 1
    if changed:
        write_result_rows(rows)
    try:
        write_leaderboard()
    except Exception as exc:  # noqa: BLE001
        eprint(f"warning: could not update leaderboard: {exc}")
    print(f"backfilled {changed} fields across {len(rows)} runs")
    return 0


FENCED_BLOCK_RE = re.compile(r"```([A-Za-z0-9_-]*)\s*\n(.*?)```", re.DOTALL)


def _fenced_blocks(body: str) -> list[tuple[str, str, str]]:
    return [
        (match.group(1).strip().lower(), match.group(0), match.group(2).strip("\n"))
        for match in FENCED_BLOCK_RE.finditer(body)
    ]


def extract_result_block(body: object) -> tuple[dict | None, list[str]]:
    """Extract exactly one opencode-bench-htj2k result block from an issue body."""
    if not isinstance(body, str):
        return None, ["issue body is missing"]
    candidates: list[dict] = []
    parse_errors: list[str] = []
    for _language, _raw, text in _fenced_blocks(body):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            if "opencode-bench/htj2k-result" in text:
                parse_errors.append(f"invalid JSON block: {exc}")
            continue
        if isinstance(parsed, dict) and parsed.get("schema") == SUBMISSION_SCHEMA:
            candidates.append(parsed)
    if len(candidates) == 1:
        return candidates[0], []
    if len(candidates) > 1:
        return None, ["issue contains more than one result block"]
    if parse_errors:
        return None, parse_errors
    try:
        parsed = json.loads(body.strip())
    except json.JSONDecodeError:
        return None, ["issue body does not contain a fenced JSON result block"]
    if isinstance(parsed, dict) and parsed.get("schema") == SUBMISSION_SCHEMA:
        return parsed, []
    return None, [f"JSON block must contain schema {SUBMISSION_SCHEMA!r}"]


def submission_from_score(meta: dict) -> dict:
    outcome = str(meta.get("outcome") or "")
    agent_error = meta.get("agent_error")
    if agent_error is None and outcome in {"agent-error", "provider-error"}:
        agent_error = {"type": "agent-error", "message": outcome}
    eligible = bool(meta.get("eligible")) and agent_error is None
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
        "agent_error": agent_error,
        "provider_errors": meta.get("provider_errors"),
        "provider_error_count": meta.get("provider_error_count"),
        "provider_recovered": meta.get("provider_recovered"),
        "provider_retries": meta.get("provider_retries"),
        "provider_retry_after_s": meta.get("provider_retry_after_s"),
        "score": round(float(meta.get("score") or 0.0), 2),
        "eligible": eligible and not bool(meta.get("integrity_violation")),
        "integrity_violation": bool(meta.get("integrity_violation")),
        "gate_ok": bool(meta.get("gate_ok")),
        "gate_pass": meta.get("gate_pass"),
        "gate_total": meta.get("gate_total"),
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
        "steps": meta.get("steps"),
        "files_changed": meta.get("files_changed"),
        "compactions": meta.get("compactions"),
        **{key: meta.get(key) for key in EFFORT_FIELDS},
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
    if data.get("agent_error") and data.get("eligible"):
        problems.append("a run with an agent/provider error cannot be eligible")
    if data.get("integrity_violation") is not None and not isinstance(
        data.get("integrity_violation"), bool
    ):
        problems.append("integrity_violation must be boolean when present")
    elif data.get("integrity_violation") and data.get("eligible"):
        problems.append("a run with an integrity violation cannot be eligible")
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
    for key in (
        "ttft_s",
        "think_s",
        "output_s",
        "tool_s",
        "other_s",
        "self_test_s",
        "build_tool_s",
        "wall_ex_self_test_s",
        "retries",
        "retry_s",
        "step_median_s",
        "step_p90_s",
        "step_max_s",
        "compactions",
        "compaction_tokens",
        "cost_usd",
        "tokens_per_step",
        "gen_tokens_per_s",
        "gen_tokens_per_s_median",
        "max_request_tokens",
        "mean_request_tokens",
    ):
        value = data.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            problems.append(f"{key} must be numeric when present")
        else:
            if number < 0:
                problems.append(f"{key} must be non-negative")
    for key in (
        "ttft_share",
        "think_share",
        "output_share",
        "tool_share",
        "other_share",
        "self_test_share",
        "cache_hit_rate",
    ):
        value = data.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            problems.append(f"{key} must be numeric when present")
        else:
            if not 0 <= number <= 1:
                problems.append(f"{key} must be between 0 and 1")
    if data.get("gate_pass") is not None and data.get("gate_total") is not None:
        try:
            gate_pass = int(data["gate_pass"])
            gate_total = int(data["gate_total"])
        except (TypeError, ValueError):
            problems.append("gate_pass and gate_total must be integers")
        else:
            if gate_pass < 0 or gate_total < 0 or gate_pass > gate_total:
                problems.append("gate_pass must be between 0 and gate_total")
    return problems


def cmd_submit(args: argparse.Namespace) -> int:
    if args.check:
        if args.check == "-":
            text = sys.stdin.read()
        else:
            try:
                text = Path(args.check).read_text(encoding="utf-8")
            except OSError as exc:
                eprint(f"error: cannot read {args.check}: {exc}")
                return 1
        data, errors = extract_result_block(text)
        if data is not None:
            errors = validate_submission(data)
        if errors:
            for error in errors:
                eprint(f"error: {error}")
            return 2
        print("ok")
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
    if (
        not meta.get("eligible")
        or meta.get("agent_error")
        or str(meta.get("outcome") or "") in {"agent-error", "provider-error"}
    ):
        eprint("error: refusing to submit an ineligible or agent-error run")
        return 2
    if refresh_run_metrics(meta, run_dir):
        print("  refreshed effort metrics from the saved run artifacts")
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
    try:
        payload = json.loads(Path(args.issues).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        eprint(f"error: cannot read {args.issues}: {exc}")
        return 1
    if not isinstance(payload, list):
        eprint("error: issues JSON must be an array")
        return 1
    errors: list[dict] = []
    rows: list[dict] = []
    for issue in payload:
        if not isinstance(issue, dict):
            continue
        labels = {label.get("name") for label in issue.get("labels") or []}
        if BENCHMARK_LABEL not in labels or HIDDEN_LABEL in labels:
            continue
        data, block_errors = extract_result_block(issue.get("body"))
        if data is None:
            errors.append(
                {
                    "issue": issue.get("number"),
                    "url": issue.get("url"),
                    "errors": block_errors,
                }
            )
            continue
        problems = validate_submission(data)
        if problems:
            errors.append(
                {
                    "issue": issue.get("number"),
                    "url": issue.get("url"),
                    "errors": problems,
                }
            )
            continue
        row = {
            "run_id": data.get("run_id"),
            "model": data.get("model"),
            "provider": data.get("provider"),
            "variant": data.get("variant"),
            "thinking": data.get("thinking"),
            "reasoning_effort": data.get("reasoning_effort"),
            "score": data.get("score"),
            "decode_speedup": data.get("decode_speedup"),
            "encode_speedup": data.get("encode_speedup"),
            "eligible": data.get("eligible"),
            "integrity_violation": data.get("integrity_violation"),
            "gate_ok": data.get("gate_ok"),
            "gate_pass": data.get("gate_pass"),
            "gate_total": data.get("gate_total"),
            "perf_valid": data.get("perf_valid"),
            "network_policy": data.get("network_policy"),
            "cpu_set": data.get("cpu_set"),
            "confidence": data.get("confidence"),
            "agent_error": data.get("agent_error"),
            "hardware": data.get("hardware"),
            "wall_s": data.get("wall_s"),
            "total_tokens": data.get("total_tokens"),
            "compactions": data.get("compactions"),
            "timestamp": data.get("timestamp"),
            "_issue": issue.get("number"),
            "_issue_url": issue.get("url"),
        }
        # Optional effort fields are additive on the v1 schema: older
        # submissions simply render dashes for the detail columns.
        for key in EFFORT_FIELDS:
            if key in data:
                row[key] = data.get(key)
        if data.get("steps") is not None:
            row["steps"] = data.get("steps")
        if data.get("files_changed") is not None:
            row["files_changed"] = data.get("files_changed")
        author = issue.get("author") or {}
        if isinstance(author, dict) and author.get("login"):
            row["submitter"] = author.get("login")
        row["evidence_level"] = "verified" if VERIFIED_LABEL in labels else "self-reported"
        if VERIFIED_LABEL in labels:
            row["verified"] = True
        rows.append(row)
    # Keep the newest submission per run_id (resubmissions update in place).
    chosen: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("run_id") or "")
        previous = chosen.get(key)
        new_key = (str(row.get("timestamp") or ""), int(row.get("_issue") or 0))
        if previous is None or new_key >= (
            str(previous.get("timestamp") or ""),
            int(previous.get("_issue") or 0),
        ):
            chosen[key] = row
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    page = render_leaderboard(
        list(chosen.values()),
        public=True,
        subtitle="Community submissions · self-reported unless verified",
    )
    (output / "index.html").write_text(page, encoding="utf-8")
    data_dir = output / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "results.json").write_text(
        json.dumps(list(chosen.values()), indent=2) + "\n", encoding="utf-8"
    )
    (output / ".nojekyll").write_text("", encoding="utf-8")
    if args.errors:
        Path(args.errors).write_text(
            json.dumps(errors, indent=2) + "\n", encoding="utf-8"
        )
    print(f"wrote {output / 'index.html'} ({len(chosen)} submissions, {len(errors)} rejected)")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default=DEFAULT_ENGINE)
    parser.add_argument("--agent-image", default=DEFAULT_AGENT_IMAGE)
    parser.add_argument(
        "--test-image",
        default=DEFAULT_TEST_IMAGE,
        help="evaluator image (env OPENCODE_BENCH_EVAL_IMAGE or OPENCODE_BENCH_TEST_IMAGE)",
    )
    parser.add_argument("--config", help="OpenCode config directory or file")
    parser.add_argument("--cpu-set", default=DEFAULT_CPU_SET,
                        help="CPU affinity set for agent and grader (default: 0)")
    parser.add_argument("--no-cpu-set", action="store_true",
                        help="disable CPU pinning (debugging only)")
    parser.add_argument("--network-policy", default=DEFAULT_NETWORK_POLICY,
                        choices=("open", "offline"),
                        help="agent network policy label; provider-only egress is enforced and grading is always offline")
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="build the public agent/build image")
    p.add_argument("--agent-image", default=argparse.SUPPRESS)
    p.add_argument("--opencode-version", default="2.2.5")
    p.add_argument("--rust-version", default="1.83")
    p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser(
        "build-eval",
        help="build the evaluator image from a private suite checkout",
    )
    p.add_argument("--agent-image", default=argparse.SUPPRESS)
    p.add_argument("--test-image", default=argparse.SUPPRESS)
    p.add_argument(
        "--tests-dir",
        required=True,
        help="private directory containing the hidden suite (runner.py, manifest.json, fixtures, oracle)",
    )
    p.add_argument(
        "--suite-version",
        default=None,
        help="suite version stored in the evaluator image (default: local)",
    )
    p.add_argument(
        "--source-revision",
        default=None,
        help="private suite source revision recorded in the evaluator image",
    )
    p.add_argument("--rust-version", default="1.83")
    p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_build_eval)

    p = sub.add_parser("check", help="validate prompt, seed, evaluator manifest and images")
    p.add_argument("--agent-image", default=argparse.SUPPRESS)
    p.add_argument("--test-image", default=argparse.SUPPRESS)
    p.add_argument("--no-docker", action="store_true")
    p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("models", help="list models from the local OpenCode config")
    p.add_argument("--provider")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("run", help="run the agent and score it")
    p.add_argument("--agent-image", default=argparse.SUPPRESS)
    p.add_argument("--test-image", default=argparse.SUPPRESS)
    p.add_argument("--model", required=True)
    p.add_argument("--variant")
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--timeout", type=int, default=4 * 3600)
    p.add_argument("--idle-timeout", type=int, default=3600)
    p.add_argument("--round-plan", default="75,75,45",
                   help="comma-separated per-round minutes; the last round is the "
                        "final checkpoint")
    p.add_argument("--no-rounds", action="store_true",
                   help="single agent run using --timeout instead of checkpoints")
    p.add_argument("--fixture-seed", type=int)
    p.add_argument("--agent")
    p.add_argument("--agent-cmd", help="override the agent command (test/debug)")
    p.add_argument("--pure", action="store_true")
    p.add_argument("--no-preflight", action="store_true")
    p.add_argument("--keep-going", action="store_true")
    p.add_argument("--provider-retries", type=int, default=2,
                   help="extra attempts for a round after a transient provider/engine failure")
    p.add_argument("--provider-retry-delay", type=float, default=30.0,
                   help="initial backoff in seconds before resuming a failed round")
    p.add_argument("--max-nudges", type=int, default=2,
                   help="resume the session this many times if the agent ends a "
                        "non-final round with time left (0 disables)")
    p.add_argument("--nudge-min", type=float, default=10.0,
                   help="minimum minutes left before an early exit is resumed")
    p.add_argument("--final-reps", type=int, default=2,
                   help="repeat the held-out final grading this many times "
                        "(median per-workload speedup; 1 disables)")
    p.add_argument("--hardware", default="")
    p.add_argument("--env", action="append", default=[])
    p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST[:PORT]|URL",
        help=(
            "allow an additional model API/authentication endpoint; repeatable. "
            "Does not disable provider-only egress enforcement"
        ),
    )
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
    p.add_argument("--agent-image", default=argparse.SUPPRESS)
    p.add_argument("--test-image", default=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    p.add_argument("--final-reps", type=int, default=2,
                   help="repeat the held-out grading this many times "
                        "(median per-workload speedup; 1 disables)")
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
    p.add_argument("--errors", default=None, help="write per-issue validation errors to this JSON file")
    p.set_defaults(func=cmd_site)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
