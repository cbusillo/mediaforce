# TODO

Keep this list short and current. Completed work should move to `CHANGELOG.md` (or git history).

## Now

- [x] **Canonical worker identity**
  - Support explicit worker name (`MEDIAFORCE_MACHINE_NAME`) and normalize dotted hostnames.
  - Ensure the dashboard + per-worker controls don’t duplicate hosts (`*.local` vs LAN domains).

- [x] **Hard stop control (kill encode)**
  - UI action to stop the current encode immediately.
  - Item returns to `pending` deterministically.

- [x] **Better worker lifecycle UX**
  - Workers show clear states and consistent wording across Dashboard/Settings.

- [x] **Progress/ETA accuracy**
  - Live progress/ETA display with sane defaults and stale-row cleanup.

- [ ] **Worker service robustness (systemd/launchd)**
  - Provide known-good unit/plist patterns to avoid restart loops / mid-encode kills.
  - Add docs for restart semantics and safe stop behavior.

- [ ] **DB reconcile/self-heal loop**
  - Periodically reconcile `media_inventory` vs `encode_progress` and reset stuck `encoding` rows.
  - Keep UI + DB consistent across worker/master restarts.

- [ ] **Stop-now cooldown**
  - Prevent the same worker immediately re-claiming the same item after Stop Now.
  - Prefer short cooldown or per-worker avoid list.

## Later

- [ ] **Worker deployments**
  - Document recommended service configs for systemd + launchd (including env file + restart semantics).
