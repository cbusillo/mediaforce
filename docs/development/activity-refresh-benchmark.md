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
SQLite allocations. SQLAlchemy-observed SQL text and counts (excluding raw-driver connection PRAGMAs), profile builds, decorated jobs and fixture display manifest-total calls are included in the JSON. The supplied source SHA and
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

With display cardinality held fixed, each fixture refresh executed 15 SQLAlchemy
statements, built profiles once, decorated N + 4 jobs, and made four display
manifest-total calls. JSON encoding medians stayed under 0.1 ms: the fixture's four
display rows, not N pending shards, reach the response. Real `needs_attention`
display rows are unbounded and are not varied in this comparison; query work,
manifest reads and response size can grow with that separate dimension.

A review control at `74cf6aa7bc5f30aaa9e86da700ad6fae025c3771` added 30 synthetic
attention parents while holding 36 pending files fixed. Waiting counts remained
12, but attention display rows grew to 31, observed statements to 50, display
manifest-total calls to 39 and response bytes to 103,419. This confirms the
fixed-display limitation; it is not a production cardinality measurement.
Exact total samples in milliseconds:

- 36: 6.83, 14.05, 15.72, 19.55, 235.25
- 1,000: 1,210.55, 897.75, 1,021.70, 1,086.32, 638.58
- 10,000: 2,574.68, 3,965.88, 1,527.38, 2,053.12, 1,961.16

An earlier Always/Never-only probe produced medians 7.64/318.55/3,659.24 ms and
allocation peaks 231,005/4,129,134/41,073,640 bytes. It omitted clock-based windows
and is retained as an initial probe, not the main comparison. The spread reinforces
why individual timings should not be read as precise scaling ratios. Both passes
show constant query/profile work at fixed display cardinality and roughly linear
pending-inventory allocations.

Recommendation: pursue a focused compact-column/streaming inventory experiment in a
separate item. The 10,000-file fixture already consumes about 39 MiB of traced Python
allocations in this deliberately wide-row fixture. The timing samples include connection
opening, all summary queries and media-scope reads; they do not isolate the pending
query and cannot establish its latency or justify priority on timing alone.
Base the follow-up on the demonstrated allocation scaling, first compare short/empty
notes, and isolate pending-query work before claiming a timing win. Measure a projection containing
only classification inputs, while retaining the full display rows and per-file
classification. This matrix covers shards under folder parents; standalone `single`
jobs and variable attention-parent cardinality belong in the follow-up comparison.
Compare exact counts across that expanded matrix before adopting an optimization,
and retain the existing scheduler tests for open windows and the Never profile;
do not replace classification with parent-row counts or introduce cached counts
without a correctness design. The current production read remains unchanged by
this measurement. There is no demonstrated production incident or numeric latency
budget, and no deployment is part of this work.

## October 4, 2026 compact inventory comparison

The follow-up for #758 reproduces the unchanged #755 benchmark, then adopts a
compact pending read after the expanded comparison demonstrates lower allocations.
The scheduler and display reads remain unchanged. The pending read selects the eight
inputs used by scheduler decoration and hydrates each row as the cursor is consumed;
it no longer retains a fetched list of full database rows alongside hydrated jobs.
Classification still visits every pending runnable file, with the same statuses,
ordering and limit. There is no persisted count, display cap or dispatch change.

[Baseline reproduction](evidence/activity-refresh-758-baseline.json) and
[comparison data](evidence/pending-inventory-758.json) retain the samples and SQL.
The baseline was run before source edits at `9ab84cc91db07c08b3aec785a875b98eededd79b`
(its raw caller-supplied SHA is abbreviated). The comparison records this base SHA,
SHA-256 fingerprints of the measured product files, and both benchmark scripts.
These file fingerprints identify the uncommitted comparison source exactly;
they are not a claim that the base commit already contains the projection.

```bash
uv run python scripts/benchmark_pending_inventory.py \
  --source-sha "$(git rev-parse HEAD)" --sizes 36,1000,10000 --iterations 5 \
  --output /path/outside/the/repo/pending-inventory.json
```

The fixture uses empty, 22-byte short and original 1,104-byte wide notes. It adds
non-bypassed Never files, closed-profile retry backoff and closed-profile unrelated
reasons to the original cases. Uneven frequencies and 136 independent one-file
checks at noon and 11 PM UTC under attention/queued/running parents and as singles
prevent cancelling aggregate errors. Open night, closed night, Never and bypass
counts agree. Existing cross-midnight scheduler tests remain part of the full gate.

Timing is untraced; allocation, SQL and profile passes are separate. Isolated pending
query time uses a warm, already-open connection and includes cursor consumption and
hydration, excluding opening, other summary queries and media-scope reads. Full runs
precede compact runs; busy-host variation and this fixed order prevent causal or
precise timing-ratio claims. Both paths use the same fixture. The allocation result,
including empty notes, supports adoption without a production latency claim.

| Files | Notes | Full pending peak bytes | Compact pending peak bytes | Full query median ms | Compact query median ms | Full refresh peak bytes | Compact refresh peak bytes |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 36 | empty | 104,580 | 44,480 | 0.45 | 0.27 | 173,490 | 147,977 |
| 1,000 | empty | 2,726,353 | 1,002,795 | 7.03 | 3.84 | 3,009,478 | 1,546,086 |
| 10,000 | empty | 27,321,148 | 9,987,839 | 86.79 | 41.77 | 29,842,792 | 15,083,334 |
| 36 | short | 106,848 | 43,680 | 0.43 | 0.26 | 190,633 | 148,585 |
| 1,000 | short | 2,789,353 | 1,003,899 | 7.16 | 4.05 | 3,071,980 | 1,555,512 |
| 10,000 | short | 27,949,180 | 9,987,911 | 85.50 | 40.80 | 30,487,570 | 15,084,038 |
| 36 | wide | 145,800 | 43,440 | 0.49 | 0.26 | 207,217 | 152,557 |
| 1,000 | wide | 3,869,753 | 992,323 | 15.22 | 4.54 | 4,159,008 | 1,552,966 |
| 10,000 | wide | 38,771,148 | 9,987,583 | 186.36 | 45.64 | 41,297,372 | 15,087,664 |

Pending hydration peaks measure the isolated query return. Decoration incremental
peaks in the raw data exclude already-retained raw inventory and measure the copied
decorated rows separately. Whole-refresh peaks include summary rows, pending
inventory, decoration and serialization together. These peaks have different
lifetimes and are not additive. They cover traced Python allocations, not RSS or
native SQLite memory; caller tracing stays enabled but its peak history is reset.

At 10,000 files, empty/short pending allocations fall about 63–64%; whole-refresh
allocations fall about 49–51%. The wide-row peak falls further because unused notes
are not loaded. The small fixture has modest absolute savings. No real queue size
was measured, and these results do not promise a production memory saving.

All comparisons require identical complete display payloads and exact
pending/window counts. SQL/profile work remains equal across paths: 15 observed
statements and one profile build with four display telemetry calls at fixed display
cardinality. SQLAlchemy counts exclude raw-driver connection PRAGMAs.

| Pending files | Singles | Extra attention parents | Clock UTC | Waiting files | Attention rows | SQL statements | Display telemetry calls |
| ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| 36 | 18 | 0 | 12:00 | 17 | 1 | 22 | 11 |
| 36 | 0 | 30 | 12:00 | 17 | 31 | 50 | 39 |
| 36 | 18 | 30 | 23:00 | 8 | 31 | 57 | 46 |

Adding attention rows raises query, telemetry and response work in both paths;
compact whole-refresh peaks can be higher in these small display-heavy controls.
Every attention row and all telemetry remain present. Standalone singles overlap
display and runnable inventories, but their full display rows remain unchanged.
Internal pending rows never reach the API. The optimization targets demonstrated
pending hydration cost rather than capping display cost. No deployment, live media,
host operation or change to quality/validation rules is part of this experiment.
