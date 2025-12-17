# Mediaforce

Content-aware media encoding for TV and movie libraries. Automatically analyzes
source quality and applies appropriate compression settings.

## Philosophy

**Maximum compression with watchable quality, not source fidelity.**

- Clean, modern content gets efficient encoding that preserves detail
- Noisy, grainy, or upscaled content gets denoised—don't waste bits on artifacts
- Old Star Trek shouldn't use more bitrate than modern prestige TV

## Supported Platforms

| OS                           | CPU/GPU                                | Notes                                                       |
|------------------------------|----------------------------------------|-------------------------------------------------------------|
| macOS 13+                    | Apple Silicon                          | Tested with Homebrew ffmpeg + libsvtav1                     |
| Linux (Ubuntu/Debian/Fedora) | x86_64 + NVIDIA (NVDEC/NVENC optional) | ffmpeg with libsvtav1; CUDA only needed for hardware decode |

Paths are automatically normalized between platforms (e.g., `/Volumes/media` ↔ `/mnt/media`).

## Pipeline

```
scan → queue → encode → verify → promote
```

## Project Layout

- `src/mediaforce/core.py` — main application logic (settings, queue, encoder, scanner).
- `src/mediaforce/config/` — shared config helpers (`settings`, structured `logging`).
- `src/mediaforce/db/` — SQLModel models and DB helpers (SQLite settings/inventory).
- `src/mediaforce/cli/` — CLI entrypoint shim (`mediaforce` console script).
- `src/mediaforce/web/` — FastAPI app, routes, templates, and static assets (`mediaforce-web`).
- `docs/` — contributor docs (e.g., `hosts.example.md`).
- `docs/workers.md` — how to run remote workers (systemd/launchd) and deploy updates.
- `TODO.md` — roadmap/features in progress.
- See `docs/architecture.md` for refactor goals and next steps.

1. **Scan**: Inventory library, probe all files for metadata
2. **Queue**: Prioritize by age + size (oldest/biggest first)
3. **Encode**: Apply tier-appropriate settings to transcode folder
4. **Verify**: Optional VMAF/SSIM spot-check to catch encoding disasters
5. **Promote**: Replace originals with encoded files, rename sidecars

## Source Quality Tiers

| Tier       | Typical Content                  | Strategy                              |
|------------|----------------------------------|---------------------------------------|
| `pristine` | Modern streaming, Blu-ray        | CRF 26, no denoise, preserve detail   |
| `good`     | Most HD TV                       | CRF 28, light film-grain synthesis    |
| `mediocre` | Older HD, moderate grain/noise   | CRF 30, light denoise (hqdn3d)        |
| `poor`     | Upscaled SD, heavy noise/grain   | CRF 32, heavy denoise (nlmeans)       |

## Requirements

- Python 3.13+
- ffmpeg with libsvtav1 encoder
- ffprobe (usually bundled with ffmpeg)

```bash
# macOS
brew install ffmpeg

# Verify SVT-AV1 support (current default encoder)
ffmpeg -encoders | grep svt
```

## Configuration

- Unified state lives at `~/.config/mediaforce/mediaforce.db` (settings + inventory).
- Library roots and weights are defined in the settings JSON/DB; defaults cover `/Volumes/media` on macOS and `/mnt/media` on Linux.
- Global settings include `max_concurrency` (per-host encode slots) and optional off-peak window (e.g., 00:00–05:00) enforced by workers.
- Logs are structured JSON to stdout. Set `MEDIAFORCE_LOG_LEVEL` (default `INFO`) and optional `MEDIAFORCE_LOG_FILE=/path/to/mediaforce.jsonl` to mirror logs to a JSONL file.

### Workers (Remote Encoders)

Workers connect to the master Web UI over the Worker API. Configure each worker with:

```bash
MEDIAFORCE_API_URL=<http://master-host:5555>
MEDIAFORCE_API_TOKEN=<shared secret>
MEDIAFORCE_MACHINE_NAME=<short-hostname>
```

Notes:
- `MEDIAFORCE_MACHINE_NAME` should be a stable, short identifier (e.g. `tdarr`, `chris-mbp`).
- If not set, Mediaforce falls back to the local hostname and strips any domain portion.
- The Web UI also canonicalizes dotted names (e.g. `foo.local` → `foo`) to reduce duplicates.

In the Web UI, go to **Settings → Workers** to see live worker state, global mode, and queue counts.

### Deploying to Remote Hosts

For simple “push a new build” workflows (no git required on the remote), use:

```bash
# Build a bundle that does NOT ship .env
scripts/deploy_bundle.sh /tmp/mediaforce-bundle.tgz

# Linux + systemd
scripts/deploy_remote.sh user@linux-host /opt/mediaforce /tmp/mediaforce-bundle.tgz --systemd mediaforce-worker

# macOS + launchd
scripts/deploy_remote.sh user@mac-host /Users/user/Developer/mediaforce /tmp/mediaforce-bundle.tgz --launchd com.mediaforce.worker
```

This bundle intentionally excludes `.env` so per-host secrets and identifiers remain local.

## Usage

### Check Platform Status

```bash
uv run mediaforce status
```

### Single Episode Test

```bash
# Analyze and show what would happen (dry run)
uv run mediaforce analyze "/path/to/episode.mkv"

# Encode single file
uv run mediaforce encode "/path/to/episode.mkv" -o "/path/to/output/" --hw-decode

# Encode with manual tier override
uv run mediaforce encode "/path/to/episode.mkv" -o "/path/to/output/" --tier pristine --hw-decode
```

### Season Batch

```bash
# Analyze entire season
uv run mediaforce analyze "/path/to/Show/Season 1/"

# Encode season (processes all video files)
uv run mediaforce encode "/path/to/Show/Season 1/" -o "/path/to/output/"
```

### Show Overrides

Show-level defaults (like forcing a specific tier for a show) are managed via the
Web UI (**Shows** page) and stored in `~/.config/mediaforce/mediaforce.db`.

Legacy note: `show_config.json` is no longer read during normal operation.
If you have one from an older version, import it once:

```bash
uv run mediaforce import-show-config --apply
```

(Omit `--apply` for a dry-run.) After importing, delete `show_config.json`.

## Technical Details

### Encoder: SVT-AV1 via ffmpeg

We use ffmpeg with libsvtav1 directly for full control over encoding parameters.

Key settings by tier:

```
pristine: -crf 26 -preset 5 -svtav1-params film-grain=0
good:     -crf 28 -preset 5 -svtav1-params film-grain=8
mediocre: -crf 30 -preset 6 -svtav1-params film-grain=4 + hqdn3d denoise
poor:     -crf 32 -preset 6 + nlmeans denoise
```

### Classification Heuristics

Source quality is estimated from:
- **Bitrate efficiency**: High bitrate + low resolution = noisy source
- **Codec age**: MPEG-2, older H.264 profiles suggest older masters
- **Resolution vs. content era**: 1080p show from 1995 = upscaled
- **Manual overrides**: show-level overrides from the Web UI/settings DB

### Audio Handling

**Codec: Opus** (best quality/size ratio, Plex transcodes for rare incompatible devices)

| Channels | Target Bitrate |
|----------|----------------|
| Mono     | 64 kbps        |
| Stereo   | 128 kbps       |
| 5.1      | 256 kbps       |
| 7.1      | 384 kbps       |

**Smart passthrough/conversion:**

| Source Codec                    | Action                 |
|---------------------------------|------------------------|
| Opus ≤ target                   | Passthrough (copy)     |
| AAC ≤ target                    | Passthrough (copy)     |
| AAC > target                    | Convert to Opus        |
| AC3/EAC3/DTS/MP3                | Always convert to Opus |
| Lossless (FLAC, TrueHD, DTS-HD) | Always convert to Opus |

**Track selection:**
- Keep English tracks only
- If no English found, keep undefined/untagged tracks as fallback
- If nothing matches, keep first track (never remove all audio)

### Subtitle Handling

**Keep English text-based subtitles only** (SRT, MOV_TEXT, SUBRIP).

Drop image-based formats (PGS, VobSub) and styled formats (ASS/SSA) since they're
incompatible with MP4 container. Bazarr handles subtitle acquisition for missing content.

## Output Naming

```
Original:  Show.S01E01.Episode.Title.1080p.BluRay.x264.mkv
Output:    Show.S01E01.Episode.Title.1080p.AV1.mp4
```

## Queue System

### Inventory Database

A single SQLite database lives at `~/.config/mediaforce/mediaforce.db` and stores:
- Full source metadata (codec, resolution, bitrate, duration, audio/subtitle tracks)
- Detected tier, classification reasoning, manual priority bumps
- Encode status/results/progress with quality metrics
- Multi-machine coordination (claim/release locking)

### Commands

```bash
# Scan library and populate inventory (no encoding)
uv run mediaforce scan /Volumes/media/tv

# Show queue (what would be encoded next)
uv run mediaforce queue /Volumes/media/tv --limit 20

# Dry run - show what would happen
uv run mediaforce encode /Volumes/media/tv --dry-run

# Encode to transcode folder (don't replace originals)
uv run mediaforce encode /Volumes/media/tv --no-replace

# Encode and replace originals
uv run mediaforce encode /Volumes/media/tv

# Promote pending encodes (after --no-replace verification)
uv run mediaforce promote /Volumes/media/tv

# Promotion keeps a hidden backup of the original by default.
# Periodically purge old backups (dry-run by default):
uv run mediaforce purge-backups --older-than-days 30

# Apply deletion (only deletes backups for successfully-promoted items
# older than the threshold, and only when the promoted file exists):
uv run mediaforce purge-backups --older-than-days 30 --apply
```

### Priority Scoring

Files are prioritized by: `(age_normalized * 0.5) + (size_normalized * 0.5)`

- **Age**: Older files encode first (already watched or less urgent)
- **Size**: Larger files encode first (more space savings)

### Skip Rules

- **Native AV1**: Marked `skipped_native_av1` (review later for high-bitrate re-encode)
- **HDR content**: Marked `skipped_hdr` (requires tone-mapping decisions)
- **Already encoded**: Skip files with completed encode in DB

### Multi-Machine Operation

Multiple machines can encode simultaneously:
- Each claims a file by writing `status='encoding', machine='hostname'`
- Stale claims (crashed machine) detected by timestamp, auto-reclaimed
- Encodes output to shared transcode folder (`/Volumes/media/transcode`)

### Night Scheduling

For cheap power windows (e.g., 12 AM - 5 AM):

```bash
# crontab entry
0 0 * * * cd /path/to/repo && uv run mediaforce encode /Volumes/media/tv --until 05:00
```

The `--until` flag finishes the current file then stops after the specified time.

### Sidecar Handling

When promoting, associated files are renamed to match:
- `.nfo`, `.srt`, `.sub`, `.idx` (metadata/subtitles)
- `-poster.jpg`, `-fanart.jpg`, `-thumb.jpg` (artwork)

### Quality Metrics

Captured during encoding for analysis:
- **Bitrate ratio**: output_bps / source_bps
- **PSNR/SSIM**: Fast quality check (built into ffmpeg)
- **VMAF**: Perceptual quality score (optional, sample clips only)

## Web Dashboard

Real-time monitoring and management interface.

```bash
# Start the web server (default port 8765)
uv run mediaforce-web
# or module style (no root scripts required)
uv run mediaforce.web

# Custom port
uv run mediaforce-web --port 5555
```

### Pages

| Page          | URL          | Description                                                            |
|---------------|--------------|------------------------------------------------------------------------|
| **Dashboard** | `/`          | Overview stats, live active-encodes, recent completions, space savings |
| **Queue**     | `/queue`     | Pending files with expandable show/season/episode hierarchy            |
| **Completed** | `/completed` | Successfully promoted files                                            |
| **Review**    | `/review`    | Quality outliers and size-increase encodes needing attention           |
| **Shows**     | `/shows`     | Per-show tier overrides management                                     |
The **Settings** tab (`/settings`) exposes the global library configuration
used by the CLI, web UI, and background watchers.

### Queue Features

- **Hierarchical view**: Expandable Show → Season → Episode drill-down
- **Episode details**: Video codec, resolution, bitrate, audio tracks, tier reasoning
- **Estimated savings**: Per-show and per-file space savings predictions
- **Expand/Collapse All**: Quick navigation buttons
- **Server pagination + cached totals**: Priority-ordered pages with cached counts for snappy navigation
- **Worker panel**: Shows active workers from encode progress with per-episode bump and send-to-worker actions

### Live Monitoring

- **Active encodes polling**: Dashboard auto-refreshes active encodes via `/api/active-encodes` every 3s (progress %, speed, ETA, frames).

### API Endpoints

```bash
# Queue drill-down
GET /api/queue/seasons/{show_name}
GET /api/queue/episodes/{show_name}/{season_name}

# Actions
POST /api/promote/{id}
POST /api/reject/{id}
POST /api/show-override
POST /api/apply-tier-to-show
POST /api/settings

## Global Settings & Libraries

Library configuration now lives in the SQLite settings DB at
`~/.config/mediaforce/mediaforce.db` and is shared by the CLI, web UI, and
workers. Edit it via the **Settings** tab (`/settings`) or the
`/api/settings` endpoint; workers can also pull `--settings-url` pointing to
`/api/settings/current`.

The settings API returns JSON shaped like:

```json
{
  "settings": {
    "global_max_height": 2160,
    "max_concurrency": 2,
    "offpeak_enabled": false,
    "offpeak_start": "00:00",
    "offpeak_end": "05:00",
    "libraries": [
      {
        "id": "tv",
        "name": "TV Library",
        "media_type": "tv",
        "mac_path": "/Volumes/media/tv",
        "linux_path": "/mnt/media/tv",
        "watch": true,
        "max_height": null,
        "weight": 1.0
      }
    ]
  }
}
```

Fresh installs seed two libraries with sensible caps: TV at 1080p and Movies
at 2160p, with a global fallback cap of 1080p when no library match is found.

When the app needs a library root for the current host, it chooses the
macOS path on Darwin and the Linux path on Linux, keeping queue and DB
paths consistent across machines.

## Automatic Library Watch

The encoder can watch configured libraries for new video files and
automatically queue them via the existing scan pipeline.

Requirements:

- `watchfiles` Python package (declared in `pyproject.toml`)
- Libraries with `watch` enabled in settings (via the Settings page/API)

Start a watcher on the local host:

```bash
uv run mediaforce watch
```

For each watched library on this host, the watcher:

- Monitors the library root recursively
- Detects new or modified files with known video extensions
- Normalizes the path between `/Volumes` and `/mnt` when needed
- Inserts/updates the file in the `media_inventory` database using the same
  logic as `scan`
- Recalculates priorities so new entries are correctly ordered in the queue

This pairs with `mediaforce run` (or `python -m mediaforce run`) on worker
nodes.

Recommended: run workers in **API mode** so they don’t open SQLite directly on
multiple hosts. Set `MEDIAFORCE_API_URL` (or pass `--api-url`) and the worker
will claim work + report progress/results via the web API.

If you want to restrict worker endpoints, set `MEDIAFORCE_API_TOKEN` on the web
server and on all workers. Workers will send `Authorization: Bearer <token>` to
`/api/worker/*` and the quality-loop evaluation submit endpoints.

## Development

```bash
# Run tests
python3 -m pytest tests/

# Type checking
python3 -m mypy src/mediaforce
```

## Workers (unified scheduler)
- Single worker can handle all libraries from the unified database at `~/.config/mediaforce/mediaforce.db`.
- Recommended: set `MEDIAFORCE_API_URL=http://<host>:5555` (or pass `--api-url`) so workers coordinate via the API instead of direct SQLite.
- Optional: set `MEDIAFORCE_API_TOKEN=<shared secret>` on the server + workers to require auth for worker claim/progress/report endpoints.
- Example systemd unit: `mediaforce-worker.service` -> `uv run python -m mediaforce run /mnt/media --output /mnt/media/transcode --api-url http://192.168.1.3:5555 --autoupdate-url http://192.168.1.3:5555/raw/ --autoupdate-interval 3600 --hw-decode`.
- Per-library weights and max heights come from settings; manual bumps use `manual_priority` (lower numbers encode first). `max_concurrency` and off-peak window can be set in the web Settings page.

Workers pull settings from the master (`/api/settings/current`) and code from `/raw/manifest.json` with hourly autoupdate and self-restart on change.
