# Calibration (htj2k-v1)

This file records the reference measurements used to derive the frozen speed
targets. It is a maintainer record, not a score input.

## Test machine

- Apple Silicon (arm64), macOS, Rust 1.99 host build for the seed.
- C++ oracle: OpenJPH 0.26.3, `-O3`, Release, static library.
- Candidate and seed are the same binary for the no-op calibration.
- Measurements are end-to-end CLI runs (file I/O included), median of seven,
  interleaved.

## No-op run (pristine seed)

| kind | workload | seed MP/s | C++ OpenJPH MP/s | oracle/seed |
|---|---|---:|---:|---:|
| decode | 12-bit 2048² 4x1024 | 62.4 | 78.3 | 1.25 |
| decode | 8-bit 2048² 64x64 | 73.3 | 118.9 | 1.62 |
| decode | 16-bit 2048² 4x1024 | 66.6 | 80.5 | 1.21 |
| decode | 12-bit 1536² 64x64 | 71.2 | 107.5 | 1.51 |
| encode | 12-bit 2048² 4x1024 | 43.4 | 35.9 | 0.83 |
| encode | 8-bit 2048² 4x1024 | 50.0 | 45.6 | 0.91 |
| encode | 16-bit 2048² 4x1024 | 45.8 | 44.0 | 0.96 |
| encode | 12-bit 1536² 4x1024 | 40.5 | 34.8 | 0.86 |

These are the single-CPU Docker measurements used for the frozen targets.
Geometric mean oracle/seed: decode ≈ 1.39, encode ≈ 0.89.

## Targets

`target_decode = max(1.25, round(geomean(oracle_decode / seed_decode), 2)) = 1.35`

`target_encode = max(1.25, geomean(oracle_encode / seed_encode)) = 1.25`

The floor prevents a target below 1.25 when the Rust seed happens to be close to
the C++ implementation on a particular workload. Matching OpenJPH therefore
scores roughly 100, and a 25% improvement over the seed is full credit per
dimension. A 5% noise floor is applied before credit, so the untouched seed and
sub-noise changes score exactly 0.

## Caveats

- Absolute MP/s is hardware-dependent. Only seed-relative speedups and the in-run
  oracle measurement are meaningful.
- Docker Desktop on macOS can add scheduling noise. The runner interleaves seed
  and candidate, uses medians, and reports unstable cases rather than blaming the
  model.
- Re-run this calibration after any change to the corpus, toolchain, image or
  suite version, then bump `suite_version`.
