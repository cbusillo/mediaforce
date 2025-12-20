# TODO

Keep this list short and current. Completed work should live in git history.

## Now (UI/UX + Cleanup)

- [ ] **Post-transfer validation**
  - After library transfer completes: run a full scan, verify shows/queue/review
    pages load real data, and confirm encode + review flows end-to-end.

- [x] **Movies page polish (filters + grouping)**
  - Add library filter and grouping toggle (folder vs file) to clarify non-series titles.

- [x] **Worker status polish (badges + copy)**
  - Add clearer visual badges for worker states (encoding/paused/unavailable/offline)
    and tighten the status text in the settings table.

- [x] **Transcode root UX**
  - Add quick-pick suggestions + inline validation feedback on Settings.

- [x] **Dashboard overview chips**
  - Add small counts for Shows/Movies/Queue at a glance.

- [x] **Queue affordances**
  - Add a shortcut to jump from Movies/Shows to Queue filtered by library.

- [x] **Movies / Non-series view**
  - Added a Movies page so non-series libraries don’t disappear behind Shows.

- [x] **Worker mount diagnostics**
  - Persist “library not mounted” per worker and surface it in the UI.

- [x] **Inspection cleanup pass**
  - Triaged remaining PyCharm warnings: SQLModel typing + optional deps are IDE
    configuration limits rather than runtime issues. No new suppressions added.
  - Generated assets remain excluded; sources remain inspectable.

- [x] **Reduce docstring/comment noise**
  - No redundant docstrings found; kept explanatory comments only.

- [x] **Style alignment pass (code)**
  - Applied type hints to signatures/public payloads, tightened TypedDict shapes.
  - Kept dynamic boundaries (logging/JSON/SQL expression maps) as the only `Any` hotspots.
  - Avoided new suppressions; asked/flagged IDE-profile artifacts instead.

## Soon (Pipeline & Data Hygiene)

- [ ] **Queue memory polish**
  - Remember the last selected library locally so the queue opens where you left it.

- [x] **Operational polish**
  - Decision: keep external fonts/HTMX for now; revisit if offline reliability or
    IDE noise becomes a recurring issue.
