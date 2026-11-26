# HTJ2K lossless codec — make encode and decode faster

You are working on **HTJ2K**, the high-throughput block-coding mode of JPEG 2000
(ISO/IEC 15444-15), in pure Rust. The seed is a working lossless single-component
HTJ2K codec derived from OpenJPH. It passes every graded correctness gate, but it
is not fast enough: your job is to improve **both lossless encode throughput and
lossless decode throughput** without changing the external contract or
regressing correctness.

This is a performance-engineering task, not a format-design task. HTJ2K is a
standard; the bitstream must remain decodable by an independent C++ OpenJPH
oracle. You may restructure the Rust implementation freely.

## Budget, CPU and checkpoints

- You have up to **4 hours** of wall time (hard cap) and a **1-hour** no-output
  timeout. The run is split into **75 / 75 / 45 minute rounds** with a checkpoint
  after each.
- The live clock is `/work/ROUND-TIME.md`; check `date -u` against its deadline.
  This is a non-interactive benchmark: the `question` tool is disabled. If your
  turn ends with time left, the harness resumes the session and asks you to keep
  going.
- The final round is graded like every other checkpoint: keep optimizing while
  time remains, but **freeze 10 minutes before the deadline** to remove
  instrumentation, build, test and write `COMPLETION.md`.
- The grader exposes **exactly one CPU**. `available_parallelism()` is 1.
  Thread-based parallelism will not help; do not build a thread pool.
- After each round the harness builds your tree and tells you the gate status
  and decode/encode speedups on a validation corpus. The final score is the best
  eligible checkpoint, rescored on a held-out final corpus.
- Do **not** write `COMPLETION.md` until the final round. In the final round,
  remove profiling/timing instrumentation, run the public tests and write
  `COMPLETION.md`.
- Do not search for, retrieve, inspect, or execute hidden tests, reference
  implementations, evaluator images, or copies of them, including through
  GitHub, container registries, web search, or other network tools. A verified
  attempt makes the run ineligible for the leaderboard. Agent network access is
  restricted to approved model-provider infrastructure; proxy denials are logged.
- Correctness is a gate. A fast codec that produces wrong pixels scores `0`.
- Optimise in this order: **decode correctness, encode correctness, decode
  speed, encode speed**. The score weights decode and encode equally.
- Do not add dependencies, build scripts or external native code. Builds are
  offline and `Cargo.lock` is checked against a small allowlist. Do not call
  external codecs, spawn processes, load shared libraries, embed binary blobs,
  or use the network during grading.

## The CLI contract (do not change)

```bash
htj2k encode INPUT.raw OUTPUT.j2c \
  --width N --height N --bit-depth 8|12|16 \
  --components 1 [--signed] \
  [--decompositions N] [--block-size WxH] [--progression LRCP|RPCL]

htj2k decode INPUT.j2c OUTPUT.raw
```

- Raw samples are interleaved little-endian: one byte for bit depth <= 8, two
  bytes little-endian for 9..=16.
- The graded profile is **single-component (grayscale), lossless (reversible
  5-3)**. DICOM transfer syntax `.201` is the real-world target.
- Encoders must honour the requested code-block size and progression order.
  The hidden encode profile uses `4x1024` code-blocks; the decoder must handle
  the wider range of legal HTJ2K codestreams in the decode corpus (including
  64x64, 32x32, 16x16, RPCL and multiple tiles).
- `decode` must derive dimensions, bit depth and signedness from the codestream
  header. Do not require side-channel metadata.

## How you are graded

The hidden suite is black-box: it invokes the built `target/release/htj2k`
binary. It never imports your library and never inspects your source for
intended behaviour.

### Correctness gates — all must pass

| Gate | Requirement |
|---|---|
| `decode-lossless` | bit-exact against the independent C++ OpenJPH oracle |
| `encode-lossless` | C++ OpenJPH decodes your output bit-exactly; size within 2% + 1 KiB of the oracle's lossless size |
| `dicom-201` | a real DICOM `.201` codestream decodes bit-exactly |
| `robustness` | truncated, garbage, empty and wrong-sized inputs fail cleanly (non-zero exit, no panic or hang) |
| `determinism` | the same input produces the same codestream twice |

### Performance — the scored part

The pristine seed and your build are run on the same generated corpus, in the
same container, on **one CPU**, interleaved. The corpus is regenerated from a
per-run seed and is larger than the public fixtures. Throughput is measured
end-to-end through the CLI, including file I/O.

```
decode_speedup = geometric mean over workloads of
                 median over repetitions of (seed_time / your_time)
encode_speedup = same for encode

noise  = 0.08   # measurement noise floor
credit = clamp((speedup - 1 - noise) / (target - 1 - noise), 0, 2)
score  = 100 * (0.5 * credit_decode + 0.5 * credit_encode)
```

- The seed and your binary alternate inside each repetition, so the ratio
  cancels per-process fast/slow modes on the host. Re-measure anything inside
  the checkpoint's reported `±` band before believing it.
- The untouched seed scores **0**; improvements below the 8% noise floor do not
  earn points.
- Matching the frozen target in both dimensions scores **100**.
- The targets are calibrated from the seed/OpenJPH ratios on the same machine,
  with a 1.25x floor so each dimension remains a real challenge. Matching the
  target is roughly state-of-the-art throughput; beating it scores above 100.
- 2x the target caps at **200**.
- A correctness failure makes the run ineligible and the score `0`.

Do not try to game the measurement: the corpus is hidden, regenerated per run,
and the expected pixels never enter your process. Optimise the actual codec.

## Public self-testing

The seed includes a small public corpus generator and benchmark:

```bash
python3 scripts/bench.py --reps 3
cargo test --workspace
```

The public numbers are for iteration only; the hidden corpus is different and
larger. Save a pristine copy of the seed before editing if you want a clean A/B
baseline:

```bash
cp -a . /tmp/htj2k-pristine
```

## Suggested optimisation surface

Measured on the graded corpus, the block coder dominates: roughly three quarters
of decode time and most of encode time. The DWT is real but secondary. The
seed's `4x1024` decode path is already close to OpenJPH; the largest decode gap
is small-block (`64x64`) decoding. On encode the seed is already faster than
OpenJPH, so the target needs a real algorithmic win, not a recompile.

The highest-value work in this codebase has been:

- **Block decoder**: batched magnitude/sign bit extraction and refinement in the
  HT cleanup pass (the single largest decode cost).
- **Block encoder**: a zero-quad-pair fast path that skips magnitude/UVLC
  book-keeping for entirely insignificant quad pairs, which dominate the
  high-frequency bands.
- **Wavelet and buffers**: remove per-line allocations, full-image copies and
  redundant zero-fill; small-block decode benefits most.

Other surfaces:

- Code-block scheduling and allocation: avoid per-row `Vec` churn, reuse
  buffers across blocks, reduce copies in the CLI wrapper.
- Quantisation/level-shift and pixel packing/unpacking.
- Compiler and code-generation choices that are legal within the frozen
  `Cargo.toml` (for example `#[target_feature]` with runtime detection). Do not
  rely on undeclared external crates.
- Memory behaviour: the graded cap is 2 GiB; throughput includes I/O but not
  process startup alone.

## Deliverables

1. Updated Rust source in the workdir, building with
   `cargo build --release --offline --locked`.
2. The same CLI and codestream semantics.
3. `COMPLETION.md` with a short summary of what you changed, what you measured,
   and any known limitations.

Read `AGENTS.md` in the seed for the exact build/test commands and the
repository layout. Good luck.
