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

Run it from a clean checkout of `main` that contains the release, so
`pyproject.toml` names the release's feed and Sparkle public key:

```bash
scripts/deploy-controller.sh --release v<version>-<build> \
  --stage "$HOME/Library/Application Support/mediaforce/deployments/release-<build>-<date>"
```

`--release` also takes the archive's URL or a local copy with `SHA256SUMS`
beside it. The stage is a fresh folder in `deployments/` beside the configured
database. Before anything on the Mac changes, the script checks, in order:

1. the archive matches its `SHA256SUMS`;
2. the feed offers this build, and the archive carries the feed's Sparkle
   signature for `public-key`;
3. the app is signed by the team, its notarization ticket is stapled and
   Gatekeeper accepts it as notarized;
4. the app is that version and build, with the committed feed and key.

The saved launch item supplies the rest. Its `--config` and `--credentials`,
or the clone runtime's own `config/defaults.toml` and `.env`, go into the new
launch item and are read where they are; nothing is copied. The running server
must report the database that config names. An older build than the installed
one, or an unfinished app update, is refused.

Then, under the deploy lock (it waits up to ten minutes for a running cleanup,
which the pause request itself can start):

1. The dashboard must show the queue paused with nothing running.
2. The stage saves the exact launch item (`launch-item.before.plist`), the
   installed app (`Mediaforce.app`) and `release.json`. The clone runtime and
   the config and credential folders are registered as protected.
3. The service stops and the port must close. Under the runtime lock, no work
   record may be running, and `before.sqlite` is an online backup that passed
   `quick_check`.
4. The app replaces `/Applications/Mediaforce.app` and its signature is
   checked. The launch item keeps its label and logs and now starts the app's
   server.
5. The server must answer within three minutes (`--startup-timeout` for a
   longer migration) as the release's build, on the same database, with the
   same queued work. Its storage check must then run again a full 90-second
   freshness window after its first pass, and either admit work or name a real
   reason such as an unmounted share (#1340).
6. The stage becomes the protected rollback and the queue resumes.

It prints the installed identity and the saved rollback. Runtime settings, the
work window, queued jobs, originals and rollback copies stay where they are.

A failure before the new server starts, such as a failed backup or app copy,
puts back the saved app and the exact saved launch item and starts the old
service: its database is untouched. Once the new server has started, or when
the port does not close, a failure leaves the service disabled. Old code never
starts against a database the release may have migrated; use the rollback
below. A lost resume response leaves the healthy release running; check the
dashboard before retrying.

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

The backup keeps the work records with its encode queue paused, as in the
online flow, and the source database is unchanged until that backup passes its
integrity check. Then the script pauses the encode queue through the
database's custody guards before the new server starts. The queue resumes only
if it was running before; an existing pause is preserved. Because the database
was touched, any failure leaves the service stopped, with the saved launch
item and backup ready for the rollback below.

### Rollback and cleanup ownership

Roll back only before the release has encoded or published anything; after
that, restoring `before.sqlite` loses those records, and a newer release is the
safer fix. Stop the service with `launchctl bootout gui/$(id -u)/com.mediaforce.web`
and verify port 8777 is closed. If the new server started, set aside the
database and its `-wal`/`-shm` files and restore the stage's verified
`before.sqlite`. Put the stage's `Mediaforce.app` back in `/Applications`.
Restore `launch-item.before.plist` atomically to the launch-item path, then use
`launchctl enable` and `launchctl bootstrap` for that exact saved plist. Do not
regenerate it: new service commands refuse to install a clone runtime. Verify
the API and queue before resuming work. No runtime directory is deleted.
After the app has updated itself, use the update's own recovery instead
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
them.

## Notarized releases

`.github/workflows/release.yml` publishes a notarized app with the server
inside it and points the Sparkle feed at it. It does not install anything on
the controller: the app there updates itself, and
[the install script](#installing-a-release-on-the-controller) covers the first
install and repairs.

`[tool.mediaforce.release]` in `pyproject.toml` is the one source of the
release identity. `project.version` is the version, `build` is the number
Sparkle compares, `feed` is the
[feed](https://cbusillo.github.io/mediaforce/appcast.xml) and `public-key` is
the Sparkle key every release ships. The tag is `v<version>-<build>`. Source
builds keep their own version and never read the feed.

### Cutting a release

1. Start only from a release request the Director made. A merge never
   releases anything.
2. Land a pull request through the merge train that raises `build`, and
   `project.version` when the version changes. Every attempt needs a new
   build; the workflow refuses a used tag.
3. Wait until that commit is the head of `main` and its CI `acceptance` check
   passed. The workflow refuses a commit without it.
4. Dispatch the workflow on `main` with the automation identity:

   ```bash
   gh-with-env-token workflow run release.yml --repo cbusillo/mediaforce --ref main
   ```

5. Watch the run. The `release` job checks the tag is unused, the live feed
   offers an older build and the Sparkle private key matches `public-key`. It
   builds the app with `scripts/build-mac-app.sh release`, which runs the
   package verifier on the app. Apple notarizes it, the ticket is stapled and
   Gatekeeper must accept it. The notarized app's server must start and answer
   as its build. Then it signs the zip for Sparkle, uploads a draft release,
   downloads it again to check the bytes and signature, publishes it and
   checks anyone can download it. The `feed` job publishes the feed to Pages
   last and checks it is served.
6. Report the release page, the build and the served feed to the Director.

If the `release` job fails before publishing, fix the cause, raise `build` and
dispatch again; delete a leftover draft after reading what failed. Once the
release is published, never run that job again. If only the `feed` job failed,
re-run that job alone: it republishes the same feed without rebuilding.
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
