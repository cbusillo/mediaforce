# Activity refresh measurement

Run the synthetic queue experiment without loading local configuration or contacting
media computers:

```bash
uv run python scripts/benchmark_activity_refresh.py \
  --source-sha "$(git rev-parse HEAD)" --sizes 36,1000,10000 --iterations 5 \
  --output /path/outside/the/repo/activity-refresh.json
```

Each size gets its own temporary SQLite database. It contains three folder parents
(needing attention, queued and running) and the requested number of pending file
shards. Every twelve-file block cycles through closed computer windows, explicit
window waits, interrupted work, interruption markers with storage/computer waits,
short windows, retries, storage waits, unavailable computers, impossible window
fits, schedule bypasses and ready work. Successive blocks rotate parent states,
so 36 files cover every case under every parent state. A fixed Monday at noon UTC
keeps the nighttime window closed independently of the host clock.

The script measures the production `summarize_encode_queue` and scheduler decoration
used by Activity and the folder content summary, followed by JSON response encoding.
It checks pending and window-wait counts against the fixture's independent case
expectations on every measured pass, and rejects an internal inventory in the
returned response. The behavioral test also deliberately corrupts a count and
requires the experiment to fail.

Setup and one warm refresh are excluded. Five untraced timing samples, one separate
tracemalloc allocation pass and one separate SQL/profile instrumentation pass avoid
mixing tracing overhead into request-component timing. `python_peak_bytes` measures
incremental traced Python allocations for one warm refresh, not total RSS or native
SQLite allocations. SQLAlchemy-observed SQL text and counts (excluding raw-driver connection PRAGMAs), profile builds, decorated jobs and bounded
display manifest-total calls are included in the JSON. The supplied source SHA and
the script's SHA-256 fingerprint identify what was measured.

If allocation tracing is already enabled, the benchmark subtracts live baseline
allocations, resets the tracer's peak before the refresh and leaves tracing enabled
on return. This resets the caller's peak history. Timing samples in that invocation
also carry tracing overhead; use the normal untraced CLI for timing comparisons.

This is a shared request **component**, not end-to-end HTTP or complete Activity or
folder-route latency. It excludes transport, other dashboard/folder work, manifest
telemetry cost and media inventory. Display telemetry totals are stubbed to an
empty mapping, with calls counted; pending file classification still uses the real
code and skips telemetry. No manifests or real media exist, no runtime server starts,
and calls through subprocess.run/Popen fail the experiment. Other queue layouts, larger stored
JSON payloads, cold storage and concurrent writes may cost differently.

## October 3, 2026 experiment

The [raw measurement](evidence/activity-refresh-755.json) preserves every timing sample
and SQL statement.

Measured benchmark revision: `86e9303cabdf87e27d0598b1640f5e5f5a625f6e`; tracing was
initially disabled. A later fix supports callers that already have tracing enabled;
the immutable evidence below was produced by the recorded original fingerprint.

Product source: `0e82b844dd19aee034428eb116fd0eb41df06392` (includes PR #754).
Benchmark fingerprint: `3f3171372045baf93b17ff8ddaca8804669533cc96dd629b94930f8fe0562fbd`.
Python 3.13.7, macOS ARM64, local fixture SQLite on the host's temporary filesystem.
Each row contains 1,104 bytes of synthetic notes, wider than empty/short operator
notes. The allocation number includes that padding; an optimization comparison
must also run short/empty notes before estimating its benefit for real queues.

The host was running an overnight capacity batch and the local acceptance suite;
these results describe this environment, not a universal latency guarantee or an
isolated hardware comparison.

| Pending files | Expected/observed window waits | Median total ms | Sample range ms | Median read/hydration ms | Median classification ms | Traced peak bytes |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 36 | 12/12 | 15.72 | 6.83–235.25 | 12.33 | 0.49 | 232,442 |
| 1,000 | 335/335 | 1,021.70 | 638.58–1,210.55 | 1,017.44 | 4.47 | 4,136,713 |
| 10,000 | 3,335/3,335 | 2,053.12 | 1,527.38–3,965.88 | 1,978.82 | 40.51 | 41,113,539 |

Each refresh executed 15 SQL statements, built profiles once, decorated N + 4 jobs,
and made four display manifest-total calls. JSON encoding medians stayed under
0.1 ms: only the bounded display rows, not N pending jobs, reach the response.
Exact total samples in milliseconds:

- 36: 6.83, 14.05, 15.72, 19.55, 235.25
- 1,000: 1,210.55, 897.75, 1,021.70, 1,086.32, 638.58
- 10,000: 2,574.68, 3,965.88, 1,527.38, 2,053.12, 1,961.16

An earlier Always/Never-only probe produced medians 7.64/318.55/3,659.24 ms and
allocation peaks 231,005/4,129,134/41,073,640 bytes. It omitted clock-based windows
and is retained as an initial probe, not the main comparison. The spread reinforces
why individual timings should not be read as precise scaling ratios. Both passes
show bounded query/profile work and roughly linear pending-inventory allocations.

Recommendation: pursue a focused compact-column/streaming inventory experiment in a
separate item. The 10,000-file fixture already consumes about 39 MiB of traced Python
allocations in this deliberately wide-row fixture. The timing samples include connection
opening, all summary queries and media-scope reads; they do not isolate the pending
query and cannot establish its latency or justify priority on timing alone.
Base the follow-up on the demonstrated allocation scaling, first compare short/empty
notes, and isolate pending-query work before claiming a timing win. Measure a projection containing
only classification inputs, while retaining the full display rows and per-file
classification. Compare exact counts across this closed-window case matrix before adopting it,
and retain the existing scheduler tests for open windows and the Never profile;
do not replace classification with parent-row counts or introduce cached counts
without a correctness design. The current production read remains unchanged by
this measurement. There is no demonstrated production incident or numeric latency
budget, and no deployment is part of this work.
