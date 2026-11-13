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

## Budget and priorities

- You have up to **4 hours** of wall time (hard cap) and a **1-hour** no-output
  timeout. Stop and write `COMPLETION.md` when done or nearly out of time.
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
decode_speedup = geometric mean(seed median time / your median time)
encode_speedup = geometric mean(seed median time / your median time)

noise  = 0.05   # measurement noise floor
credit = clamp((speedup - 1 - noise) / (target - 1 - noise), 0, 2)
score  = 100 * (0.5 * credit_decode + 0.5 * credit_encode)
```

- The untouched seed scores **0**; improvements below the 5% noise floor do not
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

- Entropy coder: HT block cleanup/refinement passes, VLC tables, bit-buffer
  handling, sign/magnitude conversion.
- Wavelet transform: reversible 5-3 horizontal/vertical lifting, cache-friendly
  traversal, SIMD where it pays off.
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
