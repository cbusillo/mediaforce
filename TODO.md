# TODO

Keep this list short and current. Completed work should live in git history.

## Now (UI/UX + Cleanup)

- [ ] **Post-transfer validation**
  - After library transfer completes: run a full scan, verify shows/queue/review
    pages load real data, and confirm encode + review flows end-to-end.

- [ ] **Inspection cleanup pass**
  - Triage remaining PyCharm warnings that represent real runtime issues vs.
    static-analysis false positives (SQLModel typing, JS template entrypoints).
  - Keep generated assets excluded (Tailwind output), keep sources inspectable.

- [ ] **Reduce docstring/comment noise**
  - Remove redundant docstrings/comments that restate function names.
  - Prefer descriptive names and small functions over inline commentary.

## Soon (Pipeline & Data Hygiene)

- [ ] **Operational polish**
  - Decide whether to self-host third-party assets (fonts/HTMX) to reduce IDE
    noise and improve offline reliability.
