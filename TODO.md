# TODO

Keep this list short and current. Completed work should live in git history.

## Now (UI/UX + Cleanup)

- [ ] **Standardize web UI**
  - Use shared macros (`partials/ui_macros.html`) and shared JS (`static/js/ui.js`)
    across all pages.
  - Align page headers, toolbars, buttons, and empty states with `Review` as the
    baseline.

- [ ] **Review workflow UX**
  - Keep row-details inline (no popups) and highlight “attention needed” changes
    (track count changes, codec/profile changes, VMAF outliers).
  - Reduce table clutter (short filename display; show full details on expand +
    compare).

- [ ] **Compare page UX**
  - Keep controls/actions pinned at the top.
  - Make layout predictable (fit/fill toggle; consistent sizing; show inspection
    inline).

- [ ] **Remove legacy/duplicated UI code**
  - Delete unused handlers/templates and converge on a single pattern for worker
    controls, filtering, and status messaging.

## Soon (Pipeline & Data Hygiene)

- [ ] **Data reset tooling**
  - Add safe UI/API action(s) to clear review/queue state and reconcile transcode
    location contents.
  - Ensure this respects per-host library mappings and does not delete original
    media.
