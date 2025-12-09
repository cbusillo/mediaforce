# Mediaforce Web UI - Feature Roadmap

- Replace print statements with structured logging (host, library, path, job id),
  configurable level.
- Documented max concurrency + off-peak settings; remaining work: finish
  print→log sweep and ORM for bulk endpoints.

## High Priority

- [x] **Bulk Actions on Review Page**
  - Select multiple files with checkboxes
  - "Promote All" / "Reject All" buttons
  - "Promote all files with >X% reduction" quick action

- [x] **Show/Series Management**
  - Dedicated page to manage show-level tier overrides
  - See all shows, their detected/override tiers, and encode stats
  - Bulk set tier for entire shows

- [x] **Multi-library Support**
  - Library selector (mac vs linux path aware)
  - Settings: add/edit libraries, watch toggle, max-height per library
  - Queue/scan/watch endpoints accept `library` param

- [x] **Review Page Playback Fixes**
  - Source/encoded playback both work with accurate position counter ✅
  - Keyboard controls (space, arrows, 1–5 speed) ✅
  - Smooth toggle between source/encoded without resetting position ✅

- [x] **Queue Performance & Clarity**
  - Server-side pagination/sorting + cached counts ✅
  - Faster movie view (no FS exists checks; current string parse OK) ✅
  - Compact “card” view for movies; codec/res shown ✅
  - Worker visibility: show connected workers; allow bump/send-to-worker ✅

- [x] **Scan/Watch UX**
  - Navbar/badge showing scan running + last scan per library
  - Buttons: Rescan library, Kick watcher, with status feedback
  - Workers panel on Dashboard: state/host/role, start/stop/pause

- [ ] **Profile Selection Quality Loop**
  - Motion-weighted 3-clip VMAF sampling (short + mid + motion chunk)
  - Min/max VMAF thresholds; never upscale; honor global + per-library max height
  - Record chosen profile + reasoning; UI button “flag bad choice” to feed retraining
  - Remote settings source only; workers fetch settings via API (no local JSON)

- [x] **Active Encoding Progress**
  - Real-time progress display (% complete, ETA) ✅
  - Current frame/total frames from ffmpeg output ✅
  - Live speed (fps) indicator ✅

- [x] **Search & Filtering**
  - Filter queue by show name, tier, size range
  - Search across all pages (queue, encoded, completed)
  - Sort options (by size, date, reduction %)

## Medium Priority

- [ ] **Architecture Extraction**
  - Move scanner/queue/encoder/watch into `services/` modules with a thin domain
    layer in `domain/`
  - Web/CLI call services instead of `core.py` directly; keep shared helpers typed

- [ ] **DB Access Layer**
  - Add repository helpers around SQLModel for common queries
  - Reduce ad-hoc SQL in web routes; keep raw SQL only for hotspots

- [ ] **Frontend Hygiene**
  - Extract shared JS (status chips, filters, fetch helpers) into a static bundle
  - Keep templates thin; consider HTMX/Alpine for small interactivity

- [ ] **Typing & Tests Discipline**
  - Run `ruff`/`mypy` on touched files in CI; keep FastAPI/SQLModel ignores scoped
  - Add minimal unit tests near newly extracted services

- [ ] **Worker/Queue Coordination**
  - Clarify worker lifecycle/state and queue handoff; reduce direct DB polling
  - Consider lightweight queue abstraction before scaling workers

- [ ] **Structured Logging & Settings**
  - Finish structured logging sweep; centralize log config in settings helpers
  - Trim legacy flags/config paths; ensure web/CLI share the same settings surface

- [ ] **Statistics Dashboard**
  - Total space saved over time (chart)
  - Encodes per day/week
  - Average reduction by tier
  - Encoding speed trends

- [ ] **Skipped Files Management**
  - View files marked as `skipped_native_av1` or other skip reasons
  - Option to force re-scan or reset status

- [ ] **Manual Queue Management**
  - Reorder queue (bump priority)
  - Pause/resume specific files
  - Add files manually to queue

- [ ] **Notifications**
  - Webhook support for encode completion
  - Email/Discord alerts for failures or size increases

## Lower Priority

- [ ] **Dark/Light Theme Toggle**
- [ ] **Mobile-responsive Design Improvements**
- [ ] **Export Reports** (CSV of completed encodes, space savings)

## Completed

- [x] **Status Renaming** - `completed` → `encoded`, `promoted` → `completed`
- [x] **Priority Scoring** - Verified oldest/biggest files encode first
- [x] **Pagination** - Page controls with per-page dropdown (25/50/100/200)
- [x] **Size Increase Filter** - Fixed to use actual reduction calculation

## Cleanup

- [ ] Remove legacy code paths (old SQL/table names, orphaned scripts, obsolete
      shims) once migrations are stable. No migration code should be left behind.
      We will be starting with a fresh database after this todo is done.
- [ ] Sweep codebase to remove non-essential comments/docstrings; rely on clear,
      descriptive identifiers instead
