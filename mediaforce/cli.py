import argparse
import json
import os
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import select

from mediaforce.core.config import DEFAULT_CONFIG_PATH, MediaforceConfig, load_config
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import run_manifests as run_manifests_table
from mediaforce.execution import describe_item_plan, encode_manifest_items, promote_manifest_items, \
    validate_manifest_items
from mediaforce.encoding.bakeoff import DEFAULT_BAKEOFF_ENGINES, build_bakeoff_plan, write_bakeoff_plan
from mediaforce.library.folder_profiles import inspect_prefix
from mediaforce.library.candidate_selection import scope_target_size_blocker
from mediaforce.library.evidence_queue import DEFAULT_EVIDENCE_BATCH_LIMIT, EvidenceQueueConflict, \
    cancel_evidence_queue, evidence_queue_summary, pause_evidence_queue, resume_evidence_queue, \
    start_evidence_work
from mediaforce.library.evidence_state import EVIDENCE_KINDS
from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked
from mediaforce.library.planner import recommend_item
from mediaforce.library.run_manifests import build_run_manifest as build_db_run_manifest, \
    select_candidates as select_run_manifest_candidates, \
    select_encode_candidates as select_run_manifest_encode_candidates, \
    write_manifest as write_run_manifest
from mediaforce.library.scanner import scan_library
from mediaforce.library.metadata_sync import sync_external_metadata
from mediaforce.review import generate_compare_clips
from mediaforce.state_cleanup import purge_transient_artifacts


class TargetSizePreflightBlocked(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manifest-driven AV1 planning for mediaforce")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to the TOML config file")

    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan source roots into SQLite state")
    scan_parser.add_argument("--prefix", action="append", default=[],
                             help="Restrict scan to rel-path prefixes such as tv/Futurama")
    scan_parser.add_argument("--limit", type=int, help="Only scan the first N matching files")

    evidence_parser = subparsers.add_parser(
        "evidence",
        help="Manage explicit bounded cadence and fingerprint evidence work",
    )
    evidence_actions = evidence_parser.add_subparsers(dest="evidence_action", required=True)
    evidence_start_parser = evidence_actions.add_parser(
        "start",
        help="Create a paused evidence batch for one item, folder, or root",
    )
    evidence_start_parser.add_argument("prefix", help="Explicit scope such as tv/Show/Season 1 or movies")
    evidence_start_parser.add_argument(
        "--kind",
        action="append",
        choices=EVIDENCE_KINDS,
        default=[],
        help="Evidence kind to include; defaults to cadence and fingerprint",
    )
    evidence_start_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_EVIDENCE_BATCH_LIMIT,
        help=f"Maximum per-kind work units to queue (default: {DEFAULT_EVIDENCE_BATCH_LIMIT})",
    )
    evidence_actions.add_parser("status", help="Show the current evidence batch")
    evidence_actions.add_parser("pause", help="Prevent new evidence claims")
    evidence_actions.add_parser("resume", help="Allow claims from the paused evidence batch")
    evidence_actions.add_parser("cancel", help="Cancel queued work and stop the active managed process")
    evidence_run_parser = evidence_actions.add_parser(
        "run",
        help="Run a bounded foreground worker pass and exit",
    )
    evidence_run_parser.add_argument(
        "--max-items",
        type=int,
        default=1,
        help="Maximum work units to process before exiting (default: 1)",
    )
    evidence_run_parser.add_argument(
        "--max-seconds",
        type=float,
        help="Stop claiming new work after this foreground time budget",
    )

    report_parser = subparsers.add_parser("report", help="Show prioritized candidates from SQLite state")
    report_parser.add_argument("--limit", type=int, default=20, help="Number of rows to show")
    report_parser.add_argument("--status", action="append", default=None, help="Statuses to include")
    report_parser.add_argument("--prefix", action="append", default=[], help="Restrict report to rel-path prefixes")

    plan_parser = subparsers.add_parser("plan", help="Generate a run manifest from current state")
    plan_parser.add_argument("--limit", type=int, help="Maximum items in the run manifest")
    plan_parser.add_argument("--prefix", action="append", default=[], help="Restrict plan to rel-path prefixes")
    plan_parser.add_argument("--bucket", action="append", default=[], help="Restrict plan to recommendation buckets")
    plan_parser.add_argument("--output", type=Path, help="Write the manifest to an explicit path")

    inspect_parser = subparsers.add_parser("inspect-folder",
                                           help="Summarize one folder prefix and print a suggested override block")
    inspect_parser.add_argument("prefix", help="Folder prefix like tv/Suits or tv/Suits/Season 5")

    campaign_parser = subparsers.add_parser("campaign",
                                            help="Scan a folder, print its summary, and create a run manifest")
    campaign_parser.add_argument("prefix", help="Folder prefix like tv/Suits/Season 5")
    campaign_parser.add_argument("--limit", type=int, help="Maximum items to put in the run manifest")
    campaign_parser.add_argument("--bucket", action="append", default=[],
                                 help="Restrict campaign items to recommendation buckets")
    campaign_parser.add_argument("--output", type=Path, help="Write the manifest to an explicit path")
    campaign_parser.add_argument("--review-first", action="store_true",
                                 help="Immediately encode, validate, and compare the first item")
    campaign_parser.add_argument("--play", action="store_true",
                                 help="Open the first generated compare clip when reviewing")

    run_parser = subparsers.add_parser("run",
                                       help="Start a folder run, review the first item, and print the next approval step")
    run_parser.add_argument("prefix", help="Folder prefix like tv/Suits/Season 5")
    run_parser.add_argument("--limit", type=int, help="Maximum items to put in the run manifest")
    run_parser.add_argument("--bucket", action="append", default=[],
                            help="Restrict run items to recommendation buckets")
    run_parser.add_argument("--output", type=Path, help="Write the manifest to an explicit path")
    run_parser.add_argument("--duration", type=float, default=8.0, help="Clip duration in seconds")
    run_parser.add_argument("--play", action="store_true", help="Open the first generated compare clip")

    review_parser = subparsers.add_parser("review",
                                          help="Encode, validate, and compare one manifest item in a single command")
    review_parser.add_argument("manifest", nargs="?", type=Path,
                               help="Path to a run manifest JSON file (defaults to latest)")
    review_parser.add_argument("--index", type=int, default=0, help="Manifest item index to review")
    _add_compare_clip_args(review_parser)

    encode_parser = subparsers.add_parser("encode", help="Encode manifest items into the staging root")
    _add_manifest_selection_args(encode_parser, require_manifest=False)
    encode_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing staged file")

    validate_parser = subparsers.add_parser("validate", help="Run machine validation on staged outputs")
    _add_manifest_selection_args(validate_parser, require_manifest=False)

    promote_parser = subparsers.add_parser("promote", help="Promote validated staged outputs into the library")
    _add_manifest_selection_args(promote_parser, require_manifest=False)
    promote_parser.add_argument("--force", action="store_true", help="Promote even if machine validation failed")

    approve_parser = subparsers.add_parser("approve", help="Promote the reviewed item from the latest manifest")
    approve_parser.add_argument("manifest", nargs="?", type=Path,
                                help="Path to a run manifest JSON file (defaults to latest)")
    approve_parser.add_argument("--index", action="append", type=int, default=[],
                                help="Manifest item indexes to promote")
    approve_parser.add_argument("--all", action="store_true", help="Promote all manifest items")
    approve_parser.add_argument("--force", action="store_true", help="Promote even if machine validation failed")

    compare_parser = subparsers.add_parser("compare", help="Generate side-by-side approval clips for staged items")
    _add_manifest_selection_args(compare_parser, require_manifest=False)
    _add_compare_clip_args(compare_parser)

    bakeoff_parser = subparsers.add_parser("bakeoff", help="Write a scene-aware engine bakeoff plan")
    _add_manifest_selection_args(bakeoff_parser, require_manifest=False)
    bakeoff_parser.add_argument(
        "--engine",
        action="append",
        choices=DEFAULT_BAKEOFF_ENGINES,
        default=[],
        help="Candidate engine to include; defaults to all candidates",
    )
    bakeoff_parser.add_argument(
        "--output",
        type=Path,
        help="Write the bakeoff plan JSON to an explicit path",
    )
    bakeoff_parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="Directory where bakeoff artifacts should be written by the candidate commands",
    )
    bakeoff_parser.add_argument(
        "--clip-duration",
        type=float,
        default=20.0,
        help="Review clip duration expected from each candidate engine",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    purge_transient_artifacts(config)
    default_review_dir = config.paths.review_dir

    if args.command == "evidence":
        return _run_evidence_command(config, args)

    with open_db(config.paths.db_path) as connection:
        if args.command == "scan":
            stats = scan_library(connection, config, prefixes=args.prefix or None, limit=args.limit)
            metadata_status = None
            if not args.prefix and args.limit is None:
                metadata_status = sync_external_metadata(connection, config).status
            print(
                f"scan_id={stats.scan_id} discovered={stats.discovered} reprobed={stats.reprobed} "
                f"unchanged={stats.unchanged} missing={stats.missing} total_seen={stats.total_seen}"
                f"{f' metadata={metadata_status}' if metadata_status else ''}"
            )
            return 0

        if args.command == "report":
            rows = _select_candidates(connection, config, args.status or ["discovered", "planned"], args.prefix,
                                      args.limit)
            _print_report(rows, config)
            return 0

        if args.command == "plan":
            blocker = _target_size_blocker_for_prefixes(connection, config, args.prefix)
            if blocker is not None:
                print(f"Plan blocked: {blocker}")
                return 2
            rows = _select_encode_candidates(
                connection,
                config,
                prefixes=args.prefix,
                limit=args.limit or int(config.planning.get("default_limit", 20)),
                buckets=args.bucket or None,
            )
            manifest = _build_run_manifest(rows, config)
            output_path = _write_manifest(connection, config, manifest, args.output)
            print(f"wrote {len(manifest['items'])} items to {output_path}")
            return 0

        if args.command == "inspect-folder":
            summary = inspect_prefix(connection, config, args.prefix)
            _print_folder_summary(summary)
            return 0

        if args.command == "campaign":
            try:
                manifest, output_path = _create_campaign_manifest(
                    connection,
                    config,
                    prefix=args.prefix,
                    limit=args.limit,
                    buckets=args.bucket or None,
                    output_path=args.output,
                )
            except TargetSizePreflightBlocked as exc:
                print(f"Campaign blocked: {exc}")
                return 2
            print(f"\nCampaign manifest: {output_path}")
            if manifest["items"]:
                print("\nFirst item plan:")
                _print_manifest_item(manifest["items"][0], 0)
                if args.review_first:
                    print("\nRunning first-item review...")
                    _run_review(
                        connection,
                        config,
                        output_path,
                        manifest,
                        index=0,
                        overwrite=True,
                        duration=args.__dict__.get("duration", 8.0),
                        timestamps=None,
                        output_dir=default_review_dir,
                        play=args.play,
                    )
                else:
                    print("\nNext step: uv run mediaforce review --play")
            return 0

        if args.command == "run":
            try:
                manifest, output_path = _create_campaign_manifest(
                    connection,
                    config,
                    prefix=args.prefix,
                    limit=args.limit,
                    buckets=args.bucket or None,
                    output_path=args.output,
                )
            except TargetSizePreflightBlocked as exc:
                print(f"Run blocked: {exc}")
                return 2
            print(f"\nRun manifest: {output_path}")
            if not manifest["items"]:
                print("No items selected for this run.")
                return 0
            print("\nFirst item plan:")
            _print_manifest_item(manifest["items"][0], 0)
            print("\nRunning first-item review...")
            _run_review(
                connection,
                config,
                output_path,
                manifest,
                index=0,
                overwrite=True,
                duration=args.duration,
                timestamps=None,
                output_dir=default_review_dir,
                play=args.play,
            )
            print("\nNext step: uv run mediaforce approve")
            return 0

        if args.command == "review":
            manifest_path = _resolve_manifest_path(connection, args.manifest)
            manifest = _load_manifest(manifest_path)
            _run_review(
                connection,
                config,
                manifest_path,
                manifest,
                index=args.index,
                overwrite=True,
                duration=args.duration,
                timestamps=args.timestamp or None,
                output_dir=args.output_dir or default_review_dir,
                play=args.play,
            )
            return 0

        if args.command == "encode":
            manifest_path = _resolve_manifest_path(connection, args.manifest)
            manifest = _load_manifest(manifest_path)
            indexes = _resolve_indexes(manifest, args)
            encode_results = encode_manifest_items(connection, config, manifest_path, manifest, indexes,
                                                   overwrite=args.overwrite,
                                                   encode_context={"origin": "cli", "owner_pid": os.getpid()})
            for result in encode_results:
                percent = _percent_string(result.staging_size_bytes, result.source_size_bytes)
                print(
                    f"encoded {result.staging_path} crf={result.chosen_crf} "
                    f"{result.quality_metric}={result.quality_score:.2f} "
                    f"size={_format_size(result.staging_size_bytes)} "
                    f"saved={_format_signed_bytes(result.source_size_bytes - result.staging_size_bytes)} ({percent})"
                )
            return 0

        if args.command == "validate":
            manifest_path = _resolve_manifest_path(connection, args.manifest)
            manifest = _load_manifest(manifest_path)
            indexes = _resolve_indexes(manifest, args)
            validation_results = validate_manifest_items(connection, config, manifest, indexes)
            for idx, validation_result in zip(indexes, validation_results, strict=True):
                status = "passed" if validation_result["passed"] else "failed"
                print(
                    f"item {idx}: validation {status} "
                    f"source={_format_size(validation_result['source_size_bytes'])} "
                    f"staged={_format_size(validation_result['staged_size_bytes'])} "
                    f"saved={_format_signed_bytes(validation_result['bytes_saved'])}"
                )
                for check in validation_result["checks"]:
                    prefix = "ok" if check["passed"] else "bad"
                    print(f"  {prefix}: {check['message']}")
            return 0

        if args.command == "promote":
            manifest_path = _resolve_manifest_path(connection, args.manifest)
            manifest = _load_manifest(manifest_path)
            indexes = _resolve_indexes(manifest, args)
            paths = promote_manifest_items(connection, config, manifest, indexes, force=args.force)
            for path in paths:
                print(f"promoted {path}")
            return 0

        if args.command == "approve":
            manifest_path = _resolve_manifest_path(connection, args.manifest)
            manifest = _load_manifest(manifest_path)
            indexes = _resolve_approve_indexes(manifest, args)
            paths = promote_manifest_items(connection, config, manifest, indexes, force=args.force)
            for path in paths:
                print(f"approved {path}")
            return 0

        if args.command == "compare":
            manifest_path = _resolve_manifest_path(connection, args.manifest)
            manifest = _load_manifest(manifest_path)
            indexes = _resolve_indexes(manifest, args)
            clips = generate_compare_clips(
                connection,
                manifest,
                indexes,
                output_dir=args.output_dir or default_review_dir,
                duration_seconds=args.duration,
                timestamps=args.timestamp or None,
                play=args.play,
            )
            for clip in clips:
                print(
                    f"compare {clip.output_path} start={clip.timestamp_seconds:.2f}s "
                    f"duration={clip.duration_seconds:.2f}s"
                )
            return 0

        if args.command == "bakeoff":
            manifest_path = _resolve_manifest_path(connection, args.manifest)
            manifest = _load_manifest(manifest_path)
            indexes = _resolve_indexes(manifest, args)
            artifact_dir = args.artifact_dir or config.paths.review_dir / "engine-bakeoff"
            plan = build_bakeoff_plan(
                config,
                manifest,
                indexes=indexes,
                engines=args.engine or None,
                output_dir=artifact_dir,
                clip_duration_seconds=args.clip_duration,
            )
            output_path = args.output or config.paths.run_manifest_dir / f"bakeoff-{manifest.get('run_id', 'latest')}.json"
            write_bakeoff_plan(plan, output_path)
            print(f"bakeoff plan {output_path}")
            for item in plan["items"]:
                print(
                    f"  item {item['index']}: target={_format_optional_size(item['target_size_bytes'])} "
                    f"runtime={item['duration_seconds']:.0f}s engines={len(item['engines'])}"
                )
            return 0

    return 1


def _run_evidence_command(config: MediaforceConfig, args: argparse.Namespace) -> int:
    action = str(args.evidence_action)
    try:
        if action == "run":
            summary = run_evidence_queue_until_blocked(
                config_path=config.paths.config_path,
                max_work_items=args.max_items,
                max_seconds=args.max_seconds,
            )
        else:
            with open_db(config.paths.db_path) as connection:
                if action == "start":
                    summary = start_evidence_work(
                        connection,
                        config,
                        args.prefix,
                        evidence_kinds=args.kind or None,
                        limit=args.limit,
                    )
                elif action == "status":
                    summary = evidence_queue_summary(connection)
                elif action == "pause":
                    summary = pause_evidence_queue(connection)
                elif action == "resume":
                    summary = resume_evidence_queue(connection)
                elif action == "cancel":
                    summary = cancel_evidence_queue(connection)
                else:
                    raise ValueError(f"Unsupported evidence action: {action}")
    except (EvidenceQueueConflict, ValueError) as exc:
        print(f"Evidence command blocked: {exc}")
        return 2
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 2 if action == "run" and summary.get("status") == "completed_with_errors" else 0


def _add_manifest_selection_args(parser: argparse.ArgumentParser, *, require_manifest: bool = True) -> None:
    manifest_kwargs: dict[str, Any] = {"type": Path, "help": "Path to a run manifest JSON file"}
    if not require_manifest:
        manifest_kwargs["nargs"] = "?"
        manifest_kwargs["help"] = "Path to a run manifest JSON file (defaults to latest)"
    parser.add_argument("manifest", **manifest_kwargs)
    parser.add_argument("--index", action="append", type=int, default=[], help="Manifest item indexes to process")
    parser.add_argument("--all", action="store_true", help="Process all manifest items")


def _add_compare_clip_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--duration", type=float, default=8.0, help="Clip duration in seconds")
    parser.add_argument("--timestamp", action="append", type=float, default=[],
                        help="Clip start timestamps in seconds")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for generated compare clips",
    )
    parser.add_argument("--play", action="store_true", help="Open the first generated clip in ffplay")


def _select_candidates(
        connection: DBClient,
        config: MediaforceConfig,
        statuses: list[str],
        prefixes: list[str],
        limit: int | None,
        buckets: list[str] | None = None,
) -> list[dict[str, Any]]:
    return select_run_manifest_candidates(
        connection,
        config,
        statuses=statuses,
        prefixes=prefixes,
        limit=limit,
        buckets=buckets,
    )


def _select_encode_candidates(
        connection: DBClient,
        config: MediaforceConfig,
        prefixes: list[str],
        limit: int | None,
        buckets: list[str] | None = None,
) -> list[dict[str, Any]]:
    return select_run_manifest_encode_candidates(
        connection,
        config,
        prefixes=prefixes,
        limit=limit,
        buckets=buckets,
    )


def _print_report(rows: list[dict[str, Any]], config: MediaforceConfig) -> None:
    if not rows:
        print("No matching items found.")
        return

    for row in rows:
        recommendation = recommend_item(row, config)
        size_gib = float(row["size_bytes"]) / (1024 ** 3)
        print(
            f"[{recommendation.bucket:16}] score={recommendation.score:6.2f} size={size_gib:5.2f}GiB "
            f"codec={row['video_codec'] or 'unknown':5} status={row['status']:10} path={row['rel_path']}"
        )
        if row.get("staging_size_bytes"):
            staged_size = int(row["staging_size_bytes"])
            print(
                f"  staged={_format_size(staged_size)} saved={_format_signed_bytes(int(row['size_bytes']) - staged_size)} "
                f"metric={row.get('quality_metric') or 'n/a'} score={row.get('quality_score') or 'n/a'}"
            )
            if row.get("validation_json"):
                validation = json.loads(row["validation_json"])
                print(f"  validation={'passed' if validation.get('passed') else 'failed'}")
        print(f"  {recommendation.reason}")


def _build_run_manifest(rows: list[dict[str, Any]], config: MediaforceConfig) -> dict[str, Any]:
    return build_db_run_manifest(rows, config)


def _write_manifest(
        connection: DBClient,
        config: MediaforceConfig,
        manifest: dict[str, Any],
        output_path: Path | None,
) -> Path:
    return write_run_manifest(connection, config, manifest, output_path)


def _load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _create_campaign_manifest(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        prefix: str,
        limit: int | None,
        buckets: list[str] | None,
        output_path: Path | None,
) -> tuple[dict[str, Any], Path]:
    stats = scan_library(connection, config, prefixes=[prefix])
    print(
        f"scan_id={stats.scan_id} discovered={stats.discovered} reprobed={stats.reprobed} "
        f"unchanged={stats.unchanged} total_seen={stats.total_seen}"
    )
    summary = inspect_prefix(connection, config, prefix)
    _print_folder_summary(summary)
    blocker = scope_target_size_blocker(connection, config, prefix)
    if blocker is not None:
        raise TargetSizePreflightBlocked(blocker.message)
    rows = _select_encode_candidates(
        connection,
        config,
        prefixes=[prefix],
        limit=limit,
        buckets=buckets,
    )
    manifest = _build_run_manifest(rows, config)
    manifest_path = _write_manifest(connection, config, manifest, output_path)
    return manifest, manifest_path


def _target_size_blocker_for_prefixes(
        connection: DBClient,
        config: MediaforceConfig,
        prefixes: list[str],
) -> str | None:
    for prefix in prefixes:
        blocker = scope_target_size_blocker(connection, config, prefix)
        if blocker is not None:
            return blocker.message
    return None


def _resolve_manifest_path(connection: DBClient, manifest_path: Path | None) -> Path:
    if manifest_path is not None:
        return manifest_path
    row = connection.execute(
        select(run_manifests_table.c.output_path)
        .order_by(run_manifests_table.c.created_at.desc())
        .limit(1)
    ).mappings().fetchone()
    if row is None:
        raise FileNotFoundError("No run manifest found. Start with mediaforce campaign or plan.")
    return Path(str(row["output_path"]))


def _resolve_indexes(manifest: dict[str, Any], args: argparse.Namespace) -> list[int]:
    item_count = len(manifest.get("items", []))
    if args.all:
        return list(range(item_count))
    if args.index:
        indexes = sorted(set(args.index))
        for index in indexes:
            if index < 0 or index >= item_count:
                raise IndexError(f"Manifest index out of range: {index}")
        return indexes
    if item_count == 1:
        return [0]
    raise ValueError("Select manifest items with --index or pass --all")


def _resolve_approve_indexes(manifest: dict[str, Any], args: argparse.Namespace) -> list[int]:
    if args.all or args.index:
        return _resolve_indexes(manifest, args)
    if not manifest.get("items"):
        return []
    return [0]


def _print_folder_summary(summary: dict[str, Any]) -> None:
    if summary.get("item_count", 0) == 0:
        print(f"No items found for prefix {summary['prefix']}")
        return

    print(
        f"prefix={summary['prefix']} items={summary['item_count']} "
        f"size={_format_size(int(summary['total_size_bytes']))}"
    )
    print(f"  statuses={summary['statuses']}")
    print(f"  video_codecs={summary['video_codecs']}")
    print(f"  audio_codecs={summary['audio_codecs']}")
    if summary.get('seasons'):
        print(f"  seasons={summary['seasons']}")

    suggestion = summary["suggested_override"]
    print("\nSuggested folder override:")
    print(f"[[overrides]]\npath_prefix = \"{suggestion['path_prefix']}\"")
    if suggestion.get("reason"):
        print(f"note = \"{' '.join(suggestion['reason'])}\"")
    for section in ("video", "audio", "planning"):
        values = suggestion.get(section) or {}
        if not values:
            continue
        print(f"\n[overrides.{section}]")
        for key, value in values.items():
            if isinstance(value, str):
                print(f"{key} = \"{value}\"")
            else:
                print(f"{key} = {json.dumps(value)}")


def _print_manifest_item(item: dict[str, Any], index: int) -> None:
    plan = describe_item_plan(item)
    audio = plan["audio"]
    video = plan["video"]
    subtitles = plan["subtitles"]
    audio_channels = _format_channel_layout(audio["channels"])
    audio_out = audio["output_codec"]
    if audio.get("output_bitrate"):
        audio_out = f"{audio_out} {audio['output_bitrate']}"
    subtitle_desc = "none"
    if subtitles["kept_track_count"]:
        subtitle_desc = f"{subtitles['kept_track_count']} English track(s)"
    print(f"  item {index}: {item['rel_path']}")
    print(
        f"    source={_format_size(int(item['source_size_bytes']))} video={video['source_codec']} -> {video['output_codec']}")
    print(
        f"    quality={video['quality_metric']} target={video['target']:.1f} min={video['min_target']:.1f} "
        f"max_size={video['max_encoded_percent']:.0f}% grain={video['default_grain']}"
    )
    print(
        f"    audio={audio['source_codec']} {audio_channels} {audio['language']} -> {audio_out} "
        f"(keep {audio['kept_track_count']} of {audio['source_track_count']})"
    )
    print(f"    subtitles={subtitle_desc}")


def _run_review(
        connection: DBClient,
        config: MediaforceConfig,
        manifest_path: Path,
        manifest: dict[str, Any],
        *,
        index: int,
        overwrite: bool,
        duration: float,
        timestamps: list[float] | None,
        output_dir: Path,
        play: bool,
) -> None:
    item_count = len(manifest.get("items", []))
    if index < 0 or index >= item_count:
        raise IndexError(f"Manifest index out of range: {index}")

    item = manifest["items"][index]
    print(f"Review manifest: {manifest_path}")
    _print_manifest_item(item, index)

    encode_result = encode_manifest_items(
        connection,
        config,
        manifest_path,
        manifest,
        [index],
        overwrite=overwrite,
        encode_context={"origin": "cli", "owner_pid": os.getpid()},
    )[0]
    percent = _percent_string(encode_result.staging_size_bytes, encode_result.source_size_bytes)
    print(
        f"encoded {encode_result.staging_path} crf={encode_result.chosen_crf} "
        f"{encode_result.quality_metric}={encode_result.quality_score:.2f} "
        f"size={_format_size(encode_result.staging_size_bytes)} "
        f"saved={_format_signed_bytes(encode_result.source_size_bytes - encode_result.staging_size_bytes)} ({percent})"
    )

    validation_result = validate_manifest_items(connection, config, manifest, [index])[0]
    status = "passed" if validation_result["passed"] else "failed"
    print(
        f"validation {status} source={_format_size(validation_result['source_size_bytes'])} "
        f"staged={_format_size(validation_result['staged_size_bytes'])} "
        f"saved={_format_signed_bytes(validation_result['bytes_saved'])}"
    )
    for check in validation_result["checks"]:
        prefix = "ok" if check["passed"] else "bad"
        print(f"  {prefix}: {check['message']}")

    clips = generate_compare_clips(
        connection,
        manifest,
        [index],
        output_dir=output_dir,
        duration_seconds=duration,
        timestamps=timestamps,
        play=play,
    )
    for clip in clips:
        print(f"compare {clip.output_path} start={clip.timestamp_seconds:.2f}s duration={clip.duration_seconds:.2f}s")


def _format_channel_layout(channels: int) -> str:
    if channels >= 8:
        return "7.1"
    if channels >= 6:
        return "5.1"
    if channels >= 2:
        return "stereo"
    if channels == 1:
        return "mono"
    return f"{channels}ch"


def _format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.2f}{unit}"
        value /= 1024
    return f"{size_bytes}B"


def _format_optional_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unset"
    return _format_size(size_bytes)


def _format_signed_bytes(size_bytes: int) -> str:
    prefix = "+" if size_bytes >= 0 else "-"
    return f"{prefix}{_format_size(abs(size_bytes))}"


def _percent_string(current_bytes: int, source_bytes: int) -> str:
    if source_bytes <= 0:
        return "n/a"
    return f"{(current_bytes / source_bytes) * 100:.1f}%"
