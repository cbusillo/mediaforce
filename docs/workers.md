# Workers

This page documents how to run Mediaforce encoder workers on remote machines.

## Concepts

- **Master**: runs the Web UI + Worker API (FastAPI) and owns the SQLite DB.
- **Worker**: runs `mediaforce run ...` with `MEDIAFORCE_API_URL` set so it uses the Worker API.

Workers must not open the SQLite DB directly.

## Environment

Workers should be configured with a stable identity and point at the master:

```bash
MEDIAFORCE_API_URL=http://<master-host>:5555
MEDIAFORCE_API_TOKEN=<shared secret>
MEDIAFORCE_MACHINE_NAME=<short-hostname>
```

Notes:

- Prefer a short, stable `MEDIAFORCE_MACHINE_NAME` like `tdarr` or `chris-mbp`.
- Mediaforce strips domain suffixes by default (e.g. `foo.local` → `foo`).

## Linux (systemd)

Example unit:

```ini
[Unit]
Description=Mediaforce worker (API mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/mediaforce
EnvironmentFile=/opt/mediaforce/.env

# Use a single slot by default; scale by running more workers on more machines.
ExecStart=/root/.local/bin/uv run mediaforce run /mnt/media/tv --max-concurrency 1

# systemd sends SIGTERM on stop/restart; treat it as a clean exit so "restart"
# doesn't spam unit failures.
SuccessExitStatus=143

# Be patient when stopping (ffmpeg can take a moment to exit).
TimeoutStopSec=30
KillSignal=SIGTERM

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Restart Semantics

Mediaforce workers handle `SIGTERM` gracefully by attempting to terminate the current `ffmpeg` subprocess. If a restart occurs, the worker will stop, and upon restart, it will look for new work. 

**Note on "Stop Now":** When using the Web UI "Stop Now" action, the worker is sent a control signal via the API. It will kill the current process and return the item to `pending` with a cooldown, preventing immediate re-claim.

## macOS (launchd)

Create a file at `~/Library/LaunchAgents/com.mediaforce.worker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mediaforce.worker</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/uv</string>
        <string>run</string>
        <string>mediaforce</string>
        <string>run</string>
        <string>/Volumes/media/tv</string>
        <string>--max-concurrency</string>
        <string>1</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/youruser/Developer/mediaforce</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>MEDIAFORCE_API_URL</key>
        <string>http://master:5555</string>
        <key>MEDIAFORCE_API_TOKEN</key>
        <string>your-secret</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/mediaforce.worker.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mediaforce.worker.stderr.log</string>
</dict>
</plist>
```

Load it with: `launchctl load ~/Library/LaunchAgents/com.mediaforce.worker.plist`

## Docker (Recommended for Linux/NAS)

A pre-built environment with `ffmpeg` (SVT-AV1) and Python 3.13 is available via the `Dockerfile.worker`.

```bash
# Build the image
docker build -t mediaforce-worker -f Dockerfile.worker .

# Run the worker
docker run -d \
  --name mediaforce-worker \
  -v /mnt/media:/media \
  -e MEDIAFORCE_API_URL=http://master:5555 \
  -e MEDIAFORCE_API_TOKEN=your-secret \
  -e MEDIAFORCE_MACHINE_NAME=my-nas-worker \
  mediaforce-worker /media/tv
```

## Operations

- **Settings → Workers** shows:
  - live worker state/progress
  - `global mode` and per-worker overrides
  - actions (run/pause/stop/stop-now) and cleanup/normalize

## Deploying updates

Use:

```bash
scripts/deploy_bundle.sh /tmp/mediaforce-bundle.tgz
scripts/deploy_remote.sh user@host /opt/mediaforce /tmp/mediaforce-bundle.tgz --systemd mediaforce-worker
```

The bundle intentionally excludes `.env` so per-host secrets and names remain local.
