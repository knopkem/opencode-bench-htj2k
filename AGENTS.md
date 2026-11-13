# AGENTS.md

Maintainer invariants for **opencode-bench-htj2k**. The agent-visible contract is
`tasks/htj2k/PROMPT.md`; this file is for people changing the benchmark.

## Shape

- `bench.py` is the whole host harness: Docker orchestration, scoring, reports,
  leaderboard and submissions. It runs no task code on the host.
- `tasks/htj2k/seed/` is copied into each run. It must build offline with
  `cargo build --release --offline --locked`, pass every correctness gate and
  score exactly `0.0` untouched.
- `tasks/htj2k/tests/` is hidden. It is mounted only into the test container.
  `manifest.json` is the catalog source of truth.
- `tasks/htj2k/tests/oracle/` holds pinned OpenJPH source and the independent
  oracle harness. Never mount it into the agent image.
- `runs/`, CSV/report/leaderboard/`docs/` are generated artifacts.

## Invariants

- Agent image has no OpenJPH binary or library. Test image has the oracle at
  `/opt/oracle` and the pinned source/build tree at `/opt/src` and `/opt/build`,
  all mode 700, so uid-1000 candidate processes cannot read them.
- The agent and test containers are pinned with `--cpuset-cpus <set> --cpus 1`.
  The default set is `0`. `nproc` and `available_parallelism()` must report 1 in
  both containers. `--no-cpu-set` is debugging only.
- Build phase has no network and no hidden tests. Test phase has no network and
  one pinned CPU. The candidate runs as uid 1000.
- The candidate only sees staged inputs under `/perf/in` and writes to
  `/perf/out`. Expected pixels, oracle codestreams and metadata live under
  `/perf/root` (mode 700).
- The agent run is checkpointed: default 75 / 75 / 45 minutes. Each checkpoint
  builds the workdir and runs the full hidden suite on a validation seed. The
  official score is the best eligible checkpoint rescored on a held-out final
  seed. Checkpoints are implemented in `run_one`, not as separate tasks.
- Correctness is bit-exact for lossless decode and encode. Lossy, RGB and
  multi-component modes are out of scope for `htj2k-v1`.
- The graded encode profile is `4x1024` code-blocks. Decode coverage includes
  64x64, 32x32, 16x16, RPCL and multiple tiles.
- `Cargo.lock` package allowlist is fixed in `bench.py`. Adding a dependency or
  a `build.rs` is a gate failure by design.
- The score is seed-normalised. The pristine seed scores 0; targets are frozen
  in `manifest.json` and calibrated so the in-run OpenJPH oracle is about 100.
  Changing targets, corpus or suite version requires recalibration and a suite
  version bump.

## Validation gates

Before publishing a suite change:

```bash
./bench.py build
./bench.py check
python3 -m unittest discover -s tests
```

Then run a no-op end-to-end check (the mock agent writes COMPLETION.md and
changes no code):

```bash
./bench.py run --model mock/noop --no-preflight \
  --agent-cmd 'echo noop > /work/COMPLETION.md' \
  --round-plan 1,1,1
./bench.py rescore <run_id>
```

`./bench.py check` probes both images with the cpuset and fails if `nproc` or
`sched_getaffinity` is not 1.

The runner can also be invoked directly inside the test image for debugging,
but the harness bundle path (root-only `/opt/private`) is the supported flow.
Mount `tasks/htj2k/tests` read-only at `/opt/tests` only for maintainer
diagnostics; never expose it to the agent.

Requirements:

- all correctness cases pass for the pristine seed;
- seed score is `0.0` on the held-out final seed;
- every checkpoint is built and tested in the pinned one-CPU environment;
- repeated perf medians are stable to roughly 5% on the reference machine;
- negative controls (wrong pixels, lossy output, bloated output, missing output,
  truncated-input acceptance, subprocess use, memory blow-up) each fail the
  intended gate;
- the oracle decodes every fixture and the seed output.

## Common pitfalls

- Do not call the C++ oracle from the candidate container or expose
  `/opt/oracle` to uid 1000.
- Do not mount `tasks/htj2k/tests` into the agent image.
- Do not make the perf corpus deterministic across runs; use `--run-seed`.
- Do not compare absolute MP/s across machines. Only seed-relative speedups and
  the in-run OpenJPH reference are meaningful.
- A timed-out or crashed candidate is a model failure, not a harness failure;
  the runner reports both separately.
- A thread-based speedup measured outside the cpuset is not a result. The
  grader exposes one CPU; check `nproc` before trusting an A/B number.
- Network is labelled `open` for cloud models. Do not mix open-network and
  offline results in the same ranking.
- Provider quota/rate-limit errors are not model failures. Stop at the first
  `provider.*`/429/5xx error, keep the checkpoint, mark the run `agent_error`
  and ineligible, and never submit it. Do not spend later rounds on a dead
  provider.
