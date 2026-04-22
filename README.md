# Mediaforce

Mediaforce is the standalone v2 home for this media encoding workflow.

This project is the first-pass replacement for the old ad hoc AV1 helper
scripts. It is built for a semi-automated workflow:

- scan the configured source roots
- keep durable state in SQLite
- apply media-wide defaults with per-folder overrides
- generate run manifests in priority order
- stage outputs under the configured transcode root
- validate and promote after review

The current implementation focuses on discovery and planning. It does not yet
assume fully unattended execution. Encoding, machine validation, and promotion
are implemented, but promotion is still an explicit operator action after
review.

## Scope

- Source roots: taken from checked-in defaults plus runtime settings
- Staging root: taken from checked-in defaults plus runtime settings
- Ignored roots: `downloads`, `books`, and the contents of `transcode`

The current checked-in defaults point at `/Volumes/media/movies`,
`/Volumes/media/tv`, and `/Volumes/media/transcode`, but those are config
defaults rather than product-level invariants.

## Current approach

- Use the Mac Studio as the primary AV1 encode host.
- Keep durable library state in SQLite and manifest files outside the repo.
- Use human-edited policy manifests with per-folder overrides.
- Generate run manifests in priority order rather than attempting a full,
  unattended library rewrite.
- Review staged outputs before promotion.

## Status

The current implementation covers:

- discovery and inventory into SQLite
- run-manifest generation
- staged encode execution
- machine validation
- side-by-side compare clip generation for approval
- explicit promotion with original-file archival under the transcode root

## Runtime state

Runtime artifacts now live outside the repo by default:

- durable state: `~/Library/Application Support/mediaforce/`
- disposable review clips: `~/Library/Caches/mediaforce/review/`
- runtime settings: `~/Library/Application Support/mediaforce/runtime-settings.json`
- learned memory artifacts: `~/Library/Application Support/mediaforce/learned-memory/`

That keeps the repository focused on code and policy while allowing the local
catalog, manifests, scan jobs, and calibration artifacts to survive repo moves
or fresh clones.

Database schema changes are now managed through SQLAlchemy 2.x plus Alembic.
Opening the app against a database will auto-apply Alembic migrations, and
legacy pre-Alembic databases are normalized to the initial revision before
later revisions run. Encode artifacts also persist richer telemetry now: source
size and path at encode time, host and worker metadata, wall-clock encode
duration, and append-only item events for encode start, completion, and
failure.
subsequent Alembic upgrades run.

For migration authoring and review workflow, see
`docs/development/database-tooling.md`.

Transient calibration artifacts are also cleaned up automatically. By default,
Mediaforce purges cached review clips, temporary calibration manifests, and
`/Volumes/media/transcode/_calibration/` scratch outputs after `14` days.
While the web UI or CLI is in active use, it also retries that cleanup sweep at
most once per hour so stale files get another chance to disappear if an earlier
pass raced or only cleaned up partially.

Completed calibration jobs also clean up their own temporary manifest and
scratch encode directory right away after compare clips are generated, so only
the review clips and saved calibration summary remain.

## Layout

- CLI entry points: `mediaforce`, `mediaforce-web`
- `bin/mediaforce.py`: Python entry point
- `config/defaults.toml`: checked-in encode defaults and policy defaults
- `mediaforce/`: internal Python package
- runtime state: stored under `~/Library/Application Support/mediaforce/`
  and `~/Library/Caches/mediaforce/review/`

## Commands

On macOS, Mediaforce now prefers Homebrew's `ffmpeg-full` and `ffprobe` from
`/opt/homebrew/opt/ffmpeg-full/bin` when present so VMAF support survives PATH
changes and normal formula upgrades. You can override either binary with
`MEDIAFORCE_FFMPEG` or `MEDIAFORCE_FFPROBE`.

The web UI also auto-starts background catalog refreshes when the full library
view is empty or stale, and it auto-refreshes the current folder before
showing calibration actions when that folder's scan data is stale.

Folder calibration now uses a simple operator flow by default: sampled
calibrations use `ab-av1` file-wide samples for fast full-size estimates plus
short hotspot preview clips for visual review. Once the current sampled draft
has been explicitly saved to the folder profile, `Queue Folder Encode` is
unlocked so the real folder job can enter the encode queue without letting a
stale unsaved preview slip into production work.

You can run Mediaforce either directly with `python3` or through `uv`:

```bash
uv run mediaforce report --limit 10
```

Run a sample scan:

```bash
uv run mediaforce scan --limit 25
```

Scan a specific show or folder:

```bash
uv run mediaforce scan \
  --prefix "tv/Futurama"
```

Inspect a folder and print a suggested override block:

```bash
uv run mediaforce inspect-folder "tv/Suits"
```

Start a folder campaign in one command:

```bash
uv run mediaforce campaign \
  "tv/Suits/Season 5"
```

For the simplest operator flow, start a run instead:

```bash
uv run mediaforce run \
  "tv/Suits/Season 5" \
  --play
```

`campaign` will:

- rescan that folder prefix
- print the folder summary and suggested override block
- write a run manifest for the matching items in that folder
- print the first item plan in plain English

`run` will do the same setup work and then immediately:

- encode item 0
- validate item 0
- render compare clips for harder/high-complexity parts of the source
- optionally play the first compare clip
- print the next approval step

After a campaign, the rest of the commands default to the latest manifest, so
you do not need to paste the run path each time.

Review the first item from the latest run:

```bash
uv run mediaforce review --play
```

Approve the reviewed item from the latest run:

```bash
uv run mediaforce approve
```

Report the best current candidates:

```bash
uv run mediaforce report --limit 15
```

Generate a reviewable run manifest:

```bash
uv run mediaforce plan \
  --prefix "movies" \
  --limit 10
```

Run manifests are written under
`~/Library/Application Support/mediaforce/runs/` by default and contain:

- source file path
- resolved policy for that file
- recommendation bucket and score
- staging output path under `/Volumes/media/transcode`
- audio/subtitle summaries for review

Encode one or more items from a run manifest:

```bash
uv run mediaforce encode \
  --index 0
```

Encode every item from the latest manifest:

```bash
uv run mediaforce encode --all
```

Run machine validation against staged outputs:

```bash
uv run mediaforce validate \
  --index 0
```

Validate every staged item from the latest manifest:

```bash
uv run mediaforce validate --all
```

Promote a validated encode into the library:

```bash
uv run mediaforce promote \
  --index 0
```

Promote everything from the latest manifest after approval:

```bash
uv run mediaforce promote --all
```

Generate side-by-side approval clips from the source and staged outputs:

```bash
uv run mediaforce compare \
  --index 0
```

Generate review clips for all items from the latest manifest:

```bash
uv run mediaforce compare --all --play
```

Without explicit timestamps, `compare` now tries to pick scene-change moments
from the source automatically and falls back to evenly spaced review points if
scene analysis does not yield useful candidates.

By default `compare` renders three evenly spaced visual review clips. You can
override that with explicit timestamps, for example:

```bash
uv run mediaforce compare \
  ~/Library/Application\ Support/mediaforce/runs/run-abc123.json \
  --index 0 \
  --timestamp 120 \
  --timestamp 640 \
  --timestamp 1100 \
  --play
```

## Policy model

`config/defaults.toml` is the source of truth for checked-in encode defaults.
Machine-specific libraries, transcode roots, and remote hosts should live in
runtime settings instead of repo-tracked config. Mediaforce resolves settings
in this order:

1. Global defaults
2. Matching per-folder overrides from `config/folder-defaults.toml` in
   declaration order
3. Matching operator-local folder overrides saved into
   `~/Library/Application Support/mediaforce/runtime-settings.json`
4. Runtime environment overrides from
   `~/Library/Application Support/mediaforce/runtime-settings.json`

This first version intentionally does **not** silently skip codecs or folders.
It ranks and labels items, but leaves the final decision in the manifest and
review loop.

`report`, `encode`, and `validate` all surface source-vs-staged size deltas so
you can see the storage win before promotion.

## Folder defaults

Keep campaign tuning in [config/folder-defaults.toml](config/folder-defaults.toml).
That file is where per-show or per-season starting policies should live.

Bench-approved drafts are saved locally in runtime settings so future runs on
that machine can reuse them without mutating the tracked repo defaults. If a
bench-learned policy should become a shared starting point for everyone, copy it
into `config/folder-defaults.toml` intentionally.

Use the web Settings page for local libraries, the transcode folder, and remote
host definitions so those environment details stay off the checked-in repo.

`mediaforce-web` reads optional startup defaults from the repo-local `.env`.
Use that file for machine-specific web launcher settings like bind address,
port, and reload mode. A checked-in template lives at `.env.example`. Startup
precedence is explicit CLI arguments, then shell environment variables, then
`.env`, then built-in defaults. Prefer the `MEDIAFORCE_WEB_*` variable names
for local defaults.

The frontend dev server now reads the same repo-local `.env` file. The clearest
local setup is:

- `MEDIAFORCE_WEB_PORT=8777` for the FastAPI app
- `MEDIAFORCE_FRONTEND_DEV_PORT=4173` for `scripts/mediaforce-dev.sh start`
- `MEDIAFORCE_FRONTEND_API_ORIGIN=http://127.0.0.1:8777` so the frontend dev
  server proxies API requests to the backend explicitly

That means the two useful local URLs are:

- `http://127.0.0.1:4173` while actively editing the frontend in dev mode
- `http://127.0.0.1:8777` when checking the backend-served built app

The web UI is now split cleanly:

- FastAPI serves the backend API and review media.
- A SvelteKit frontend lives under `frontend/`.

For local web work, use `scripts/mediaforce-dev.sh` with
`start|stop|restart|status|smoke`. It manages the backend and frontend together,
uses the repo-local `.env`, writes pid files and logs under
`~/Library/Application Support/mediaforce/`, starts Vite with `--strictPort`,
and keeps the command lines aligned with the actual configured ports. Pass
`backend` or `frontend` as a second argument when you intentionally want only
one side, for example `scripts/mediaforce-dev.sh restart backend`.

The backend also holds a Python-level singleton lock while running, so a second
`mediaforce-web` process exits instead of binding another port and confusing the
local session. `scripts/mediaforce-web-dev.sh` remains as a compatibility alias
for backend-only actions.

To enforce the local acceptance gate before each commit, point Git at the
checked-in hooks once per clone:

```bash
git config core.hooksPath .githooks
```

That pre-commit hook runs `scripts/pre-commit-check.sh`, which executes the
full backend pytest suite, CLI smoke, frontend type checks, frontend lint,
frontend unit tests, and frontend build.

For frontend development, let `scripts/mediaforce-dev.sh start` run the Svelte
app. The Vite dev server proxies `/api/*` and `/review-media/*` back to the
FastAPI backend. For the single-server local UI, build the frontend with
`npm run build`; FastAPI will then serve the built SPA from `frontend/build/`.

When packaging Mediaforce with `uv build`, the wheel build now runs
`npm ci` plus `npm run build` automatically so the packaged app always embeds a
fresh frontend bundle from source.

Host configuration is now unified too: Mediaforce no longer injects a special
synthetic local host. If you want the current machine to participate in sample
or encode-host decisions, add it as a normal SSH host entry such as
`cbusillo@localhost`, then set its priority and capabilities in Settings like
any other host.

Each host can now declare its own `max_parallel_encodes` limit and pick a
structured schedule instead of typing profile keys by hand. `Always` is the
built-in default, and you can add named windows when a machine should only run
during certain hours, on specific days of the week, or all day on explicit
exception days such as Sunday. `Never` is also built in for temporarily
disabling queued encodes on a host without removing its capabilities or setup
state. Those windows are evaluated in the local time of the host that is
actually running the work.

For a blank remote Mac, first turn on Remote Login so SSH answers. Once that is
reachable, the runtime settings UI can finish setup from the web surface: if
the host only needs first-time trust, enter the remote account password once so
Mediaforce can install this Mac's SSH public key, then let the prep step
create remote paths and install `ffmpeg-full` plus `ab-av1` for
`sample_calibration` hosts when possible. Those sample hosts now verify
`libvmaf`/`xpsnr` metric support and `libsvtav1` before they show as ready.
Shared media mounts still need to exist on the remote host before it will show
as ready.

Sampled calibration and AI note tuning can now run on any configured host with
the `sample_calibration` capability. The folder page uses one AI-guided sample
note box instead of separate baseline/tuning actions, lets the operator choose
the sample host, and still keeps `Queue Folder Encode` hostless so the encode
queue can dispatch it automatically. Runtime settings now carry remote host
priority, per-host queue capabilities, explicit schedule selections, and a
per-job `Bypass scheduler` escape hatch for urgent runs.

Each note-driven tuning attempt is now recorded in SQLite, and successful
cross-folder learnings are promoted to markdown artifacts under the learned
memory directory so future tuning requests can retrieve concise prior guidance.

The current starter profile includes `tv/Suits`, because it is a high-value AV1
target: large 1080p H.264 episodes with DTS 5.1 audio and low grain.

Promotion moves the original source into `/Volumes/media/transcode/_replaced`
before replacing it with the staged `.mkv`, which keeps rollback straightforward
without leaving the active library in an ambiguous state.
