# AGENTS.md

Maintainer invariants for **opencode-bench-htj2k**. The agent-visible contract is
`tasks/htj2k/PROMPT.md`; this file is for people changing the benchmark.

## Shape

- `bench.py` is the whole host harness: Docker orchestration, scoring, reports,
  leaderboard and submissions. It runs no task code on the host.
- `tasks/htj2k/seed/` is copied into each run. It must build offline with
  `cargo build --release --offline --locked`, pass every correctness gate and
  score exactly `0.0` untouched.
- The hidden suite lives in the private `opencode-bench-htj2k-eval` repository
  (local checkout: the gitignored `.private-eval/` directory). Its `tests/`
  tree is baked into the evaluator image; `manifest.json` is the catalog
  source of truth. The public source tree must never contain it.
- `tests/oracle/` (in the private repository) holds pinned OpenJPH source and
  the independent oracle harness. Never bake it into the agent image.
- `docker/agent.Dockerfile` builds the public agent/build image;
  `docker/evaluator.Dockerfile` is the evaluator recipe that consumes the
  private suite through a named `tests` build context.
- `.github/workflows/community-leaderboard.yml` validates
  `benchmark-result` issues, moderates invalid submissions, and rebuilds the
  public leaderboard site; Pages deployment activates when the repository is
  public.
- `runs/`, CSV/report/leaderboard/`docs/` are generated artifacts.

## Invariants

- Agent image has no OpenJPH binary or library, no `/opt/oracle` and no
  `/opt/private`. The evaluator image has the oracle at `/opt/oracle` and the
  pinned source/build tree at `/opt/src` and `/opt/build`, plus the hidden
  suite at `/opt/private`, all mode 700, so uid-1000 candidate processes
  cannot read them.
- The agent and evaluator containers are pinned with
  `--cpuset-cpus <set> --cpus 1`. The default set is `0`. `nproc` and
  `available_parallelism()` must report 1 in both containers. `--no-cpu-set`
  is debugging only.
- Build phase has no network and no hidden tests. Test phase has no network and
  one pinned CPU. The candidate runs as uid 1000.
- The candidate only sees staged inputs under `/perf/in` and writes to
  `/perf/out`. Expected pixels, oracle codestreams and metadata live under
  `/perf/root` (mode 700).
- The agent run is checkpointed: default 75 / 75 / 45 minutes. Each checkpoint
  builds the workdir and runs the full hidden suite on a validation seed. The
  official score is the best eligible checkpoint rescored on a held-out final
  seed. Checkpoints are implemented in `run_one`, not as separate tasks.
- A non-final round that ends with more than `--nudge-min` minutes left (and no
  `COMPLETION.md`) is resumed in the same session, up to `--max-nudges` times.
  The final round is allowed to optimize until its freeze point.
- Every round writes `/work/ROUND-TIME.md` and the agent config disables the
  interactive `question` tool; benchmark sessions have no user to answer it.
- Correctness is bit-exact for lossless decode and encode. Lossy, RGB and
  multi-component modes are out of scope for `htj2k-v1`.
- The graded encode profile is `4x1024` code-blocks. Decode coverage includes
  64x64, 32x32, 16x16, RPCL and multiple tiles.
- `Cargo.lock` package allowlist is fixed in `bench.py`. Adding a dependency or
  a `build.rs` is a gate failure by design.
- The score is seed-normalised. The pristine seed scores 0; targets are frozen
  in `manifest.json`. The decode target is the measured seed/OpenJPH ratio on the
  reference machine; the encode target is the 1.25x floor because the seed
  encoder is already faster than the in-run oracle (about x0.87). Consequently
  the in-run OpenJPH oracle scores roughly **64**, not 100: it gets decode credit
  but no encode credit. Changing targets, corpus or suite version requires
  recalibration and a suite version bump.
- Throughput is the **median of paired seed/candidate ratios** over 9 timed
  repetitions with randomized order. Per-process host-state changes are not
  shared, so min-of-N can put a faster candidate below the seed; it is kept as
  a diagnostic only. The held-out final grading is repeated twice and combined
  as the median per workload.

## Validation gates

Before publishing a suite change:

```bash
./bench.py build                     # public agent image
./bench.py build-eval \
  --tests-dir .private-eval/tests \
  --test-image opencode-bench-htj2k-eval:local
./bench.py check \
  --agent-image opencode-bench-htj2k-agent:local \
  --test-image opencode-bench-htj2k-eval:local
python3 -m unittest discover -s tests
```

Then run a no-op end-to-end check (the mock agent writes COMPLETION.md and
changes no code):

```bash
./bench.py run --model mock/noop --no-preflight \
  --agent-cmd 'echo noop > /work/COMPLETION.md' \
  --round-plan 1,1,1 \
  --agent-image opencode-bench-htj2k-agent:local \
  --test-image opencode-bench-htj2k-eval:local
./bench.py rescore <run_id> \
  --agent-image opencode-bench-htj2k-agent:local \
  --test-image opencode-bench-htj2k-eval:local
```

The no-op check also exercises the per-round clock, the staged agent config
(`question` denied) and the two-repeat final grading. To exercise the early-exit
nudge, use a non-final round and let the mock agent exit without COMPLETION:

```bash
./bench.py run --model mock/noop --no-preflight --max-nudges 1 --nudge-min 0.1 \
  --round-plan 1,1 --agent-cmd 'echo noop >> /work/MOCK.md'
ls runs/<run_id>/agent-round-1-continue1.log
```

`./bench.py check` probes both images with the cpuset and fails if `nproc` or
`sched_getaffinity` is not 1.

The runner can also be invoked directly inside the evaluator image for
debugging, but the harness path (root-only `/opt/private`) is the supported
flow. Maintainer diagnostics may mount the private suite checkout read-only;
never expose it to the agent.

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
  `/opt/oracle` or `/opt/private` to uid 1000.
- Do not copy the private suite checkout into a public build context, image,
  issue or other public artifact; use the named `tests` build context.
- Do not commit `.private-eval/`; it is gitignored but lives inside the
  working tree.
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
  terminal (`401`/`403`/`429`, quota, auth) error, keep the checkpoint, mark the
  run `agent_error` and ineligible, and never submit it. Do not spend later
  rounds on a dead provider. A transient engine failure that the same log shows
  the engine recovered from is a blip, not a provider failure: the run stays
  eligible. Unrecovered transient failures resume the session for a bounded
  number of attempts (`--provider-retries`, default 2) inside the round budget.

