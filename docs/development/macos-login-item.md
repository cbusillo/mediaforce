# macOS Login Item

## Purpose

The `com.mediaforce.web` LaunchAgent starts `mediaforce-web` after a user logs
in. It starts the application only. It does not watch, mount, or own SMB shares;
controller storage recovery remains inside Mediaforce after the app is running.

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
