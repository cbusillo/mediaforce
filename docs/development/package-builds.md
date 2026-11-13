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

A build checks for updates only when its Info.plist names a feed
(`MEDIAFORCE_UPDATE_FEED`) and public EdDSA key (`MEDIAFORCE_UPDATE_KEY`);
source builds leave both empty. The steps around Sparkle are in
`mediaforce/ops/controller_deploy.py`, run by the app as
`mediaforce-server service update reconcile|prepare|cancel|rollback`. While
`runtime/update-intent.json` exists beside the database, it holds new work and
cleanup; pauses a person set are never changed. A stuck update is recovered by
running `reconcile` or `rollback` from either the installed app or the stage's
saved copy.

`scripts/build-mac-app.sh proof OUTPUT CACHE BUILD FEED KEY` builds a copy for
update proofs with its own app ID, service label, port, support folder and
logs. Run its service from a folder on the internal drive: a launchd agent
stalled reading an app on an external volume.
