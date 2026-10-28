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
connection failures retry with bounded backoff. A completed helper result with a still-absent share is retryable, including older
`action_required` records that describe only that transient failure. Backoff is
persisted and capped at five minutes; restarts do not reset it.
An ambiguous timeout requires
user attention rather than another potentially overlapping mount request.
The native helper reports its status code; a fresh check must confirm the exact
expected mount path, saved SMB identity and required directory access. Merely finding a
writable directory under `/Volumes` is not proof that the share is mounted.
Saved readiness never authorizes a new web process: it must verify storage
again after restart. Controller source paths and the existing controller-side
staging requirements are checked; remote-only source overrides are not treated
as paths on the controller. Archive creation remains part of promotion.

Recovery state persists in `controller-storage-recovery.json` beside runtime
settings. `GET /api/hosts` exposes it as `controller_storage` without attempting
a connection. A manual reconnect through Finder or the existing Set up computer action
is accepted after a fresh readiness check; no server restart is needed to clear
the incident. Automatic and explicit controller connection attempts share a
nonblocking process lock.

If the expected volume path is occupied by an ordinary directory or another
volume, recovery stops for inspection. Do not delete that directory without
checking its contents and ownership. If NetFS mounts at a suffixed path such as
`/Volumes/media-1`, the expected-path check fails and leaves the mount intact.
Inspect Finder for such a mount and eject it deliberately before restoring
the expected path. Automatic recovery never unmounts volumes or removes folders.

### Computer recovery and outage warnings

A separate supervised worker under the same elected background owner checks
remote computers independently of
queued work, pause and work windows. Checks retry after 30 seconds, increasing
to at most five minutes. `host-recovery.json` retains the outage start, last
check/connection and next retry beside runtime settings. Missing mounted-media
shares on macOS use the existing saved SMB mapping and native NetFS no-UI helper.
Batch SSH starts it as a temporary job in the signed-in user's desktop session,
through the same per-share job and lock as the Finder helper, because only that
session can read the login Keychain. A no-UI request cannot open a dialog, so it
is ended if it does not finish within the attempt. A full host readiness check
follows before the computer becomes usable. No Finder fallback runs in this worker.
When the share's password is not saved, or a leftover folder or a share connected
under a suffixed name is in the way, the computer needs attention: one warning
names the computer and says what to do there, and automatic connections stop
until a check finds the storage connected, for example after Retry connection or
Set up computer. When no one
is logged in at the computer, the warning asks for a login, and connections keep
retrying so storage returns on its own after that.
These checks also update the computer cards, and each controller storage pass
re-checks the controller's own card when its storage changed, so the cards stay
current with an idle queue while web reads stay check-free.
Explicit Set up computer retains its existing Finder behavior and shares that
computer's recovery process lock. Remote probes and connections cannot hold the
controller readiness lock or delay its background checks. An interrupted/ambiguous remote mount is persisted before SSH
and remains suppressed across service restarts until fresh readiness succeeds.

Retry connection, on a computer's card and on its warning, runs one of these
recovery passes for that computer now (`POST /api/hosts/retry-connection`). It
skips the retry delay and tries again after a pause for someone at that
computer, but uses the same no-UI connection, so it never opens a dialog, and
it never repeats a request whose result is unconfirmed. It is refused while a
check or connection for that computer is running. Each pass saves only its own
computer's record, so a background pass and Retry connection never overwrite
each other.

New incidents and recovery are logged once, with the active notification IDs in
`recovery-alerts.json`; a restart does not emit the same outage again. Recovered
incidents disappear. No email, chat or system notification destination is
assumed. Checks do not unpause work, retry stopped jobs, unmount storage or
change service/runtime locations.

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
- carries the existing runtime's `.env` into a new runtime checkout with mode
  `0600`, and refuses the checkout switch when neither location has one;
- contains no `/Volumes`, `WatchPaths`, or `QueueDirectories` dependency;
- restarts through launchd's `KeepAlive` behavior with a 30-second throttle;
- writes durable logs to `~/Library/Logs/mediaforce/web.log` and
  `~/Library/Logs/mediaforce/web.err.log`.

The service manager rotates either log to a single `.1` file when it exceeds
16 MiB before enabling or restarting the item.

## Development Handoff

The [local web development helper](../../README.md#local-web-development) owns development
process reuse, checkout isolation and bounded login-item shutdown/startup.
Its bootout is temporary; the persistent enable/disable state remains owned
by `mediaforce service`. The source lifecycle tests use fake launchctl output
and fixture-owned processes. Native `program` and `pid` output for the generated
ProgramArguments-only plist and installed service lifecycle remain unverified
by those tests.

## Verification

For ordinary non-disruptive verification:

1. Run `uv run mediaforce service enable`.
2. Run `uv run mediaforce service status` and confirm a PID is reported.
3. Open the configured web URL and verify the computers list reports a
   controller storage problem when a required share is missing
   (`GET /api/hosts` returns it as `controller_storage`).
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
