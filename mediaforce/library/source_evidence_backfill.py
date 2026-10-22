import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select, update

from mediaforce.core.db import DBClient, DBRow
from mediaforce.core.db_tables import encode_jobs, item_events, library_items, run_manifests, source_admissions, \
    source_generations, source_measurements, source_transitions, source_versions, staged_artifacts, target_production_outcomes
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.core.utils import timestamp
from mediaforce.library.evidence_state import EVIDENCE_KINDS, parse_evidence_summary
from mediaforce.library.source_evidence import HISTORICAL_OUTPUT_PROFILES, SUMMARY_KEYS, append_measurement, \
    append_version, json_text, load_source_evidence, load_source_evidence_context, normalize_codec, pending_source_operation, SourceEvidenceContext


@dataclass(slots=True)
class SourceEvidenceRestoreStats:
    published_item_count: int = 0
    historical_probable: Counter[str] = field(default_factory=Counter)
    already_recorded: Counter[str] = field(default_factory=Counter)
    unknown: dict[str, Counter[str]] = field(default_factory=lambda: {kind: Counter() for kind in EVIDENCE_KINDS})
    plan_gaps: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {"published_item_count": self.published_item_count, "historical_probable": dict(self.historical_probable),
                "already_recorded": dict(self.already_recorded),
                "unknown": {kind: dict(reasons) for kind, reasons in self.unknown.items()}, "plan_gaps": self.plan_gaps}


@dataclass(frozen=True, slots=True)
class _PlanItem:
    path: str
    sha256: str
    run_id: str
    index: int
    item: dict[str, Any]

    def reference(self) -> dict[str, Any]:
        return {"plan_path": self.path, "plan_sha256": self.sha256, "run_id": self.run_id, "item_index": self.index,
                "library_item_id": self.item["library_item_id"]}


@dataclass(frozen=True, slots=True)
class _Edge:
    before: tuple[str, int]
    after: tuple[str, int]
    reference: dict[str, Any]


def _identity(item: Mapping[str, Any]) -> tuple[str, int] | None:
    sample = item.get("content_version_fingerprint")
    size = item.get("source_size_bytes")
    if not isinstance(sample, str) or len(sample) != 40 or any(c not in "0123456789abcdef" for c in sample):
        return None
    return (sample, size) if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None


def _stamp(value: object) -> bool:
    if isinstance(value, dict):
        return any(str(key).casefold() == "mediaforce_encoded_by" or _stamp(child) for key, child in value.items())
    return isinstance(value, list) and any(_stamp(child) for child in value)


def _read_plans(connection: DBClient, stats: SourceEvidenceRestoreStats) -> dict[int, list[_PlanItem]]:
    paths = set(connection.execute(select(run_manifests.c.output_path)).scalars())
    paths.update(connection.execute(select(encode_jobs.c.manifest_path)).scalars())
    paths.update(connection.execute(select(staged_artifacts.c.manifest_path)).scalars())
    items: dict[int, list[_PlanItem]] = defaultdict(list)
    for path in sorted(str(path) for path in paths if path):
        try:
            raw = Path(path).read_bytes()
            payload = json.loads(raw)
            json_text(payload)
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or not payload.get("run_id"):
                raise ValueError("invalid run plan")
        except (OSError, ValueError, UnicodeError) as exc:
            stats.plan_gaps[path] = type(exc).__name__
            continue
        digest = hashlib.sha256(raw).hexdigest()
        for index, item in enumerate(payload["items"]):
            if (isinstance(item, dict) and isinstance(item.get("library_item_id"), int)
                    and not isinstance(item["library_item_id"], bool)):
                items[item["library_item_id"]].append(_PlanItem(path, digest, str(payload["run_id"]), index, item))
    return items


def _completion_link(plan: _PlanItem, stage: Mapping[str, Any] | DBRow, job: Mapping[str, Any] | None,
                     events: list[dict[str, Any]], catalog: Mapping[str, Any]) -> dict[str, Any] | None:
    if (job is None or job["manifest_path"] != plan.path or job["job_id"] != stage["encode_job_id"]
            or stage["manifest_path"] != plan.path or stage["manifest_run_id"] != plan.run_id
            or stage["item_index"] != plan.index or stage["library_item_id"] != plan.item["library_item_id"]
            or not plan.item.get("source_fingerprint") or stage["source_fingerprint"] != plan.item["source_fingerprint"]
            or stage["source_size_bytes"] != plan.item.get("source_size_bytes")
            or normalize_codec(stage["source_video_codec"]) != normalize_codec(plan.item.get("video_codec"))
            or stage["source_path"] != plan.item.get("source_path")
            or stage["promoted_path"] != catalog["source_path"] or stage["staging_size_bytes"] != catalog["size_bytes"]
            or not stage["encode_completed_at"] or stage["promoted_at"] < stage["encode_completed_at"]):
        return None
    try:
        indexes = json.loads(job["manifest_indexes_json"] or "null")
    except (TypeError, ValueError):
        return None
    if indexes is not None and (not isinstance(indexes, list) or plan.index not in indexes):
        return None
    for event in events:
        details = event["details"]
        if (event["event_type"] == "encoding_completed" and details.get("manifest") == plan.path
                and details.get("item_index") == plan.index and details.get("encode_job_id") == job["job_id"]
                and details.get("manifest_run_id", plan.run_id) == plan.run_id
                and details.get("source_size_bytes") == stage["source_size_bytes"]
                and details.get("source_path") == stage["source_path"]
                and normalize_codec(details.get("source_video_codec")) == normalize_codec(stage["source_video_codec"])
                and details.get("staging_path") == stage["staging_path"]
                and details.get("staging_size_bytes") == stage["staging_size_bytes"]
                and details.get("encode_completed_at") == stage["encode_completed_at"]
                and details.get("source_fingerprint", stage["source_fingerprint"]) == stage["source_fingerprint"]):
            return {**plan.reference(), "encode_job_id": job["job_id"], "completion_event_id": event["id"],
                    "published_path": stage["promoted_path"], "published_size_bytes": stage["staging_size_bytes"],
                    "promoted_at": stage["promoted_at"], "strength": "historical-operation"}
    return None


def _receipt_edges(connection: DBClient, item_id: int) -> list[_Edge]:
    edges: list[_Edge] = []
    for row in connection.execute(select(target_production_outcomes).where(
        target_production_outcomes.c.library_item_id == item_id,
    )).mappings():
        try:
            payload = json.loads(row["payload_json"])
            if stable_json_hash(payload) != row["payload_sha256"]:
                continue
            lineage = payload["lineage"]
            if stable_json_hash({key: value for key, value in lineage.items() if key != "payload_sha256"}) != lineage["payload_sha256"]:
                continue
            capsule = lineage["capsule"]
            if stable_json_hash({key: value for key, value in capsule.items() if key != "payload_sha256"}) != capsule["payload_sha256"]:
                continue
            before, after = lineage["identity_before"], lineage["identity_after"]
            item_identity = before["item_identity"]
            operation = payload["encode_identity"]
            if (before["status"] != "available" or after["status"] != "available"
                    or before["content_fingerprint"] != after["content_fingerprint"]
                    or item_identity != after["item_identity"] or operation["library_item_id"] != item_id
                    or item_identity["library_item_id"] != item_id
                    or any(capsule[key] != operation[key] for key in ("manifest_run_id", "item_index"))
                    or any(operation[key] != row[key] for key in ("manifest_run_id", "item_index", "encode_job_id"))
                    or payload["promoted_content_fingerprint"] != row["promoted_content_fingerprint"]):
                continue
            input_identity = _identity({"content_version_fingerprint": before["content_fingerprint"],
                                        "source_size_bytes": item_identity["source_size_bytes"]})
            output_identity = _identity({"content_version_fingerprint": payload["promoted_content_fingerprint"],
                                         "source_size_bytes": payload["promoted_size_bytes"]})
            if input_identity and output_identity:
                edges.append(_Edge(input_identity, output_identity, {"receipt_id": row["receipt_id"], **operation,
                                                                      "source_fingerprint": before["source_fingerprint"],
                                                                      "input_identity": input_identity, "output_identity": output_identity,
                                                                      "strength": "historical-sampled"}))
        except (KeyError, TypeError, ValueError):
            continue
    return edges


def _reachable(start: tuple[str, int], edges: list[_Edge]) -> set[tuple[str, int]]:
    reached = {start}
    while True:
        added = {edge.after for edge in edges if edge.before in reached} - reached
        if not added:
            return reached
        reached.update(added)


def _eligibility(connection: DBClient, catalog: Mapping[str, Any], stage: Mapping[str, Any] | DBRow,
                 plans: list[_PlanItem], jobs: dict[str, dict[str, Any]], events: list[dict[str, Any]],
                 unavailable_reason: str | None, context: SourceEvidenceContext) -> tuple[_PlanItem | None, dict[str, Any], str]:
    candidates = [plan for plan in plans if any(isinstance(plan.item.get(key), dict) for key in SUMMARY_KEYS.values())]
    if not candidates:
        return None, {}, "no_readable_source_summary_plan"
    direct = next(((plan, link) for plan in plans if (link := _completion_link(
        plan, stage, jobs.get(str(stage["encode_job_id"])), events, catalog,
    )) is not None), None)
    if direct is None:
        return None, {}, "incomplete_publish_link"
    direct_plan, direct_link = direct
    direct_identity = _identity(direct_plan.item)
    if direct_identity is None:
        return None, {}, "missing_sampled_identity_or_size"
    edges = _receipt_edges(connection, int(catalog["id"]))
    roots = [plan for plan in candidates if (identity := _identity(plan.item)) and direct_identity in _reachable(identity, edges)
             and (plan == direct_plan or any(
                 edge.before == _identity(plan.item) and edge.reference.get("manifest_run_id") == plan.run_id
                 and edge.reference.get("item_index") == plan.index
                 and edge.reference.get("source_fingerprint") == plan.item.get("source_fingerprint")
                 and jobs.get(str(edge.reference.get("encode_job_id")), {}).get("manifest_path") == plan.path
                 for edge in edges))]
    identities = {_identity(plan.item) for plan in roots}
    # The original is the earliest input in a fully connected chain, never the latest output's plan.
    roots = [plan for plan in roots if not any(_identity(plan.item) in _reachable(other, edges) - {other}
                                              for other in identities if other is not None)]
    if len({_identity(plan.item) for plan in roots}) != 1:
        return None, {}, "conflicting_or_unidentified_inputs"
    selected = sorted(roots, key=lambda plan: (plan.path, plan.index))[0]
    source_identity = _identity(selected.item)
    assert source_identity is not None
    reached = _reachable(source_identity, edges)
    if any(_identity(plan.item) not in reached for plan in plans):
        return None, {}, "conflicting_or_unidentified_inputs"
    source_plans = [plan for plan in plans if _identity(plan.item) == source_identity]
    if any(_stamp(plan.item) for plan in source_plans):
        return None, {}, "mediaforce_stamp"
    codecs = {normalize_codec(plan.item.get("video_codec")) for plan in source_plans}
    if len(codecs) != 1 or next(iter(codecs)) not in HISTORICAL_OUTPUT_PROFILES["known_input_codecs"]:
        return None, {}, "excluded_or_unknown_input_codec"
    if context.rule_conflicts(selected.item.get("video_codec")):
        return None, {}, "historical_output_conflict"
    for event in events:
        if event["event_type"] not in {"encoding_started", "encoding_completed"}:
            continue
        details = event["details"]
        matching_plan = next((plan for plan in plans if plan.path == details.get("manifest")
                              and plan.index == details.get("item_index")), None)
        expected_size = matching_plan.item.get("source_size_bytes") if matching_plan else source_identity[1]
        expected_codec = normalize_codec(matching_plan.item.get("video_codec")) if matching_plan else next(iter(codecs))
        if matching_plan is None:
            linked = next((edge for edge in edges if edge.reference.get("encode_job_id") == details.get("encode_job_id")
                           and edge.reference.get("item_index") == details.get("item_index")
                           and jobs.get(str(details.get("encode_job_id")), {}).get("manifest_path") == details.get("manifest")), None)
            if linked:
                expected_size = linked.before[1]
                expected_codec = next(iter(codecs)) if linked.before == source_identity else "av1"
        if ((details.get("source_size_bytes") is not None and details["source_size_bytes"] != expected_size)
                or (details.get("source_video_codec") and normalize_codec(details["source_video_codec"]) != expected_codec)):
            return None, {}, "conflicting_operation_input"
    for kind, key in SUMMARY_KEYS.items():
        summaries = {json_text(plan.item[key]) for plan in source_plans if isinstance(plan.item.get(key), dict)}
        if len(summaries) > 1:
            return None, {}, "conflicting_source_summaries"
    removed = next((event for event in events if source_identity == direct_identity
                    and event["event_type"] == "originals_removed_confirmed"
                    and event["created_at"] >= stage["promoted_at"]
                    and event["details"].get("archived_source_path") == stage["archived_source_path"]), None)
    if not removed and not unavailable_reason:
        return None, {}, "source_unavailability_not_established"
    if any(event["event_type"] == "published_copy_replaced" for event in events):
        return None, {}, "unexplained_replacement"
    checks = {"basis": "historical-plan", "raw_tags": "unavailable", "full_sha256": "unavailable",
              "codec_rule_hash": stable_json_hash(HISTORICAL_OUTPUT_PROFILES),
              "codec_rule": HISTORICAL_OUTPUT_PROFILES, "encode_link": direct_link,
              "published_input_identity": direct_identity,
              "ancestry_edges": [edge.reference for edge in edges if edge.before in reached],
              "plans": sorted((plan.reference() for plan in source_plans), key=json_text),
              "unavailability": {"event_id": removed["id"]} if removed else {"reason": unavailable_reason}}
    return selected, checks, ""


def _existing_summaries(connection: DBClient, catalog: Mapping[str, Any]) -> dict[str, set[str]]:
    active = connection.execute(select(source_versions.c.generation_id).where(
        source_versions.c.version_id == catalog.get("active_source_version_id"),
    )).scalar_one_or_none()
    summaries: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(select(source_measurements.c.kind, source_measurements.c.summary_json).join(source_versions).where(
        source_versions.c.generation_id == active, source_measurements.c.binding == "historical-plan-sampled",
    )).mappings():
        summaries[row["kind"]].add(row["summary_json"])
    return summaries


def _admission_conflict(original: Mapping[str, Any], checks: Mapping[str, Any], plans: list[_PlanItem],
                        events: list[dict[str, Any]], summaries: dict[str, set[str]], context: SourceEvidenceContext) -> str:
    identity = (original["sample_fingerprint"], original["size_bytes"])
    output_check = context.output_check(original)
    if output_check in {"match", "unresolved"}:
        return "known_output_match" if output_check == "match" else "possible_output_match"
    if context.rule_conflicts(original["video_codec"]):
        return "historical_output_conflict"
    references = {(reference["plan_path"], reference["run_id"], reference["item_index"]) for reference in checks.get("plans", [])}
    source_plans = []
    for plan in plans:
        if (plan.path, plan.run_id, plan.index) in references and _identity(plan.item) != identity:
            return "conflicting_or_unidentified_inputs"
        if _identity(plan.item) == identity:
            if normalize_codec(plan.item.get("video_codec")) != original["video_codec"]:
                return "historical_plan_changed"
            source_plans.append(plan)
    if any(_stamp(plan.item) for plan in source_plans):
        return "mediaforce_stamp"
    for kind, key in SUMMARY_KEYS.items():
        if any(isinstance(plan.item.get(key), dict) and summaries.get(kind)
               and json_text(plan.item[key]) not in summaries[kind] for plan in source_plans):
            return "historical_plan_changed"
    operations = {(plan.path, plan.index) for plan in source_plans}
    operations.update((path, index) for path, _run, index in references)
    for event in events:
        details = event["details"]
        if (event["event_type"] not in {"encoding_started", "encoding_completed"}
                or (details.get("manifest"), details.get("item_index")) not in operations):
            continue
        if ((details.get("source_size_bytes") is not None and details["source_size_bytes"] != identity[1])
                or (details.get("source_video_codec") and normalize_codec(details["source_video_codec"]) != original["video_codec"])):
            return "conflicting_operation_input"
    return ""


def _reassess_admission(connection: DBClient, catalog: Mapping[str, Any], plans: list[_PlanItem],
                        events: list[dict[str, Any]], context: SourceEvidenceContext,
                        stats: SourceEvidenceRestoreStats, *, apply: bool, now: str) -> bool:
    generation = connection.execute(select(source_versions.c.generation_id).where(
        source_versions.c.version_id == catalog.get("active_source_version_id"),
    )).scalar_one_or_none()
    superseded = select(source_admissions.c.supersedes_id).where(source_admissions.c.supersedes_id.is_not(None))
    admissions = connection.execute(select(source_admissions).where(
        source_admissions.c.generation_id == generation, source_admissions.c.basis == "historical-plan",
        source_admissions.c.admission_id.not_in(superseded),
    )).mappings().all()
    if len(admissions) != 1:
        return False
    admission = admissions[0]
    checks = json.loads(admission["checks_json"])
    original = connection.execute(select(source_versions).where(source_versions.c.version_id == admission["version_id"])).mappings().one()
    summaries = _existing_summaries(connection, catalog)
    reason = (str(checks.get("reason") or "original_not_assessed") if admission["tier"] != "probable" else
              _admission_conflict(original, checks, plans, events, summaries, context))
    if pending_source_operation(connection, int(catalog["id"])):
        reason = "pending_source_operation"
    if apply:
        _quarantine_candidates(connection, plans, reason or "historical-plan candidate", now)
        if reason:
            _invalidate_previous(connection, catalog, reason, now)
    source = load_source_evidence(connection, int(catalog["id"]), context=context)
    for kind, trait in source.traits.items():
        if reason or trait.summary is None:
            stats.unknown[kind][reason or trait.reason or "missing_source_measurement"] += 1
        else:
            stats.historical_probable[kind] += 1
            stats.already_recorded[kind] += 1
    return True


def restore_published_source_evidence(connection: DBClient, *, apply: bool = False,
                                      unavailable_sources: Mapping[int, str] | None = None) -> SourceEvidenceRestoreStats:
    """Recover advisory historical associations without opening original or output media."""
    stats = SourceEvidenceRestoreStats()
    plans = _read_plans(connection, stats)
    jobs = {row["job_id"]: dict(row) for row in connection.execute(select(encode_jobs)).mappings()}
    events: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in connection.execute(select(item_events).order_by(item_events.c.id)).mappings():
        try:
            details = json.loads(row["details_json"])
        except ValueError:
            details = {}
        events[row["library_item_id"]].append({**row, "details": object_dict(details)})
    tables = set(inspect(connection).get_table_names())
    has_storage = "source_versions" in tables
    context = load_source_evidence_context(connection)
    stages = connection.execute(select(staged_artifacts).where(staged_artifacts.c.promoted_at.is_not(None))).mappings().all()
    columns = {column["name"] for column in inspect(connection).get_columns("library_items")}
    catalog_rows = connection.execute(select(*(column for column in library_items.c if column.name in columns))).mappings()
    catalogs = {row["id"]: dict(row) for row in catalog_rows}
    now = timestamp()
    for stage in stages:
        catalog = catalogs.get(stage["library_item_id"])
        if catalog is None:
            continue
        stats.published_item_count += 1
        item_id = int(catalog["id"])
        if has_storage and _reassess_admission(connection, catalog, plans.get(item_id, []), events[item_id], context,
                                               stats, apply=apply, now=now):
            continue
        candidate, checks, reason = _eligibility(connection, catalog, stage, plans.get(item_id, []), jobs, events[item_id],
                                               (unavailable_sources or {}).get(item_id), context)
        if has_storage and pending_source_operation(connection, item_id):
            candidate, reason = None, "pending_source_operation"
        if candidate is not None and has_storage:
            identity = _identity(candidate.item)
            assert identity is not None
            if context.output_check({"sample_fingerprint": identity[0], "size_bytes": identity[1],
                                      "video_codec": candidate.item.get("video_codec")}) == "unresolved":
                candidate, reason = None, "possible_output_match"
        existing = _existing_summaries(connection, catalog) if has_storage else {}
        if candidate is not None and any(
            existing.get(kind) and isinstance(candidate.item.get(SUMMARY_KEYS[kind]), dict)
            and json_text(candidate.item[SUMMARY_KEYS[kind]]) not in existing[kind] for kind in EVIDENCE_KINDS
        ):
            candidate, reason = None, "historical_plan_changed"
        if apply and not has_storage:
            raise RuntimeError("Source evidence storage must be migrated before applying recovery")
        if apply:
            _quarantine_candidates(connection, plans.get(item_id, []), reason or "historical-plan candidate", now)
        if candidate is None:
            for kind in EVIDENCE_KINDS:
                stats.unknown[kind][reason] += 1
            if apply:
                _invalidate_previous(connection, catalog, reason, now)
            continue
        if apply:
            _import_candidate(connection, catalog, stage, candidate, checks, now)
        for kind in EVIDENCE_KINDS:
            summary = candidate.item.get(SUMMARY_KEYS[kind])
            if not isinstance(summary, dict):
                stats.unknown[kind]["missing_source_measurement"] += 1
            else:
                stats.historical_probable[kind] += 1
                if json_text(summary) in existing.get(kind, set()):
                    stats.already_recorded[kind] += 1
    return stats


def _quarantine_candidates(connection: DBClient, plans: list[_PlanItem], reason: str, now: str) -> None:
    for plan in plans:
        for kind in EVIDENCE_KINDS:
            summary = plan.item.get(SUMMARY_KEYS[kind])
            if isinstance(summary, dict):
                append_measurement(connection, version_id=None, kind=kind, binding="unbound", summary=summary,
                                   provenance={**plan.reference(), "reason": reason, "origin": "unverified historical measurement"},
                                   measured_at=None, recorded_at=now)


def _invalidate_previous(connection: DBClient, catalog: Mapping[str, Any], reason: str, now: str) -> None:
    if reason not in {"known_output_match", "possible_output_match", "historical_output_conflict", "conflicting_or_unidentified_inputs",
                      "conflicting_source_summaries", "historical_plan_changed", "mediaforce_stamp", "unexplained_replacement",
                      "conflicting_operation_input"}:
        return
    active = catalog.get("active_source_version_id")
    generation = connection.execute(select(source_versions.c.generation_id).where(source_versions.c.version_id == active)).scalar_one_or_none()
    superseded = select(source_admissions.c.supersedes_id).where(source_admissions.c.supersedes_id.is_not(None))
    for admission in connection.execute(select(source_admissions).where(
        source_admissions.c.generation_id == generation, source_admissions.c.tier == "probable",
        source_admissions.c.admission_id.not_in(superseded),
    )).mappings().all():
        connection.execute(source_admissions.insert().values(
            admission_id=stable_json_hash({"supersedes": admission["admission_id"], "reason": reason}),
            generation_id=generation, version_id=admission["version_id"], tier="unknown", basis="historical-plan",
            checks_json=json_text({"reason": reason}), supersedes_id=admission["admission_id"], recorded_at=now,
        ))


def _import_candidate(connection: DBClient, catalog: Mapping[str, Any], stage: Mapping[str, Any] | DBRow, plan: _PlanItem,
                      checks: dict[str, Any], now: str) -> None:
    source_identity = _identity(plan.item)
    assert source_identity is not None
    generation = stable_json_hash({"item": catalog["id"], "discovered_at": catalog["discovered_at"], "source": source_identity})
    active = connection.execute(select(source_versions).where(
        source_versions.c.version_id == catalog.get("active_source_version_id"),
    )).mappings().one_or_none()
    if active is not None and active["role"] == "output":
        generation = active["generation_id"]
    if not connection.execute(select(source_generations.c.generation_id).where(source_generations.c.generation_id == generation)).first():
        connection.execute(source_generations.insert().values(generation_id=generation, created_at=now))
    source = append_version(connection, generation_id=generation, role="input", recorded_at=now,
                            version_id=f"source_{generation}",
                            identity={"path": stage["archived_source_path"], "size_bytes": source_identity[1],
                                      "sample_fingerprint": source_identity[0], "strength": "historical-plan-sampled",
                                      "unavailability": checks["unavailability"],
                                      **plan.reference()}, profile={key: plan.item.get(key) for key in
                                                                   ("video_codec", "width", "height", "duration_seconds", "audio_summary")})
    output = active["version_id"] if active is not None and active["role"] == "output" else append_version(connection, generation_id=generation, role="output", recorded_at=now,
                            version_id=f"published_{stable_json_hash(checks['encode_link'])}",
                            identity={"path": stage["promoted_path"], "size_bytes": stage["staging_size_bytes"],
                                      "sample_fingerprint": catalog["content_version_fingerprint"], "strength": "historical-published-catalog"},
                            profile={key: catalog.get(key) for key in ("video_codec", "width", "height", "duration_seconds")})
    versions = {source_identity: source, (catalog["content_version_fingerprint"], stage["staging_size_bytes"]): output}
    for edge in checks["ancestry_edges"]:
        before_identity, after_identity = tuple(edge["input_identity"]), tuple(edge["output_identity"])
        for identity in (before_identity, after_identity):
            if identity not in versions:
                versions[identity] = append_version(
                    connection, generation_id=generation, role="output", recorded_at=now,
                    version_id=f"historical_{stable_json_hash({'generation': generation, 'identity': identity})}",
                    identity={"size_bytes": identity[1], "sample_fingerprint": identity[0], "strength": "historical-sampled"},
                    profile={},
                )
        edge_id = stable_json_hash({"kind": "historical-encode", "receipt": edge["receipt_id"]})
        if not connection.execute(select(source_transitions.c.operation_id).where(source_transitions.c.operation_id == edge_id)).first():
            connection.execute(source_transitions.insert().values(
                operation_id=edge_id, library_item_id=catalog["id"], before_version_id=versions[before_identity],
                after_version_id=versions[after_identity], expected_revision=int(catalog.get("source_revision") or 0),
                kind="historical-encode", outcome="committed", evidence_json=json_text(edge), recorded_at=now,
            ))
    operation_id = stable_json_hash({"kind": "historical-publish", "link": checks["encode_link"]})
    if not connection.execute(select(source_transitions.c.operation_id).where(source_transitions.c.operation_id == operation_id)).first():
        connection.execute(source_transitions.insert().values(
            operation_id=operation_id, library_item_id=catalog["id"],
            before_version_id=versions[tuple(checks["published_input_identity"])], after_version_id=output,
            expected_revision=int(catalog.get("source_revision") or 0), kind="historical-publish", outcome="committed",
            evidence_json=json_text(checks), recorded_at=now,
        ))
    superseded = select(source_admissions.c.supersedes_id).where(source_admissions.c.supersedes_id.is_not(None))
    previous = connection.execute(select(source_admissions).where(
        source_admissions.c.version_id == source, source_admissions.c.basis == "historical-plan",
        source_admissions.c.admission_id.not_in(superseded),
    )).mappings().one_or_none()
    if previous is not None:
        previous_checks = json.loads(previous["checks_json"])
        references = {json_text(reference): reference for reference in previous_checks.get("plans", []) + checks["plans"]}
        checks = {**checks, "plans": [references[key] for key in sorted(references)]}
    admission_id = stable_json_hash({"source": source, "checks": checks})
    if previous is None or (previous["tier"] == "probable" and previous["checks_json"] != json_text(checks)):
        connection.execute(source_admissions.insert().values(
            admission_id=admission_id, generation_id=generation, version_id=source, tier="probable", basis="historical-plan",
            checks_json=json_text(checks), supersedes_id=previous["admission_id"] if previous else None, recorded_at=now,
        ))
    for kind in EVIDENCE_KINDS:
        summary = parse_evidence_summary(json_text(plan.item.get(SUMMARY_KEYS[kind])))
        if summary is not None:
            append_measurement(connection, version_id=source, kind=kind, binding="historical-plan-sampled", summary=summary,
                               provenance={**plan.reference(), "basis": "historical-plan", "codec_rule_hash": checks["codec_rule_hash"]},
                               measured_at=None, recorded_at=now)
    if catalog.get("active_source_version_id") != output:
        result = connection.execute(update(library_items).where(
            library_items.c.id == catalog["id"], library_items.c.source_revision == int(catalog.get("source_revision") or 0),
        ).values(active_source_version_id=output, source_revision=library_items.c.source_revision + 1))
        if result.rowcount != 1:
            raise RuntimeError("Source revision changed during historical recovery")
