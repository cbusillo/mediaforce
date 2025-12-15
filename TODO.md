# TODO

Keep this list short and current. Completed work should move to `CHANGELOG.md` (or git history).

## Now

- [ ] **Canonical worker identity**
  - Add support for explicit worker name (`MEDIAFORCE_MACHINE_NAME`) and document it.
  - Ensure the dashboard + per-worker controls don’t duplicate hosts (`*.local` vs LAN domains).

- [ ] **Hard stop control (kill encode)**
  - Add a separate UI action to stop the current encode immediately (encodes are not resumable).
  - Must not corrupt the queue: item should return to `pending` (or `failed`) deterministically.

- [ ] **Better worker lifecycle UX**
  - Workers should show clear states: `waiting`, `encoding`, `paused` (drain), `stopping` (stop after current), `stopped`.
  - Dashboard wording should match behavior (no “watch pause”; watch is settings-only start/stop).

- [ ] **Progress/ETA accuracy**
  - Avoid “100% at start / 0 / ?” display glitches.
  - Prefer real frame counts when available; handle “unknown total_frames” cleanly.

## Later

- [ ] **Worker deployments**
  - Document recommended service configs for systemd + launchd (including env file + restart semantics).

- [ ] **Queue robustness**
  - Add explicit invariants for claim/progress/release (no stuck `encoding` without a live worker).
