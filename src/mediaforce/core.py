#!/usr/bin/env python3
# mypy: ignore-errors
import argparse
import logging
import pathlib
import platform
import shutil
import subprocess
import sys
from typing import Any, Dict

from mediaforce.config.logging import (
    configure_logging,
    env_log_config,
    log_event as _structured_log_event,
)
from mediaforce.config.paths import default_transcode_root, get_media_roots

configure_logging(env_log_config(component="mediaforce"))
CLI_LOGGER = configure_logging(env_log_config(component="mediaforce.cli"))


def log_event(level: int, message: str, **fields: Any) -> None:
    _structured_log_event(level, message, logger=CLI_LOGGER, **fields)


def log_info(message: str, **fields: Any) -> None:
    log_event(logging.INFO, message, **fields)


def log_warn(message: str, **fields: Any) -> None:
    log_event(logging.WARNING, message, **fields)


def log_error(message: str, **fields: Any) -> None:
    log_event(logging.ERROR, message, **fields)


def detect_platform() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "system": platform.system(),
        "machine": platform.machine(),
        "hostname": platform.node(),
        "has_nvidia": False,
        "media_roots": [],
    }

    if info["system"] == "Linux":
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                info["has_nvidia"] = True
                info["nvidia_gpus"] = [g.strip() for g in result.stdout.strip().split("\n")]
        except FileNotFoundError:
            pass

    for root in get_media_roots():
        if pathlib.Path(root).exists():
            info["media_roots"].append(root)

    return info


def find_ffmpeg() -> str | None:
    for candidate in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"]:
        if shutil.which(candidate):
            return candidate
    return None


def cmd_status(args: argparse.Namespace) -> int:
    info = detect_platform()

    log_info("platform_status", system=info['system'], machine=info['machine'], hostname=info['hostname'])

    roots = info.get("media_roots", [])
    if roots:
        log_info("media_roots", roots=roots)
    else:
        log_warn("media_roots_missing", expected=list(get_media_roots()))

    if info["system"] == "Darwin":
        if info["machine"] == "arm64":
            log_info("hardware_gpu", type="apple_silicon")
        else:
            log_info("hardware_gpu", type="intel_mac")
    elif info.get("has_nvidia"):
        log_info("hardware_gpu", type="nvidia", gpus=info.get("nvidia_gpus", []))
    else:
        log_info("hardware_gpu", type="none")

    ffmpeg = find_ffmpeg()
    if ffmpeg:
        result = subprocess.run(
            [ffmpeg, "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        )
        svt = "libsvtav1" in result.stdout
        log_info("ffmpeg_status", path=ffmpeg, svt_av1=svt)
    else:
        log_error("ffmpeg_missing", stage="status")

    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_analyze
    return run_analyze(args.path)


def cmd_scan(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_scan
    return run_scan(args)


def cmd_queue(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_queue
    return run_queue(args)


def cmd_promote(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_promote
    return run_promote(args)


def cmd_purge_backups(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_purge_backups
    return run_purge_backups(args)


def cmd_import_show_config(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_import_show_config
    return run_import_show_config(args)


def cmd_verify(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_verify_single
    return run_verify_single(args)


def cmd_verify_batch(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_verify_batch
    return run_verify_batch(args)


def cmd_review_list(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_review_list
    return run_review_list(args)


def cmd_review_approve(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_review_approve
    return run_review_approve(args)


def cmd_review_reject(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_review_reject
    return run_review_reject(args)


def cmd_review_compare(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_review_compare
    return run_review_compare(args)


def cmd_compare_clips(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_compare_clips
    return run_compare_clips(args)


def cmd_compare_full(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_compare_full
    return run_compare_full(args)


def cmd_watch(args: argparse.Namespace) -> int:
    from mediaforce.services.worker_service import run_watch
    return run_watch(
        autoupdate_url=args.autoupdate_url,
        autoupdate_interval=args.autoupdate_interval,
        settings_url=args.settings_url,
    )


def cmd_run(args: argparse.Namespace) -> int:
    from mediaforce.services.worker_service import run_worker_loop
    return run_worker_loop(
        path_str=args.path,
        output_dir_str=args.output,
        until=args.until,
        dry_run=args.dry_run,
        force=args.force,
        verify=args.verify,
        verify_duration=args.verify_duration,
        sample_vmaf=args.sample_vmaf,
        sample_count=args.sample_count,
        sample_length=args.sample_length,
        sample_motion_aware=args.sample_motion_aware,
        hw_decode=args.hw_decode,
        hw_encode=args.hw_encode,
        offpeak_enabled=args.offpeak_enabled,
        offpeak_start=args.offpeak_start,
        offpeak_end=args.offpeak_end,
        autoupdate_url=args.autoupdate_url,
        autoupdate_interval=args.autoupdate_interval,
        api_url=args.api_url,
        settings_url=args.settings_url,
        profile_settings_url=args.profile_settings_url,
        max_concurrency=args.max_concurrency,
    )


def cmd_encode(args: argparse.Namespace) -> int:
    from mediaforce.services.orchestrator import run_encode_batch
    return run_encode_batch(
        path_str=args.path,
        output_dir_str=args.output,
        tier_override=args.tier,
        force=args.force,
        dry_run=args.dry_run,
        hw_decode=args.hw_decode,
        hw_encode=args.hw_encode,
        sample_vmaf_enabled=args.sample_vmaf,
        sample_count=args.sample_count,
        sample_length=args.sample_length,
        sample_motion_aware=args.sample_motion_aware,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mediaforce content-aware encoder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_status = subparsers.add_parser("status", help="Show platform info")
    p_status.set_defaults(func=cmd_status)

    p_analyze = subparsers.add_parser("analyze", help="Analyze media files")
    p_analyze.add_argument("path", help="Video file or directory")
    p_analyze.set_defaults(func=cmd_analyze)

    p_scan = subparsers.add_parser("scan", help="Scan library")
    p_scan.add_argument("path", help="Library path")
    p_scan.set_defaults(func=cmd_scan)

    p_queue = subparsers.add_parser("queue", help="Show queue")
    p_queue.add_argument("path", help="Library path")
    p_queue.add_argument("--limit", type=int, default=20)
    p_queue.set_defaults(func=cmd_queue)

    p_run = subparsers.add_parser("run", help="Run worker")
    p_run.add_argument("path", help="Library path")
    p_run.add_argument("-o", "--output", default=default_transcode_root())
    p_run.add_argument("--until")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--force", action="store_true")
    p_run.add_argument("--verify", action="store_true")
    p_run.add_argument("--verify-duration", type=int, default=60)
    p_run.add_argument("--sample-vmaf", dest="sample_vmaf", action="store_true", default=True)
    p_run.add_argument("--no-sample-vmaf", dest="sample_vmaf", action="store_false")
    p_run.add_argument("--sample-count", type=int, default=3)
    p_run.add_argument("--sample-length", type=float, default=8.0)
    p_run.add_argument("--sample-motion-aware", dest="sample_motion_aware", action="store_true", default=True)
    p_run.add_argument("--no-sample-motion-aware", dest="sample_motion_aware", action="store_false")
    p_run.add_argument("--max-concurrency", type=int)
    p_run.add_argument("--hw-decode", dest="hw_decode", action="store_true", default=True)
    p_run.add_argument("--no-hw-decode", dest="hw_decode", action="store_false")
    p_run.add_argument("--hw-encode", dest="hw_encode", action="store_true", default=False)
    p_run.add_argument("--no-hw-encode", dest="hw_encode", action="store_false")
    p_run.add_argument("--offpeak", dest="offpeak_enabled", action="store_true")
    p_run.add_argument("--offpeak-start", type=str)
    p_run.add_argument("--offpeak-end", type=str)
    p_run.add_argument("--autoupdate-url")
    p_run.add_argument("--autoupdate-interval", type=int, default=0)
    p_run.add_argument("--api-url", dest="api_url")
    p_run.add_argument("--settings-url")
    p_run.add_argument("--profile-settings-url", dest="profile_settings_url")
    p_run.set_defaults(func=cmd_run)

    p_watch = subparsers.add_parser("watch", help="Watch libraries")
    p_watch.add_argument("--autoupdate-url")
    p_watch.add_argument("--autoupdate-interval", type=int, default=3600)
    p_watch.add_argument("--settings-url")
    p_watch.set_defaults(func=cmd_watch)

    p_encode = subparsers.add_parser("encode", help="Encode files")
    p_encode.add_argument("path", help="Video file or directory")
    p_encode.add_argument("-o", "--output", required=True)
    p_encode.add_argument("--tier", choices=["pristine", "good", "mediocre", "poor"])
    p_encode.add_argument("--dry-run", action="store_true")
    p_encode.add_argument("--force", action="store_true")
    p_encode.add_argument("--sample-vmaf", dest="sample_vmaf", action="store_true", default=True)
    p_encode.add_argument("--no-sample-vmaf", dest="sample_vmaf", action="store_false")
    p_encode.add_argument("--sample-count", type=int, default=3)
    p_encode.add_argument("--sample-length", type=float, default=8.0)
    p_encode.add_argument("--sample-motion-aware", dest="sample_motion_aware", action="store_true", default=True)
    p_encode.add_argument("--no-sample-motion-aware", dest="sample_motion_aware", action="store_false")
    p_encode.add_argument("--hw-decode", dest="hw_decode", action="store_true", default=True)
    p_encode.add_argument("--no-hw-decode", dest="hw_decode", action="store_false")
    p_encode.add_argument("--hw-encode", dest="hw_encode", action="store_true", default=False)
    p_encode.add_argument("--no-hw-encode", dest="hw_encode", action="store_false")
    p_encode.set_defaults(func=cmd_encode)

    p_promote = subparsers.add_parser("promote", help="Promote encoded files")
    p_promote.add_argument("path", help="Library path")
    p_promote.add_argument("--transcode-root", default=default_transcode_root())
    p_promote.add_argument("--dry-run", action="store_true")
    p_promote.add_argument("--delete-original", action="store_true", default=True)
    p_promote.add_argument("--no-delete", action="store_false", dest="delete_original")
    p_promote.set_defaults(func=cmd_promote)

    p_purge = subparsers.add_parser("purge-backups", help="Purge backups")
    p_purge.add_argument("--older-than-days", type=int, default=30)
    p_purge.add_argument("--limit", type=int, default=0)
    p_purge.add_argument("--apply", action="store_true")
    p_purge.set_defaults(func=cmd_purge_backups)

    p_import = subparsers.add_parser("import-show-config", help="Import show config")
    p_import.add_argument("--path")
    p_import.add_argument("--apply", action="store_true")
    p_import.add_argument("--overwrite-existing", dest="overwrite_existing", action="store_true")
    p_import.set_defaults(func=cmd_import_show_config)

    p_verify = subparsers.add_parser("verify", help="Verify single file")
    p_verify.add_argument("source")
    p_verify.add_argument("encoded")
    p_verify.add_argument("--sample-duration", type=int, default=30)
    p_verify.add_argument("--no-vmaf", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    p_verify_batch = subparsers.add_parser("verify-batch", help="Batch verify")
    p_verify_batch.add_argument("path")
    p_verify_batch.add_argument("--transcode-root", default=default_transcode_root())
    p_verify_batch.add_argument("--sample-duration", type=int, default=30)
    p_verify_batch.add_argument("--no-vmaf", action="store_true")
    p_verify_batch.set_defaults(func=cmd_verify_batch)

    p_review_list = subparsers.add_parser("review-list", help="List review items")
    p_review_list.add_argument("path")
    p_review_list.add_argument("--all", action="store_true")
    p_review_list.add_argument("-v", "--verbose", action="store_true")
    p_review_list.set_defaults(func=cmd_review_list)

    p_review_approve = subparsers.add_parser("review-approve", help="Approve item")
    p_review_approve.add_argument("path")
    p_review_approve.add_argument("id", type=int)
    p_review_approve.set_defaults(func=cmd_review_approve)

    p_review_reject = subparsers.add_parser("review-reject", help="Reject item")
    p_review_reject.add_argument("path")
    p_review_reject.add_argument("id", type=int)
    p_review_reject.add_argument("--delete", action="store_true")
    p_review_reject.set_defaults(func=cmd_review_reject)

    p_review_compare = subparsers.add_parser("review-compare", help="Compare item")
    p_review_compare.add_argument("path")
    p_review_compare.add_argument("id", type=int)
    p_review_compare.add_argument("-o", "--output")
    p_review_compare.add_argument("--seek", type=float)
    p_review_compare.add_argument("--duration", type=int, default=10)
    p_review_compare.set_defaults(func=cmd_review_compare)

    p_compare_clips = subparsers.add_parser("compare-clips", help="Compare clips")
    p_compare_clips.add_argument("source")
    p_compare_clips.add_argument("encoded")
    p_compare_clips.add_argument("-o", "--output")
    p_compare_clips.add_argument("--seek", type=float)
    p_compare_clips.add_argument("--duration", type=int, default=30)
    p_compare_clips.set_defaults(func=cmd_compare_clips)

    p_compare_full = subparsers.add_parser("compare-full", help="Compare full")
    p_compare_full.add_argument("source")
    p_compare_full.add_argument("encoded")
    p_compare_full.add_argument("-o", "--output")
    p_compare_full.set_defaults(func=cmd_compare_full)

    return parser


def main() -> int:
    from mediaforce.config.dotenv import load_dotenv_if_present

    load_dotenv_if_present()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
