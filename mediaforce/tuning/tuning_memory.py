import json
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import learning_artifacts
from mediaforce.core.db_tables import tuning_sessions
from mediaforce.core.type_defs import object_dict, object_list


def learned_memory_dir(config: MediaforceConfig) -> Path:
    path = config.paths.runtime_settings_path.parent / "learned-memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_tuning_session(
        connection: DBClient,
        *,
        prefix: str,
        note: str,
        response: dict[str, Any],
        applied_policy: dict[str, Any],
        toolbelt: dict[str, Any],
        created_at: str,
        requested_experiment: dict[str, Any] | None = None,
) -> str:
    session_id = uuid.uuid4().hex[:12]
    stored_toolbelt = dict(toolbelt or {})
    if requested_experiment:
        stored_toolbelt["requested_experiment"] = requested_experiment
    connection.execute(
        tuning_sessions.insert().values(
            session_id=session_id,
            prefix=prefix,
            note=note,
            summary=str(response.get("summary") or ""),
            diagnosis=str(response.get("diagnosis") or ""),
            confidence=str(response.get("confidence") or ""),
            evidence_checked_json=json.dumps(response.get("evidence_checked") or [], sort_keys=True),
            suggested_follow_up=response.get("suggested_follow_up"),
            prompt_version=response.get("prompt_version"),
            proposed_policy_json=json.dumps(response.get("proposed_policy") or {}, sort_keys=True),
            applied_policy_json=json.dumps(applied_policy or {}, sort_keys=True),
            toolbelt_json=json.dumps(stored_toolbelt, sort_keys=True),
            self_check_json=(
                json.dumps(response.get("self_check") or {}, sort_keys=True)
                if response.get("self_check")
                else None
            ),
            raw_response=str(response.get("raw") or ""),
            created_at=created_at,
        )
    )
    return session_id


def promote_learning_artifact(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        session_id: str,
        prefix: str,
        note: str,
        sample_item: dict[str, Any],
        response: dict[str, Any],
        applied_policy: dict[str, Any],
        created_at: str,
) -> dict[str, Any] | None:
    if not applied_policy:
        return None
    confidence = str(response.get("confidence") or "").lower()
    if confidence not in {"high", "medium"}:
        return None
    self_check = object_dict(response.get("self_check"))
    if str(self_check.get("status") or "pass") == "fail":
        return None

    artifact_id = uuid.uuid4().hex[:12]
    tags = _artifact_tags(prefix=prefix, sample_item=sample_item, note=note, response=response)
    title = _artifact_title(prefix=prefix, response=response, sample_item=sample_item)
    artifact_path = learned_memory_dir(config) / f"{artifact_id}-{_slug(title)}.md"
    artifact_path.write_text(
        _render_artifact_markdown(
            title=title,
            prefix=prefix,
            note=note,
            sample_item=sample_item,
            response=response,
            applied_policy=applied_policy,
            tags=tags,
            created_at=created_at,
        )
    )
    connection.execute(
        learning_artifacts.insert().values(
            artifact_id=artifact_id,
            session_id=session_id,
            prefix=prefix,
            title=title,
            artifact_path=str(artifact_path),
            summary=str(response.get("summary") or ""),
            tags_json=json.dumps(tags, sort_keys=True),
            created_at=created_at,
            updated_at=created_at,
        )
    )
    return {
        "artifact_id": artifact_id,
        "title": title,
        "artifact_path": str(artifact_path),
        "tags": tags,
    }


def retrieve_learning_context(
        connection: DBClient,
        *,
        prefix: str,
        sample_item: dict[str, Any],
        note: str,
        limit: int = 3,
) -> list[dict[str, Any]]:
    candidate_rows = connection.execute(
        select(
            learning_artifacts.c.title,
            learning_artifacts.c.artifact_path,
            learning_artifacts.c.summary,
            learning_artifacts.c.tags_json,
            learning_artifacts.c.updated_at,
        ).order_by(learning_artifacts.c.updated_at.desc())
    ).mappings().fetchall()
    desired_tags = set(_artifact_tags(prefix=prefix, sample_item=sample_item, note=note, response={}))
    ranked: list[tuple[int, dict[str, Any]]] = []
    for row in candidate_rows:
        tags = [str(value) for value in json.loads(str(row["tags_json"] or "[]"))]
        overlap = len(desired_tags.intersection(tags))
        same_prefix = 1 if str(row["artifact_path"]).find(_slug(prefix)) != -1 else 0
        score = overlap + same_prefix
        if score <= 0:
            continue
        artifact_path = Path(str(row["artifact_path"]))
        excerpt = _artifact_excerpt(artifact_path)
        ranked.append(
            (
                score,
                {
                    "title": str(row["title"]),
                    "summary": str(row["summary"] or ""),
                    "tags": tags,
                    "updated_at": str(row["updated_at"]),
                    "excerpt": excerpt,
                },
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]["updated_at"]))
    return [payload for _, payload in ranked[:limit]]


def sibling_approved_season_memory(
        connection: DBClient,
        *,
        prefix: str,
) -> dict[str, Any] | None:
    normalized_prefix = str(prefix).strip().strip("/")
    root_prefix = _season_root_prefix(normalized_prefix)
    series_root_scope = root_prefix is None
    if root_prefix is None:
        root_prefix = _series_root_prefix(normalized_prefix)
    if root_prefix is None:
        return None

    rows = connection.execute(
        select(
            learning_artifacts.c.prefix,
            learning_artifacts.c.tags_json,
            learning_artifacts.c.updated_at,
        )
        .where(learning_artifacts.c.prefix.like(f"{_sql_like_escape(root_prefix)}/%", escape="\\"))
        .order_by(learning_artifacts.c.updated_at.desc())
    ).mappings().fetchall()

    sibling_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        artifact_prefix = str(row["prefix"] or "").strip().strip("/")
        if not artifact_prefix or artifact_prefix == normalized_prefix:
            continue
        if not _is_season_prefix(artifact_prefix):
            continue
        if _season_root_prefix(artifact_prefix) != root_prefix:
            continue
        tags = {str(value).strip() for value in json.loads(str(row["tags_json"] or "[]"))}
        if "decision:accepted" not in tags:
            continue
        sibling_rows.setdefault(
            artifact_prefix,
            {
                "prefix": artifact_prefix,
                "label": _season_label(artifact_prefix),
                "updated_at": str(row["updated_at"] or ""),
            },
        )

    if not sibling_rows:
        return None

    siblings = sorted(sibling_rows.values(), key=lambda entry: _season_sort_key(str(entry.get("label") or "")))
    season_labels = [str(entry["label"]) for entry in siblings if str(entry.get("label") or "").strip()]
    show_label = Path(root_prefix).name
    return {
        "root_prefix": root_prefix,
        "root_label": show_label,
        "count": len(siblings),
        "season_labels": season_labels,
        "season_prefixes": [str(entry["prefix"]) for entry in siblings],
        "suggested_note": _approved_season_shortcut_note(
            show_label=show_label,
            season_labels=season_labels,
            series_scope=series_root_scope,
        ),
    }


def record_visual_approval_artifact(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        prefix: str,
        note: str,
        sample_item: dict[str, Any],
        calibration: dict[str, Any],
        run_verdict: dict[str, Any] | None,
        created_at: str,
) -> dict[str, Any] | None:
    sample_result = object_dict(calibration.get("sample_result"))
    policy = object_dict(calibration.get("policy"))
    if not sample_result or not policy:
        return None

    session_id = uuid.uuid4().hex[:12]
    approval_note = note.strip() or "Operator approved this sampled calibration after visual review."
    verdict_payload = object_dict(run_verdict)
    summary = str(verdict_payload.get("summary") or "Operator approved this sampled calibration after review.")
    diagnosis = "Visual approval recorded so future tuning can learn from this accepted draft."
    evidence_checked = ["operator_visual_review", "sample_result", "saved_policy"]
    connection.execute(
        tuning_sessions.insert().values(
            session_id=session_id,
            prefix=prefix,
            note=approval_note,
            summary=summary,
            diagnosis=diagnosis,
            confidence="high",
            evidence_checked_json=json.dumps(evidence_checked, sort_keys=True),
            suggested_follow_up=None,
            prompt_version="visual-approval-v1",
            proposed_policy_json=json.dumps(policy, sort_keys=True),
            applied_policy_json=json.dumps(policy, sort_keys=True),
            toolbelt_json=json.dumps({"sample_result": sample_result, "run_verdict": run_verdict or {}}, sort_keys=True),
            self_check_json=json.dumps(
                {"status": "pass", "summary": "Approved by operator after review.", "issues": []},
                sort_keys=True,
            ),
            raw_response="",
            created_at=created_at,
        )
    )

    artifact_id = uuid.uuid4().hex[:12]
    tags = _artifact_tags(prefix=prefix, sample_item=sample_item, note=approval_note, response={})
    tags = sorted(set(tags + ["approval:visual", "decision:accepted"]))
    title = f"{prefix} visual approval"
    artifact_path = learned_memory_dir(config) / f"{artifact_id}-{_slug(title)}.md"
    artifact_path.write_text(
        _render_visual_approval_markdown(
            title=title,
            prefix=prefix,
            note=approval_note,
            sample_item=sample_item,
            calibration=calibration,
            run_verdict=run_verdict,
            tags=tags,
            created_at=created_at,
        )
    )
    connection.execute(
        learning_artifacts.insert().values(
            artifact_id=artifact_id,
            session_id=session_id,
            prefix=prefix,
            title=title,
            artifact_path=str(artifact_path),
            summary=summary,
            tags_json=json.dumps(tags, sort_keys=True),
            created_at=created_at,
            updated_at=created_at,
        )
    )
    return {
        "artifact_id": artifact_id,
        "session_id": session_id,
        "sample_job_id": str(calibration.get("job_id") or ""),
        "title": title,
        "artifact_path": str(artifact_path),
        "tags": tags,
    }


def _artifact_title(*, prefix: str, response: dict[str, Any], sample_item: dict[str, Any]) -> str:
    diagnosis = str(response.get("diagnosis") or "").strip()
    if diagnosis:
        return diagnosis[:80]
    return f"{prefix} tuning note for {Path(str(sample_item.get('rel_path') or prefix)).name}"


def _artifact_tags(*, prefix: str, sample_item: dict[str, Any], note: str, response: dict[str, Any]) -> list[str]:
    tags = {
        f"prefix:{prefix.split('/')[0]}" if "/" in prefix else f"prefix:{prefix}",
        f"codec:{str(sample_item.get('video_codec') or 'unknown').lower()}",
        f"bucket:{str(sample_item.get('recommendation') or 'unknown').lower()}",
    }
    video_policy = object_dict(object_dict(sample_item.get("resolved_policy")).get("video"))
    if "quality_metric" in video_policy:
        tags.add(f"metric:{str(video_policy.get('quality_metric') or 'auto').lower()}")
    for token in re.findall(r"[a-z0-9]{5,}", note.lower()):
        tags.add(f"note:{token}")
    for token in re.findall(r"[a-z0-9]{5,}", str(response.get("diagnosis") or "").lower()):
        tags.add(f"diagnosis:{token}")
    return sorted(tags)


def _season_root_prefix(prefix: str) -> str | None:
    parts = Path(str(prefix).strip().strip("/")).parts
    if len(parts) < 3:
        return None
    if parts[0].lower() != "tv":
        return None
    if not parts[2].lower().startswith("season"):
        return None
    return str(Path(parts[0]) / parts[1])


def _series_root_prefix(prefix: str) -> str | None:
    parts = Path(str(prefix).strip().strip("/")).parts
    if len(parts) != 2:
        return None
    if parts[0].lower() != "tv":
        return None
    return str(Path(parts[0]) / parts[1])


def _is_season_prefix(prefix: str) -> bool:
    parts = Path(str(prefix).strip().strip("/")).parts
    return len(parts) == 3 and parts[0].lower() == "tv" and parts[2].lower().startswith("season")


def _season_label(prefix: str) -> str:
    parts = Path(str(prefix).strip().strip("/")).parts
    if len(parts) >= 3:
        return str(parts[2])
    return str(prefix).strip().strip("/")


def _season_sort_key(label: str) -> tuple[int, str]:
    match = re.search(r"(\d+)", label)
    if match:
        return int(match.group(1)), label.lower()
    return 10 ** 9, label.lower()


def _approved_season_shortcut_note(*, show_label: str, season_labels: list[str], series_scope: bool = False) -> str:
    reference_labels = _human_join(season_labels[:3])
    if len(season_labels) > 3:
        reference_labels = f"{reference_labels}, and the other approved seasons"
    target = "the remaining episodes" if series_scope else "this season"
    if reference_labels:
        return (
            f"Match the approved {show_label} seasons unless {target} clearly need a different tradeoff. "
            f"Keep this draft aligned with {reference_labels}."
        )
    return f"Match the approved {show_label} seasons unless {target} clearly need a different tradeoff."


def _human_join(values: list[str]) -> str:
    items = [str(value).strip() for value in values if str(value).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _sql_like_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _render_artifact_markdown(
        *,
        title: str,
        prefix: str,
        note: str,
        sample_item: dict[str, Any],
        response: dict[str, Any],
        applied_policy: dict[str, Any],
        tags: list[str],
        created_at: str,
) -> str:
    lines = [
        f"# {title}",
        "",
        f"- Created: {created_at}",
        f"- Folder: {prefix}",
        f"- Sample item: {sample_item.get('rel_path')}",
        f"- Tags: {', '.join(tags)}",
        "",
        "## Operator Note",
        note or "None.",
        "",
        "## Summary",
        str(response.get("summary") or "No summary."),
        "",
        "## Diagnosis",
        str(response.get("diagnosis") or "No diagnosis."),
        "",
        "## Applied Policy",
        json.dumps(applied_policy or {}, indent=2, sort_keys=True),
        "",
        "## Evidence Checked",
        ", ".join(str(item) for item in object_list(response.get("evidence_checked"))) or "None recorded.",
    ]
    self_check = object_dict(response.get("self_check"))
    if self_check:
        lines.extend(
            [
                "",
                "## Self Check",
                json.dumps(self_check, indent=2, sort_keys=True),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_visual_approval_markdown(
        *,
        title: str,
        prefix: str,
        note: str,
        sample_item: dict[str, Any],
        calibration: dict[str, Any],
        run_verdict: dict[str, Any] | None,
        tags: list[str],
        created_at: str,
) -> str:
    sample_result = object_dict(calibration.get("sample_result"))
    lines = [
        f"# {title}",
        "",
        f"- Created: {created_at}",
        f"- Folder: {prefix}",
        f"- Sample item: {sample_item.get('rel_path')}",
        f"- Tags: {', '.join(tags)}",
        "",
        "## Approval",
        note or "Operator approved this draft after review.",
        "",
        "## Sample Result",
        json.dumps(sample_result, indent=2, sort_keys=True),
        "",
        "## Saved Policy",
        json.dumps(calibration.get("policy") or {}, indent=2, sort_keys=True),
    ]
    if run_verdict:
        lines.extend(
            [
                "",
                "## Run Verdict",
                json.dumps(run_verdict, indent=2, sort_keys=True),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _artifact_excerpt(path: Path, *, max_lines: int = 12) -> str:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    return "\n".join(lines[:max_lines])


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "learning"
