from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from mediaforce.config.logging import log_event


@dataclass(frozen=True)
class ProbeSummary:
    path: pathlib.Path
    duration_seconds: Optional[float]
    video_codec: Optional[str]
    width: Optional[int]
    height: Optional[int]
    audio_streams: int


@dataclass(frozen=True)
class PromoteManifest:
    version: int
    source_path: str
    encoded_path: str
    dest_path: str
    backup_source_path: Optional[str]
    sidecar_renames: list[tuple[str, str]]

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "source_path": self.source_path,
            "encoded_path": self.encoded_path,
            "dest_path": self.dest_path,
            "backup_source_path": self.backup_source_path,
            "sidecar_renames": self.sidecar_renames,
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def from_json(payload: str) -> PromoteManifest:
        raw = json.loads(payload)
        return PromoteManifest(
            version=int(raw.get("version") or 1),
            source_path=str(raw["source_path"]),
            encoded_path=str(raw["encoded_path"]),
            dest_path=str(raw["dest_path"]),
            backup_source_path=(str(raw["backup_source_path"]) if raw.get("backup_source_path") else None),
            sidecar_renames=[
                (str(old), str(new))
                for old, new in (raw.get("sidecar_renames") or [])
            ],
        )


@dataclass(frozen=True)
class PromoteResult:
    dest_path: pathlib.Path
    backup_source_path: Optional[pathlib.Path]
    manifest: PromoteManifest


@dataclass
class PromoteRollbackState:
    source_path: pathlib.Path
    encoded_path: pathlib.Path
    dest_path: pathlib.Path
    backup_source_path: Optional[pathlib.Path]
    sidecar_renames: list[tuple[pathlib.Path, pathlib.Path]]
    staged_path: Optional[pathlib.Path]
    copied_encoded: bool


SIDECAR_EXTENSIONS = {
    ".nfo",
    ".srt",
    ".sub",
    ".idx",
    ".ass",
    ".ssa",
}

IMAGE_SIDECAR_SUFFIXES = [
    "-poster",
    "-fanart",
    "-thumb",
    "-banner",
    "-landscape",
    "-clearlogo",
    "-clearart",
]


def find_ffprobe() -> Optional[str]:
    for candidate in [
        "/opt/homebrew/bin/ffprobe",
        "/usr/local/bin/ffprobe",
        "ffprobe",
    ]:
        if shutil.which(candidate):
            return candidate
    return None


def _fsync_file(path: pathlib.Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: pathlib.Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _same_filesystem(a: pathlib.Path, b: pathlib.Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def probe_with_ffprobe(path: pathlib.Path) -> Optional[ProbeSummary]:
    ffprobe = find_ffprobe()
    if not ffprobe:
        log_event(logging.ERROR, "ffprobe_missing", path=str(path))
        return None

    cmd = [
        ffprobe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        log_event(logging.ERROR, "ffprobe_failed", path=str(path), error=str(exc))
        return None

    duration: Optional[float] = None
    fmt = data.get("format") or {}
    if raw_dur := fmt.get("duration"):
        try:
            duration = float(raw_dur)
        except ValueError:
            duration = None

    video_codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    audio_streams = 0

    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and video_codec is None:
            video_codec = stream.get("codec_name")
            width = stream.get("width")
            height = stream.get("height")
        elif codec_type == "audio":
            audio_streams += 1

    return ProbeSummary(
        path=path,
        duration_seconds=duration,
        video_codec=video_codec,
        width=width,
        height=height,
        audio_streams=audio_streams,
    )


def find_sidecars(source_video_path: pathlib.Path) -> list[pathlib.Path]:
    sidecars: list[pathlib.Path] = []
    parent = source_video_path.parent
    stem = source_video_path.stem

    for ext in SIDECAR_EXTENSIONS:
        candidate = parent / f"{stem}{ext}"
        if candidate.exists():
            sidecars.append(candidate)

    for suffix in IMAGE_SIDECAR_SUFFIXES:
        for img_ext in [".jpg", ".jpeg", ".png", ".webp"]:
            candidate = parent / f"{stem}{suffix}{img_ext}"
            if candidate.exists():
                sidecars.append(candidate)

    return sidecars


def compute_sidecar_destination(
    *,
    source_video_path: pathlib.Path,
    dest_video_path: pathlib.Path,
    sidecar_path: pathlib.Path,
) -> pathlib.Path:
    old_stem = source_video_path.stem
    new_stem = dest_video_path.stem

    # Direct extension match (e.g., ep.mkv -> ep.srt)
    if sidecar_path.name.startswith(old_stem):
        if sidecar_path.suffix.lower() in SIDECAR_EXTENSIONS:
            return sidecar_path.with_name(f"{new_stem}{sidecar_path.suffix}")

        # Image sidecars (e.g., ep-thumb.jpg)
        for suffix in IMAGE_SIDECAR_SUFFIXES:
            for img_ext in [".jpg", ".jpeg", ".png", ".webp"]:
                if sidecar_path.name == f"{old_stem}{suffix}{img_ext}":
                    return sidecar_path.with_name(f"{new_stem}{suffix}{img_ext}")

    return sidecar_path


def verify_before_promote(
    *,
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    probe: Callable[[pathlib.Path], Optional[ProbeSummary]] | None = None,
    require_av1: bool = True,
) -> tuple[bool, list[str], Optional[ProbeSummary], Optional[ProbeSummary]]:
    """Lightweight ffprobe-based sanity checks to avoid promoting bad outputs."""

    reasons: list[str] = []

    if not source_path.exists():
        reasons.append("source_missing")
        return False, reasons, None, None

    if not encoded_path.exists():
        reasons.append("encoded_missing")
        return False, reasons, None, None

    try:
        if encoded_path.stat().st_size < 1024 * 1024:
            reasons.append("encoded_too_small")
    except OSError:
        reasons.append("encoded_stat_failed")

    probe_fn = probe or probe_with_ffprobe
    source = probe_fn(source_path)
    encoded = probe_fn(encoded_path)
    if source is None:
        reasons.append("source_ffprobe_failed")
    if encoded is None:
        reasons.append("encoded_ffprobe_failed")
    if source is None or encoded is None:
        return False, reasons, source, encoded

    if not encoded.video_codec:
        reasons.append("encoded_no_video_stream")
    if require_av1 and (encoded.video_codec or "").lower() != "av1":
        reasons.append("encoded_not_av1")

    if (
        encoded.width is None
        or encoded.height is None
        or encoded.width <= 0
        or encoded.height <= 0
    ):
        reasons.append("encoded_invalid_dimensions")
    if source.width and source.height and encoded.width and encoded.height:
        src_ar = source.width / source.height if source.height else None
        enc_ar = encoded.width / encoded.height if encoded.height else None
        if src_ar and enc_ar:
            if abs(src_ar - enc_ar) / src_ar > 0.03:
                reasons.append("aspect_ratio_changed")
        if encoded.height > source.height or encoded.width > source.width:
            reasons.append("unexpected_upscale")

    if source.audio_streams > 0 and encoded.audio_streams == 0:
        reasons.append("audio_dropped")

    if source.duration_seconds and encoded.duration_seconds:
        tol = max(2.0, source.duration_seconds * 0.02)
        if abs(source.duration_seconds - encoded.duration_seconds) > tol:
            reasons.append("duration_mismatch")

    return not reasons, reasons, source, encoded


def _default_dest_for_encoded(source_path: pathlib.Path, encoded_path: pathlib.Path) -> pathlib.Path:
    return source_path.parent / encoded_path.name


def _backup_name_for_source(source_path: pathlib.Path) -> pathlib.Path:
    token = uuid.uuid4().hex[:10]
    return source_path.with_name(f".{source_path.name}.mediaforce-orig-{token}")


def promote_encoded_file_atomic(
    *,
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    dest_path: Optional[pathlib.Path] = None,
    dry_run: bool = False,
    move_original_to_backup: bool = True,
    rename_sidecars: bool = True,
    verify: bool = True,
    probe: Callable[[pathlib.Path], Optional[ProbeSummary]] | None = None,
    logger: Optional[logging.Logger] = None,
) -> tuple[PromoteResult, Optional[PromoteRollbackState]]:
    """Atomically promote an encoded file into the library with rollback support.

    - Verify-before-promote: refuse to touch the library when ffprobe checks fail.
    - Atomic staging: stage encoded into the destination directory then rename.
    - Rollback: caller can rollback later (e.g., DB commit failure).
    """

    dest = dest_path or _default_dest_for_encoded(source_path, encoded_path)

    if dest.resolve() == source_path.resolve():
        raise ValueError("Refusing to promote: destination is the source path")

    if dest.exists():
        raise FileExistsError(f"Destination already exists: {dest}")

    if verify:
        probe_fn = probe or probe_with_ffprobe
        ok, reasons, src_probe, enc_probe = verify_before_promote(
            source_path=source_path,
            encoded_path=encoded_path,
            probe=probe_fn,
            require_av1=True,
        )
        if not ok:
            log_event(
                logging.WARNING,
                "promote_verify_failed",
                logger=logger,
                source=str(source_path),
                encoded=str(encoded_path),
                reasons=reasons,
                source_codec=(src_probe.video_codec if src_probe else None),
                encoded_codec=(enc_probe.video_codec if enc_probe else None),
            )
            raise RuntimeError(f"Verify-before-promote failed: {', '.join(reasons)}")

    sidecars = find_sidecars(source_path) if rename_sidecars else []
    sidecar_moves: list[tuple[pathlib.Path, pathlib.Path]] = []
    for sidecar in sidecars:
        dest_sidecar = compute_sidecar_destination(
            source_video_path=source_path,
            dest_video_path=dest,
            sidecar_path=sidecar,
        )
        if dest_sidecar != sidecar:
            sidecar_moves.append((sidecar, dest_sidecar))

    backup_source = _backup_name_for_source(source_path) if move_original_to_backup else None
    staged = dest.with_name(f".{dest.name}.mediaforce-staging-{uuid.uuid4().hex[:10]}")

    manifest = PromoteManifest(
        version=1,
        source_path=str(source_path),
        encoded_path=str(encoded_path),
        dest_path=str(dest),
        backup_source_path=(str(backup_source) if backup_source else None),
        sidecar_renames=[(str(old), str(new)) for old, new in sidecar_moves],
    )

    rollback_state = PromoteRollbackState(
        source_path=source_path,
        encoded_path=encoded_path,
        dest_path=dest,
        backup_source_path=backup_source,
        sidecar_renames=sidecar_moves,
        staged_path=staged,
        copied_encoded=False,
    )

    if dry_run:
        log_event(
            logging.INFO,
            "promote_dry_run",
            logger=logger,
            source=str(source_path),
            encoded=str(encoded_path),
            dest=str(dest),
            backup_source=str(backup_source) if backup_source else None,
            sidecars=len(sidecar_moves),
        )
        return PromoteResult(dest_path=dest, backup_source_path=backup_source, manifest=manifest), None

    dest.parent.mkdir(parents=True, exist_ok=True)

    copied_encoded = False
    try:
        # Stage encoded into destination dir so the final rename is atomic.
        if _same_filesystem(encoded_path, dest.parent):
            os.replace(str(encoded_path), str(staged))
        else:
            shutil.copy2(str(encoded_path), str(staged))
            copied_encoded = True
            _fsync_file(staged)
            _fsync_dir(staged.parent)

        rollback_state.copied_encoded = copied_encoded

        os.replace(str(staged), str(dest))
        _fsync_dir(dest.parent)

        for old, new in sidecar_moves:
            if new.exists() and new != old:
                raise FileExistsError(f"Sidecar destination exists: {new}")
            os.replace(str(old), str(new))
        _fsync_dir(dest.parent)

        if backup_source:
            if backup_source.exists():
                raise FileExistsError(f"Backup destination exists: {backup_source}")
            os.replace(str(source_path), str(backup_source))
            _fsync_dir(source_path.parent)

        if copied_encoded:
            try:
                encoded_path.unlink(missing_ok=True)
            except OSError:
                log_event(
                    logging.WARNING,
                    "promote_cleanup_encoded_failed",
                    logger=logger,
                    encoded=str(encoded_path),
                )

        log_event(
            logging.INFO,
            "promote_success",
            logger=logger,
            source=str(source_path),
            encoded=str(encoded_path),
            dest=str(dest),
            backup_source=str(backup_source) if backup_source else None,
            sidecars=len(sidecar_moves),
        )

        return PromoteResult(dest_path=dest, backup_source_path=backup_source, manifest=manifest), rollback_state
    except Exception as exc:
        log_event(
            logging.ERROR,
            "promote_failed",
            logger=logger,
            source=str(source_path),
            encoded=str(encoded_path),
            dest=str(dest),
            error=str(exc),
        )
        try:
            rollback_promote(rollback_state)
        except Exception as rollback_exc:
            log_event(
                logging.ERROR,
                "promote_rollback_failed",
                logger=logger,
                source=str(source_path),
                encoded=str(encoded_path),
                dest=str(dest),
                error=str(rollback_exc),
            )
        raise


def rollback_promote(state: PromoteRollbackState) -> None:
    """Best-effort rollback for a partially-applied promotion."""

    for old, new in reversed(state.sidecar_renames):
        if new.exists() and not old.exists():
            os.replace(str(new), str(old))

    if state.backup_source_path and state.backup_source_path.exists() and not state.source_path.exists():
        os.replace(str(state.backup_source_path), str(state.source_path))

    if state.dest_path.exists() and state.source_path.exists():
        if not state.copied_encoded and not state.encoded_path.exists():
            try:
                state.encoded_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(state.dest_path), str(state.encoded_path))
            except OSError:
                log_event(logging.WARNING, "rollback_left_promoted_file", dest=str(state.dest_path))
        else:
            try:
                state.dest_path.unlink()
            except OSError:
                log_event(logging.WARNING, "rollback_left_promoted_file", dest=str(state.dest_path))

    if (
        state.staged_path
        and state.staged_path.exists()
        and not state.encoded_path.exists()
        and not state.copied_encoded
    ):
        try:
            os.replace(str(state.staged_path), str(state.encoded_path))
        except OSError:
            log_event(logging.WARNING, "rollback_left_staged_file", staged=str(state.staged_path))


def rollback_from_manifest(payload: str) -> None:
    manifest = PromoteManifest.from_json(payload)
    source = pathlib.Path(manifest.source_path)
    dest = pathlib.Path(manifest.dest_path)
    backup = pathlib.Path(manifest.backup_source_path) if manifest.backup_source_path else None
    sidecars = [(pathlib.Path(old), pathlib.Path(new)) for old, new in manifest.sidecar_renames]

    for old, new in reversed(sidecars):
        if new.exists() and not old.exists():
            os.replace(str(new), str(old))

    if backup and backup.exists():
        if source.exists():
            raise FileExistsError(f"Refusing rollback: source path already exists: {source}")
        os.replace(str(backup), str(source))

    if dest.exists() and source.exists():
        dest.unlink()
