#!/usr/bin/env bash
set -euo pipefail

# Minimal release smoke:
# - creates synthetic source + AV1 encoded samples
# - exercises verify-before-promote + rollback (no DB needed)
# - runs unit tests

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[smoke] Running unit tests"
uv run pytest -q

if ! command -v ffmpeg >/dev/null 2>&1; then
	echo "[smoke] ffmpeg not found; skipping promotion/rollback smoke" >&2
	exit 0
fi

if ! command -v ffprobe >/dev/null 2>&1; then
	echo "[smoke] ffprobe not found; skipping promotion/rollback smoke" >&2
	exit 0
fi

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

LIB_DIR="$TMP_ROOT/library"
TRANSCODE_DIR="$TMP_ROOT/transcode"
mkdir -p "$LIB_DIR" "$TRANSCODE_DIR"

export LIB_DIR TRANSCODE_DIR

SOURCE="$LIB_DIR/source.mp4"
ENCODED="$TRANSCODE_DIR/source.AV1.mp4"

echo "[smoke] Generating synthetic source.mp4"
ffmpeg -y \
	-f lavfi -i testsrc2=size=640x360:rate=30 \
	-f lavfi -i sine=frequency=440:sample_rate=44100 \
	-t 20 \
	-c:v libx264 -pix_fmt yuv420p -preset veryfast \
	-c:a aac -shortest \
	"$SOURCE" >/dev/null 2>&1

echo "[smoke] Generating synthetic AV1 encoded file"

# Use libaom-av1 for portability and predictable rate control.
# Keep bitrate high enough that the file clears verify_before_promote's 1MB minimum.
ffmpeg -y -i "$SOURCE" -t 20 \
	-c:v libaom-av1 -cpu-used 8 -b:v 1M -minrate 1M -maxrate 1M -bufsize 2M -row-mt 1 \
	-c:a aac -b:a 96k \
	"$ENCODED" >/dev/null 2>&1

echo "[smoke] Promote (verify-before-promote)"
uv run python - <<'PY'
import os
import pathlib

from mediaforce.services.promote import promote_encoded_file_atomic, rollback_promote

lib_dir = pathlib.Path(os.environ["LIB_DIR"])
transcode_dir = pathlib.Path(os.environ["TRANSCODE_DIR"])
source = lib_dir / "source.mp4"
encoded = transcode_dir / "source.AV1.mp4"

result, rollback_state = promote_encoded_file_atomic(
    source_path=source,
    encoded_path=encoded,
    dest_path=lib_dir / encoded.name,
    dry_run=False,
    move_original_to_backup=True,
    rename_sidecars=False,
    verify=True,
)
assert rollback_state is not None
assert result.dest_path.exists(), "promoted file missing"
assert result.backup_source_path is not None and result.backup_source_path.exists(), "backup missing"

rollback_promote(rollback_state)
assert source.exists(), "source not restored after rollback"
assert not result.dest_path.exists(), "promoted file should be removed after rollback"
print("[smoke] Promotion + rollback OK")
PY

echo "[smoke] Worker API mode (no SQLite access)"

PORT="$(
	python - <<'PY'
import socket

s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

export MEDIAFORCE_API_TOKEN="smoke-token"

uv run mediaforce-web --host 127.0.0.1 --port "$PORT" >/dev/null 2>&1 &
WEB_PID=$!
trap 'kill "$WEB_PID" >/dev/null 2>&1 || true; rm -rf "$TMP_ROOT"' EXIT

for _ in $(seq 1 50); do
	if curl -fsS "http://127.0.0.1:${PORT}/api/settings/current" >/dev/null 2>&1; then
		break
	fi
	sleep 0.1
done

uv run mediaforce run "$TMP_ROOT" \
	--output "$TMP_ROOT/out" \
	--dry-run \
	--api-url "http://127.0.0.1:${PORT}" \
	--no-sample-vmaf \
	>/dev/null 2>&1

kill "$WEB_PID" >/dev/null 2>&1 || true

echo "[smoke] OK"
