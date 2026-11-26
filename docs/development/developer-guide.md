# Developer guide

This guide moved out of the README so the README can speak to people deciding
whether to try Mediaforce. [DIRECTION.md](../../DIRECTION.md) and
[AGENTS.md](../../AGENTS.md) come first; [the docs index](../README.md) lists
the architecture and design references.

## Layout

- `mediaforce/`: the Python package. The server is FastAPI and serves the API
  the Mac app uses, plus review media.
- `macos/`: the Mac app (SwiftUI). `scripts/build-mac-app.sh test` builds and
  tests it; it needs Xcode and `xcodegen`.
- `bin/mediaforce.py`: Python entry point. CLI entry points are `mediaforce`
  and `mediaforce-web`.
- `config/defaults.toml`: checked-in encode and policy defaults for source
  checkouts. Installed packages use
  `mediaforce/package_defaults/defaults.toml`; see
  [Package Builds](package-builds.md).
- `config/folder-defaults.toml`: shared per-folder starting points, with one
  sample block (`tv/Suits`). Shows normally follow the global defaults and the
  Director's approval; do not pin per-show or per-season tuning there.

Runtime state lives outside the repository:

- durable state and settings: `~/Library/Application Support/mediaforce/`
  (including `runtime-settings.json` and `learned-memory/`)
- disposable review clips: `~/Library/Caches/mediaforce/review/`
- service logs: `~/Library/Logs/mediaforce/`

Schema changes use SQLAlchemy 2.x and Alembic; opening a database applies
migrations. See [database tooling](database-tooling.md).

## Running from source

Prepare a checkout with `uv sync --locked`. Then use
`scripts/mediaforce-dev.sh start|stop|restart|status|smoke backend`. Commands
read the repo-local `.env` (template: `.env.example`; prefer the
`MEDIAFORCE_WEB_*` names). Startup precedence is CLI arguments, then the shell
environment, then `.env`, then built-in defaults. Start reuses an existing
owner; use Restart to apply a changed host, port or configuration. Cleanup
needs `ps` (install `procps` on minimal Linux systems).

The helper runs each server under a watchdog in its own session and process
group. Stop sends the group TERM, then KILL if needed, and the watchdog
releases its lock only after no group member remains. If Start reaches its
deadline, the server keeps starting; use Status or Stop. A failed watchdog
blocks new starts. Deleting a lock file or using old PID records does not
recover custody; if the watchdog itself was killed, end that component's
processes with the OS process manager before starting again. Sockets, locks
and logs live under
`~/Library/Application Support/mediaforce/development/<checkout hash>/`, or
`MEDIAFORCE_DEV_STATE_DIR`. The helper manages only servers it launched and
refuses an occupied port. The backend also holds a singleton lock, so a second
`mediaforce-web` exits instead of binding another port.

Enable the checked-in pre-commit hook once per clone:

```bash
git config core.hooksPath .githooks
```

It runs `scripts/pre-commit-check.sh`. Select focused local tests through
[the testing guide](../style/testing.md); the complete backend suite, Mac app
tests and macOS custody tests run in CI.

## The installed service

The maintainer's controller runs the server as the per-user LaunchAgent
`~/Library/LaunchAgents/com.mediaforce.web.plist`, which starts the server
inside the installed, notarized `Mediaforce.app`. The first install comes from
`scripts/deploy-controller.sh --release`, and later releases update themselves;
see [Releases](release-flow.md). `uv run mediaforce service
status|logs|enable|disable|restart` address that installed service from any
checkout; with no service installed, a development checkout cannot install
one. The service always runs with
`--no-reload`, has no `/Volumes` dependency and does not mount storage. See
[macOS login item](macos-login-item.md).

## Configuration

Settings resolve in this order:

1. global defaults
2. matching blocks in `config/folder-defaults.toml`, in declaration order
3. local folder overrides saved in `runtime-settings.json`
4. runtime environment overrides in `runtime-settings.json`

Machine-specific libraries, the working (transcode) folder and computers
belong in runtime settings through the Mac app's Settings, not in tracked
config. A library path may not contain or sit inside the working folder.
Changing an existing library's type needs a compatibility preview and never
moves media; see [typed library settings](../architecture/typed-library-settings.md).

Enter the Plex and TMDB tokens in Settings › Plex and TMDB. The server keeps
them in the login Keychain (service `Mediaforce`, accounts `plex` and `tmdb`;
a server on another state folder uses its own `Mediaforce <hash>` entries)
through Apple's `security` tool. Settings only learns whether a key is set and
accepted. `MEDIAFORCE_PLEX_TOKEN` and `MEDIAFORCE_TMDB_TOKEN` in the
environment or `.env` still work until a key is entered in Settings.

On macOS Mediaforce prefers Homebrew's `ffmpeg-full` from
`/opt/homebrew/opt/ffmpeg-full/bin`; override with `MEDIAFORCE_FFMPEG` or
`MEDIAFORCE_FFPROBE`. The sample sound check runs ViSQOL from a local Docker
image; build it once with `docker build -t mediaforce-visqol:38d0b01
docker/visqol` (see [sample sound check](../architecture/sample-sound-check.md)).

The checked-in video defaults are tuned to the maintainer's taste: about
300 MB per 45-minute episode at up to 1080p, with measured picture scores as
guardrails. `video.max_crf` is the starting search range;
size-directed tests may search up to `video.target_search_max_crf` while still
enforcing the picture floor.

## Space saved

Space saved draws the ring from the current folder sizes and published savings already in the main window.
A season’s original size is its current size plus the bytes already saved. Selecting a show keeps
its seasons in view while new files finish. Left to save labels each segment with its size. Pink shows waiting original backups. Gray shows how much space TV shows, movies and compressed VR & 3D files still use after estimated savings.
Review and delete in Needs you reveals the Original backups section. Deletion still requires confirmation.

## Mac page screenshots

Render live server data into PNGs, in light and dark, from the app
executable in a logged-in session. It draws into a window that is never shown
and needs no Screen Recording permission:

```bash
/Applications/Mediaforce.app/Contents/MacOS/Mediaforce render /path/to/new/empty/dir \
  --page needs-you --page 'sample:tv/Show/Season 1'
```

Without `--page` it renders the default set. Page names include `library`, `media`,
`tv`, `movies`, `other` (VR & 3D), `needs-you`, `activity`, `computers`,
`computer:ADDRESS`, `space-saved`, `kind:CODE`, `item:PREFIX`,
`sample:PATH`, `settings:libraries`, `settings:compression`,
`settings:computers`, `settings:metadata` and `menu`. `--server URL`,
`--width` and `--height` adjust the source and viewport. A page whose data
fails to load fails the command. Captures omit the sidebar and Settings tab
selector, and compare-player frames may be blank. The `mac-app` CI job runs
the same renderer and keeps its PNGs in the `mac-pages` artifact; use that
run to check a branch without touching the workstation's app or server.

## Command line

The Mac app is the normal way to run Mediaforce. The CLI is for inspection,
recovery and one-off runs; `uv run mediaforce --help` lists every command.

- `scan [--prefix tv/Show]`: refresh inventory with `ffprobe` metadata (and
  configured Plex and TMDB metadata on a full scan). Inventory also refreshes
  every `media.catalog_refresh_hours` (default 6; 0 turns it off). API reads
  never start scans, computer checks or media analysis.
- `evidence start|status|pause|resume|cancel|run|restore-source`: bounded
  motion-pattern and media-fingerprint batches. A batch prepared by hand
  starts paused; the background worker runs the checks a sample or
  production is waiting on. See [evidence worker](../architecture/evidence-worker.md).
- `evidence register-original ITEM_ID... --assertion TEXT`: prepare a list of
  independently acquired originals for confirmation in the Mac app's Needs
  you, which confirms with macOS LocalAuthentication. It never confirms an
  original itself. A format exception names its item, for example
  `--codec-override '123=Independently acquired AV1 download'`. Approval files
  live under the app's `mediaforce/mac-app/original-approvals`; back them up
  with the database. Agents do not write confirmations or delete originals.
- `report`, `content-kinds`, `quality-memory`, `target-defaults`,
  `target-production-evidence`, `staged-integrity SCOPE [--details]`:
  read-only reports.
- `backfill-approval-boundaries [--apply]`: record saved approvals missing
  from the learning tables; without `--apply` it only reports.
- `campaign`, `run`, `plan`, `review`, `encode`, `validate`, `compare`,
  `promote`, `approve`: the manual run path over a run manifest (written under
  `~/Library/Application Support/mediaforce/runs/`; later commands default to
  the latest). `promote` is an explicit override of automatic publishing.

## How the background work behaves

### Looks and samples

Approving a look's sample (live action or cartoons) approves that kind of
content across its shows; "Approve only this season" limits it to one
season. Mediaforce requests a first sample for each unapproved look with
waiting shows and puts it in Needs you once there is something to watch or
choose. Animation metadata puts a season or movie in line for a short AV1 test on an idle computer. The worker divides the existing picture measurements into ten-second windows and picks separate scenes for fast motion, fine detail, and sustained texture. It measures the bytes used by the sound and other streams the file will keep, then fits an AV1 sample to the remaining part of the 150 MB per 45 minutes budget. A sample outside the configured size tolerance cannot decide a look. The lowest of the three picture scores decides the test. A score within one point of the quality floor keeps that scene for a side-by-side choice. Native HDR needs a display-matched reference before it can be scored. Automatic assignment is on hold pending real-title validation. A saved choice still applies to its original. The Cartoons page includes finished titles and keeps the result and any choice until the tested source changes. A folder's own approval still takes precedence. VR & 3D has no look until
Mediaforce can compress it. A look's approval says whether 4K stays 4K or
comes down to the tallest picture in Settings. Seasons missing original
measurements wait for measuring first. When a sample re-encodes the audio,
the sound is scored too.

Needs you, the kind page and notifications offer to make a sample again when its clips are gone or the sample failed. A new sample uses the current size goal if the old sample recorded neither a size nor a requested budget. An ambiguous saved size still needs a choice.
Make sample shows “Preparing the sample…” while Mediaforce chooses settings and queues the job.
If preparation stops, the app shows the reason. If the server takes too long to answer,
the sample page can still show preparation. A second request for the same season reports that preparation is already underway.

### Checking, publishing and rollback

Each finished production file is checked and published on its own under its
current approval. Publishing moves the original into the Cleanup folder under
the working folder before the new file takes its place. Temporary holds retry
without limit; unknown failures retry with a delay before asking. Changed
approvals, failed checks and file conflicts stay with that file, and sample
outputs never publish. Files kept original after repeated failed checks or an
oversized copy say why and stop asking. Rollback copies stay until the user
approves cleanup.

A transient SSH failure, such as a remote reboot, retries with backoff; the
interrupted episode restarts from the beginning once its computer is back.
Starting a folder again never removes queued or running work; only ended
episodes are retried.

### Work windows

Each computer has a work window (`Always`, `Never`, or named hours on chosen
days, optionally with seasonal blocks), evaluated in that computer's own time
zone. Before starting a file, Mediaforce estimates whether it will finish
before the window closes, from the source's length and that computer's recent
runs, and may pick a shorter waiting file that fits. At close, encoding stops,
the partial output is discarded and the episode returns to the queue without
counting as a failure. A window can instead let the current file finish
(Settings › Computers › Work windows). `Bypass scheduler` overrides the
window for one job. A computer held out after repeated failures shows
`Cooling down`; Set up computer clears the hold.

### Computers and shared storage

Mediaforce runs encodes, samples and video quality measurements at low CPU priority on the
computer doing the work. Linux workers also use idle disk priority and need `ionice` from
util-linux. Macs use `nice -n 19` so other applications can take precedence.

A blank remote Mac needs Remote Login first; Set up computer then installs
this Mac's SSH key with the account password once and creates its work folders.
Readiness runs FFmpeg, ffprobe and ab-av1 and identifies the SVT-AV1, Opus and
libvmaf libraries FFmpeg loaded. The controller's installed Homebrew receipts
identify the sources for each release. An idle remote computer prepares changed
tools under `~/.local/share/mediaforce/tools`. A Mac uses Homebrew in a separate
prefix so its previous libraries remain usable. When only ab-av1 needs updating,
it uses the already-matching FFmpeg. Linux builds from the checked
source archives at lowest priority. Workers select a version through `current`;
a pending switch holds the worker out of service until picture, sound and quality
checks pass. An interrupted switch restores the previous selection.
Computers shows a previously reached `.local` address as possibly off or asleep
when its name disappears from the network.

The controller is not a computer by
default; add it as an SSH address such as `user@localhost` to give it work.
Computers reach media mounted, streamed, or copied to scratch space; see
[staged encode hosts](../architecture/staged-encode-hosts.md).

The controller and remote Macs reconnect missing SMB shares in the background
without dialogs, using passwords already in the login Keychain. Only Set up
computer opens Finder's connection; each computer keeps at most one Finder
request per share. Work waits until fresh checks verify the mount path, share
and access. If a connected share stops answering, Computers shows the storage wait.
A sample retries automatically, and Mediaforce checks each finished copy before publishing it. See
[controller storage recovery](macos-login-item.md#controller-storage-recovery).

### Library lifecycle

TV series protect their current season (`Auto`, `On` or `Off`), and every
season waits 30 days after its newest addition before automatic encoding.
Eligible media is ranked by Plex `addedAt`, oldest first. See
[library lifecycle policy](../architecture/library-lifecycle-policy.md).

### Cleanup of Mediaforce's own files

Cached review clips no approval references, temporary manifests and
calibration scratch outputs are removed after 14 days, retried at most hourly.
Run plans holding original measurements are kept. Only the newest two
`deployments/` folders and 30 days of `operations/` evidence are kept; change
this with `kept_deploy_backups` and `operation_evidence_retention_days` under
`[state.cleanup]`.
