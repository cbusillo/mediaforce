import argparse
import logging
import pathlib
import subprocess
import time
import sys
import platform
import re
import json
from datetime import datetime, timedelta
from typing import Any, Optional, List, Callable, TypedDict, cast

from sqlmodel import Session, select, desc, func, col
from mediaforce.config.logging import log_event, CLI_LOGGER
from mediaforce.config.paths import (
    get_media_roots,
    iter_libraries_for_current_host,
    normalize_path,
    get_db_path,
    get_library_root,
    find_library_for_path,
    get_transcode_output_path,
)
from mediaforce.config.settings import ENGINE, load_app_settings, init_db, CONFIG_DIR
from mediaforce.domain.types import MediaInfo, QualityMetrics
from mediaforce.services.classification import adjust_tier_with_vmaf, classify_source
from mediaforce.services.encoder import build_ffmpeg_command, find_ffmpeg
from mediaforce.services.media_probe import (
    probe_media,
    probe_media_with_interlace_detection,
    find_ffprobe,
)
from mediaforce.services.metrics import sample_vmaf, verify_encode_quality, generate_compare_html
from mediaforce.services.scanner import (
    VIDEO_EXTENSIONS,
    scan_file_to_db,
    recalculate_priorities,
)
from mediaforce.services.show_overrides import get_default_tier_for_show, import_show_config_json
from mediaforce.services.queue import check_missing_outputs
from mediaforce.services.promote import promote_encoded_file_atomic, rollback_promote
from mediaforce.services.outlier_detection import check_for_outliers, OutlierResult
from mediaforce.db import MediaItem, EncodeResult, now_iso


class QueueItemSummary(TypedDict):
    id: Optional[int]
    path: str
    priority_score: Optional[float]
    size_bytes: Optional[int]
    potential_savings_bytes: Optional[int]
    tier: Optional[str]
    bitrate_kbps: Optional[int]
    library_id: Optional[str]


class QueueTotalsPayload(TypedDict):
    count: int
    total_bytes: int


class SpaceSavedPayload(TypedDict):
    encodes: int
    source_bytes: int
    output_bytes: int
    saved_bytes: int
    saved_pct: float


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_bitrate(kbps: Optional[int]) -> str:
    if kbps is None:
        return "unknown"
    if kbps >= 1000:
        return f"{kbps / 1000:.1f} Mbps"
    return f"{kbps} kbps"


def collect_video_files(path: pathlib.Path) -> list[pathlib.Path]:
    if path.is_file():
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            return [path]
        return []

    if path.is_dir():
        files = []
        for f in sorted(path.iterdir()):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                files.append(f)
        return files

    return []


def guess_show_name(path: pathlib.Path) -> Optional[str]:
    parts = path.parts
    for i, part in enumerate(parts):
        if part.lower().startswith("season"):
            if i > 0:
                return parts[i - 1]
    return None


def run_analyze(path_str: str) -> int:
    path = normalize_path(pathlib.Path(path_str).resolve())
    files = collect_video_files(path)

    if not files:
        log_event(logging.ERROR, "analyze_no_files", path=str(path))
        return 1

    show_name = guess_show_name(path)
    override_tier: Optional[str] = None
    if show_name:
        with Session(ENGINE) as session:
            override_tier = get_default_tier_for_show(session, show_name=show_name)

    log_event(logging.INFO, "analyze_start", files=len(files), show=show_name)

    for f in files:
        info = probe_media(f)
        if info is None:
            log_event(logging.WARNING, "analyze_probe_failed", file=str(f))
            continue

        classification = classify_source(info, override_tier)

        log_event(
            logging.INFO,
            "analyze_file",
            file=str(f),
            duration=format_duration(info.duration_seconds),
            video=f"{info.video_codec} {info.video_width}x{info.video_height} {info.video_bit_depth}-bit",
            bitrate=format_bitrate(info.video_bitrate_kbps),
            interlaced=info.is_interlaced,
            audio_tracks=len(info.audio_tracks),
            tier=classification.tier.value,
            confidence=classification.confidence,
            reasons=classification.reasons,
            recommended_crf=classification.recommended_settings.crf,
            recommended_preset=classification.recommended_settings.preset,
            recommended_denoise=classification.recommended_settings.denoise or "none",
            already_av1=info.is_already_av1,
        )

        if info.is_already_av1:
            log_event(logging.INFO, "analyze_skip_av1", file=str(f))

    return 0


def run_encode_batch(
    path_str: str,
    output_dir_str: str,
    tier_override: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
    hw_decode: bool = False,
    hw_encode: bool = False,
    sample_vmaf_enabled: bool = False,
    sample_count: int = 3,
    sample_length: float = 8.0,
    sample_motion_aware: bool = True,
) -> int:
    path = normalize_path(pathlib.Path(path_str).resolve())
    output_dir = normalize_path(pathlib.Path(output_dir_str).resolve())
    files = collect_video_files(path)

    if not files:
        log_event(logging.ERROR, "encode_no_files", path=str(path))
        return 1

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log_event(logging.ERROR, "encode_ffmpeg_missing")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    show_name = guess_show_name(path)
    override_tier: Optional[str] = None
    if show_name:
        with Session(ENGINE) as session:
            override_tier = get_default_tier_for_show(session, show_name=show_name)

    if tier_override:
        override_tier = tier_override

    log_event(logging.INFO, "encode_start_cli", files=len(files), output=str(output_dir), show=show_name)

    success_count = 0
    for i, f in enumerate(files, 1):
        log_event(logging.INFO, "encode_file_start", index=i, total=len(files), file=str(f))

        info = probe_media_with_interlace_detection(f)
        if info is None:
            log_event(logging.ERROR, "encode_probe_failed", file=str(f))
            continue

        if info.is_already_av1 and not force:
            log_event(logging.INFO, "encode_skip_av1", file=str(f))
            continue

        classification = classify_source(info, override_tier)
        settings = classification.recommended_settings

        log_event(
            logging.INFO,
            "encode_classification",
            file=str(f),
            tier=classification.tier.value,
            crf=settings.crf,
            preset=settings.preset,
            denoise=settings.denoise or "none",
            interlaced=info.is_interlaced,
        )

        app_settings = load_app_settings()
        target_height = app_settings.global_max_height

        for lib, root in iter_libraries_for_current_host(app_settings):
            if f.is_relative_to(root):
                if lib.max_height:
                    target_height = lib.max_height
                break

        if sample_vmaf_enabled:
            vmaf_stats = sample_vmaf(
                info,
                settings,
                max_height=target_height,
                sample_count=sample_count,
                sample_length=sample_length,
                motion_aware=sample_motion_aware,
            )
            if vmaf_stats:
                classification = adjust_tier_with_vmaf(classification, vmaf_stats)
                settings = classification.recommended_settings
                log_event(
                    logging.INFO,
                    "encode_vmaf_adjust",
                    file=str(f),
                    median=vmaf_stats['median'],
                    minimum=vmaf_stats['min'],
                    tier=classification.tier.value,
                )

        source_str = str(f)
        rel_path: Optional[pathlib.Path] = None
        for root_str in get_media_roots():
            if source_str.startswith(root_str):
                rel_path = f.relative_to(pathlib.Path(root_str))
                break

        if rel_path:
            file_output_dir = output_dir / rel_path.parent
        else:
            file_output_dir = output_dir

        file_output_dir.mkdir(parents=True, exist_ok=True)

        stem = f.stem
        for marker in [".x264", ".x265", ".h264", ".h265", ".HEVC", ".AVC"]:
            stem = stem.replace(marker, "")
        output_name = f"{stem}.AV1.mp4"
        output_path = file_output_dir / output_name

        if output_path.exists() and not force:
            log_event(logging.INFO, "encode_skip_output_exists", output=output_path.name)
            continue

        cmd = build_ffmpeg_command(
            f,
            output_path,
            settings,
            info,
            max_height=target_height,
            hw_decode=hw_decode,
            hw_encode=hw_encode,
        )

        if dry_run:
            log_event(logging.INFO, "encode_dry_run", output=str(output_path))
            continue

        log_event(logging.INFO, "encode_launch_cli", output=output_path.name)
        try:
            subprocess.run(cmd, check=True)
            success_count += 1
            orig_size = f.stat().st_size
            new_size = output_path.stat().st_size
            ratio = new_size / orig_size * 100
            log_event(
                logging.INFO,
                "encode_done_cli",
                source_mb=orig_size // 1024 // 1024,
                output_mb=new_size // 1024 // 1024,
                ratio=ratio,
            )

        except subprocess.CalledProcessError as e:
            log_event(logging.ERROR, "encode_failed_cli", error=str(e))
            continue
    log_event(logging.INFO, "encode_complete_cli", success=success_count, total=len(files))
    return 0


def run_scan(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.path).resolve()
    if not path.exists():
        log_event(logging.ERROR, "path_missing", path=str(path))
        return 1

    settings = load_app_settings()
    lib, library_root = find_library_for_path(path, settings)
    if library_root is None:
        library_root = get_library_root(path)

    db_path = get_db_path(library_root)
    log_event(logging.INFO, "scan_start", library=str(library_root), database=str(db_path))

    session = init_db(db_path)

    if path.is_file():
        files = [path]
    else:
        files = []
        for ext in VIDEO_EXTENSIONS:
            files.extend(path.rglob(f"*{ext}"))
        files = sorted(files)

    if not files:
        log_event(logging.INFO, "scan_no_files")
        return 0

    log_event(logging.INFO, "scan_files", count=len(files))

    stats = [int(f.stat().st_mtime) for f in files]
    max_age = max(int(time.time()) - s for s in stats) if stats else 1

    counts = {"pending": 0, "skipped_native_av1": 0, "skipped_hdr": 0, "error": 0}

    for i, f in enumerate(files, 1):
        if i % 10 == 0 or i == len(files):
            log_event(logging.INFO, "scan_progress", index=i, total=len(files), file=f.name)

        try:
            result = scan_file_to_db(
                session, f, 1, max_age, 
                library_id=lib.id if lib else None,
                classify_source=classify_source,
                probe_media=probe_media,
                now_iso=now_iso,
            )
            if result:
                status = result["status"]
                if status in counts:
                    counts[status] += 1
                else:
                    counts["pending"] += 1
            else:
                counts["error"] += 1
        except Exception as e:
            log_event(logging.ERROR, "scan_file_failed", file=str(f), error=str(e))
            counts["error"] += 1

        if i % 50 == 0:
            session.commit()

    session.commit()

    log_event(logging.INFO, "recalculate_priorities")
    
    def calc_prio(potential_savings_bytes: Optional[int], mtime: int, max_savings: int, max_age: int) -> float:
        from mediaforce.services.scanner import calculate_priority
        return calculate_priority(potential_savings_bytes, mtime, max_savings, max_age)

    recalculate_priorities(session, max_age=max_age, calculate_priority=calc_prio)

    log_event(logging.INFO, "check_missing_outputs")
    missing_reset, missing_files = check_missing_outputs(session, now_iso=now_iso)
    for entry in missing_files:
        log_event(logging.WARNING, "missing_output_reset", source=entry.get("source"), output=entry.get("output"))

    log_event(
        logging.INFO,
        "scan_complete",
        pending=counts["pending"],
        skipped_native_av1=counts["skipped_native_av1"],
        skipped_hdr=counts["skipped_hdr"],
        errors=counts["error"],
        reset_missing=missing_reset,
    )

    return 0


def run_queue(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.path).resolve()
    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        log_event(logging.ERROR, "run_no_db", path=str(db_path))
        return 1

    session = init_db(db_path)
    limit = args.limit or 20

    media_cols = cast(Any, MediaItem.__table__.c)
    encode_cols = cast(Any, EncodeResult.__table__.c)

    rows: list[MediaItem] = session.exec(
        select(MediaItem)
        .where(media_cols.status == "pending")
        .order_by(desc(media_cols.priority_score))
        .limit(limit)
    ).all()

    if not rows:
        log_event(logging.INFO, "queue_empty", library=str(library_root), limit=limit)
        return 0

    items: list[QueueItemSummary] = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "path": row.path,
                "priority_score": row.priority_score,
                "size_bytes": row.size_bytes,
                "potential_savings_bytes": row.potential_savings_bytes,
                "tier": row.detected_tier,
                "bitrate_kbps": row.bitrate_kbps,
                "library_id": row.library_id,
            }
        )

    log_event(logging.INFO, "inventory_summary")
    summary: list[tuple[str, int, int | None]] = session.exec(
        select(media_cols.status, media_cols.id, media_cols.size_bytes)
    ).all()
    totals: dict[str, tuple[int, int]] = {}
    for status, mid, size_bytes in summary:
        cnt, total = totals.get(status, (0, 0))
        totals[status] = (cnt + 1, total + (size_bytes or 0))

    totals_payload: dict[str, QueueTotalsPayload] = {}
    for status, (cnt, total_bytes) in totals.items():
        totals_payload[status] = {
            "count": cnt,
            "total_bytes": total_bytes,
        }

    encode_rows: list[tuple[int | None, int | None]] = session.exec(
        select(encode_cols.output_size_bytes, media_cols.size_bytes)
        .select_from(EncodeResult.__table__)
        .join(MediaItem.__table__, encode_cols.source_id == media_cols.id)
        .where(func.coalesce(encode_cols.output_size_bytes, 0) > 0)
    ).all()

    space_saved_payload: Optional[SpaceSavedPayload] = None
    if encode_rows:
        source_bytes = sum(src or 0 for _, src in encode_rows)
        output_bytes = sum(out or 0 for out, _ in encode_rows)
        if source_bytes and output_bytes:
            saved_bytes = source_bytes - output_bytes
            saved_pct = (1 - output_bytes / source_bytes) * 100
            space_saved_payload = {
                "encodes": len(encode_rows),
                "source_bytes": source_bytes,
                "output_bytes": output_bytes,
                "saved_bytes": saved_bytes,
                "saved_pct": saved_pct,
            }

    log_event(
        logging.INFO,
        "queue_listing",
        library=str(library_root),
        limit=limit,
        count=len(items),
        items=items,
        totals=totals_payload,
        space_saved=space_saved_payload,
    )

    session.close()
    return 0


def run_promote(args: argparse.Namespace) -> int:
    path = normalize_path(pathlib.Path(args.path).resolve())
    transcode_root = normalize_path(pathlib.Path(args.transcode_root).resolve())

    if not path.exists():
        log_event(logging.ERROR, "promote_path_missing", path=str(path))
        return 1

    if not transcode_root.exists():
        log_event(logging.ERROR, "promote_transcode_root_missing", transcode_root=str(transcode_root))
        return 1

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if path.is_file():
        files = [path]
    else:
        files = []
        for ext in VIDEO_EXTENSIONS:
            files.extend(path.rglob(f"*{ext}"))
        files = sorted(files)

    files = [f for f in files if f.is_file() and not f.name.startswith(".")]
    if not files:
        log_event(logging.INFO, "promote_no_files", path=str(path))
        return 0

    log_event(
        logging.INFO,
        "promote_scan",
        files=len(files),
        transcode_root=str(transcode_root),
        dry_run=args.dry_run,
    )

    promoted = 0
    skipped = 0
    errors = 0

    session = None
    if db_path.exists():
        session = init_db(db_path)

    for source_path in files:
        if ".AV1." in source_path.name or source_path.suffix.lower() == ".av1":
            continue

        encoded_path = get_transcode_output_path(source_path, transcode_root)
        if encoded_path is None:
            skipped += 1
            continue

        dest_path = source_path.parent / encoded_path.name
        log_event(
            logging.INFO,
            "promote_candidate",
            source=str(source_path),
            encoded=str(encoded_path),
            dest=str(dest_path),
        )

        rollback_state = None
        try:
            result, rollback_state = promote_encoded_file_atomic(
                source_path=source_path,
                encoded_path=encoded_path,
                dest_path=dest_path,
                dry_run=args.dry_run,
                move_original_to_backup=args.delete_original,
                rename_sidecars=True,
                verify=True,
                logger=CLI_LOGGER,
            )

            if args.dry_run:
                promoted += 1
                continue

            if session:
                try:
                    media_cols = cast(Any, MediaItem.__table__.c)
                    encode_cols = cast(Any, EncodeResult.__table__.c)
                    now_str = now_iso()
                    item = session.exec(select(MediaItem).where(media_cols.path == str(source_path))).first()
                    if item:
                        item.status = "completed"
                        item.path = str(result.dest_path)
                        item.updated_at = now_str
                        session.add(item)

                    enc = session.exec(
                        select(EncodeResult).where(encode_cols.source_path == str(source_path))
                    ).first()
                    if enc:
                        enc.promoted = True
                        enc.promoted_at = now_str
                        enc.output_path = str(result.dest_path)
                        session.add(enc)

                    session.commit()
                except Exception:
                    session.rollback()
                    if rollback_state:
                        rollback_promote(rollback_state)
                    raise

            promoted += 1
        except Exception as e:
            log_event(logging.ERROR, "promote_item_failed", source=str(source_path), error=str(e))
            errors += 1

    if session:
        session.close()

    log_event(logging.INFO, "promote_summary", promoted=promoted, skipped=skipped, errors=errors)
    return 0 if errors == 0 else 1


def run_purge_backups(args: argparse.Namespace) -> int:
    older_than_days = int(args.older_than_days)
    limit = int(args.limit)
    apply = bool(args.apply)
    dry_run = not apply

    cutoff_dt = datetime.now() - timedelta(days=older_than_days)
    cutoff_iso = cutoff_dt.isoformat()

    db_path = get_db_path()
    if not db_path.exists():
        log_event(logging.INFO, "purge_backups_no_db", db=str(db_path))
        return 0

    session = init_db(db_path)
    try:
        encode_cols = cast(Any, EncodeResult.__table__.c)
        stmt: Any = (
            select(EncodeResult)
            .where(
                encode_cols.promoted.is_(True),
                encode_cols.promoted_at.is_not(None),
                encode_cols.promoted_at < cutoff_iso,
                encode_cols.source_backup_path.is_not(None),
                encode_cols.output_path.is_not(None),
            )
            .order_by(encode_cols.promoted_at)
        )
        candidates: list[EncodeResult] = session.exec(stmt).all()
    finally:
        session.close()

    considered = 0
    eligible = 0
    deleted = 0
    skipped = 0
    errors = 0
    freed_bytes = 0

    for enc in candidates:
        considered += 1
        if limit and deleted >= limit:
            break

        promoted_at_raw = enc.promoted_at
        promoted_at = datetime.fromisoformat(promoted_at_raw) if promoted_at_raw else None
        if promoted_at is None:
            skipped += 1
            continue

        if not enc.output_path or not enc.source_backup_path:
            skipped += 1
            continue

        promoted_path = normalize_path(pathlib.Path(enc.output_path))
        backup_path = normalize_path(pathlib.Path(enc.source_backup_path))

        if not promoted_path.exists() or not backup_path.exists():
            skipped += 1
            continue

        source_name = pathlib.Path(enc.source_path).name
        expected_prefix = f".{source_name}.mediaforce-orig-"
        if not backup_path.name.startswith(expected_prefix):
            skipped += 1
            continue

        eligible += 1
        try:
            size = backup_path.stat().st_size
        except OSError:
            size = 0

        if dry_run:
            log_event(
                logging.INFO,
                "purge_backups_dry_run",
                encode_id=enc.id,
                backup=str(backup_path),
                bytes=size,
            )
            continue

        try:
            backup_path.unlink()
            deleted += 1
            freed_bytes += size
            log_event(
                logging.INFO,
                "purge_backups_deleted",
                encode_id=enc.id,
                backup=str(backup_path),
                bytes=size,
            )
        except OSError as e:
            errors += 1
            log_event(logging.ERROR, "purge_backups_delete_failed", error=str(e))

    log_event(
        logging.INFO,
        "purge_backups_summary",
        deleted=deleted,
        skipped=skipped,
        errors=errors,
        freed_bytes=freed_bytes,
    )
    return 0 if errors == 0 else 1


def run_import_show_config(args: argparse.Namespace) -> int:
    apply = bool(getattr(args, "apply", False))
    dry_run = not apply
    overwrite_existing = bool(getattr(args, "overwrite_existing", False))

    config_path: Optional[pathlib.Path] = None
    if getattr(args, "path", None):
        config_path = pathlib.Path(args.path).expanduser()
    else:
        candidates = [
            pathlib.Path("show_config.json"),
            CONFIG_DIR / "show_config.json",
        ]
        for c in candidates:
            if c.exists():
                config_path = c
                break

    if not config_path or not config_path.exists():
        log_event(logging.ERROR, "show_config_missing")
        return 1

    try:
        with Session(ENGINE) as session:
            result = import_show_config_json(
                session,
                config_path=config_path,
                dry_run=dry_run,
                overwrite_existing=overwrite_existing,
            )
    except Exception as e:
        log_event(logging.ERROR, "show_config_import_failed", error=str(e))
        return 1

    log_event(
        logging.INFO,
        "show_config_import_summary",
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
    )
    return 0


def run_verify_single(args: argparse.Namespace) -> int:
    source_path = pathlib.Path(args.source).resolve()
    encoded_path = pathlib.Path(args.encoded).resolve()

    if not source_path.exists() or not encoded_path.exists():
        log_event(logging.ERROR, "verify_files_missing")
        return 1

    metrics = verify_encode_quality(
        source_path,
        encoded_path,
        sample_duration_sec=args.sample_duration,
        use_vmaf=not args.no_vmaf,
    )

    log_event(
        logging.INFO,
        "verify_result",
        grade=metrics.quality_grade,
        acceptable=bool(metrics.is_acceptable),
        ssim=metrics.ssim,
        psnr=metrics.psnr,
        vmaf=metrics.vmaf,
    )
    return 0


def run_verify_batch(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.path).resolve()
    transcode_root = pathlib.Path(args.transcode_root).resolve()

    if not path.exists():
        return 1

    if path.is_file():
        files = [path]
    else:
        files = []
        for ext in VIDEO_EXTENSIONS:
            files.extend(path.rglob(f"*{ext}"))
        files = sorted(files)

    verified = 0
    failed = 0

    for f in files:
        if ".AV1." in f.name:
            continue
        encoded = get_transcode_output_path(f, transcode_root)
        if not encoded:
            continue

        metrics = verify_encode_quality(
            f,
            encoded,
            sample_duration_sec=args.sample_duration,
            sample_positions=[0.5],
            use_vmaf=not args.no_vmaf,
        )
        if metrics.is_acceptable:
            verified += 1
        else:
            failed += 1

    log_event(logging.INFO, "verify_batch_summary", verified=verified, failed=failed)
    return 0 if failed == 0 else 1


def run_review_list(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.path).resolve()
    library_root = get_library_root(path)
    db_path = get_db_path(library_root)
    
    if not db_path.exists():
        return 1
        
    session = init_db(db_path)

    encode_cols = cast(Any, EncodeResult.__table__.c)
    
    stmt: Any = select(EncodeResult)
    if not args.all:
        stmt = stmt.where(encode_cols.is_outlier.is_(True), encode_cols.review_status == "pending")

    results = session.exec(stmt).all()
    items = []
    for row in results:
        items.append({
            "id": row.id,
            "source": row.source_path,
            "outlier": row.is_outlier,
            "vmaf": row.vmaf,
        })
    
    log_event(logging.INFO, "review_list", count=len(items), items=items)
    return 0


def run_review_approve(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.path).resolve()
    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        log_event(logging.ERROR, "review_db_missing", db=str(db_path))
        return 1

    session = init_db(db_path)
    enc = session.get(EncodeResult, args.id)
    if not enc:
        log_event(logging.ERROR, "review_encode_not_found", id=int(args.id))
        session.close()
        return 1

    enc.review_status = "approved"
    enc.reviewed_at = now_iso()
    session.add(enc)
    session.commit()

    log_event(
        logging.INFO,
        "review_approved",
        id=int(args.id),
        source=str(enc.source_path),
        output=str(enc.output_path) if enc.output_path else None,
    )
    session.close()
    return 0


def run_review_reject(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.path).resolve()
    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        log_event(logging.ERROR, "review_db_missing", db=str(db_path))
        return 1

    session = init_db(db_path)
    enc = session.get(EncodeResult, args.id)
    if not enc:
        log_event(logging.ERROR, "review_encode_not_found", id=int(args.id))
        session.close()
        return 1

    source_path = pathlib.Path(enc.source_path)
    output_path = pathlib.Path(enc.output_path) if enc.output_path else None

    enc.review_status = "rejected"
    enc.reviewed_at = now_iso()
    session.add(enc)
    session.commit()
    session.close()

    deleted = False
    if getattr(args, "delete", False) and output_path and output_path.exists():
        output_path.unlink()
        deleted = True

    log_event(
        logging.INFO,
        "review_rejected",
        id=int(args.id),
        source=str(source_path),
        output=str(output_path) if output_path else None,
        deleted=deleted,
    )
    return 0


def run_compare_clips(args: argparse.Namespace) -> int:
    source_path = pathlib.Path(args.source).resolve()
    encoded_path = pathlib.Path(args.encoded).resolve()

    if not source_path.exists() or not encoded_path.exists():
        log_event(logging.ERROR, "compare_files_missing")
        return 1

    if args.output:
        compare_dir = pathlib.Path(args.output)
    else:
        compare_dir = pathlib.Path("/Volumes/media/transcode/_compare")

    safe_name = re.sub(r"[^\w\-.]", "_", source_path.stem)[:50]
    clip_dir = compare_dir / f"compare_{safe_name}"
    clip_dir.mkdir(parents=True, exist_ok=True)

    source_info = probe_media(source_path)
    if source_info is None:
        log_event(logging.ERROR, "compare_probe_failed", source=str(source_path))
        return 1

    duration = source_info.duration_seconds or 60
    seek_pos = args.seek if args.seek is not None else duration / 2
    clip_duration = args.duration

    if seek_pos + clip_duration > duration:
        seek_pos = max(0, duration - clip_duration - 5)

    log_event(
        logging.INFO,
        "compare_clips_start",
        source=str(source_path),
        encoded=str(encoded_path),
        clip_dir=str(clip_dir),
    )

    source_clip = clip_dir / "source.mp4"
    encoded_clip = clip_dir / "encoded.mp4"

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log_event(logging.ERROR, "compare_ffmpeg_missing")
        return 1

    cmd_source = [
        ffmpeg, "-y", "-ss", str(seek_pos), "-i", str(source_path),
        "-t", str(clip_duration), "-c:v", "copy", "-an", str(source_clip),
    ]
    cmd_encoded = [
        ffmpeg, "-y", "-ss", str(seek_pos), "-i", str(encoded_path),
        "-t", str(clip_duration), "-c:v", "copy", "-an", str(encoded_clip),
    ]

    try:
        subprocess.run(cmd_source, check=True, capture_output=True)
        subprocess.run(cmd_encoded, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        log_event(logging.ERROR, "compare_extract_failed", error=str(e.stderr.decode() if e.stderr else e))
        return 1

    html_file = clip_dir / "compare.html"
    generate_compare_html(source_path, encoded_path, html_file, source_info=source_info)

    log_event(logging.INFO, "compare_ready", html=str(html_file))
    if platform.system() == "Darwin":
        try:
            subprocess.run(["open", str(html_file)], check=False)
        except Exception:
            pass
    return 0


def run_compare_full(args: argparse.Namespace) -> int:
    source_path = pathlib.Path(args.source).resolve()
    encoded_path = pathlib.Path(args.encoded).resolve()

    if not source_path.exists() or not encoded_path.exists():
        return 1

    if args.output:
        compare_dir = pathlib.Path(args.output)
    else:
        compare_dir = pathlib.Path("/Volumes/media/transcode/_compare")
    compare_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^\w\-.]", "_", source_path.stem)[:50]
    html_file = compare_dir / f"compare_{safe_name}.html"

    source_info = probe_media(source_path)
    generate_compare_html(source_path, encoded_path, html_file, source_info=source_info)

    log_event(logging.INFO, "compare_ready", html=str(html_file))
    if platform.system() == "Darwin":
        try:
            subprocess.run(["open", str(html_file)], check=False)
        except Exception:
            pass
    return 0


def run_review_compare(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.path).resolve()
    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        return 1

    session = init_db(db_path)
    enc = session.get(EncodeResult, args.id)
    if not enc:
        session.close()
        return 1

    source_path = pathlib.Path(enc.source_path)
    output_path = pathlib.Path(enc.output_path) if enc.output_path else None
    session.close()

    if not source_path.exists() or not output_path or not output_path.exists():
        return 1

    if args.output:
        compare_dir = pathlib.Path(args.output)
    else:
        compare_dir = pathlib.Path("/tmp/av1_compare")
    compare_dir.mkdir(parents=True, exist_ok=True)

    compare_file = compare_dir / f"compare_{args.id}_{source_path.stem}.mp4"
    source_info = probe_media(source_path)
    if source_info is None:
        return 1
    duration = source_info.duration_seconds or 60
    seek_pos = args.seek if args.seek is not None else duration / 2
    clip_duration = args.duration

    cmd = [
        "ffmpeg", "-y", "-ss", str(seek_pos), "-i", str(source_path),
        "-ss", str(seek_pos), "-i", str(output_path), "-t", str(clip_duration),
        "-filter_complex",
        "[0:v]drawtext=text='SOURCE':fontsize=36:fontcolor=white:borderw=2:bordercolor=black:x=10:y=10[top];"
        "[1:v]drawtext=text='ENCODED':fontsize=36:fontcolor=white:borderw=2:bordercolor=black:x=10:y=10[bottom];"
        "[top][bottom]vstack=inputs=2[v]",
        "-map", "[v]", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        str(compare_file),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        log_event(logging.INFO, "compare_video_ready", output=str(compare_file))
    except subprocess.CalledProcessError as e:
        log_event(logging.ERROR, "compare_video_failed", error=str(e.stderr.decode() if e.stderr else e))
        return 1

    return 0
