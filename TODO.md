# Mediaforce Web UI - Feature Roadmap

- Replace print statements with structured logging (host, library, path, job id), configurable level.
- Documented max concurrency + off-peak settings; remaining work: finish print→log sweep and ORM for bulk endpoints.

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

- [ ] **Review Page Playback Fixes**
  - Source/encoded playback both work with accurate position counter
  - Keyboard controls (space, arrows, 1–5 speed)
  - Smooth toggle between source/encoded without resetting position

- [ ] **Queue Performance & Clarity**
  - Server-side pagination/sorting (ORDER BY priority DESC) and cached counts
  - Faster movie view (no FS exists checks; current string parse OK)
  - Optional compact “card” view for movies; inline codec/res already shown
  - Worker visibility: show connected workers + state, allow bump/send-to-worker

- [ ] **Scan/Watch UX**
  - Navbar/badge showing scan running + last scan per library
  - Buttons: Rescan library, Kick watcher, with status feedback
  - Workers panel on Dashboard: state/host/role, start/stop/pause

- [ ] **Profile Selection Quality Loop**
  - Motion-weighted 3-clip VMAF sampling (short + mid + motion chunk)
  - Min/max VMAF thresholds; never upscale; honor global + per-library max height
  - Record chosen profile + reasoning; UI button “flag bad choice” to feed retraining
  - Remote settings source only; workers fetch settings via API (no local JSON)

- [ ] **Active Encoding Progress**
  - Real-time progress display (% complete, ETA)
  - Current frame/total frames from ffmpeg output
  - Live speed (fps) indicator

- [ ] **Search & Filtering**
  - Filter queue by show name, tier, size range
  - Search across all pages (queue, encoded, completed)
  - Sort options (by size, date, reduction %)

## Medium Priority

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
- [x] **Pagination** - Page controls with items-per-page dropdown (25, 50, 100, 200)
- [x] **Size Increase Filter** - Fixed to use actual reduction calculation
