import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.execution import describe_item_plan, estimate_output_overhead_bytes
from mediaforce.review import render_audio_spectrogram_compare, render_review_contact_sheet, render_review_timeline_strip
from mediaforce.reviewing.helpers import planned_audio_action, planned_opus_bitrate, select_primary_audio_track
from mediaforce.tuning.size_goals import operator_intent_from_policy
from mediaforce.web.runtime.folder_tuning_advice import audio_tradeoff_hint
from mediaforce.web.runtime.folder_tuning_helpers import size_budget_sample_analysis

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FolderTuningRuntimeDeps:
    tuning_policy_focus: Any
    summarize_calibration_result: Any
    tuning_policy_key_paths: Any
    review_media_context: Any
    review_file_from_url: Any
    review_url: Any
    slug: Any


def proposal_context_snapshot(
        deps: FolderTuningRuntimeDeps,
        *,
        goal: str,
        current_policy: dict[str, Any],
        sample_item: dict[str, Any],
        runtime_toolbelt: dict[str, Any] | None = None,
        learning_context: list[dict[str, Any]] | None = None,
        recent_calibration: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
        metric_support: dict[str, Any] | None = None,
        requested_experiment: dict[str, Any] | None = None,
        multimodal_review_pack: dict[str, Any] | None = None,
        review_artifact_critique: dict[str, Any] | None = None,
        latest_failed_sample_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "goal": goal,
        "current_policy": deps.tuning_policy_focus(current_policy),
        "sample_item": {
            "rel_path": sample_item.get("rel_path"),
            "source_size_bytes": sample_item.get("source_size_bytes"),
            "video_codec": sample_item.get("video_codec"),
            "video_bitrate": sample_item.get("video_bitrate"),
            "width": sample_item.get("width"),
            "height": sample_item.get("height"),
            "duration_seconds": sample_item.get("duration_seconds"),
            "audio_summary": sample_item.get("audio_summary"),
            "subtitle_summary": sample_item.get("subtitle_summary"),
        },
    }
    if runtime_toolbelt:
        snapshot["runtime_toolbelt"] = runtime_toolbelt
    if learning_context:
        snapshot["retrieved_memory"] = learning_context
    if recent_calibration:
        snapshot["recent_calibration"] = deps.summarize_calibration_result(recent_calibration)
    if summary:
        snapshot["folder_summary"] = {
            "item_count": summary.get("item_count"),
            "total_size_bytes": summary.get("total_size_bytes"),
            "video_codecs": summary.get("video_codecs"),
            "audio_codecs": summary.get("audio_codecs"),
            "statuses": summary.get("statuses"),
        }
    if metric_support:
        snapshot["metric_support"] = metric_support
    if requested_experiment:
        snapshot["requested_experiment"] = requested_experiment
    if multimodal_review_pack:
        snapshot["multimodal_review_pack"] = multimodal_review_pack
    if review_artifact_critique:
        snapshot["review_artifact_critique"] = review_artifact_critique
    if latest_failed_sample_job:
        snapshot["latest_failed_sample_job"] = latest_failed_sample_job
    return snapshot


def build_tuning_runtime_toolbelt(
        deps: FolderTuningRuntimeDeps,
        *,
        sample_item: dict[str, Any],
        current_policy: dict[str, Any],
        calibration: dict[str, Any] | None,
        metric_support: dict[str, bool],
        operator_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sample_plan_item = dict(sample_item)
    sample_plan_item["resolved_policy"] = current_policy
    try:
        item_plan = describe_item_plan(sample_plan_item)
        overhead = estimate_output_overhead_bytes(sample_plan_item)
    except (KeyError, TypeError, ValueError):
        item_plan = {}
        overhead = {}
    sample_result = object_dict(object_dict(calibration).get("sample_result"))
    video_policy = object_dict(current_policy.get("video"))
    target_size_mb = float_value(video_policy.get("target_size_mb"))
    target_runtime_minutes = float_value(video_policy.get("target_runtime_minutes"))
    sample_duration_seconds = float_value(sample_item.get("duration_seconds"))
    operator_intent = operator_intent_from_policy(
        video_policy,
        default_video_policy=video_policy,
        audio_policy=object_dict(current_policy.get("audio")),
        subtitle_policy=object_dict(current_policy.get("subtitle")),
    )
    resolved_size_goal = operator_intent.size_goal.resolve(sample_duration_seconds or None)
    target_size_bytes = resolved_size_goal.target_size_bytes
    toolbelt = {
        "allowed_policy_keys": deps.tuning_policy_key_paths(current_policy),
        "metric_support": metric_support,
        "current_policy_focus": deps.tuning_policy_focus(current_policy),
        "decision_defaults": {
            "decision_model": str(video_policy.get("decision_model") or "size_first_review"),
            "quality_engine": str(video_policy.get("quality_engine") or "ab_av1_fast_sample"),
            "target_size_mb": target_size_mb or None,
            "target_runtime_minutes": target_runtime_minutes or None,
            "target_size_bytes": target_size_bytes,
            "sample_runtime_seconds": sample_duration_seconds or None,
            "sample_target_size_bytes": target_size_bytes,
            "operator_intent": operator_intent.to_payload(item_runtime_seconds=sample_duration_seconds or None),
            "max_height": video_policy.get("max_height"),
            "quality_metric": video_policy.get("quality_metric"),
            "target_vmaf": video_policy.get("target_vmaf"),
            "min_target_vmaf": video_policy.get("min_target_vmaf"),
            "target_xpsnr": video_policy.get("target_xpsnr"),
            "min_target_xpsnr": video_policy.get("min_target_xpsnr"),
        },
        "item_plan": item_plan,
        "estimated_overhead_bytes": overhead,
        "recent_sample_result": {
            key: sample_result.get(key)
            for key in (
                "chosen_crf",
                "quality_metric",
                "quality_target",
                "quality_score",
                "predicted_total_size_bytes",
                "predicted_encode_percent",
            )
            if key in sample_result
        },
    }
    tradeoff_hint = audio_tradeoff_hint(sample_item, object_dict(current_policy.get("audio")))
    if tradeoff_hint is not None:
        toolbelt["audio_tradeoff_hint"] = tradeoff_hint
    size_target_analysis = size_budget_sample_analysis(
        operator_request=operator_request,
        calibration_payload=object_dict(calibration),
    )
    if size_target_analysis:
        toolbelt["size_target_analysis"] = size_target_analysis
    review_media_context = deps.review_media_context(calibration)
    if review_media_context.get("review_media_ready") or review_media_context.get("moment_count"):
        toolbelt["review_media_context"] = review_media_context
    return toolbelt


def build_multimodal_review_pack(
        deps: FolderTuningRuntimeDeps,
        *,
        config: MediaforceConfig,
        sample_item: dict[str, Any],
        current_policy: dict[str, Any],
        calibration: dict[str, Any] | None,
        output_dir: Path,
) -> dict[str, Any] | None:
    review_context = object_dict(deps.review_media_context(calibration))
    moments = [object_dict(moment) for moment in object_list(review_context.get("moments"))]
    if not moments:
        return None

    artifacts: list[dict[str, Any]] = []
    images: list[str] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    moment_signals: list[dict[str, Any]] = []

    for moment in moments[:3]:
        source_clip_path = deps.review_file_from_url(config, str(moment.get("source_clip_path") or ""))
        preview_clip_path = deps.review_file_from_url(config, str(moment.get("preview_clip_path") or ""))
        compare_clip_path = deps.review_file_from_url(config, str(moment.get("compare_clip_path") or ""))
        duration_seconds = float_value(moment.get("duration_seconds")) or 8.0
        source_size = int_value(moment.get("source_clip_size_bytes"))
        preview_size = int_value(moment.get("preview_clip_size_bytes"))
        size_ratio = round(preview_size / source_size, 3) if source_size > 0 and preview_size > 0 else None
        moment_signals.append(
            {
                "moment": int_value(moment.get("moment")),
                "timestamp_seconds": float_value(moment.get("timestamp_seconds")),
                "duration_seconds": duration_seconds,
                "preview_to_source_size_ratio": size_ratio,
                "has_compare_clip": bool(compare_clip_path and compare_clip_path.exists()),
            }
        )
        if compare_clip_path is not None and compare_clip_path.exists():
            compare_artifact_path = output_dir / f"review-compare-moment-{int_value(moment.get('moment')):02d}.png"
            try:
                render_review_timeline_strip(
                    clip_path=compare_clip_path,
                    output_path=compare_artifact_path,
                    duration_seconds=duration_seconds,
                )
            except Exception as exc:
                LOGGER.warning("Failed to build compare timeline strip", exc_info=exc)
            else:
                images.append(str(compare_artifact_path))
                artifacts.append(
                    {
                        "kind": "video_compare_timeline",
                        "label": (
                            f"Compare timeline for review moment {int_value(moment.get('moment'))} at "
                            f"{float_value(moment.get('timestamp_seconds')):.1f}s"
                        ),
                        "detail": "Evenly spaced frames sampled from the actual source-versus-draft compare clip for this moment.",
                    }
                )
        if source_clip_path is None or preview_clip_path is None:
            continue
        if not source_clip_path.exists() or not preview_clip_path.exists():
            continue
        artifact_path = output_dir / f"review-video-moment-{int_value(moment.get('moment')):02d}.png"
        try:
            render_review_contact_sheet(
                source_clip_path=source_clip_path,
                preview_clip_path=preview_clip_path,
                output_path=artifact_path,
            )
        except Exception as exc:
            LOGGER.warning("Failed to build review contact sheet", exc_info=exc)
            continue
        images.append(str(artifact_path))
        artifacts.append(
            {
                "kind": "video_contact_sheet",
                "label": (
                    f"Review moment {int_value(moment.get('moment'))} at "
                    f"{float_value(moment.get('timestamp_seconds')):.1f}s"
                ),
                "detail": "Top row is the retained source review clip; bottom row is the retained draft preview clip. Each row is a three-frame contact sheet across the moment.",
            }
        )

    audio_context = planned_audio_review_context(sample_item=sample_item, current_policy=current_policy)
    primary_audio_track = object_dict(audio_context.get("primary_track")) or None
    audio_action = str(audio_context.get("action") or "")
    if primary_audio_track and audio_action == "libopus":
        first_moment = moments[0]
        source_path = Path(str(sample_item.get("source_path") or ""))
        if source_path.exists():
            artifact_path = output_dir / "review-audio-compare.png"
            try:
                audio_result = render_audio_spectrogram_compare(
                    source_path=source_path,
                    output_path=artifact_path,
                    clip_time=float_value(first_moment.get("timestamp_seconds")),
                    duration_seconds=float_value(first_moment.get("duration_seconds")) or 8.0,
                    audio_track=primary_audio_track,
                    audio_policy=object_dict(current_policy.get("audio")),
                )
            except Exception as exc:
                LOGGER.warning("Failed to build audio review artifact", exc_info=exc)
            else:
                if audio_result:
                    images.append(str(artifact_path))
                    artifacts.append(
                        {
                            "kind": "audio_spectrogram_compare",
                            "label": "Primary audio compare",
                            "detail": "Top row is the source audio spectrogram for the first review moment. Bottom row is the planned Opus transcode spectrogram for the same segment.",
                        }
                    )
                    audio_context["comparison"] = audio_result

    if not artifacts:
        return None
    pack = {
        "artifacts": artifacts,
        "images": images,
        "audio_plan": audio_context,
    }
    if moment_signals:
        pack["moment_signals"] = moment_signals
    return pack


def review_pack_dir(deps: FolderTuningRuntimeDeps, config: MediaforceConfig, prefix: str, request_id: str | None = None) -> Path:
    digest = hashlib.sha1(prefix.encode()).hexdigest()[:10]
    root = config.paths.review_dir / "tuning-review-packs" / f"{deps.slug(prefix)}-{digest}"
    if request_id is None:
        return root
    return root / request_id


def multimodal_review_pack_public_view(
        deps: FolderTuningRuntimeDeps,
        config: MediaforceConfig,
        review_pack: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(review_pack, dict):
        return None
    raw_artifacts = [object_dict(artifact) for artifact in object_list(review_pack.get("artifacts")) if isinstance(artifact, dict)]
    raw_images = [str(value) for value in object_list(review_pack.get("images")) if str(value).strip()]
    public_artifacts: list[dict[str, Any]] = []
    for index, artifact in enumerate(raw_artifacts):
        image_value = str(artifact.get("image_url") or "").strip()
        if not image_value and index < len(raw_images):
            image_value = raw_images[index]
        image_url = ""
        if image_value.startswith("/review-media/"):
            review_file = deps.review_file_from_url(config, image_value)
            if review_file is not None and review_file.exists():
                image_url = image_value
        elif image_value:
            candidate = Path(image_value)
            if candidate.exists():
                try:
                    image_url = deps.review_url(config, candidate)
                except ValueError:
                    image_url = ""
        if not image_url:
            continue
        public_artifacts.append(
            {
                "kind": artifact.get("kind"),
                "label": artifact.get("label"),
                "detail": artifact.get("detail"),
                "image_url": image_url,
            }
        )
    if not public_artifacts:
        return None
    public_view: dict[str, Any] = {
        "artifact_count": len(public_artifacts),
        "artifacts": public_artifacts,
    }
    audio_plan = object_dict(review_pack.get("audio_plan"))
    if audio_plan:
        public_view["audio_plan"] = audio_plan
    moment_signals = [object_dict(signal) for signal in object_list(review_pack.get("moment_signals")) if isinstance(signal, dict)]
    if moment_signals:
        public_view["moment_signals"] = moment_signals
    return public_view


def planned_audio_review_context(*, sample_item: dict[str, Any], current_policy: dict[str, Any]) -> dict[str, Any]:
    audio_tracks = [object_dict(track) for track in object_list(sample_item.get("audio_summary")) if isinstance(track, dict)]
    if not audio_tracks:
        return {"action": "none", "summary": "No audio tracks were available on the representative item."}
    try:
        primary_track = select_primary_audio_track(audio_tracks)
    except ValueError:
        return {"action": "none", "summary": "No audio tracks were available on the representative item."}
    audio_policy = object_dict(current_policy.get("audio"))
    codec = str(primary_track.get("codec_name") or "").lower()
    action = planned_audio_action(primary_track, audio_policy)
    channels = int_value(primary_track.get("channels")) or 2
    bitrate = planned_opus_bitrate(primary_track, audio_policy)
    summary = (
        f"Primary track {codec or 'unknown'} is planned for Opus at {bitrate}."
        if action == "libopus"
        else f"Primary track {codec or 'unknown'} is planned to copy unchanged."
    )
    return {
        "action": action,
        "summary": summary,
        "target_bitrate": bitrate if action == "libopus" else None,
        "primary_track": {
            "index": primary_track.get("index"),
            "codec_name": codec or None,
            "channels": channels,
            "language": primary_track.get("language"),
        },
    }
