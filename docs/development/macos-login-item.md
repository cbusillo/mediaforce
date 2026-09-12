# macOS Login Item

## Purpose

The `com.mediaforce.web` LaunchAgent starts `mediaforce-web` after a user logs
in. It starts the application only. It does not watch, mount, or own SMB shares;
controller storage recovery remains inside Mediaforce after the app is running.

## Controller storage recovery

The elected background-worker owner checks controller storage independently of
the encode queue. This runs even if the controller is not an encode worker, its
work window is closed, or processing is paused. Reconnecting storage does not
unpause work or retry stopped jobs. The normal scheduler decides which queued
jobs may start after storage readiness is verified.

Learned SMB mappings remain in `controller-smb-mounts.json` beside runtime
settings. Automatic connections use the system NetFS API with `UIOption=NoUI`;
Mediaforce does not retrieve passwords or fall back to Finder dialogs. Clean
connection failures retry with bounded backoff. An ambiguous timeout requires
operator attention rather than another potentially overlapping mount request.
The native helper must return the expected mount path, and a fresh check must
confirm the saved SMB identity and required directory access. Merely finding a
writable directory under `/Volumes` is not proof that the share is mounted.
Saved readiness never authorizes a new web process: it must verify storage
again after restart. Controller source paths and the existing controller-side
staging requirements are checked; remote-only source overrides are not treated
as paths on the controller. Archive creation remains part of promotion.

Recovery state persists in `controller-storage-recovery.json` beside runtime
settings. `GET /api/hosts` exposes it as `controller_storage` without attempting
a connection. A manual reconnect through Finder or the existing Prepare action
is accepted after a fresh readiness check; no server restart is needed to clear
the incident. Automatic and explicit controller connection attempts share a
nonblocking process lock.

If the expected volume path is occupied by an ordinary directory or another
volume, recovery stops for inspection. Do not delete that directory without
checking its contents and ownership. If NetFS mounts at a suffixed path such as
`/Volumes/media-1`, the diagnostic records that path and leaves the mount intact.
Check it in Finder and eject the unintended mount deliberately before restoring
the expected path. Automatic recovery never unmounts volumes or removes folders.

### Installed acceptance

Mocked checks do not establish that the signed-in user's Keychain permits
unattended mounting. Before treating automatic recovery as operationally
qualified, perform these checks during an approved maintenance window:

1. Confirm no encode is active and preserve queue/background pause settings.
2. Confirm the saved share mapping and existing Keychain credentials. Exercise
   the no-UI helper and verify the exact mounted share and staging access.
3. With controlled unavailable storage and unavailable credentials, verify
   bounded failure, visible retry/action-required state, and no dialogs.
4. Reconnect manually after an ambiguous failure; verify the incident clears
   from fresh evidence without restarting the application.
5. Reboot/login with storage initially unavailable. Restore network/storage
   and verify automatic recovery followed by one eligible encode under the
   existing schedule. Verify paused and schedule-closed work remains idle.

Keep interrupted children and retained outputs under the separate job-recovery
procedure. Never delete output or force a terminal job back into the queue just
to qualify storage recovery.

## Commands

Run these from the Mediaforce checkout:

```bash
uv run mediaforce service install
uv run mediaforce service enable
uv run mediaforce service status
uv run mediaforce service restart
uv run mediaforce service logs
uv run mediaforce service logs --stderr
uv run mediaforce service disable
uv run mediaforce service uninstall
```

`install` writes the generated plist without loading it. `enable` writes the
current plist, enables the label, and bootstraps it into the signed-in user's
GUI domain. `disable` unloads and persistently disables the label. `uninstall`
also removes the plist. All operations are idempotent.

## Runtime Contract

The generated plist:

- executes `.venv/bin/mediaforce-web --no-reload` directly;
- uses the checkout as its working directory;
- reads the normal repo-local `.env` through the web entrypoint;
- contains no `/Volumes`, `WatchPaths`, or `QueueDirectories` dependency;
- restarts through launchd's `KeepAlive` behavior with a 30-second throttle;
- writes durable logs to `~/Library/Logs/mediaforce/web.log` and
  `~/Library/Logs/mediaforce/web.err.log`.

The service manager rotates either log to a single `.1` file when it exceeds
16 MiB before enabling or restarting the item.

## Development Handoff

`scripts/mediaforce-dev.sh start backend` unloads the LaunchAgent before
starting the development backend so both processes cannot compete for the same
runtime lock or port. This bootout is temporary; the persistent enable/disable
state remains owned by `mediaforce service`.

## Verification

For ordinary non-disruptive verification:

1. Run `uv run mediaforce service enable`.
2. Run `uv run mediaforce service status` and confirm a PID is reported.
3. Open the configured web URL and verify Settings shows controller storage
   recovery when a required share is missing.
4. Inspect both durable logs for restart loops or configuration errors.
5. Run `uv run mediaforce service disable` when the service should remain off.

The final login-item acceptance check requires a real logout/login or reboot:

1. Enable the item before ending the desktop session.
2. Log back in and confirm the app starts without manual action.
3. Confirm startup succeeds even when media shares are initially unavailable.
4. Record the name shown in System Settings > General > Login Items. Depending
   on macOS process attribution, it may display the Python interpreter rather
   than the console-script filename.
5. Disable the item, repeat login, and confirm it remains stopped before
   enabling it again if desired.

## Raw Recovery

If the project environment is unavailable, use launchctl directly:

```bash
launchctl bootout "gui/$(id -u)/com.mediaforce.web"
launchctl disable "gui/$(id -u)/com.mediaforce.web"
```

After restoring the checkout and `.venv`, run `uv sync` followed by
`uv run mediaforce service enable` to regenerate and reload the item.
