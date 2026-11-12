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

A bundled app updates through Sparkle's standard windows, with automatic
installation off. A build checks only when its Info.plist names a feed
(`MEDIAFORCE_UPDATE_FEED`) and its public EdDSA key (`MEDIAFORCE_UPDATE_KEY`);
source builds leave both empty.

Sparkle downloads, verifies, replaces and relaunches the app. The controller
deploy's staged transaction (`mediaforce/ops/controller_deploy.py`) does the
rest, split around Sparkle and run as
`mediaforce-server --config <saved> service update reconcile|prepare|cancel|rollback`.
`runtime/update-intent.json` beside the database spans quit, replacement and
reboot. While it exists, encodes, samples, background work and automatic
publication admit nothing new, resume and cleanup wait, and the server starts
only in a phase that allows it.

- `prepare` pauses new work and waits for running files, including a
  publication holding its delivery lock. It then saves the signed old app, its
  exact launch item and a checked `before.sqlite` in a fresh `deployments/`
  stage and stops the service. Only then may Sparkle offer that exact build.
  A failure restarts the unchanged old service.
- `reconcile`, run at every launch, starts the installed candidate with the
  saved launch item (only its trusted build changes), checks that the server
  answering is that build on that database with the same queued work and
  storage, then restores the earlier pauses. It also finishes an interrupted
  save or ending.
- `cancel` restarts the old service when Sparkle shows nothing was downloaded.
- A failed candidate stays stopped and its build is never offered again. The
  app offers to go back; `rollback` restores the saved database, app and launch
  item into fresh copies and keeps the failed ones in the stage.

`scripts/build-mac-app.sh proof OUTPUT CACHE BUILD FEED KEY` builds a copy for
update proofs with its own app ID, service label and port. The build refuses
an app that shares the live identity; the copy never asks for notifications
and reaches only a service set up with its own config.
