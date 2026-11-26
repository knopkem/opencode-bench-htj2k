# Troubleshooting

## `docker info` fails

Start Docker Desktop or OrbStack. The harness refuses to build or run without a
working engine.

## `./bench.py check` reports a missing image

The agent image is built locally with `./bench.py build`. The evaluator image
(`--test-image`, default `ghcr.io/knopkem/opencode-bench-htj2k-eval:edge`) is
pulled automatically from GHCR when it is public; maintainer builds use
`./bench.py build-eval --tests-dir <private-suite>` with the private
`opencode-bench-htj2k-eval` checkout. The evaluator image compiles the pinned
C++ OpenJPH oracle, so the first build takes a few minutes.

## The agent cannot build the codec

The seed is built with `cargo build --release --offline --locked` against the
vendored `vendor/` tree. If the agent added a dependency the build fails by
design. The hidden suite also rejects `build.rs`, git dependencies and packages
outside the allowlist.

## `rescore` says the seed build failed

The evaluator image runs both the candidate workdir build and the pristine
seed build. A seed failure is a harness/infrastructure error, not a model
failure. Check `runs/<id>/rounds/round-N/artifacts/build-seed.log`. Do not
accept a score produced with a broken seed.

## `check` says the image is not pinned to one CPU

The harness runs both agent and grader with `--cpuset-cpus 0 --cpus 1`. If the
engine does not support cpuset, `check` fails instead of silently using a
quota-only limit. Use Docker Desktop/OrbStack, or pass `--cpu-set` for a
supported CPU. `--no-cpu-set` is for debugging only.

## Performance looks noisy

The test container is pinned to one CPU and the runner alternates pristine seed
and candidate measurements inside each repetition. The score uses the median of
paired ratios and the held-out final is rescored twice, but on a busy host
individual workloads can still move by several percent (and a per-process fast
mode can make min-of-N look inflated). Treat the `±` band in the checkpoint
prompt and the `cv` column in `report.md` as the noise level, repeat the run
before drawing conclusions, and do not compare absolute MP/s across machines. A
threaded local benchmark on an unpinned container is not a valid result: check
`nproc` first.

## The run stopped early / rounds

The default plan is 75 / 75 / 45 minutes with a checkpoint after each round. If
the model ends a non-final round with more than `--nudge-min` minutes left (and
no `COMPLETION.md`), the harness resumes the same session, up to `--max-nudges`
times. The final round is graded like the others and may optimize until its
10-minute freeze. The official score is the best eligible checkpoint rescored
twice on a held-out final corpus (median per workload). Look at
`runs/<id>/rounds/round-N/` and `rounds` in `score.json`.

## The run ended with a provider error

OpenCode can exit 0 even when the provider rejects a request. The harness scans
the JSON log and separates two cases:

- **Recovered blip.** OpenCode retries a request in place when the engine dies
  mid-generation ("the next request restarts it"). If the log shows more steps
  after the error, the round was not lost: the run continues and stays eligible.
  `provider_error_count` / `provider_recovered` in `score.json` record it.
- **Unrecovered failure.** A quota/auth/rate-limit error is terminal: the
  harness stops further rounds, keeps the checkpoint for diagnostics, marks the
  run `agent_error` / ineligible and will not submit it. `provider_retry_after_s`
  records the provider's cooldown; free-tier limits are often hours long, longer
  than the 4-hour budget. Switch providers or use a paid model.

Other unrecovered engine errors (5xx, `provider.internal`) are retried by
resuming the same session and workdir, up to `--provider-retries` (default 2)
attempts with backoff, while the round has budget left. Tune with
`--provider-retries 0` to disable, or `--provider-retry-delay` for the initial
backoff. Retry logs are written as `agent-round-N-retryK.log`.

## The leaderboard shows a single run

One run is accepted but labelled `single-run`. Run three times with different
seeds for a multi-run median; the page groups rows by model and variant.

## `p0.dicom-201` fails

The fixture ships inside the evaluator image
(`/opt/private/fixtures/ABDOM_1_htj2k.dcm`, with the codestream extracted to
`ABDOM_1_htj2k.j2c`); the private `opencode-bench-htj2k-eval` repository holds
the copy used at build time. If it fails, first verify the seed against the
fixture from a maintainer suite checkout:

```bash
./target/release/htj2k decode /path/to/tests/fixtures/ABDOM_1_htj2k.j2c /tmp/out.raw
cmp /tmp/out.raw /path/to/tests/fixtures/ABDOM_1_raw.u16le
```

## `p0.robustness` fails

A candidate that exits `0` on truncated or garbage input, hangs, or crashes with
a signal is a failure. Check `rounds/round-N/final/test.log` for the case and the
return code. The
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
