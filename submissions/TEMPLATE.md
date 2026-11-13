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
`bench.py submit` is what the site and issue form consume.

## Ranking

Primary: score (higher is better). The score is seed-normalised and already
includes the correctness gate. Ties: model time (`TTFT + thinking + output`),
then wall time, then the run timestamp. Ineligible runs keep 75% of their score
on the local page.

Only `eligible: true` runs with `gate_ok: true` and `perf_valid: true` should be
treated as validated results. A self-reported run that fails a gate is not a
codec result.
