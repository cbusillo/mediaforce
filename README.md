# Mediaforce

Mediaforce is the standalone v2 home for this media encoding workflow.

This project is the first-pass replacement for the old ad hoc AV1 helper
scripts. It is built for a semi-automated workflow:

- scan the configured source roots
- keep durable state in SQLite
- apply media-wide defaults with per-folder overrides
- generate run manifests from eligible media, oldest catalog additions first
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
- Protect active and newly acquired TV seasons before generating run manifests,
  then rank eligible media by catalog age rather than attempting a full,
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

Web and API reads only report persisted catalog and job state; opening or
polling a page does not start scans, host probes, or media analysis. Save a
library-path change in Settings or run `mediaforce scan` when inventory should
be reconciled. A full scan also refreshes configured Plex and TMDB metadata;
provider failures leave the last successful metadata in place and surface a
warning instead of blocking the catalog scan.

Scans update inventory with `ffprobe` metadata only. Cadence and media
fingerprint analysis is remembered as canonical evidence and refreshed through
an explicit paused batch; it is never launched by web startup or an idle scan.
For a small folder pilot:

```bash
uv run mediaforce evidence start "tv/Futurama/Season 1" --limit 10
uv run mediaforce evidence status
uv run mediaforce evidence resume
uv run mediaforce evidence run --max-items 1
```

Repeat the final command to resume durable progress. `evidence pause` prevents
the next claim, while `evidence cancel` terminates the active managed process
and cancels the remainder. See `docs/architecture/evidence-worker.md`.

The `/folders` index can browse either season folders or whole TV series
prefixes. Use the Scope control there when an operation should cover an entire
series instead of one season.

Folder calibration now uses a size-first review flow by default. The checked-in
defaults aim for roughly 300 MB per 45-minute episode at up to 1080p, then use
sampled metrics as guardrails and representative picture-and-sound clips as the
operator decision point. The comparison can open in a focused full-screen
workspace with side-by-side and instant Original/New views, shared playback,
and actual-size inspection. Technical encoding evidence remains under Details,
and approval stays on the calm folder page. The first size note is measured
before it becomes a ceiling; once a
follow-up target lands above the band, the next sample draft carries the learned
size ceiling forward instead of repeating the oversized run. The current fast
sample engine is still `ab-av1`; scene-aware engine work is tracked separately so
host orchestration and review workflow can stay stable while that bakeoff happens.
Once the current sampled draft has been explicitly saved to the folder profile,
`Queue Folder Encode` is unlocked so the real folder job can enter the encode
queue without letting a stale unsaved preview slip into production work.

For this personal workflow, source-resolution 1080p AV1 around 200–300 MB per
40 minutes is an established operator-approved baseline, including conventional
and dark or stylized TV material. Direct operator instructions and accepted
visual samples outrank generic bitrate guidance; real sample evidence decides
whether a particular folder needs adjustment.

`video.max_crf` is the initial quality-search range, not a hidden veto on an
approved size goal. Size-directed tests may expand in measured steps up to
`video.target_search_max_crf` (63 by default) while still enforcing the metric
floor and requiring operator review. Saved jobs created before that ceiling was
recorded replay their original CRF range exactly; make a fresh test to use the
new search contract.

For scene-aware engine research, generate a repeatable bakeoff plan from an
existing manifest instead of replacing the production engine path directly:

```bash
uv run mediaforce bakeoff path/to/run-manifest.json --all \
  --output ~/Desktop/mediaforce-bakeoff.json
```

The bakeoff plan carries the same size-first defaults and per-item resolved
policy used by Folder Studio, then lays out candidate commands and tool
requirements for the current `ab-av1` path plus Av1an, Xav, and Auto-Boost. Use
the plan to collect output size, runtime, selected CRF or quantizer, metric
score, and review artifacts before choosing a production engine migration.

You can run Mediaforce either directly with `python3` or through `uv`:

```bash
uv run mediaforce report --limit 10
```

Inspect read-only quality-memory readiness, active observations, concurrent
holdouts, and safety evidence without starting media work:

```bash
uv run mediaforce quality-memory
uv run mediaforce quality-memory --prefix "tv/Show/Season 1" --json
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

Inspect staged-output integrity for one explicit scope without changing media,
runtime state, or database rows. Add `--details` to perform bounded discovery
of untracked and temporary files under configured staging roots:

```bash
uv run mediaforce staged-integrity "tv/Show/Season 1" --details
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

TV promotion is fail-closed at the selected season or show scope: every
in-scope episode must already be promoted or have a locally available,
unchanged, validated staged output made with one coherent approved policy.
Active or attention-needed encode jobs, integrity findings, policy drift, and
destination conflicts block replacement of the entire selected TV scope.
Movie and exact-file scopes remain item-granular.

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

Codec and quality recommendations still rank and label rather than silently
excluding media. TV lifecycle policy is a separate eligibility gate: protected
seasons remain visible with their hold reason, do not enter automatic manifests,
and can only be bypassed through an explicit season-level confirmation. The
underlying lifecycle status and the existing runnable queue order do not change.

The checked-in video defaults are intentionally operator-taste defaults, not a
near-transparent archival preset. The baseline AV1 policy uses a size-first
review model: 300 MB per 45-minute episode, VMAF 85 with an 80 floor as a
guardrail, and max 1080p output unless the operator explicitly asks for another
resolution. Raise the metric floors or add a folder override when a class needs
a more conservative pass; use an explicit scale request when downsampling is
desired.

`report`, `encode`, and `validate` all surface source-vs-staged size deltas so
you can see the storage win before promotion.

## Folder defaults

Keep campaign tuning in [config/folder-defaults.toml](config/folder-defaults.toml).
That file is where per-show or per-season starting policies should live.

Bench-approved drafts are saved locally in runtime settings so future runs on
that machine can reuse them without mutating the tracked repo defaults. If a
bench-learned policy should become a shared starting point for everyone, copy it
into `config/folder-defaults.toml` intentionally.

Use the web Settings page for ordered typed library roots, the transcode folder,
remote host definitions, the Plex server URL, and Plex-to-Mediaforce path
mappings so those environment details stay off the checked-in repo. Library
labels are editable while root IDs remain stable. TV can run in Production;
Movies, 3D/VR, and Other can be configured as Browse only until their dedicated
workflow and safety plans land. Changing an existing type requires an explicit
compatibility preview and never moves media files. See
`docs/architecture/typed-library-settings.md` for the durable config contract.

## Library lifecycle policy

TV series use a per-series current-season mode:

- `Auto` protects the highest positive numbered season while cached TMDB status
  says the series is active. Missing or stale status is treated conservatively.
- `On` protects the highest numbered season regardless of provider status.
- `Off` disables current-season protection for that series.

Specials and Season 0 never identify the current season. A protected current
season releases when a higher numbered season appears or after 365 days without
a newly added or replaced episode. Independently, every season waits 30 days
after its newest addition or replacement before automatic encoding. A manual
override applies only to the exact season the operator confirms. An explicitly
selected episode may use that same parent-season override while its manifest
remains bounded to that one file. The resulting manifest records both the hold
reasons and the override.

Eligible media is ranked by Plex `addedAt`, oldest first. When Plex age is not
available, Mediaforce records and uses its own discovery timestamp, then the
filesystem modification time as the final fallback. Selection provenance is
written into the run manifest so retries and recovery do not silently recompute
membership under newer policy or metadata.

Plex and TMDB credentials are never written through the Settings UI. Set them in
the launch environment instead:

- `MEDIAFORCE_PLEX_TOKEN`: Plex server token
- `MEDIAFORCE_TMDB_TOKEN`: TMDB API read-access token

The configured SSH account and media paths are unrelated to these credentials.
Plex path mappings translate the paths reported by Plex to the corresponding
Mediaforce source roots using exact root boundaries. See
`docs/architecture/library-lifecycle-policy.md` for the durable data and
selection contract.

`mediaforce-web` reads optional startup defaults from the repo-local `.env`.
Use that file for machine-specific web launcher settings like bind address,
port, and reload mode. A checked-in template lives at `.env.example`. Startup
precedence is explicit CLI arguments, then shell environment variables, then
`.env`, then built-in defaults. Prefer the `MEDIAFORCE_WEB_*` variable names
for local defaults.

The macOS launch item is a long-running operator service, not a development
reloader. Give it an explicit `--no-reload` argument even when `.env` enables
reload for an active development session; otherwise Uvicorn `StatReload`
continuously walks the checkout and consumes CPU while the app appears idle.

Manage the local macOS login item through the Mediaforce CLI:

```bash
uv run mediaforce service install
uv run mediaforce service enable
uv run mediaforce service status
uv run mediaforce service logs --stderr
uv run mediaforce service disable
```

The generated `~/Library/LaunchAgents/com.mediaforce.web.plist` invokes the
repo-local `.venv/bin/mediaforce-web` entrypoint directly, not `uv`, and always
passes `--no-reload`. It has no `/Volumes` dependency and does not mount storage;
the running app remains the only owner of SMB recovery. Durable stdout and
stderr logs live under `~/Library/Logs/mediaforce/`. Use `restart` to regenerate
and reload the item after moving the checkout or recreating `.venv`, and use
`uninstall` to disable it and remove the plist. See
`docs/development/macos-login-item.md` for verification and raw launchctl
fallbacks.

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
local session. Busy startup reports the active owner PID and bind address when
that metadata is available. `scripts/mediaforce-web-dev.sh` remains as a
compatibility alias for backend-only actions.

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
actually running the work. Host probes retain an IANA timezone when the
operating system exposes one, use fixed UTC offsets only as a compatibility
fallback, and publish exact UTC close and next-open transitions for runtime
enforcement and operator surfaces.

Bounded host schedules are hard execution windows. A non-bypassed quality
search or episode encode receives the selected host's absolute UTC close
deadline; controller-side cancellation and a host-side watchdog stop encode
CPU at that boundary even if SSH or the web process disappears. Mediaforce
discards that episode's partial output and returns it directly to the queue
without consuming a failure attempt or applying host cooldown. Completed
episodes stay complete, and the interrupted episode restarts from the beginning
when a compatible window opens. Mediaforce does not create resumable media or
phase checkpoints. `Bypass scheduler` intentionally omits the deadline.

Before starting non-bypassed work on a bounded host, Mediaforce now estimates
the episode's quality-search and full-encode time from its source duration and
recent successful runs on that host. Sparse history receives a larger safety
margin. If the oldest queued episode cannot safely finish before any compatible
host closes, the queue may choose the fitting episode that leaves the least
unused window time; normal FIFO order resumes whenever the oldest episode fits.
When no episode fits, the host drains without consuming attempts, and an episode
that exceeds every compatible configured window receives an actionable waiting
reason. The hard close deadline remains the correctness backstop when an
estimate is wrong, and `Bypass scheduler` skips duration admission entirely.

Activity and Folder Studio present those schedule outcomes directly. Worker rows
show exact host-local open/close transitions, active episodes show their hard
stop time, and draining is distinct from off-schedule or unavailable. An episode
stopped at close is labeled `Paused by schedule` with its automatic whole-episode
restart expectation, while an explicit bypass is labeled `Bypassing schedule`
and a job that cannot fit any configured window links to the work-window
settings that need attention.

For a blank remote Mac, first turn on Remote Login so SSH answers. Once that is
reachable, the runtime settings UI can finish setup from the web surface: if
the host only needs first-time trust, enter the remote account password once so
Mediaforce can install this Mac's SSH public key, then let the prep step
create remote paths and install `ffmpeg-full` plus `ab-av1` for
`sample_calibration` hosts when possible. Those sample hosts now verify
`libvmaf`/`xpsnr` metric support and `libsvtav1` before they show as ready.
For mounted-media macOS hosts, including the controller itself, Mediaforce can
reconnect an SMB volume through Finder. Recovery runs before preparation,
sampling, or encode dispatch. The active signed-in console user must already
have the share password saved in the login Keychain; Mediaforce never reads,
stores, or transports it. While a required controller share is healthy,
Mediaforce learns a password-free mount mapping into
`~/Library/Application Support/mediaforce/controller-smb-mounts.json`. Status
reads use that machine-local mapping instead of probing or mutating mount state.
For first bootstrap, a private `controller_smb_mounts` list in runtime settings
may supply the same `source` and `/Volumes/...` `mount_point` fields until a
healthy mount can be observed and learned. Repeated automatic failures use a
bounded cooldown, and a missing GUI session remains suppressed until the console
login session changes. The operator can use Prepare for an explicit retry. If
Finder cannot use a saved credential, the action identifies the host and share
that need one manual Finder connection with the password saved to Keychain.

Sampled calibration and AI note tuning can now run on any configured host with
the `sample_calibration` capability. The folder page uses one AI-guided sample
note box instead of separate baseline/tuning actions, lets the operator choose
the sample host, and still keeps `Queue Folder Encode` hostless so the encode
queue can dispatch it automatically. For mounted-media remote sample hosts,
source and encoded review excerpts are rendered where the selected host can
read the media, then copied back as small browser-ready clips; the controller
does not need the full source mount for that review path. Runtime settings now
carry remote host
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
