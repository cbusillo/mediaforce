# Releases

Mediaforce ships as a notarized Mac app with its server inside. Agents publish
a release when the Director asks ([Notarized releases](#notarized-releases)),
and the installed app updates itself between files. The Director decided that
agents release Mediaforce themselves on 2026-10-13
([direction#82](https://github.com/cbusillo/direction/issues/82)) and that it
updates itself automatically on 2026-11-12
([#1314](https://github.com/cbusillo/mediaforce/issues/1314#issuecomment-6841277256)).
Launchplane does not manage Mediaforce.

## Installing a release on the controller

`scripts/deploy-controller.sh` installs one published release through a
staged transaction. It moves the controller from the clone-based runtime to
the app once, and repairs an installed app that cannot update itself.
Ordinary updates need nothing from it.

Run it from a clean checkout of the release's commit: the script refuses a
release whose version differs from `pyproject.toml`'s, whose feed and Sparkle
public key it also uses. The build comes from the feed's item for that
release:

```bash
scripts/deploy-controller.sh --release v<version> \
  --stage "$HOME/Library/Application Support/mediaforce/deployments/release-<version>-<date>"
```

`--release` also takes the archive's URL or a local copy with `SHA256SUMS`
beside it. The stage is a fresh folder in `deployments/` beside the configured
database. Before anything on the Mac changes, the script checks, in order:

1. the archive matches its `SHA256SUMS`;
2. the feed offers this archive, and it carries the feed's Sparkle signature
   for `public-key`;
3. the app is signed by the team, its notarization ticket is stapled and
   Gatekeeper accepts it as notarized;
4. the app is that version, with the feed's build and the committed feed and
   key.

Then, under the deploy lock (it waits up to ten minutes for a running cleanup,
which the pause request itself can start), it reads the saved launch item. Its
`--config` and `--credentials`, or the clone runtime's own
`config/defaults.toml` and `.env`, go into the new launch item and are read
where they are; nothing is copied. The running server must report the database
that config names, and the credential file must be private (`chmod 600`). It
refuses an unfinished app update, and a release older than the build that last
ran: the one the launch item records (`MediaforceAppBuild`, kept there so a
damaged or missing app cannot hide it), or else the one the running server
reports. Only when neither is known does it accept `--allow-unknown-build`,
given after confirming the release is not older.

1. The dashboard must show the queue paused, with nothing compressing,
   sampling, checking the library, measuring or publishing.
2. The stage saves the exact launch item (`launch-item.before.plist`), the
   installed app (`Mediaforce.app`) and `release.json`, which also records the
   previous build. The clone runtime and the exact config and credential files
   are registered as protected. The dashboard check runs again.
3. The service stops and the port must close. Under the runtime lock, no work
   record may be running, no publication may hold its delivery lock, and
   `before.sqlite` is an online backup that passed `quick_check`.
4. `runtime/update-intent.json` is written with phase `installing`. As during
   an app update, it holds new work, publication, putting originals back,
   original-backup cleanup and hourly cleanup on whichever server reads it, and
   lets only the release's own server start.
5. The app replaces `/Applications/Mediaforce.app` and its signature is
   checked. The launch item keeps its label and logs and now starts the app's
   server.
6. The server must answer within three minutes (`--startup-timeout` for a
   longer migration) as the release's build, on the same database, with the
   same queued work. Its storage check must then run again a full 90-second
   freshness window after its first pass, and either admit work or name a real
   reason such as an unmounted share (#1340).
7. A usable app and database pair becomes the protected rollback. If the
   installed app was already missing or damaged, the previous protected stage
   stays in place. Without a usable prior stage, the installer saves the
   repaired app, launch item and matching database in a separate stage while
   work is held. The result names the retained stage and its contents.
   Removing the update record releases work. An online install resumes the
   encode queue alone. A stopped install preserves each queue's prior state.
8. A Mediaforce app still running from the installed path, found by its
   bundle path, is asked to quit as from its menu and opens again from the new
   bundle, as after an update. A proof install restarts only the proof's own
   app. One that has not quit within 30 seconds is left open.

It prints the installed identity, the saved rollback, and under `app` whether
the app was `relaunched`, was `not running`, or `needs a manual restart`. Runtime settings, the
work window, queued jobs, originals and rollback copies stay where they are.

A failure before the new server starts, such as a failed backup or app copy,
puts back the saved app, then starts the exact saved launch item under the
record's `restoring` phase, which admits the previous build, and removes the
record once the old service is healthy: its database is untouched. If the app
cannot be put back or the old service does not come back, the service stays
disabled and the record stays. Once the new server has started, when the port
does not close, or after any failure in a stopped install, the service stays
disabled and the record stays, with the failure in it. Old code never starts
against a database the release may have migrated. The update record holds work through the final check.

A record left in place, by a failure or an interrupted run, has two ways
forward:

- Forward: stop the service if it runs (`launchctl bootout
  gui/$(id -u)/com.mediaforce.web`) and run the script again with `--stopped`,
  a fresh stage and the same or a newer release. It checks the port and work
  records, saves the new attempt and keeps the original rollback pair.
- Back: run `/Applications/Mediaforce.app/Contents/MacOS/mediaforce-server
  service update rollback`. It is the app update's own rollback: it keeps the
  current database and app aside, restores the stage's `before.sqlite`, app and
  exact launch item, starts the previous build under the `restoring` phase and
  removes the record once it answers. When the saved launch item runs the clone
  runtime, it rolls back without the app and says so. When it runs a bundled app
  whose saved copy is missing or damaged, it refuses before changing anything;
  repair forward instead. Older stages without a recorded database path
  prove the destination by comparing a fresh SQLite backup before replacement. If the
  destination cannot be verified, use the stopped forward repair.

A release that fails validation after starting is added to
`runtime/failed-update-builds.json`, so the app never installs it again
through Sparkle. A dropped identity response retries. Transport errors during
that check allow the build to be offered again after rollback.

### First install from the clone runtime

The app installed before the first release has no Sparkle, so that release
goes through this script:

- The release is published and the feed offers it.
- Pause the queue with `POST /api/encode-queue/pause` and wait until the
  dashboard's encode, sample and full-run counts reach zero.
- Be at the Mac. The server is a new program to macOS, which may ask once to
  let Mediaforce use network volumes or the local network.

Afterwards, quit and reopen Mediaforce so the new app checks for updates. The
clone runtime stays: the service reads its config and `.env`, and its saved
launch item is the rollback. Moving them beside the database is a later step.

### A controller intentionally stopped for repair

Use `--stopped` when the controller must stay stopped until its replacement is
installed. The script skips the dashboard and verifies that the port is closed
and that no encode, sample, scan or evidence record is running. Missing or
running work records require reconciliation; this mode never clears them.

The stopped installer saves a rollback copy with encoding paused. It leaves
the source database at its original schema until the candidate starts. The
update record holds new work while the candidate is checked, including
automatically approved seasons. The app asks you to try changes again once
validation finishes. During the earlier drain it can still take changes. A stopped
repair preserves the saved encoding, background and measurement pauses.

### Rollback and cleanup ownership

The install's record held every change to the library, so a release that
failed its checks changed nothing there and its `before.sqlite` loses nothing;
use the rollback above. Once a release has passed and resumed work, roll back
only before it has encoded or published anything; after that a newer release
is the safer fix. Then stop the service and verify port 8777 is closed, set
aside the database and its `-wal`/`-shm` files, restore the stage's
`before.sqlite` and `Mediaforce.app`, restore `launch-item.before.plist`
atomically and start that exact plist with `launchctl enable` and `launchctl
bootstrap`. Do not regenerate it: new service commands refuse to install a
clone runtime. No runtime directory is deleted. After the app has updated
itself, use the update's own recovery instead
([package builds](package-builds.md#updates-with-sparkle)).

`runtime/protected-roots.json` beside the configured database lists every
runtime, config and credential folder a service has used. Registration is
cumulative. The hourly cleanup refuses an entire sweep when any configured
cleanup tree overlaps the support folder's `runtime/`, a registered path or the
executing source tree, and an unreadable registry refuses cleanup too. It never
prunes `runtime/`. Routine development cleanup owns development worktrees
only; it must leave clone runtimes, the registry and rollback material alone.
These guards cover Mediaforce's supported retention cleanup, not arbitrary
filesystem deletion or another repository's cleanup program.

The hourly cleanup keeps the newest `state.cleanup.kept_deploy_backups` stage
folders (two by default), plus the rollback stage named in
`runtime/rollback-stage.txt`. Retain a stage outside that cleanup tree when its
backup is needed after a later release replaces that pointer. Clone runtimes
under `runtime/releases/` stay until a verified retirement shows nothing uses
them: no cleanup removes anything under `runtime/`, and the clone whose config
and `.env` the service reads is registered as protected.

Moving that config and `.env` beside the database is a later step with its own
procedure: the config's relative paths and `include_files` resolve from its own
folder, so a moved copy can select other state unless each is made absolute
first.

## Notarized releases

`.github/workflows/release.yml` publishes a notarized app with the server
inside it and points the Sparkle feed at it. It does not install anything on
the controller: the app there updates itself, and
[the install script](#installing-a-release-on-the-controller) covers the first
install and repairs.

`pyproject.toml` is the one source of the release identity. `[project]
version` is the version every build shows, source builds included, as
`MAJOR.MINOR.PATCH`. In `[tool.mediaforce.release]`, `feed` is the
[feed](https://cbusillo.github.io/mediaforce/appcast.xml) and `public-key` is
the Sparkle key every release ships. The tag is `v<version>` and the archive
`Mediaforce-<version>.zip`.

Sparkle compares the build number (`CFBundleVersion`), not the version. The
workflow sets it to one above the newest build on the live feed, and never
lower than 4: Mediaforce 0.1.0 was build 3. Source builds are build 1 and never
read the feed. If a release already exists and the feed has no build history,
restore the feed before publishing. A version string such as `0.4.0` as the build would not update
an app on build 3, since Sparkle reads it as older.

### Cutting a release

1. Start only from a release request the Director made. A merge never
   releases anything.
2. Land a pull request through the merge train that raises `[project]
   version`: the patch number for fixes, the minor number for features.
3. Wait until that commit is the head of `main` and its CI `acceptance` check
   passed. The workflow refuses a commit without it.
4. Dispatch the workflow on `main` with the automation identity:

   ```bash
   gh-with-env-token workflow run release.yml --repo cbusillo/mediaforce --ref main
   ```

5. Watch the run. The `release` job checks the tag is unused, the version is
   higher than the live feed's, numbers the build and checks the Sparkle
   private key matches `public-key`. It builds the app with `scripts/build-mac-app.sh release`, which runs the
   package verifier on the app. Apple notarizes it, the ticket is stapled and
   Gatekeeper must accept it. The notarized app's server must start and answer
   as its build. It signs the zip for Sparkle, saves the Pages artifact and
   verifies the draft release's bytes and signature. The separate `publish`
   job makes that draft public and checks its downloaded SHA-256. The `feed` job publishes the feed to Pages
   last and checks the served feed offers the new build.
6. Report the release page, the version and the served feed to the Director.

If the `release` job fails before publishing, read what failed, delete a
leftover draft, fix the cause and dispatch again: the version stays and the
build is numbered again from the unchanged feed. Once the
draft is verified, rerun failed jobs on the same run. A publication retry
uses the same archive digest and feed artifact. A Pages retry needs only the
`feed` job. The feed artifact is retained for 30 days. Both jobs check for
a newer public release before proceeding.
Removing a feed item does not downgrade an installed app; a bad release is
replaced by a newer build.

### One-time setup

The workflow runs in the `release` environment, limited to `main`, which
holds these secrets:

- `MACOS_CERTIFICATE_P12` and `MACOS_CERTIFICATE_PASSWORD`: the team's
  Developer ID Application certificate, base64-encoded, and its password.
- `APP_STORE_CONNECT_API_KEY_P8_BASE64`, `APP_STORE_CONNECT_KEY_ID` and
  `APP_STORE_CONNECT_ISSUER_ID`: an App Store Connect API key for notarization.
- `SPARKLE_EDDSA_PRIVATE_KEY`: Mediaforce's own Sparkle key. Create it once
  with Sparkle's `generate_keys --account mediaforce` (in
  `.build/mac-app/SourcePackages/artifacts/sparkle/Sparkle/bin/` after a
  build) and export it with `-x FILE`. Its public half, from `-p`, goes in
  `public-key`. Never reuse another app's key.

GitHub Pages must publish from GitHub Actions. Proving the notarized artifact
and an update on this Mac uses the proof identity in
[package builds](package-builds.md#updates-with-sparkle), never the real app.

## Work evidence

`GET /api/release/work` reports fresh, uncached database work evidence:

```json
{
  "observed_at": "2026-10-09T20:00:00+00:00",
  "counts": {
    "pending_encode": 1,
    "active_encode": 1,
    "active_calibration": 0,
    "unfinished_scan_rows": 0,
    "active_evidence": 0
  }
}
```

`pending_encode` uses the same runnable-work counter as the dashboard's
`encode_queue.pending_work_count`: queued or retrying episodes count even when
their parent needs attention. `active_encode` includes queued, retrying and
running jobs, including folder parents and children. Counts overlap; do not sum
them to display a number of files. Samples/full runs and evidence work are also
counted. Paused queues still contain work; terminal jobs and
samples waiting only for human review do not count as database execution work.
`unfinished_scan_rows` covers the catalog traversal recorded in `scan_runs`,
not the entire scan job.

The response contains only counts and the observation-start timestamp, read
within one SQLite transaction. It deliberately has no idle or release-ready
flag, even when every count is zero. Two
ordinary kinds of running work are not represented by these counts:

- A scan can be queued before its database row exists, or continue refreshing
  external metadata after that row completes. The authoritative job state is
  loaded by `load_scan_job_state` in `mediaforce/web/runtime/job_runtime.py`;
  full and prefix-scoped scan jobs must both be considered.
- Checking and publishing an encoded file can continue after its encode job
  becomes terminal. `publish_checked_files_once` in
  `mediaforce/web/runtime/automatic_publish.py` selects the pending files, and
  publishing and held-file recovery use `mediaforce/encoding/delivery_lock.py`.
  Unpromoted staged files are a separate database-resident publishing queue;
  they are not included in these counts.
  The controller must stop new publishing admissions and verify those locks are
  released, rather than treating terminal encode jobs as finished delivery.

An unavailable database fails the request; a failed or missing response is
unknown, never idle. The response observes database work, not release readiness. It
does not stop new admissions, reserve an idle window, verify remote processes,
approve a commit, or prove a backup. The future controller must fence its
release, quiesce all work producers, recheck work and process custody before
stopping the service, then follow the authoritative release path. Candidate,
backup, runtime and post-check evidence must stay bound to that exact release;
uncertain effects require reconciliation rather than replay.

## M4 night window (Q1015)

The Director chose 12:00 AM–5:00 AM local time for this Mac's encoding
starts. Deliver the reviewed scheduling change in a release before applying its
new setting. Then use the existing Computers
settings API (`GET` and `POST /api/app/settings/computers`, preserving the
returned revision and all other computers and schedules), or the Mac app's
Settings → Computers:

- Add a separate work window named `Night window`, every day, From
  `12 AM`, Until `5 AM`, with no all-day exception days.
- Allow the current file to finish when the window closes. Its API field is
  `finish_current_file: true`; the hours are `start_hour: 0`, `end_hour: 5`.
- Assign it only to the existing M4 Mac Studio computer. Do not identify it
  by a guessed address or replace a schedule other computers still use.
- Read the saved settings back. Check Activity's Computers row outside the
  window: it waits, or says it is finishing the current file first. No new ordinary queued file or
  sample starts on that computer until midnight in its own timezone.

At 5:00 AM, a file already running finishes normally, including quality
checks and publication, even if this takes it past 5:00 AM. There is no
checkpoint to resume a partial encode safely. Finishing the file preserves
that work at the window boundary; the next file waits for midnight. Duration
estimates still hold back files unlikely to finish before 5:00 AM. A service
restart can interrupt a file under the existing recovery rules; do not deploy
until the documented idle gate passes. Legacy multi-file jobs retain their
hard stop; normal queue work is already one file per job. Originals and rollback copies
stay under the existing publication and cleanup rules. A deliberate bypass
request still overrides the schedule and must not be used for ordinary
night-window work. No launchd, energy, sleep or other system setting changes
are needed. The schedule is runtime configuration, shared by server releases,
so reverting source does not restore an older setting: record the previous
computer assignment and restore it through the same settings endpoint if needed.
