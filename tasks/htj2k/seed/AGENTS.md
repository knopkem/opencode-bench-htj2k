# HTJ2K seed — agent notes

This is the candidate-visible codec. The harness copies this directory to the
run workdir and gives the agent four hours across checkpointed rounds. The hidden
suite is not here. The grader exposes exactly one pinned CPU; thread-based
parallelism will not help.

## Layout

```text
Cargo.toml                 workspace (htj2k + htj2k-cli)
Cargo.lock                 pinned dependency graph
.cargo/config.toml         offline vendored sources + native target flags
vendor/                    thiserror and its transitive deps
crates/htj2k/              pure-Rust HTJ2K codestream library (OpenJPH port)
crates/htj2k-cli/          bin/htj2k CLI wrapper
scripts/bench.py           public corpus generator and throughput benchmark
corpus/                    optional small fixtures created by scripts/bench.py
```

## Build and test

```bash
cargo build --release --offline --locked
cargo test --workspace --offline
./target/release/htj2k --help
```

The binary is `target/release/htj2k`. The graded build is the same release
build with `--offline --locked`.

## Public benchmark

```bash
python3 scripts/bench.py --reps 3
```

It first checks the committed public fixtures under `corpus/public/` against the
independent oracle's codestreams, then generates raw images under
`corpus/generated/`, runs the same CLI calls the hidden suite uses, and prints
encode/decode MP/s. Use it to compare a modified tree against a pristine copy.
Public fixtures are small and are not the score.

The public fixtures cover 64x64, 16x16, 4x1024, RPCL and a tiled codestream.
`--skip-fixtures` skips the interop check during quick iterations.

## Contract

```bash
htj2k encode IN.raw OUT.j2c --width N --height N --bit-depth 8|12|16 \
  --components 1 [--signed] [--decompositions N] \
  [--block-size WxH] [--progression LRCP|RPCL]
htj2k decode IN.j2c OUT.raw
```

- Interleaved little-endian raw samples: one byte for bit depth <= 8, two bytes
  for 9..=16.
- Single-component lossless is the graded profile. Multi-component is rejected
  by the CLI on purpose in this version.
- Graded encode cases use 4x1024 code-blocks. The decoder must handle legal
  HTJ2K codestreams with 64x64, 32x32, 16x16, RPCL and multiple tiles.
- Keep the command surface stable; the hidden runner calls the binary only.

## Rules

- No new dependencies, build scripts, network, subprocesses, FFI, dynamic
  loading, external codecs or embedded binary blobs (`include_bytes!` /
  `include_str!`). `Cargo.lock` is checked against an allowlist.
- Do not try to read the hidden corpus or the grading oracle. The candidate
  process runs as an unprivileged user and the oracle directory is unreadable.
- Correctness is a gate. A faster codec that changes pixels scores zero.

## Known seed limitations

- The in-tree HT encoder is validated for the graded `4x1024` profile. Some
  other block sizes may produce codestreams that the independent oracle rejects
  on noisy data. Decode coverage is deliberately broader than encode coverage.
- Signed samples are decoded as sign-extended little-endian values; the graded
  corpus is unsigned.
- The crate is a port of OpenJPH and still has room in the block coder, DWT and
  allocation paths. That is the point of the task.
