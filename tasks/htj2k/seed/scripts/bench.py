#!/usr/bin/env python3
"""Public HTJ2K self-benchmark.

This is deliberately small and fast. It is not the hidden score: the graded
corpus is longer, randomised per run, and validated against an independent C++
OpenJPH oracle. Use this to compare a modified tree with a pristine copy.

    python3 scripts/bench.py --reps 3
    python3 scripts/bench.py --binary /tmp/htj2k-pristine/target/release/htj2k
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / "target" / "release" / "htj2k"
CORPUS = ROOT / "corpus" / "generated"
PUBLIC = ROOT / "corpus" / "public"

WORKLOADS = [
    {"id": "gray-12-1536-d5-4x1024", "width": 1536, "height": 1536, "bit_depth": 12, "decompositions": 5, "block_size": "4x1024"},
    {"id": "gray-8-1536-d5-4x1024", "width": 1536, "height": 1536, "bit_depth": 8, "decompositions": 5, "block_size": "4x1024"},
    {"id": "gray-16-1536-d5-4x1024", "width": 1536, "height": 1536, "bit_depth": 16, "decompositions": 5, "block_size": "4x1024"},
    {"id": "gray-12-768-d4-4x1024", "width": 768, "height": 768, "bit_depth": 12, "decompositions": 4, "block_size": "4x1024"},
]


def image_bytes(width: int, height: int, bit_depth: int, seed: int) -> bytes:
    bps = 1 if bit_depth <= 8 else 2
    mask = (1 << bit_depth) - 1
    rng = random.Random(seed)
    out = bytearray(width * height * bps)
    index = 0
    for y in range(height):
        row = (y * 13) & mask
        for x in range(width):
            value = (row + x * 7 + ((x * y) >> 3) + (rng.getrandbits(min(bit_depth, 8)) >> 2)) & mask
            if bps == 1:
                out[index] = value
            else:
                out[index] = value & 0xFF
                out[index + 1] = (value >> 8) & 0xFF
            index += bps
    return bytes(out)


def run(command: list[str]) -> float:
    start = time.perf_counter()
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    elapsed = time.perf_counter() - start
    if result.returncode != 0:
        raise SystemExit(
            "command failed: " + " ".join(command) + "\n" + result.stderr.decode(errors="replace")
        )
    return elapsed


def check_public_fixtures(binary: Path) -> None:
    manifest_path = PUBLIC / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text())
    cases = manifest.get("cases") or []
    if not cases:
        return
    CORPUS.mkdir(parents=True, exist_ok=True)
    print(f"public fixtures ({len(cases)})")
    for case in cases:
        j2c = PUBLIC / case["j2c"]
        raw = PUBLIC / case["raw"]
        decoded = CORPUS / f"public-{case['id']}.decoded.raw"
        run([str(binary), "decode", str(j2c), str(decoded)])
        if decoded.read_bytes() != raw.read_bytes():
            raise SystemExit(f"public fixture decode mismatch: {case['id']}")
        encoded = CORPUS / f"public-{case['id']}.self.j2c"
        roundtrip = CORPUS / f"public-{case['id']}.self.raw"
        run(
            [
                str(binary),
                "encode",
                str(raw),
                str(encoded),
                "--width",
                str(case["width"]),
                "--height",
                str(case["height"]),
                "--bit-depth",
                str(case["bit_depth"]),
                "--components",
                "1",
                "--decompositions",
                str(case["decompositions"]),
                "--block-size",
                case["block_size"],
                "--progression",
                case["progression"],
            ]
        )
        run([str(binary), "decode", str(encoded), str(roundtrip)])
        if roundtrip.read_bytes() != raw.read_bytes():
            raise SystemExit(f"public fixture roundtrip mismatch: {case['id']}")
        print(f"  {case['id']}: ok")
        decoded.unlink(missing_ok=True)
        encoded.unlink(missing_ok=True)
        roundtrip.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=str(DEFAULT_BINARY))
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-fixtures", action="store_true")
    args = parser.parse_args()
    binary = Path(args.binary)
    if not binary.exists():
        raise SystemExit(f"missing {binary}; run cargo build --release --offline --locked")
    try:
        affinity = len(os.sched_getaffinity(0))
    except AttributeError:
        affinity = os.cpu_count() or 1
    if affinity != 1:
        print(
            f"WARNING: {affinity} CPUs visible; the grader pins one CPU. "
            "Run under `taskset -c 0` or a --cpuset-cpus container for comparable numbers.",
            file=sys.stderr,
        )

    CORPUS.mkdir(parents=True, exist_ok=True)
    if not args.skip_fixtures:
        check_public_fixtures(binary)
    results = []
    for index, workload in enumerate(WORKLOADS):
        raw = CORPUS / f"{workload['id']}.raw"
        j2c = CORPUS / f"{workload['id']}.j2c"
        decoded = CORPUS / f"{workload['id']}.decoded.raw"
        raw.write_bytes(image_bytes(workload["width"], workload["height"], workload["bit_depth"], 1234))
        common = [
            "--width", str(workload["width"]),
            "--height", str(workload["height"]),
            "--bit-depth", str(workload["bit_depth"]),
            "--components", "1",
            "--decompositions", str(workload["decompositions"]),
            "--block-size", workload["block_size"],
        ]
        encode_cmd = [str(binary), "encode", str(raw), str(j2c), *common]
        decode_cmd = [str(binary), "decode", str(j2c), str(decoded)]

        # Correctness smoke test through the seed's own decoder.
        run(encode_cmd)
        run(decode_cmd)
        if raw.read_bytes() != decoded.read_bytes():
            raise SystemExit(f"roundtrip mismatch for {workload['id']}")

        encode_times = []
        decode_times = []
        for _ in range(max(1, args.reps)):
            encode_times.append(run(encode_cmd))
            decode_times.append(run(decode_cmd))
        megapixels = workload["width"] * workload["height"] / 1e6
        results.append(
            {
                **workload,
                "encode_s": statistics.median(encode_times),
                "decode_s": statistics.median(decode_times),
                "encode_mp_s": megapixels / statistics.median(encode_times),
                "decode_mp_s": megapixels / statistics.median(decode_times),
                "j2c_bytes": j2c.stat().st_size,
            }
        )
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    print(f"binary {binary}")
    print(f"{'workload':30s} {'encode':>10s} {'decode':>10s} {'size':>10s}")
    for item in results:
        print(
            f"{item['id']:30s} "
            f"{item['encode_mp_s']:8.1f} MP/s "
            f"{item['decode_mp_s']:8.1f} MP/s "
            f"{item['j2c_bytes']:10d}"
        )
    print("\npublic corpus only — hidden runs use different images and validation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
