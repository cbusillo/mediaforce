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
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Operational notes:

- If you see repeated `status=143` (SIGTERM) restarts during encodes, check for
  external watchdogs/timeouts and increase `TimeoutStopSec`.
- Avoid killing the entire cgroup if you rely on graceful shutdown.

## macOS (launchd)

Example LaunchAgent values (as a reference):

- `ProgramArguments`: `uv run mediaforce run /Volumes/media/tv -o <output> --max-concurrency 1`
- `EnvironmentVariables`: set `MEDIAFORCE_ENV_FILE` to the worker `.env` path.
- `KeepAlive`: `true`

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
