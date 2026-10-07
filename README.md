# Mediaforce

Mediaforce reclaims space on a media library by re-encoding it to AV1 video
and Opus audio with minimal, acceptable quality loss.

The Director's [overall direction](https://github.com/cbusillo/direction/blob/main/DIRECTION.md)
comes first, followed by this repository's [DIRECTION.md](DIRECTION.md).
Those files set purpose, work order, and stop boundaries and take precedence
over this README. [AGENTS.md](AGENTS.md) is the repository's agent-instruction
entry point; this README describes the current implementation and how to use it.

## Scope

- Source roots: taken from checked-in defaults plus runtime settings
- Staging root: taken from checked-in defaults plus runtime settings
- Only configured library roots are scanned; a library path may not contain or
  sit inside the working (transcode) folder

The current checked-in defaults point at `/Volumes/media/movies`,
`/Volumes/media/tv`, and `/Volumes/media/transcode`, but those are config
defaults rather than product-level invariants.

## Status

The current implementation covers:

- discovery and inventory into SQLite, refreshed in the background
- the web app: TV, Movies, and Other libraries, Activity, Finished, and
  Settings
- per-file sample, encode, and machine validation across configured encode
  hosts, with work-window schedules
- measured evidence (motion pattern, media fingerprint) checked automatically
  for files production is waiting on
- side-by-side compare clips for sample review
- automatic per-file checking and publishing under a current approval, with
  original-file archival under the transcode root,
  and user-approved cleanup of those rollback copies

A sample is still approved per show. Finished production files are checked and
published automatically one at a time under their current approval. Manual
checking and publishing remain explicit overrides. Rollback copies stay until
the user approves cleanup.

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
polling a page does not start scans, host probes, or media analysis. Catalog
inventory refreshes in the background every `media.catalog_refresh_hours`
(default 6; 0 turns it off). Save a library-path change in Settings or run
`mediaforce scan` to reconcile sooner. A full scan also refreshes configured Plex and TMDB metadata;
provider failures leave the last successful metadata in place and surface a
warning instead of blocking the catalog scan.

Scans update inventory with `ffprobe` metadata only. Cadence and media
fingerprint analysis is remembered as canonical evidence and refreshed through
bounded evidence batches. A background worker runs unpaused batches, such as
the checks production is waiting on; a batch prepared by hand starts paused,
and a catalog scan never starts analysis. For a small folder pilot:

```bash
uv run mediaforce evidence start "tv/Futurama/Season 1" --limit 10
uv run mediaforce evidence status
uv run mediaforce evidence resume
uv run mediaforce evidence run --max-items 1
```

Repeat the final command to resume durable progress. `evidence pause` prevents
the next claim, while `evidence cancel` terminates the active managed process
and cancels the remainder. See `docs/architecture/evidence-worker.md`.

The TV library (`/` or `/folders`) lists shows. Open a show for show-wide
actions, or a season for season actions.

Folder calibration now uses a size-first review flow by default. The checked-in
defaults aim for roughly 300 MB per 45-minute episode at up to 1080p, then use
sampled metrics as guardrails and representative picture-and-sound clips as the
user decision point. The comparison can open in a focused full-screen
workspace with side-by-side and instant Original/New views, shared playback,
and actual-size inspection. Technical encoding evidence remains under Details,
and approval stays on the calm folder page. The first size note is measured
before it becomes a ceiling; once a
follow-up target lands above the band, the next sample draft carries the learned
size ceiling forward instead of repeating the oversized run. The current fast
sample engine is still `ab-av1`; scene-aware engine work is tracked separately so
host orchestration and review workflow can stay stable while that bakeoff happens.
Once a sample is approved, the show or season page offers the compress action
(for example `Compress the season`), which queues the real encode from that
approval rather than from an unsaved preview.

For this personal workflow, source-resolution 1080p AV1 around 200–300 MB per
45 minutes is an established user-approved baseline, including conventional
and dark or stylized TV material. Direct user instructions and accepted
visual samples outrank generic bitrate guidance; real sample evidence decides
whether a particular folder needs adjustment.

`video.max_crf` is the initial quality-search range, not a hidden veto on an
approved size goal. Size-directed tests may expand in measured steps up to
`video.target_search_max_crf` (63 by default) while still enforcing the metric
floor and requiring user review. Saved jobs created before that ceiling was
recorded replay their original CRF range exactly; make a fresh test to use the
new search contract.

For scene-aware engine research, generate a repeatable bakeoff plan from an
existing manifest instead of replacing the production engine path directly:

```bash
uv run mediaforce bakeoff path/to/run-manifest.json --all \
  --output ~/Desktop/mediaforce-bakeoff.json
```

The bakeoff plan carries the same size-first defaults and per-item resolved
policy used by the TV season page, then lays out candidate commands and tool
requirements for the current `ab-av1` path plus Av1an, Xav, and Auto-Boost. Use
the plan to collect output size, runtime, selected CRF or quantizer, metric
score, and review artifacts before choosing a production engine migration.

Run Mediaforce through the project entrypoint with `uv`:

```bash
uv run mediaforce report --limit 10
```

Inspect read-only quality-memory readiness, active observations, concurrent
holdouts, and safety evidence without starting media work:

```bash
uv run mediaforce quality-memory
uv run mediaforce quality-memory --prefix "tv/Show/Season 1" --json
```

Inspect a target-size default proposal using a current visual boundary observation
ID from the configured database:

```bash
uv run mediaforce target-defaults cibo1_example
```

This read-only JSON report explains item, folder, and measured-content scope,
evidence counts, confidence, and fallback. It does not apply a target or start
media work. Exact-item Studio pages also expose this evidence when the saved
review still matches the current source, calibration, intent and budget.
Suggestions remain review-only; sample approvals do not establish promoted
production evidence. See [target-default rules](docs/architecture/content-intent-boundary-evidence.md#target-default-proposals)
for the evidence thresholds and required user confirmation.

Inspect the separately captured production lineage for an approved boundary:

```bash
uv run mediaforce target-production-evidence cibo1_example
```

This read-only report links the exact reviewed item to its validated, promoted
output and explains current evidence eligibility. It never adopts a default or
backfills historical promotions. Output identity uses sampled content bytes,
size, and metadata, not a full-file digest. See the
[production lineage contract](docs/architecture/content-intent-boundary-evidence.md#production-outcome-lineage).

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

The web app is the normal way to run Mediaforce. For a manual one-off or
debugging run from the CLI, start a run instead:

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

The web app checks finished production files and publishes each passing file
automatically, retaining the original in the Cleanup folder. Temporary holds
retry; unknown failures retry with a delay before pausing for the manual check or publish override.
Changed approvals, failed checks, and file conflicts stay with that file.
Reasons appear in its staged-integrity details. Sample outputs never
publish automatically. Older outputs without an unambiguous recorded production origin use
the manual override, so sample files cannot be mistaken for full replacements.

Manually publish a checked file as an explicit override:

```bash
uv run mediaforce promote \
  --index 0
```

Manually try every checked file from the latest manifest:

```bash
uv run mediaforce promote --all
```

Promotion is decided file by file, for TV seasons and shows as for movies and
exact files. An episode publishes when its staged output is locally available,
unchanged, validated, made under an approved policy (the current approval or
the approval its run recorded), not being written by an active encode, and not
in conflict with an existing library file. Every other episode waits and is
reported with its reason; it never holds back the rest. Promotion stops for the
whole scope only when Mediaforce cannot judge files safely: an incomplete
integrity report, an unavailable policy check, or an active encode whose files
cannot be read.

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

For episodes short enough to scan, it tries high-complexity moments before
scene changes. You can override the choice with explicit timestamps, for
example:

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

Source checkouts use `config/defaults.toml` for runtime encode defaults.
Installed packages use the separate install-safe resource
`mediaforce/package_defaults/defaults.toml`; see [Package Builds](docs/development/package-builds.md).
Machine-specific libraries, transcode roots, and remote hosts should live in
runtime settings instead of repo-tracked config. Mediaforce resolves settings
in this order:

1. Global defaults
2. Matching per-folder overrides from `config/folder-defaults.toml` in
   declaration order
3. Matching local folder overrides saved into
   `~/Library/Application Support/mediaforce/runtime-settings.json`
4. Runtime environment overrides from
   `~/Library/Application Support/mediaforce/runtime-settings.json`

Codec and quality recommendations still rank and label rather than silently
excluding media. TV lifecycle policy is a separate eligibility gate: protected
seasons remain visible with their hold reason, do not enter automatic manifests,
and can only be bypassed through an explicit season-level confirmation. The
underlying lifecycle status and the existing runnable queue order do not change.

The checked-in video defaults are intentionally tuned to the user's taste, not a
near-transparent archival preset. The baseline AV1 policy uses a size-first
review model: 300 MB per 45-minute episode, VMAF 85 with an 80 floor as a
guardrail, and max 1080p output unless the user explicitly asks for another
resolution. Raise the metric floors or add a folder override when a class needs
a more conservative pass; use an explicit scale request when downsampling is
desired.

`report`, `encode`, and `validate` all surface source-vs-staged size deltas so
you can see the storage win before promotion.

## Folder defaults

[config/folder-defaults.toml](config/folder-defaults.toml) holds shared
per-folder starting points and ships with one sample block (`tv/Suits`). Shows
normally follow the global defaults and the Director's approval; do not pin
per-show or per-season tuning there.

Bench-approved drafts are saved locally in runtime settings so future runs on
that machine can reuse them without changing the tracked repo defaults.

Use the web Settings page for ordered typed library roots, the transcode folder,
remote host definitions, the Plex server URL, and Plex-to-Mediaforce path
mappings so those environment details stay off the checked-in repo. Library
labels are editable while root IDs remain stable. TV, Movies, and Other can
run in Production; 3D/VR stays Browse only until its workflow and safety plans
land. Changing an existing type requires an explicit
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
override applies only to the exact season the user confirms. An explicitly
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

The macOS launch item is a long-running user service, not a development
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

## Local web development

The web UI is now split cleanly:

- FastAPI serves the backend API and review media.
- A SvelteKit frontend lives under `frontend/`.

The frontend dev server now reads the same repo-local `.env` file. The clearest
local setup is:

- `MEDIAFORCE_WEB_PORT=8777` for the FastAPI app
- `MEDIAFORCE_FRONTEND_DEV_PORT=4173` for Vite frontend development
- `MEDIAFORCE_FRONTEND_API_ORIGIN=http://127.0.0.1:8777` so the frontend dev
  server proxies API requests to the backend explicitly

That means the two useful local URLs are:

- `http://127.0.0.1:4173` while actively editing the frontend in dev mode
- `http://127.0.0.1:8777` when checking the backend-served built app

For macOS local web work, use `scripts/mediaforce-dev.sh` with
`start|stop|restart|status|smoke`. It manages the backend and frontend together,
uses the repo-local `.env`, writes pid files and logs under
`~/Library/Application Support/mediaforce/`, starts Vite with `--strictPort`,
and keeps the command lines aligned with the actual configured ports. Pass
`backend` or `frontend` as a second argument when you intentionally want only
one side, for example `scripts/mediaforce-dev.sh restart backend`.

Backend ownership requires this checkout's executable as the command itself
or as the script launched by Python. A wrapper merely mentioning that path
does not own the backend or its siblings. Listener discovery resolves and
deduplicates owned roots before stopping their trees. Stop captures the owned subtree's
native process identities before signalling, so workers remain eligible for
forced cleanup after their parent exits without targeting a reused PID.
If cleanup fails after capture, a small development cleanup supervisor keeps
the native identities alive. Stop reports the error and retains PID bookkeeping;
Start refuses to launch another process while cleanup is pending. Resolve the
reported error and retry the same Stop or Restart command: it contacts that
supervisor before looking for the original root, even after workers reparent.
The supervisor exits when cleanup is proved or its whole captured tree exits
with ownership still proven.
If completed cleanup cannot retire its pending state, the supervisor retains
that completion proof and retries; Stop remains unsuccessful until retirement
succeeds. Retirement renames the directory without allocating another one.
A retained supervisor retires only the marker it opened. If that marker
disappears or is replaced, it fails and leaves the replacement alone.
Interrupted disposal and unpublished setup artifacts are retried in bounded
sweeps under the publication lock. Unknown contents, foreign directories and
symlinks are preserved. Artifacts from earlier helper versions are not swept.
Boot receipts are synced before publication, followed by their directory and
the parent directory. Injected write/sync failures are qualified; physical
power-loss durability has not been tested. An absent or invalid boot receipt
never proves that pending custody belongs to a previous boot.
If startup and its retirement both fail, the surviving marker has no persisted
proof that custody was never acquired. Stop retains it until a new system boot
can be verified, even after the original filesystem problem is resolved.
Temporary errors while checking retained processes keep their native handles
and save the last error for the next Stop. A concurrent Stop for a different
root reports a conflict instead of consuming another tree's cleanup result.
If the supervisor itself is lost, Stop fails visibly rather than reconstructing
custody from saved PIDs. After the next system restart, Stop can clear its pending
state using the kernel's boot identity; do not delete that state to bypass cleanup.
Pending state also stays when [native containment cannot be proved](docs/architecture/module-boundaries.md),
including incomplete capture or a strict Darwin fork.
Development stop requires the checkout's prepared Python environment
(`uv sync --locked`) and loads only that checkout's standalone cleanup and native
custody modules, even when invoked from another directory or unrelated package
edits cannot import. Native custody failures remain visible rather than falling
back to bare PID signals.

On Linux, the launcher refuses Start and supplies the foreground commands below,
including replacements requested through Restart. For an
existing tree, stop can terminate the identities it captured but cannot prove that
an existing worker did not fork and exit between discovery passes, leaving an
unseen grandchild. It therefore reports `Linux existing-tree descendant custody
is unproven`, retains bookkeeping, and blocks restart even when the captured
processes exited. Repeating stop cannot establish the missing custody. For Linux
web development, keep the backend in its foreground terminal instead:

```bash
uv run mediaforce-web --no-reload
```

Run `npm --prefix frontend run dev` in a second terminal when editing the UI.
End those foreground commands from their terminals; do not use development
stop/restart to claim Linux descendant cleanup. A launcher Stop against the
foreground backend also leaves unproven cleanup state. Stop preserves that state
until a verified system restart; retries cannot recover missing custody. Stop All attempts both components and
reports failure; `stop backend` also attempts the backend independently.
Start foreground development only after the earlier processes are resolved.
This limitation does not apply to
Linux commands launched inside Mediaforce's scoped subprocess supervisor, which
establishes child custody before launch. Darwin retains its strict fork guard.

Development PID files live in a directory under that state path keyed by the
physical checkout: `development/<checkout hash>/`. Each checkout manages its
own records, so a reused PID cannot wedge start or discard another checkout's
bookkeeping. Legacy shared PID files are left in place. Different checkouts can
run frontends on different configured ports; port collisions still refuse start.

Frontend discovery checks the npm/Vite process or its ancestors against the
exact checkout working directory. The launcher must be the command or the
script launched by Node or Python; a shared wrapper merely mentioning npm or
Vite's path is preserved with its unrelated children. Native argument readers
on macOS and Linux keep interpreter and script paths separate, including spaces
and an interpreter alias removed after startup. Direct Node launches support
`--inspect`, `--inspect-brk`, and `--max-old-space-size` before the script.
Other Node options are not inferred; use the managed npm path with `NODE_OPTIONS`
for runtime options. If native arguments cannot be read, ownership is unknown:
stop preserves the process and its PID record and reports the error.
Python startup or reader failures also leave ownership unknown; repair the
reported environment error and retry the same command with `uv sync --locked`
completed for this checkout. A reader crash does not prove a process is foreign.
Managed npm starts in `frontend/` so its
rewritten process title remains attributable. For a legacy frontend launched
from the repository root, stop targets its owned Vite child; npm exits after
the child ends. Stop and restart preserve another checkout's process tree. Stale records in this checkout's development directory can be
replaced; the shared backend runtime lock is always preserved.

Backend actions temporarily unload a login item only when its working directory
and executable both match the physical checkout. Before continuing, the helper
waits for the item to unload and its backend processes to finish, checking up to
20 times at quarter-second intervals. The wait tracks the item's reported PID
and its backend root; a PID file or runtime lock alone does not make an independent
development backend part of that group. Failed unload or unfinished shutdown stops
the command with a clear error, without force-killing the service or starting a
replacement. Pending shutdown PIDs are kept outside the checkout in a record
keyed by its physical path; retrying keeps waiting instead of reusing a dying
service. The record is removed only after shutdown completes. A login item
that uses this binary with a different working directory is preserved and
reported for correction; regenerate it from the
intended service checkout with `uv run mediaforce service restart` before
retrying. Other checkouts' services and backend processes stay running.
The shared runtime lock is preserved.

Ordinary development backends can still be reused through their PID file,
runtime lock or listener. Fresh starts and PID-based reuse get the same bounded
polling interval to start listening. Success requires that process tree to
listen on the configured port, so retrying a timed-out start cannot report a
process that is still starting as healthy or spawn another copy. Inspect the
backend log and retry when it is listening; use `smoke backend` to check HTTP
readiness.
An old backend launched through a logical symlink path before physical-path
matching was introduced may remain unrecognized and is left running. Quit that
older backend in Activity Monitor before starting a replacement.

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
frontend unit tests, frontend build, and the managed web route smoke
(`npm --prefix frontend run smoke:web`).

See [Local web development](#local-web-development) for platform-specific
backend/frontend startup and [Production-style build](frontend/README.md#production-style-build)
for the backend-served frontend build.

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
enforcement and user surfaces.

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

Activity and the show and season pages present those schedule outcomes
directly. Worker rows
show exact host-local open/close transitions, active episodes show their hard
stop time, and draining is distinct from off-schedule or unavailable. An episode
stopped at close is labeled `Paused until the next work window` with its
automatic whole-episode restart expectation, while an explicit bypass is
labeled `Not limited by the work window` and a job that cannot fit any configured window links to the work-window
settings that need attention.

Transient SSH transport failures, including a remote host reboot or an OpenSSH
`closed by remote host` disconnect, enter bounded retry backoff instead of
requiring a new encode request. Mediaforce preserves unverified remote artifacts
while the host is unreachable, removes the interrupted partial output once the
host is available again, and then requeues the same episode from the beginning.
It does not resume a partial media stream or promote interrupted output.
A measured quality-floor conflict within the automatic allowance retries that
one file with an item-local size exception; other deterministic encode and
policy failures stop and ask about that file.

Starting a folder again while some of its episodes are still queued, retrying,
or encoding never removes that work. Only the episodes that ended are retried,
inside the same folder encode. An episode that missed its approved final size,
or any ended episode when the approved settings changed after the folder was
queued, waits and is planned again with the current settings once the rest of
the folder has finished. A folder is never planned twice while any overlapping
encode, such as a show-wide one, still has parts of it queued or running.

For a blank remote Mac, first turn on Remote Login so SSH answers. Once that is
reachable, the runtime settings UI can finish setup from the web surface: if
the host only needs first-time trust, enter the remote account password once so
Mediaforce can install this Mac's SSH public key, then let the prep step
create remote paths and install `ffmpeg-full` plus `ab-av1` for
`sample_calibration` hosts when possible. Those sample hosts now verify
`libvmaf`/`xpsnr` metric support and `libsvtav1` before they show as ready.
The controller's automatic background recovery reconnects required SMB storage
with a saved share mapping through the macOS NetFS API with `UIOption=NoUI`,
using existing Keychain credentials without reading,
storing, or transporting passwords. Its background recovery runs even when
processing is paused or the work window is closed; reconnecting storage does
not unpause work. Clean connection failures retry with bounded backoff;
missing mappings and failures that cannot be retried safely, including ambiguous
timeouts, wait for user attention.
Fresh checks must verify the expected mount path, share identity, and directory
access before work starts. See [controller storage recovery](docs/development/macos-login-item.md#controller-storage-recovery)
for the runtime contract and installed acceptance procedure.

While a required controller share is healthy,
Mediaforce learns a password-free mount mapping into
`~/Library/Application Support/mediaforce/controller-smb-mounts.json`. Status
reads use that machine-local mapping instead of probing or mutating mount state.
For first bootstrap, a private `controller_smb_mounts` list in runtime settings
may supply the same `source` and `/Volumes/...` `mount_point` fields until a
healthy mount can be observed and learned.

Remote mounted-media macOS hosts reconnect through Finder before preparation,
sampling, or encode dispatch. Explicit Prepare on the controller also uses
Finder. These Finder paths require a signed-in console user with the share
password saved in the login Keychain. Repeated automatic failures use a bounded
cooldown. After a missing desktop session, sign in on that computer and use
Prepare to retry; on the controller, attempts remain suppressed until its
console login session changes. Each computer keeps at most one Finder request
per share: a request Finder has not
answered within the attempt, usually because a dialog is open, is left running
rather than ended, since ending it would not close the dialog. Later attempts,
explicit or automatic, report that request instead of opening another dialog
until someone answers or cancels it on that Mac. A failed connection says what
Finder or the helper reported: a sign-in failure, an unreachable server, a
cancelled dialog, a missing desktop session, a share connected under a
suffixed name such as `/Volumes/media-1`, a helper that could not start, or a
timeout with no reason given. Only a sign-in failure asks for one manual Finder
connection with the password saved to Keychain.

Sampled calibration and AI note tuning can now run on any configured host with
the `sample_calibration` capability. The folder page uses one AI-guided sample
note box instead of separate baseline/tuning actions, lets the user choose
the sample host, and still keeps the compress action hostless so the encode
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
