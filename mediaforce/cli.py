from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from mediaforce.config import DEFAULT_CONFIG_PATH, HarnessConfig, load_config
from mediaforce.db import open_db
from mediaforce.execution import describe_item_plan, encode_manifest_items, promote_manifest_items, validate_manifest_items
from mediaforce.folder_profiles import inspect_prefix
from mediaforce.planner import build_manifest_item, recommend_item
from mediaforce.review import generate_compare_clips
from mediaforce.scanner import scan_library
from mediaforce.state_cleanup import purge_transient_artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manifest-driven AV1 planning harness")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to the TOML config file")

    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan source roots into SQLite state")
    scan_parser.add_argument("--prefix", action="append", default=[], help="Restrict scan to rel-path prefixes such as tv/Futurama")
    scan_parser.add_argument("--limit", type=int, help="Only scan the first N matching files")

    report_parser = subparsers.add_parser("report", help="Show prioritized candidates from SQLite state")
    report_parser.add_argument("--limit", type=int, default=20, help="Number of rows to show")
    report_parser.add_argument("--status", action="append", default=None, help="Statuses to include")
    report_parser.add_argument("--prefix", action="append", default=[], help="Restrict report to rel-path prefixes")

    plan_parser = subparsers.add_parser("plan", help="Generate a run manifest from current state")
    plan_parser.add_argument("--limit", type=int, help="Maximum items in the run manifest")
    plan_parser.add_argument("--prefix", action="append", default=[], help="Restrict plan to rel-path prefixes")
    plan_parser.add_argument("--bucket", action="append", default=[], help="Restrict plan to recommendation buckets")
    plan_parser.add_argument("--output", type=Path, help="Write the manifest to an explicit path")

    inspect_parser = subparsers.add_parser("inspect-folder", help="Summarize one folder prefix and print a suggested override block")
    inspect_parser.add_argument("prefix", help="Folder prefix like tv/Suits or tv/Suits/Season 5")

    campaign_parser = subparsers.add_parser("campaign", help="Scan a folder, print its summary, and create a run manifest")
    campaign_parser.add_argument("prefix", help="Folder prefix like tv/Suits/Season 5")
    campaign_parser.add_argument("--limit", type=int, help="Maximum items to put in the run manifest")
    campaign_parser.add_argument("--bucket", action="append", default=[], help="Restrict campaign items to recommendation buckets")
    campaign_parser.add_argument("--output", type=Path, help="Write the manifest to an explicit path")
    campaign_parser.add_argument("--review-first", action="store_true", help="Immediately encode, validate, and compare the first item")
    campaign_parser.add_argument("--play", action="store_true", help="Open the first generated compare clip when reviewing")

    run_parser = subparsers.add_parser("run", help="Start a folder run, review the first item, and print the next approval step")
    run_parser.add_argument("prefix", help="Folder prefix like tv/Suits/Season 5")
    run_parser.add_argument("--limit", type=int, help="Maximum items to put in the run manifest")
    run_parser.add_argument("--bucket", action="append", default=[], help="Restrict run items to recommendation buckets")
    run_parser.add_argument("--output", type=Path, help="Write the manifest to an explicit path")
    run_parser.add_argument("--duration", type=float, default=8.0, help="Clip duration in seconds")
    run_parser.add_argument("--play", action="store_true", help="Open the first generated compare clip")

    review_parser = subparsers.add_parser("review", help="Encode, validate, and compare one manifest item in a single command")
    review_parser.add_argument("manifest", nargs="?", type=Path, help="Path to a run manifest JSON file (defaults to latest)")
    review_parser.add_argument("--index", type=int, default=0, help="Manifest item index to review")
    review_parser.add_argument("--duration", type=float, default=8.0, help="Clip duration in seconds")
    review_parser.add_argument("--timestamp", action="append", type=float, default=[], help="Clip start timestamps in seconds")
    review_parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for generated compare clips",
    )
    review_parser.add_argument("--play", action="store_true", help="Open the first generated clip in ffplay")

    encode_parser = subparsers.add_parser("encode", help="Encode manifest items into the staging root")
    _add_manifest_selection_args(encode_parser, require_manifest=False)
    encode_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing staged file")

    validate_parser = subparsers.add_parser("validate", help="Run machine validation on staged outputs")
    _add_manifest_selection_args(validate_parser, require_manifest=False)

    promote_parser = subparsers.add_parser("promote", help="Promote validated staged outputs into the library")
    _add_manifest_selection_args(promote_parser, require_manifest=False)
    promote_parser.add_argument("--force", action="store_true", help="Promote even if machine validation failed")

    approve_parser = subparsers.add_parser("approve", help="Promote the reviewed item from the latest manifest")
    approve_parser.add_argument("manifest", nargs="?", type=Path, help="Path to a run manifest JSON file (defaults to latest)")
    approve_parser.add_argument("--index", action="append", type=int, default=[], help="Manifest item indexes to promote")
    approve_parser.add_argument("--all", action="store_true", help="Promote all manifest items")
    approve_parser.add_argument("--force", action="store_true", help="Promote even if machine validation failed")

    compare_parser = subparsers.add_parser("compare", help="Generate side-by-side approval clips for staged items")
    _add_manifest_selection_args(compare_parser, require_manifest=False)
    compare_parser.add_argument("--duration", type=float, default=8.0, help="Clip duration in seconds")
    compare_parser.add_argument("--timestamp", action="append", type=float, default=[], help="Clip start timestamps in seconds")
    compare_parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for generated compare clips",
    )
    compare_parser.add_argument("--play", action="store_true", help="Open the first generated clip in ffplay")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    purge_transient_artifacts(config)
    default_review_dir = config.paths.review_dir

    with open_db(config.paths.db_path) as connection:
        if args.command == "scan":
            stats = scan_library(connection, config, prefixes=args.prefix or None, limit=args.limit)
            print(
                f"scan_id={stats.scan_id} discovered={stats.discovered} reprobed={stats.reprobed} "
                f"unchanged={stats.unchanged} missing={stats.missing} total_seen={stats.total_seen}"
            )
            return 0

        if args.command == "report":
            rows = _select_candidates(connection, config, args.status or ["discovered", "planned"], args.prefix, args.limit)
            _print_report(rows, config)
            return 0

        if args.command == "plan":
            rows = _select_candidates(
                connection,
                config,
                statuses=["discovered", "planned", "validated"],
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
            manifest, output_path = _create_campaign_manifest(
                connection,
                config,
                prefix=args.prefix,
                limit=args.limit,
                buckets=args.bucket or None,
                output_path=args.output,
            )
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
            manifest, output_path = _create_campaign_manifest(
                connection,
                config,
                prefix=args.prefix,
                limit=args.limit,
                buckets=args.bucket or None,
                output_path=args.output,
            )
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
            encode_results = encode_manifest_items(connection, config, manifest_path, manifest, indexes, overwrite=args.overwrite)
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

    return 1


def _add_manifest_selection_args(parser: argparse.ArgumentParser, *, require_manifest: bool = True) -> None:
    manifest_kwargs: dict[str, Any] = {"type": Path, "help": "Path to a run manifest JSON file"}
    if not require_manifest:
        manifest_kwargs["nargs"] = "?"
        manifest_kwargs["help"] = "Path to a run manifest JSON file (defaults to latest)"
    parser.add_argument("manifest", **manifest_kwargs)
    parser.add_argument("--index", action="append", type=int, default=[], help="Manifest item indexes to process")
    parser.add_argument("--all", action="store_true", help="Process all manifest items")


def _select_candidates(
    connection: sqlite3.Connection,
    config: HarnessConfig,
    statuses: list[str],
    prefixes: list[str],
    limit: int | None,
    buckets: list[str] | None = None,
) -> list[dict[str, Any]]:
    query = """
        SELECT
            library_items.*,
            staged_artifacts.staging_size_bytes,
            staged_artifacts.staging_path,
            staged_artifacts.quality_metric,
            staged_artifacts.quality_score,
            staged_artifacts.validation_json
        FROM library_items
        LEFT JOIN staged_artifacts ON staged_artifacts.library_item_id = library_items.id
        WHERE library_items.status IN ({statuses})
    """.format(
        statuses=",".join("?" for _ in statuses)
    )
    params: list[Any] = list(statuses)

    if prefixes:
        query += " AND (" + " OR ".join("rel_path LIKE ?" for _ in prefixes) + ")"
        params.extend([f"{prefix}%" for prefix in prefixes])

    query += " ORDER BY priority_score DESC, size_bytes DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = [dict(row) for row in connection.execute(query, params).fetchall()]
    if buckets:
        rows = [row for row in rows if recommend_item(row, config).bucket in buckets]
    return rows


def _print_report(rows: list[dict[str, Any]], config: HarnessConfig) -> None:
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


def _build_run_manifest(rows: list[dict[str, Any]], config: HarnessConfig) -> dict[str, Any]:
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    run_id = uuid.uuid4().hex[:12]
    items = [build_manifest_item(row, config) for row in rows]
    return {
        "run_id": run_id,
        "created_at": now,
        "config_path": str(config.paths.config_path),
        "db_path": str(config.paths.db_path),
        "staging_root": str(config.staging_root),
        "output_container": config.output_container,
        "items": items,
    }


def _write_manifest(
    connection: sqlite3.Connection,
    config: HarnessConfig,
    manifest: dict[str, Any],
    output_path: Path | None,
) -> Path:
    config.paths.run_manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_path or config.paths.run_manifest_dir / f"run-{manifest['run_id']}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    selection = {
        "item_count": len(manifest["items"]),
        "sources": [item["source_path"] for item in manifest["items"]],
    }
    connection.execute(
        "INSERT INTO run_manifests(run_id, created_at, output_path, selection_json, item_count) VALUES (?, ?, ?, ?, ?)",
        (
            manifest["run_id"],
            manifest["created_at"],
            str(manifest_path),
            json.dumps(selection, separators=(",", ":")),
            len(manifest["items"]),
        ),
    )

    if manifest["items"]:
        connection.executemany(
            "UPDATE library_items SET status = 'planned', updated_at = ? WHERE source_path = ?",
            [(manifest["created_at"], item["source_path"]) for item in manifest["items"]],
        )

    return manifest_path


def _load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _create_campaign_manifest(
    connection: sqlite3.Connection,
    config: HarnessConfig,
    *,
    prefix: str,
    limit: int | None,
    buckets: list[str] | None,
    output_path: Path | None,
) -> tuple[dict[str, Any], Path]:
    stats = scan_library(connection, config, prefixes=[prefix], limit=None)
    print(
        f"scan_id={stats.scan_id} discovered={stats.discovered} reprobed={stats.reprobed} "
        f"unchanged={stats.unchanged} total_seen={stats.total_seen}"
    )
    summary = inspect_prefix(connection, config, prefix)
    _print_folder_summary(summary)
    rows = _select_candidates(
        connection,
        config,
        statuses=["discovered", "planned", "validated"],
        prefixes=[prefix],
        limit=limit,
        buckets=buckets,
    )
    manifest = _build_run_manifest(rows, config)
    manifest_path = _write_manifest(connection, config, manifest, output_path)
    return manifest, manifest_path


def _resolve_manifest_path(connection: sqlite3.Connection, manifest_path: Path | None) -> Path:
    if manifest_path is not None:
        return manifest_path
    row = connection.execute(
        "SELECT output_path FROM run_manifests ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
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
    print(f"    source={_format_size(int(item['source_size_bytes']))} video={video['source_codec']} -> {video['output_codec']}")
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
    connection: sqlite3.Connection,
    config: HarnessConfig,
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

    encode_result = encode_manifest_items(connection, config, manifest_path, manifest, [index], overwrite=overwrite)[0]
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


def _format_signed_bytes(size_bytes: int) -> str:
    prefix = "+" if size_bytes >= 0 else "-"
    return f"{prefix}{_format_size(abs(size_bytes))}"


def _percent_string(current_bytes: int, source_bytes: int) -> str:
    if source_bytes <= 0:
        return "n/a"
    return f"{(current_bytes / source_bytes) * 100:.1f}%"
