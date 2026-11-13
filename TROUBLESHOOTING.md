# Troubleshooting

## `docker info` fails

Start Docker Desktop or OrbStack. The harness refuses to build or run without a
working engine.

## `./bench.py check` reports a missing image

Run `./bench.py build`. The test image compiles the pinned C++ OpenJPH oracle,
so the first build takes a few minutes.

## The agent cannot build the codec

The seed is built with `cargo build --release --offline --locked` against the
vendored `vendor/` tree. If the agent added a dependency the build fails by
design. The hidden suite also rejects `build.rs`, git dependencies and packages
outside the allowlist.

## `rescore` says the seed build failed

The test image compiles both the candidate workdir and the pristine seed. A seed
failure is a harness/infrastructure error, not a model failure. Check
`runs/<id>/artifacts/build-seed.log`. Do not accept a score produced with a
broken seed.

## Performance looks noisy

The test container is limited to one CPU and the runner interleaves pristine seed
and candidate measurements. On a busy host, medians can still move by a few
percent. Repeat the run before drawing conclusions; do not compare absolute MP/s
across machines.

## `p0.dicom-201` fails

The fixture is `tasks/htj2k/tests/fixtures/ABDOM_1_htj2k.dcm` with the
codestream extracted to `ABDOM_1_htj2k.j2c`. If it fails, first verify the seed:

```bash
./target/release/htj2k decode tasks/htj2k/tests/fixtures/ABDOM_1_htj2k.j2c /tmp/out.raw
cmp /tmp/out.raw tasks/htj2k/tests/fixtures/ABDOM_1_raw.u16le
```

## `p0.robustness` fails

A candidate that exits `0` on truncated or garbage input, hangs, or crashes with
a signal is a failure. Check `test.log` for the case and the return code. The
runner distinguishes a non-zero clean failure from a timeout or signal.

## The scorer gives 0 even though the codec is faster

All correctness gates must pass. Check `runs/<id>/report.md`; a single failed
gate or an invalid perf case makes the run ineligible. Also confirm the candidate
was measured on the same corpus as the seed (`fixture_seed` in `score.json`).

## Long agent runs

Use `--timeout` and `--idle-timeout` on `./bench.py run`. The agent stops early
when `COMPLETION.md` appears and exits within 60 seconds. A hard timeout still
grades whatever the agent produced.

## Community leaderboard

`./bench.py submit --run RUN_ID --print` emits the JSON block. `--gh --repo`
creates an issue. `./bench.py site --issues issues.json --output docs` renders a
static page from labeled issue JSON. Results are self-reported unless a
maintainer adds `verified`.
