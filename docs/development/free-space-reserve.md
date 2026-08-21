# Production Free-Space Reserve

Production queueing and promotion fail closed unless Mediaforce can measure a
reserve on every configured storage volume involved in the operation.

## Contract

- Group capacity by the actual local filesystem or remote `df` filesystem, not
  by hard-coded path names.
- Reserve staged output, a cross-volume source-to-archive copy, and the staged
  output's cross-volume promotion copy. The rollback archive backup is a
  same-directory rename and does not require a second copy reserve.
- Add the configured operating headroom once per affected volume.
- Recheck immediately before dispatch and before every promotion because other
  work can consume capacity after queue admission.
- Add the persisted requirements of already-running encode jobs before admitting
  another job on the same measured volume. Sub-threshold shards cannot each
  claim the same free bytes independently.
- Defer promotion while another runnable encode job is active so the promotion
  copy cannot consume bytes that active work has already reserved.
- Missing reserve inputs, a missing mount, a failed local probe, or a failed
  remote probe is a waiting blocker. Mediaforce does not dispatch, delete,
  archive, or promote files in that case.
- Remote encode staging is measured on the selected worker. Archive and final
  promotion capacity are measured on the controller, where promotion runs.

## Configuration

`[media.free_space_reserve]` is enabled by default and has no feature flag:

```toml
[media.free_space_reserve]
operating_headroom_gib = 16
staged_output_overhead_percent = 10
large_job_gib = 16
```

The staged-output reserve uses the approved stream-budget target when one is
available and adds `staged_output_overhead_percent` for normal size variance. It
falls back to the configured source-relative cap, then the full source size,
rather than guessing optimistically. Cross-volume source archiving reserves the
full source as the rollback copy; same-volume moves are renames and require no
second copy. A job at or above `large_job_gib` serializes with existing running
encode work using the current persisted queue state. Capacity probes are reused
within one scheduler pass, including best-fit retries and running-job reserve
reconstruction.
