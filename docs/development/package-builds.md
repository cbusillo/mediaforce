# Package Builds

`uv build` creates the Mediaforce wheel and source distribution.

The sdist uses an explicit source allowlist in `pyproject.toml`. User
runtime config, including root `config/defaults.toml` and
`config/folder-defaults.toml`, is excluded. This prevents user folder
rules, untracked IDE files, `.env`, `state/`, `scratch/`, runtime databases,
review media, and machine-local paths from entering an archive merely because
they exist in the checkout.

`mediaforce/package_defaults/defaults.toml` is a separate install-safe package
resource.
It contains no configured source roots, folder overrides, or machine-specific
absolute paths. Source checkouts continue to prefer root `config/defaults.toml`;
an installed wheel falls back to the package resource so its entrypoints do not
depend on a repository checkout.

## Verification

Build and verify both archives:

```bash
rm -rf dist
uv build
uv run python scripts/verify_package_contents.py dist/*.whl dist/*.tar.gz
```

The verifier requires:

- one byte-identical install-safe package default config
- no forbidden runtime/private archive members
- no current-machine or machine-specific user path in bounded scannable text
- no oversized scannable text or traversal path

Package verification complements the normal acceptance gate; it does not
replace backend, Mac app, CLI, or PyCharm checks.

The CI `tests` job runs the package build and verifier, and the required
`acceptance` check passes only when it does, so package privacy
is a required pull-request signal rather than a manual release convention.

## Embedded Mac server

The approved bundled-server plan is being delivered in steps. The existing
controller deploy remains the installed delivery path until the separate
update/rollback and release cutover work is complete.

Build a signed app with its own universal Python runtime and locked server
packages using the existing Mac build entrypoint:

```bash
bash scripts/build-mac-app.sh bundle /absolute/output /absolute/runtime-cache
```

The cache holds the digest-pinned Python support archive. Packaging installs
only wheels from the lockfile, separately for the architectures in the built
Release app. Native code must support the app's minimum macOS version, and is
signed inside out with library validation retained. No encoder, SSH account,
private config, credential file, database or media is bundled. The existing
package verifier also accepts a staged `.app` directory.

The embedded entrypoint is `Mediaforce.app/Contents/MacOS/mediaforce-server`.
It requires `--config /absolute/saved-config.toml` and keeps the configured
database, settings and other writable state outside the app. A private `.env`
beside that database supplies optional environment defaults. An existing
credential source can instead be read in place with `--credentials /absolute/path`;
packaging and service installation never copy it. Shell environment values
continue to take precedence.

For an isolated installation with no existing service, the same executable
accepts the existing `service install|enable|restart|disable|uninstall|status|logs`
commands and uses
the existing `com.mediaforce.web` LaunchAgent and log paths. Its working
directory is the configured database's parent. Service setup verifies the
signed bundle and refuses to replace another runtime, build or config. A moved
copy of the same signed app can repair its saved path once the old copy is
absent; status and stop commands still address the saved service. Use the later
safe update transaction for that transition; this build command does not
perform a deployment. Closing the UI leaves the background service running.
The existing runtime lock prevents another app copy from opening the same
runtime or database, even on a different port.
For service commands, put `--config` before `service` and the bundled
`--credentials` option after `service`, before the action.

The application uses macOS certificate trust through `truststore`, preserving
HTTPS verification without a checkout-specific OpenSSL certificate path.
The verifier checks private/runtime names and the current build user's home
path throughout the app, including binaries and property lists. Its generic
machine-path heuristics apply to
Mediaforce's own files; Python and third-party packages contain public path
examples and are assembled from the digest-pinned runtime and hashed lockfile.
Upstream Python tests, development objects and local wheel-origin records are
omitted from the app.
Staged native code has debug symbols stripped before signing; qualification
rejects retained object-file debug maps. Sparkle's framework helpers are
signed as whole bundles, inside out, with the same team.

## Updates with Sparkle

A bundled app updates through Sparkle's standard windows. A build only checks
when its Info.plist names a feed (`MEDIAFORCE_UPDATE_FEED`) and the feed's
public EdDSA key (`MEDIAFORCE_UPDATE_KEY`); source builds leave both empty and
never contact a feed. Automatic installation is off: Mediaforce gets ready
first, then the Director accepts the install.

Sparkle owns download, archive verification, replacement and relaunch. The
app's own server executable owns everything around it, as
`mediaforce-server --config <saved> service update status|prepare|cancel|finish|rollback`.
One record, `runtime/update-intent.json` beside the configured database, spans
app quit, replacement and reboot. While it exists, the encode queue, samples,
background work and automatic publication admit nothing new, resume is
refused, the hourly cleanup does not run, and the server starts only as the
step names. Every ending passes through a `releasing` phase before any pause
is lifted, so nothing rolls back under work that already resumed.

1. When Sparkle finds a build, the app refuses it and starts preparation.
   The server pauses new work and records what was already paused; running
   files finish, including any publication holding its delivery lock. Each
   minute the app rechecks and shows what is still running.
2. When nothing runs, preparation copies and verifies the signed old app and
   its exact launch item into a fresh `deployments/update-<old>-to-<new>-<time>/`
   stage, stops the service, verifies the port closed and the database's
   running-work records empty, and saves a checked `before.sqlite`. Only then
   does the rollback-stage pointer move. A failure here restarts the unchanged
   old service from its saved launch item and restores the pauses.
3. Sparkle may now offer and install exactly that build. A dismissed offer
   that never downloaded restarts the old service; a downloaded or pending
   install keeps it stopped.
4. The relaunched app starts its own server through the saved service path,
   with the exact saved launch item except its trusted build, checks its
   build, API, queued work and storage, records the pair in
   `candidate.txt`, then restores the pauses so only a previously running
   queue resumes.
5. A candidate that fails stays stopped and its build is recorded in
   `runtime/failed-update-builds.json`; Sparkle never offers it again. `rollback`
   restores `before.sqlite` and the saved app into fresh copies, moves the
   failed database and app into a new `failed-<time>/` folder in the stage,
   switches the copies in, restores the exact saved launch item
   and starts the old build. Roll back only before new work was released.
