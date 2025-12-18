# TODO

Keep this list short and current. Completed work should move to `CHANGELOG.md` (or git history).

## Priority (Architecture & Stability)

- [x] **Refactor `core.py`**
  - Finish extracting orchestration logic (encoding loops, scan triggers) into a dedicated `OrchestrationService` or `PipelineService`.
  - Reduce `core.py` to a thin CLI wiring layer.

- [x] **Dockerize Worker**
  - Create `Dockerfile.worker` to provide a reproducible environment with the correct `ffmpeg` build (libsvtav1) and Python dependencies.
  - Simplify deployment on NAS/servers (Unraid, Synology, etc.).

## Features

- [x] **Notification Channels**
  - Implement a `NotificationService` for key events:
    - Encode completion (Season/Movie finished).
    - Worker failure/stuck alerts.
    - Quality Loop decisions (e.g., "Downgraded to `mediocre` due to low VMAF").
  - Support generic Webhooks (Discord, Slack, Gotify).

## Documentation & Polish

- [x] **Worker Service Docs**
  - Document recommended service configs for systemd + launchd (env files, restart semantics).