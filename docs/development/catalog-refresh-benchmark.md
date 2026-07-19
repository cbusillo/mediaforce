# Catalog Refresh Benchmark

Use the generated benchmark to prove that an unchanged catalog refresh stays
inside the inventory path and launches no media-analysis child processes.

## Run

```bash
uv run scripts/benchmark_catalog_refresh.py
```

The default matrix covers 1,000 items, 10,000 items, the current catalog size,
and twice the current catalog size. Each generated catalog is refreshed twice
to catch work that only reappears on a later pass. Use `--sizes` or
`--iterations` for a focused local run:

```bash
uv run scripts/benchmark_catalog_refresh.py --sizes 1000,10000
```

Use `--output` to retain point-in-time JSON outside the repository:

```bash
uv run scripts/benchmark_catalog_refresh.py \
  --output ~/Desktop/mediaforce-catalog-refresh-benchmark.json
```

## Metrics

Each generated catalog contains unchanged files whose cadence and fingerprint
evidence is intentionally mixed between missing, malformed, and retryable
states. A passing result records:

- zero child-process launches
- zero discovered or reprobed items
- every generated item counted as unchanged
- wall and CPU time
- Python allocation peak and process peak RSS
- database operation count and elapsed time
- database calls over the lock-wait threshold and any SQLite busy/locked errors

`db_lock_wait_events` counts database calls whose elapsed time meets the
configured threshold. It is a conservative contention signal rather than an
internal SQLite lock-wait counter. `db_lock_errors` records explicit busy or
locked failures.

The benchmark creates all media, database, and runtime state under a temporary
directory and removes it after each case. It never writes review media or
runtime state into the repository.
