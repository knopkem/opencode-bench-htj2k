# Submit a result

Run the benchmark, then:

```bash
./bench.py submit --run RUN_ID --print
```

Paste the JSON block into a new issue using the benchmark-result issue form, or
create the issue directly:

```bash
./bench.py submit --run RUN_ID --gh --repo knopkem/opencode-bench-htj2k
```

Include the hardware line and the model/variant reference in the issue title.
Results are self-reported unless a maintainer adds `verified`.

The canonical schema is [`schema.json`](schema.json). The block printed by
`bench.py submit` is what the site and issue form consume. Include
`network_policy` (`open` or `offline`), `cpu_set`, `rounds` and `confidence` so
the page does not mix incompatible results. The block also records the
`agent_image` / `test_image` references and the evaluator suite version used,
so runs scored against different images stay distinguishable.

The block additionally carries informational effort fields (`ttft_s`,
`think_s`, `output_s`, `tool_s`, `self_test_s`, their shares, request-token
peak/mean, `gen_tokens_per_s`, `steps`, `compactions`, `cost_usd` and
`metrics_source`). They power the leaderboard detail columns and tooltips and
never affect the score or eligibility. Older blocks without them remain valid
and simply render dashes. `./bench.py backfill` rebuilds the fields from the
saved `agent-round-*.log` and `opencode-data/opencode.db` artifacts.

## Ranking

Primary: score (higher is better). The score is seed-normalised and already
includes the correctness gate. Ties: model time (`TTFT + thinking + output`),
then wall time, then the run timestamp. Ineligible runs keep 75% of their score
on the local page.

Only `eligible: true` runs with `gate_ok: true` and `perf_valid: true` should be
treated as validated results. A self-reported run that fails a gate is not a
codec result. Include `integrity_violation` from `score.json`; a true value makes
`eligible` false while preserving the measured score for diagnostics. One run is
accepted, but `confidence: single-run` is noisy;
three runs with different seeds are recommended. Runs with an `agent_error`
(provider quota/auth/rate limit) are ineligible and must not be submitted.
A transient engine failure that the agent log shows the engine recovered from
is not an `agent_error`; `provider_errors` records it for transparency.
