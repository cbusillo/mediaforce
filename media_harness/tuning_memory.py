from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from media_harness.config import HarnessConfig


def learned_memory_dir(config: HarnessConfig) -> Path:
    path = config.paths.runtime_settings_path.parent / "learned-memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_tuning_session(
    connection: sqlite3.Connection,
    *,
    prefix: str,
    note: str,
    response: dict[str, Any],
    applied_policy: dict[str, Any],
    toolbelt: dict[str, Any],
    created_at: str,
) -> str:
    session_id = uuid.uuid4().hex[:12]
    connection.execute(
        """
        INSERT INTO tuning_sessions(
            session_id, prefix, note, summary, diagnosis, confidence,
            evidence_checked_json, suggested_follow_up, prompt_version,
            proposed_policy_json, applied_policy_json, toolbelt_json,
            self_check_json, raw_response, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            prefix,
            note,
            str(response.get("summary") or ""),
            str(response.get("diagnosis") or ""),
            str(response.get("confidence") or ""),
            json.dumps(response.get("evidence_checked") or [], sort_keys=True),
            response.get("suggested_follow_up"),
            response.get("prompt_version"),
            json.dumps(response.get("proposed_policy") or {}, sort_keys=True),
            json.dumps(applied_policy or {}, sort_keys=True),
            json.dumps(toolbelt or {}, sort_keys=True),
            json.dumps(response.get("self_check") or {}, sort_keys=True) if response.get("self_check") else None,
            str(response.get("raw") or ""),
            created_at,
        ),
    )
    return session_id


def promote_learning_artifact(
    connection: sqlite3.Connection,
    config: HarnessConfig,
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
    self_check = dict(response.get("self_check") or {})
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
        """
        INSERT INTO learning_artifacts(
            artifact_id, session_id, prefix, title, artifact_path, summary,
            tags_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            session_id,
            prefix,
            title,
            str(artifact_path),
            str(response.get("summary") or ""),
            json.dumps(tags, sort_keys=True),
            created_at,
            created_at,
        ),
    )
    return {
        "artifact_id": artifact_id,
        "title": title,
        "artifact_path": str(artifact_path),
        "tags": tags,
    }


def retrieve_learning_context(
    connection: sqlite3.Connection,
    *,
    prefix: str,
    sample_item: dict[str, Any],
    note: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    candidate_rows = connection.execute(
        "SELECT title, artifact_path, summary, tags_json, updated_at FROM learning_artifacts ORDER BY updated_at DESC"
    ).fetchall()
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
    ranked.sort(key=lambda item: (-item[0], item[1]["updated_at"]), reverse=False)
    return [payload for _, payload in ranked[:limit]]


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
    if "quality_metric" in (sample_item.get("resolved_policy") or {}).get("video", {}):
        tags.add(f"metric:{str(sample_item['resolved_policy']['video'].get('quality_metric') or 'auto').lower()}" )
    for token in re.findall(r"[a-z0-9]{5,}", note.lower()):
        tags.add(f"note:{token}")
    for token in re.findall(r"[a-z0-9]{5,}", str(response.get("diagnosis") or "").lower()):
        tags.add(f"diagnosis:{token}")
    return sorted(tags)


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
        ", ".join(response.get("evidence_checked") or []) or "None recorded.",
    ]
    self_check = dict(response.get("self_check") or {})
    if self_check:
        lines.extend(
            [
                "",
                "## Self Check",
                json.dumps(self_check, indent=2, sort_keys=True),
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
