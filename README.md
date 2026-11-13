# opencode-bench-htj2k

A local-LLM benchmark where the model optimises a **working pure-Rust HTJ2K
codec** for lossless encode and decode throughput.

HTJ2K (JPEG 2000 Part 15) is the high-throughput block-coding mode used by
DICOM transfer syntax `1.2.840.10008.1.2.4.201`. The seed is a pure-Rust port of
[OpenJPH](https://github.com/aous72/OpenJPH). It is correct, but there is
substantial headroom in the entropy coder, wavelet transform and allocation
paths. The model gets the seed and four hours; the hidden suite measures the
result.

This repository is a Docker-only fork of
[`opencode-bench-plusplus`](https://github.com/knopkem/opencode-bench-plusplus)
with the codec-optimisation framing inspired by
[`codec-reverse-bench`](https://github.com/andreasknopke/codec-reverse-bench).
There is no host sandbox: the agent, build and grading steps all run in
containers.

## Quick start

```bash
./bench.py build                 # build agent and test images
./bench.py check                 # prompt, seed, manifest, images
./bench.py models                # local OpenCode providers/models
./bench.py run --model provider/model          # 75/75/45 checkpointed rounds
./bench.py run --model provider/model --variant high
./bench.py run --model provider/model --round-plan 90,90,30
./bench.py run --model provider/model --max-nudges 2 --nudge-min 10 --final-reps 2
./bench.py status
./bench.py report
./bench.py leaderboard           # serve + open the local page
./bench.py rescore --run RUN_ID
./bench.py submit --run RUN_ID --print
python3 -m unittest discover -s tests
```

Docker/OrbStack must be running (`docker info`). The harness reads the local
OpenCode configuration; there is no model list or credential in this repo.
Both the agent and grader containers are pinned to one CPU with
`--cpuset-cpus 0` by default. That makes `nproc` and
`available_parallelism()` report 1, so local measurements match grading.

## What the model does

Each run copies `tasks/htj2k/seed/` into a fresh workdir and adds
`tasks/htj2k/PROMPT.md` as `PROMPT.md`. The seed builds and passes every
correctness gate. The model must keep the CLI and codestream contract while
making decode and encode faster.

The hidden suite is mounted only after the agent exits. The test container has
no network, runs on one pinned CPU, and executes the candidate as an unprivileged
user. The independent C++ OpenJPH oracle lives in a mode-700 directory that the
candidate cannot read.

The agent run is split into three rounds (75 / 75 / 45 minutes). After each round
the harness builds the workdir and reports gate status and speedups on a
validation corpus. The final round is graded like the others and may keep
optimizing until a 10-minute freeze before its deadline. A non-final round that
ends early with time left is resumed automatically. The official score is the
best eligible checkpoint, rescored twice on a held-out final corpus and combined
as the median per workload.

## Grading

Correctness is a gate:

- lossless decode bit-exact against C++ OpenJPH;
- lossless encode decodes bit-exactly with C++ OpenJPH and stays within 2% + 1
  KiB of the oracle's compressed size;
- real DICOM `.201` codestream decode;
- malformed-input handling and determinism.

Performance is seed-normalised:

```text
per workload:
  speedup = median over repetitions of (seed_time / candidate_time)
decode_speedup = geomean(workload speedups)
encode_speedup = geomean(workload speedups)

noise  = 0.08   # measurement noise floor
credit = clamp((speedup - 1 - noise) / (target - 1 - noise), 0, 2)
score  = 100 * (0.5 * credit_decode + 0.5 * credit_encode)
```

Seed and candidate alternate inside every repetition, so the paired ratio
cancels the per-process fast/slow modes seen on shared hosts. Min-of-N is
reported as a diagnostic but is not scored: one binary can hit a transient fast
mode that the other misses. The held-out final grading is repeated twice and
combined as the median per workload. The untouched seed scores `0`. Matching the
frozen target in both dimensions scores `100`; targets are derived from the
seed/OpenJPH ratios on the reference machine with a 1.25x floor. Correctness
failure means ineligible and `0`.

The frozen catalog is `tasks/htj2k/tests/manifest.json`: suite `htj2k-v1`,
per-run validation and held-out final seeds, ten encode+decode cases, five
decode-only layouts, a DICOM `.201` fixture, malformed-input checks and four
decode plus four encode throughput workloads. Checkpoints use the validation
seed; the official score uses the final seed.

## Frozen limits

| Limit | Value |
|---|---:|
| Agent wall clock | 4 h |
| Agent idle timeout | 1 h |
| Agent/grader CPU | 1 pinned CPU (`--cpuset-cpus 0`) |
| Round plan | 75 / 75 / 45 min |
| Held-out final corpus | yes |
| Candidate memory cap | 2 GiB |
| File deadline | 120 s |
| Perf suite deadline | 15 min |
| Perf repetitions | 9 |
| Perf CPU budget | 1 CPU |
| Final grading repeats | 2 |
| Encode size slack | 2% + 1 KiB |

## Test families

| Family | Purpose |
|---|---|
| `p0.decode-lossless` | bit-exact decode across bit depths, block sizes, progression orders and tiles |
| `p0.encode-lossless` | oracle-decodable output, size gate |
| `p0.dicom-201` | real DICOM `.201` codestream |
| `p0.robustness` | truncated, garbage, empty and wrong-sized inputs fail cleanly |
| `p0.determinism` | repeated encode output is identical |

Performance families are `decode` and `encode`, each with four generated
workloads. They are continuous, not pass/fail.

## Repository map

```text
bench.py                       harness, Docker orchestration, scoring, leaderboard
docker/agent.Dockerfile        Ubuntu + Rust + OpenCode (no oracle)
docker/test.Dockerfile         Ubuntu + Rust + Python + pinned C++ OpenJPH oracle
tasks/htj2k/PROMPT.md          candidate-visible contract
tasks/htj2k/seed/              codec, CLI, public benchmark and public fixtures
tasks/htj2k/tests/             hidden suite, manifest, corpus and oracle
tasks/htj2k/CALIBRATION.md     reference measurements and target derivation
submissions/                   community submission schema and example
tests/test_bench.py            harness/scoring unit tests
```

## Hidden-suite validation

The seed must pass all gates and score `0.0` with no changes. `./bench.py check`
verifies that both containers report one pinned CPU. The independent oracle must
decode every fixture. Negative controls and repeated runs are maintainer release
gates; see `AGENTS.md`.

## Trust model

The agent container has network access for the model provider. A model could look
up OpenJPH source online; the seed is already a port of it, so the gain is
limited, but this is **capability evidence, not a security boundary**. Results
record `network: open`; do not compare them directly with offline/local-model
results. The hidden corpus is regenerated per run and expected pixels never enter
the candidate process.

One run is accepted on the local leaderboard, but it is labelled `single-run`
and is low-confidence. Three runs with different seeds are recommended for a
strong claim. Community submissions are self-reported unless a maintainer adds
`verified`.

Provider quota, auth and rate-limit errors (`provider.free-limit`,
`provider.rate-limit`, HTTP 401/403/429) are detected in the agent log. The
harness stops immediately, keeps the checkpoint for diagnostics, marks the run
`agent_error` and ineligible, and refuses to submit it. Such a run is not a
model result.

Transient engine failures are different. OpenCode retries a request in place
when the backend dies mid-generation, so a `provider.internal` / 5xx error
followed by more steps in the same log is a blip: the run continues, stays
eligible, and the report records it. An unrecovered transient failure resumes
the same session and workdir for up to `--provider-retries` extra attempts
(default 2) with backoff, but only while the round budget lasts.

## License and attribution

The codec seed is derived from OpenJPH-RS, a BSD-2-Clause pure-Rust port of
OpenJPH v0.26.3. The oracle is built from the pinned OpenJPH source under
`tasks/htj2k/tests/oracle/OpenJPH` (BSD-2-Clause). See the notices in
`tasks/htj2k/seed/crates/htj2k/NOTICE` and
`tasks/htj2k/tests/oracle/OpenJPH/LICENSE`.
