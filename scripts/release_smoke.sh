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

echo "[smoke] Purge backups (DB-driven)"

PURGE_LIB_DIR="$TMP_ROOT/purge/library"
PURGE_TRANSCODE_DIR="$TMP_ROOT/purge/transcode"
mkdir -p "$PURGE_LIB_DIR" "$PURGE_TRANSCODE_DIR"

PURGE_SOURCE="$PURGE_LIB_DIR/purge-source.mp4"
PURGE_ENCODED="$PURGE_TRANSCODE_DIR/purge-source.AV1.mp4"

ffmpeg -y \
	-f lavfi -i testsrc2=size=640x360:rate=30 \
	-f lavfi -i sine=frequency=220:sample_rate=44100 \
	-t 10 \
	-c:v libx264 -pix_fmt yuv420p -preset veryfast \
	-c:a aac -shortest \
	"$PURGE_SOURCE" >/dev/null 2>&1

ffmpeg -y -i "$PURGE_SOURCE" -t 10 \
	-c:v libaom-av1 -cpu-used 8 -b:v 1M -minrate 1M -maxrate 1M -bufsize 2M -row-mt 1 \
	-c:a aac -b:a 96k \
	"$PURGE_ENCODED" >/dev/null 2>&1

export PURGE_SOURCE PURGE_ENCODED PURGE_LIB_DIR

PROMOTE_JSON="$(
	uv run python - <<-'PY'
		import json
		import logging
		import os
		import pathlib
		from mediaforce.services.promote import promote_encoded_file_atomic

		source = pathlib.Path(os.environ["PURGE_SOURCE"])
		encoded = pathlib.Path(os.environ["PURGE_ENCODED"])
		dest = pathlib.Path(os.environ["PURGE_LIB_DIR"]) / encoded.name

		logger = logging.getLogger("mediaforce.smoke")
		logger.handlers.clear()
		logger.addHandler(logging.NullHandler())
		logger.propagate = False
		logger.setLevel(logging.CRITICAL)

		result, rollback_state = promote_encoded_file_atomic(
		    source_path=source,
		    encoded_path=encoded,
		    dest_path=dest,
		    dry_run=False,
		    move_original_to_backup=True,
		    rename_sidecars=False,
		    verify=True,
		    logger=logger,
		)

		assert rollback_state is not None
		assert result.backup_source_path is not None

		print(
		    json.dumps(
		        {
		            "source": str(source),
		            "dest": str(result.dest_path),
		            "backup": str(result.backup_source_path),
		        },
		        ensure_ascii=False,
		    )
		)
	PY
)"

export PROMOTE_JSON

PURGE_SOURCE_PATH="$(python -c 'import json,os; print(json.loads(os.environ["PROMOTE_JSON"])["source"])')"
PURGE_PROMOTED_PATH="$(python -c 'import json,os; print(json.loads(os.environ["PROMOTE_JSON"])["dest"])')"
PURGE_BACKUP_PATH="$(python -c 'import json,os; print(json.loads(os.environ["PROMOTE_JSON"])["backup"])')"

export PURGE_SOURCE_PATH PURGE_PROMOTED_PATH PURGE_BACKUP_PATH

HOME_OVERRIDE="$TMP_ROOT/home"
mkdir -p "$HOME_OVERRIDE"

HOME="$HOME_OVERRIDE" uv run python - <<'PY'
import os
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session

from mediaforce.db.models import EncodeResult, ensure_schema, init_engine

db = Path.home() / ".config" / "mediaforce" / "mediaforce.db"
engine = init_engine(str(db))
ensure_schema(engine)

promoted_at = (datetime.now() - timedelta(days=1)).isoformat()

with Session(engine) as session:
    session.add(
        EncodeResult(
            source_id=1,
            source_path=os.environ["PURGE_SOURCE_PATH"],
            output_path=os.environ["PURGE_PROMOTED_PATH"],
            output_size_bytes=1234,
            tier="good",
            crf=28,
            preset=5,
            film_grain=8,
            promoted=True,
            promoted_at=promoted_at,
            promoted_path=os.environ["PURGE_PROMOTED_PATH"],
            source_backup_path=os.environ["PURGE_BACKUP_PATH"],
        )
    )
    session.commit()
PY

HOME="$HOME_OVERRIDE" uv run mediaforce purge-backups --older-than-days 0 --limit 10 >/dev/null
HOME="$HOME_OVERRIDE" uv run mediaforce purge-backups --older-than-days 0 --limit 10 --apply >/dev/null

if [ -f "$PURGE_BACKUP_PATH" ]; then
	echo "[smoke] purge-backups failed: backup still exists at $PURGE_BACKUP_PATH" >&2
	exit 1
fi

if [ ! -f "$PURGE_PROMOTED_PATH" ]; then
	echo "[smoke] purge-backups failed: promoted missing at $PURGE_PROMOTED_PATH" >&2
	exit 1
fi

echo "[smoke] OK"
