# Migration Plan: Public Harness with a Separate Evaluator Image

Status: complete — public history rewritten and force-pushed; private
evaluator repository created; agent/evaluator packages public with anonymous
pulls verified; source repository public; community leaderboard live at
https://knopkem.github.io/opencode-bench-htj2k/
Date: 2026-11-26

## 0. Current implementation state

Implemented locally:

- `bench.py` uses separate agent/build and evaluator image configuration,
  defaults to the public GHCR `edge` images, supports `build-eval`, validates
  the evaluator manifest from the image, and records image provenance
  (`agent_image`, `agent_image_id`, `test_image`, `test_image_id`,
  `test_suite_version`) in run metadata.
- `docker/agent.Dockerfile` remains the public agent/build recipe.
- `docker/evaluator.Dockerfile` builds the private-suite evaluator layer; the
  suite enters through a named `tests` build context that points at the
  private `opencode-bench-htj2k-eval` checkout.
- `.github/workflows/agent-image.yml` publishes the public agent image.
- The private suite, pinned OpenJPH oracle and raw detailed reports now live
  in the private `knopkem/opencode-bench-htj2k-eval` repository. Its local
  checkout is the ignored `.private-eval/` directory inside this working
  tree, including the private evaluator-image workflow under
  `.github/workflows/`.
- Public documentation, submission metadata and regression tests reflect the
  split.

Completed for the public repository:

- The migration was committed and all public branches were rewritten with
  `git-filter-repo`.
- The rewritten history was verified for removed paths and distinctive hidden
  test content, then force-pushed.
- The private pre-rewrite bundle, ref map and old-to-new commit map are stored
  under `.private-eval/history/`.

Image releases:

- `ghcr.io/knopkem/opencode-bench-htj2k-agent:edge` was built by the public
  repository's `agent-image.yml` workflow (linux/amd64 + linux/arm64),
  verified (no `/opt/oracle`/`/opt/private`, cargo/python3/opencode present)
  and pushed. Its package is **public**; anonymous pulls verified (registry
  token dance + `docker pull`/`docker manifest inspect` with no credentials,
  then a full no-op run with the default image references).
- `ghcr.io/knopkem/opencode-bench-htj2k-eval:edge` was built by the private
  evaluator repository's `evaluator-image.yml` workflow (linux/amd64 +
  linux/arm64), passed the private verification (manifest, suite bytecode,
  oracle startup, mode-700 probes) and was pushed. Its package is **public**
  as well; anonymous pulls verified the same way.

Note: a bare unauthenticated `curl` to GHCR manifest URLs always receives a
`401` challenge first; clients must complete the bearer-token dance (docker
does this automatically). A public package is visible after the dance, a
private one is not.

Completed by the repository owner on 2026-11-26:

1. Both GHCR packages were made public; anonymous pulls verified end-to-end
   (registry token dance, `docker pull`/`docker manifest inspect` with no
   credentials, and a full no-op run against the default image references).
2. The issue/artifact audit passed: open `benchmark-result` issues contain
   aggregate submission data only; no PRs, releases, or artifacts.
3. The source repository was made public. GitHub Pages was enabled by the
   community-leaderboard workflow (`configure-pages` with `enablement: true`)
   and the leaderboard is live.

## 1. Purpose and threat model

Migrate `opencode-bench-htj2k` to a public source repository without
publishing the current evaluation sources and detailed run artifacts as
ordinary GitHub files.

The primary goals are to:

- reduce the chance that LLMs discover evaluation tests through GitHub search,
  source-code crawls, or future source-code training corpora;
- prevent the agent container from reading evaluation tests during a run;
- avoid publishing new detailed test results, assertion failures, traces, and
  run artifacts as easily crawled text;
- preserve convenient, reproducible, one-command scoring using publicly
  pullable container images.

This plan treats a public evaluator image as **scrape reduction, not
confidentiality**. Anyone can pull an OCI image and inspect its files and
layers. Deleting a file in a later Dockerfile layer does not remove it from
the image. A determined person or an agent with suitable network access can
therefore recover and republish the tests and the oracle.

This plan assumes that publishing sanitized aggregate scores is acceptable.
Those scores and the public leaderboard remain crawlable. If no benchmark
results may be public at all, the public issue-based leaderboard must also be
disabled or moved behind access control. Neither `robots.txt`, `noai`
metadata, nor license terms can guarantee exclusion from training.

## 2. Target architecture

### Public source repository

Keep the following public:

- `bench.py`, `tasks/htj2k/PROMPT.md`, and the task seed;
- public harness unit tests;
- the generic agent/build Dockerfile and the evaluator recipe;
- scoring, reporting, submission, and leaderboard code;
- sanitized aggregate results and community submission tooling.

Remove from the public source tree:

- `tasks/htj2k/tests/` (hidden suite, fixtures, manifest, oracle);
- raw generated run artifacts and detailed hidden-test output.

### Private evaluation-source repository

Create a private repository, `opencode-bench-htj2k-eval`, containing:

- the hidden runner, corpus generator, frozen manifest and fixtures;
- the pinned OpenJPH oracle source and the independent oracle harness;
- an evaluator Dockerfile and suite layout;
- the trusted workflow that builds and publishes evaluator images;
- private official run artifacts and detailed reports.

The private repository is the source of truth for evaluation-source changes.
The public repository remains the source of truth for the harness, prompt,
seed, and scoring contract.

### Public agent image

Publish an image such as:

```text
ghcr.io/knopkem/opencode-bench-htj2k-agent:<version>
```

It contains OpenCode, Rust, and generic build tooling. It must not contain
hidden tests, the oracle, run results, registry credentials, or OpenCode
credentials.

Use this image for both the agent and application build containers.

### Public evaluator image

Publish a separate image such as:

```text
ghcr.io/knopkem/opencode-bench-htj2k-eval:<suite-version>
```

Build it in the private evaluation-source repository from a pinned Rust base.
Copy the hidden suite into `/opt/private` and the oracle into `/opt/oracle`,
both mode 700, and include the frozen manifest at
`/opt/private/manifest.json`. Record the suite version and source revision in
image labels.

Use this image only after the agent container has exited. Do not bake
generated results into it.

Record the immutable evaluator image digest in each local score. Use
versioned, immutable tags for releases; `latest` may be a convenience alias
but must not be the value recorded for reproducibility.

## 3. Runtime flow

The target flow is:

1. Pull or build the public agent image.
2. Start the agent container using only the agent image, the seed workdir, and
   the required OpenCode configuration.
3. Stop and remove the agent container.
4. Build the candidate and pristine seed using the agent image.
5. Start the evaluator container using the evaluator image, with the built
   binaries mounted read-only and the run directory mounted for output.
6. Run the suite with networking disabled, one pinned CPU, and the candidate
   as uid 1000.
7. Write detailed artifacts only to the local, gitignored run directory.
8. Publish only explicitly sanitized aggregate submission data, if requested.

The evaluator container no longer receives a host suite bind mount. It must
still receive no agent credentials and must not mount the agent workdir
read-write.

Image separation prevents accidental local filesystem discovery by the agent.
It does not prevent an agent from retrieving a public evaluator image over a
network. If runtime retrieval becomes a concern, keep the evaluator package
private or add network egress restrictions that allow only required
model/provider endpoints.

## 4. Harness changes

`bench.py` was updated in a small, reviewable change set.

### Image configuration

- `OPENCODE_BENCH_AGENT_IMAGE` and `--agent-image` select the agent/build
  image (default `ghcr.io/knopkem/opencode-bench-htj2k-agent:edge`);
- `OPENCODE_BENCH_EVAL_IMAGE` (legacy alias `OPENCODE_BENCH_TEST_IMAGE`) and
  `--test-image` select the evaluator image (default
  `ghcr.io/knopkem/opencode-bench-htj2k-eval:edge`).

Image usage is routed explicitly:

- `start_agent()` and `build_app()` -> agent image;
- `run_tests()` -> evaluator image;
- image checks in `check` -> the appropriate image for each stage.

Public GHCR images are pulled on demand when missing.

### Test execution

`run_tests()` now:

- uses the evaluator image;
- runs `/opt/private/runner.py` already present in that image;
- no longer builds or mounts a suite bundle from a local checkout;
- preserves `--network none`, one pinned CPU, read-only binaries, JSON
  output, and existing result parsing and scoring behavior.

`run`, `check`, and `rescore` no longer require a local hidden-test checkout.
Commands that do not evaluate a candidate, such as `report`, `leaderboard`,
`backfill`, `submit`, and `site`, do not require either image.

### Validation and metadata

`check` verifies:

- both images are available or provides actionable pull/build instructions;
- both images report one pinned CPU;
- the evaluator image contains the oracle, the frozen manifest, the runner
  and the fixtures, and the oracle starts;
- the agent image has cargo/python3/opencode and contains neither
  `/opt/oracle` nor `/opt/private`.

Local run metadata records:

- agent image reference and digest;
- evaluator image reference and digest;
- evaluator suite version from the image label;
- harness commit, which is already captured for submissions.

The result schema remains `opencode-bench/htj2k-result@1`; the new fields are
optional metadata and the scoring contract is unchanged.

## 5. Image build and release process

### Agent image workflow

`.github/workflows/agent-image.yml` publishes the public agent image. It:

- builds only from the public build context;
- verifies the agent image has no `/opt/oracle`/`/opt/private` and has
  cargo/python3/opencode;
- pushes a versioned GHCR tag;
- uses only the permissions it needs: `contents: read` and
  `packages: write`.

### Evaluator image workflow

The local staging tree includes
`.private-eval/.github/workflows/evaluator-image.yml`; that workflow runs in
the private evaluation-source repository. In that repository:

1. Build the evaluator image from `docker/evaluator.Dockerfile` with the
   named `tests` build context.
2. Validate the manifest, compile the suite, probe the oracle and the
   mode-700 permissions.
3. Publish a versioned image and explicitly attempt to make the GHCR package
   public.
4. Keep workflow logs, temporary build context, and artifacts private.

The private repository is never copied into the Docker build context; only
the `docker/` directory is used as the context, with `tests` as a named
context.

Publishing an image is effectively irreversible: deleting a tag does not
recover copies already pulled. Treat every evaluator release as public
disclosure to a determined inspector.

## 6. Results and public-data hygiene

The existing ignores for `runs/`, raw CSV/report output, and generated
`docs/` are retained.

Keep private:

- `runs/` detailed artifacts (test logs, reports, round artifacts);
- raw per-run hidden-suite reports;
- private CI artifacts containing any of the above.

Public submissions may contain only the existing allowlisted aggregate
fields: counts, quality, eligibility, model/run metadata, and performance
metrics, plus the agent/evaluator image references. Do not attach raw logs or
detailed reports to public issues. Audit existing public issues and prose
around result blocks for accidentally included failure details.

The public leaderboard and issue tracker will still be crawlable. Publishing
aggregate numbers is a deliberate tradeoff, not a technical guarantee that
they will be excluded from model training.

## 7. Required GitHub history prune and force-push

Removing `tasks/htj2k/tests/` in new commits does not remove it from Git
history. Before making the repository public, those paths were pruned from
**all branches and tags**, then the rewritten refs were force-pushed to
GitHub.

### 7.1 Prepare and preserve a private backup

1. Freeze pushes until the rewrite is complete.
2. Confirm that the current suite and oracle are safely stored in the private
   evaluation-source repository.
3. Create a private mirror backup kept outside any public build context or
   artifact directory.
4. Make a separate mirror copy for the rewrite so the backup remains
   untouched. Record the pre-rewrite default-branch and tag hashes privately.

The backup contains the exposed tests and results and must remain private.
Never push it back to the public remote as a recovery branch or tag.

### 7.2 Rewrite every branch and tag

`git-filter-repo` was run in the disposable mirror:

```bash
git filter-repo --invert-paths \
  --path tasks/htj2k/tests
```

No other historical paths required removal: generated results (`runs/`,
CSV, leaderboard HTML, `docs/`) were never committed to this repository, and
the public root `tests/` harness unit suite is required by the rewritten
HEAD.

The generated old-to-new commit map is retained privately under
`.private-eval/history/`. Existing `score.json` files may contain
pre-rewrite harness commit hashes; those links no longer resolve after the
force-push and are interpreted through the private commit map.

### 7.3 Verify before and after the force-push

Before pushing, verify that no matching paths remain in any rewritten ref:

```bash
git log --all --oneline -- tasks/htj2k/tests
git rev-list --objects --all | grep -E 'tasks/htj2k/tests'
```

Both commands must produce no output. After local verification, every
rewritten branch and tag was force-pushed, and the checks were repeated
against a fresh mirror of the remote refs.

### 7.4 Clean up GitHub data not covered by force-push

A force-push rewrites normal Git refs, but it does not scrub every GitHub
object or product surface. Audit and handle separately:

- pull-request refs and open/closed PR contents;
- issue comments, attachments, and existing result prose;
- release notes, release assets, and releases attached to rewritten tags;
- Actions logs/artifacts and Pages deployments;
- container packages, package provenance, and old image tags;
- direct links to old commit SHAs and GitHub's cached/dangling objects.

History pruning does not erase existing forks, clones, external caches, prior
crawling, or training. For a cleaner future benchmark baseline, author the
next suite privately rather than reusing the previously public assertions
unchanged. Do not claim that old tests or old results are secret again.

## 8. Documentation updates

The public documentation covers:

- anonymous pulling of both images;
- versioned agent/evaluator defaults and overrides;
- local agent-image builds versus private evaluator-source builds;
- the fact that evaluator tests are publicly recoverable from a public
  evaluator image;
- the distinction between sanitized public scores and private raw artifacts;
- offline evaluator execution after images have been pulled;
- migration from the local `tasks/htj2k/tests` layout.

## 9. Validation plan

### Harness regression checks

```bash
python3 -m py_compile bench.py
python3 -m unittest discover -s tests
./bench.py check
```

Plus the no-op end-to-end check (mock agent writes COMPLETION.md and changes
no code):

```bash
./bench.py run --model mock/noop --no-preflight \
  --agent-cmd 'echo noop > /work/COMPLETION.md' \
  --round-plan 1,1,1
./bench.py rescore <run_id>
```

The no-op check also exercises the per-round clock, the staged agent config
(`question` denied) and the two-repeat final grading.

### Evaluator correctness

Using the pristine seed and the private suite:

- the seed passes all correctness gates and scores `0.0`;
- `check` probes pass in the pinned one-CPU environment;
- the evaluator runs with no network and no host suite mount;
- a saved workdir rescored with the new evaluation path produces identical
  test counts and quality.

### Isolation and release checks

Verify that:

- the agent image contains no suite files or oracle;
- the agent container receives no evaluator files, evaluator credentials, or
  Docker socket;
- the evaluator image contains the expected suite and manifest but no run
  results;
- both images can be pulled anonymously once the packages are public;
- a clean public clone can run the documented workflow without a private
  checkout;
- a sample end-to-end run and `rescore` record the evaluator digest and
  suite version correctly.

## 10. Rollout sequence

1. Create the private evaluation-source repository and copy the current suite
   and oracle there without exposing new CI logs or artifacts.
2. Implement and review the two-image harness changes while the main
   repository is still private.
3. Build release-candidate agent and evaluator images and complete all
   validation.
4. Execute the required all-branch/all-tag history prune and force-push,
   preserve the private commit map, and audit existing issues, PRs, releases,
   artifacts, and cached objects.
5. Publish versioned images and test anonymous pulls in a clean environment.
6. Update documentation and pin the public harness defaults to the validated
   image versions.
7. Make the source repository public.
8. Run one clean-clone end-to-end smoke evaluation and compare it with the
   private pre-release result.
9. For subsequent suite changes, publish a new immutable evaluator version
   rather than mutating an existing release tag.

## 11. Rollback and future hardening

Before rollout, the private pre-prune mirror, its old-to-new commit map, and
known-good local images are retained. If migration validation fails, keep the
repository private and restore the previous local-suite workflow. Use the
pre-prune mirror only for private recovery; never force-push it back to the
public remote. Do not attempt to solve an accidental evaluator release merely
by deleting its tag.

If stronger protection becomes necessary later, keep the evaluator package
private with authenticated pulls, or return to private tests mounted only on
trusted evaluators. For official self-service scoring without distributing
tests at all, use a remote trusted scoring service instead.

## 12. Completion criteria

The migration is complete when:

- every publicly reachable branch and tag has been rewritten and verified to
  contain no evaluation-source or generated-result paths, including after a
  fresh mirror fetch from GitHub;
- historical submissions with pre-rewrite harness hashes remain interpretable
  via the private old-to-new commit map;
- agent and evaluator responsibilities use separate images;
- no agent-visible local path contains the evaluator suite;
- all harness, seed, smoke, rescore-parity, and anonymous-pull checks pass;
- raw results remain private and only approved aggregate fields are
  published;
- image versions and evaluator provenance are recorded for reproducibility;
- documentation clearly states that public evaluator images reduce
  casual/source corpus discovery but do not provide test secrecy or guarantee
  training exclusion.
