import hashlib
import json
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import exists, inspect, literal_column, or_, select, update

from mediaforce.core.db import DBClient, DBRow
from mediaforce.core.db_tables import library_items, source_admissions, source_generations, source_measurements, \
    source_moves, source_transitions, source_versions
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.db_tables import target_production_outcomes
from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.utils import descriptor_content_version_fingerprint, descriptor_sha256, file_stat_signature, timestamp
from mediaforce.library.evidence_state import EVIDENCE_KINDS, EvidenceKind, parse_evidence_summary
from mediaforce.encoding.cadence import cadence_measurement_complete

# Design #971 section 6 records library encoders from the first commit through the reviewed history.
# Tag/profile coverage for readable-source admission is separate and is not implemented here.
HISTORICAL_OUTPUT_PROFILES = {
    "version": "library-av1-v1",
    "codecs": ["av1"],
    "encoders": ["libsvtav1", "av1_videotoolbox"],
    "known_input_codecs": ["h264", "hevc", "mpeg2video", "mpeg4", "mpeg1video", "vc1", "vp8", "vp9", "theora",
                           "wmv3", "prores", "mjpeg", "ffv1", "dnxhd", "h263", "rawvideo"],
    "scope": "mediaforce library outputs, 5e47472b through 39f88fd1, including pre-2026-04-01",
    "evidence": ["5e47472b:mediaforce.py", "a3f24133:src/mediaforce/services/encoder.py",
                 "9ee6ffad:config/defaults.toml", "b8cc8ad2:mediaforce/encoding/commands.py", "#971 design section 6"],
}
SUMMARY_KEYS = {"cadence_analysis": "cadence_summary", "media_fingerprint": "media_fingerprint"}
TraitTier = Literal["proven", "probable", "unknown"]


@dataclass(frozen=True, slots=True)
class SourceTrait:
    kind: str
    tier: TraitTier = "unknown"
    basis: str | None = None
    binding: str | None = None
    measurement_ids: tuple[str, ...] = ()
    summary: dict[str, Any] | None = None
    reason: str | None = "original_not_assessed"


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    generation_id: str | None
    active_version_id: str | None
    original_version_id: str | None
    availability: str
    source_path: str | None
    profile: dict[str, Any]
    traits: dict[EvidenceKind, SourceTrait]

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    def to_reference(self) -> dict[str, Any]:
        return {"generation_id": self.generation_id, "active_version_id": self.active_version_id,
                "original_version_id": self.original_version_id, "availability": self.availability,
                "traits": {kind: {"tier": trait.tier, "basis": trait.basis, "binding": trait.binding,
                                  "measurement_ids": list(trait.measurement_ids), "reason": trait.reason}
                           for kind, trait in self.traits.items()}}


def json_text(payload: object) -> str:
    return canonical_json_bytes(payload).decode("utf-8")


def normalize_codec(codec: object) -> str:
    value = str(codec or "").strip().casefold()
    for family in ("hevc", "h264", "av1", "prores"):
        if value.startswith(f"{family}_"):
            return family
    return {"h265": "hevc", "h.265": "hevc", "h.264": "h264", "avc": "h264", "libx265": "hevc",
            "libx264": "h264", "libsvtav1": "av1", "libaom-av1": "av1",
            "librav1e": "av1", "libvpx-vp9": "vp9", "libvpx": "vp8"}.get(value, value)


def observe_file(path: Path, *, full_digest: bool = False,
                 check_cancelled: Callable[[], None] | None = None) -> dict[str, Any]:
    """An observation, never a claim of uninterrupted custody on local or shared storage."""
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        sample = descriptor_content_version_fingerprint(handle.fileno(), size_bytes=before.st_size)
        digest = descriptor_sha256(handle.fileno(), check_cancelled=check_cancelled, size_bytes=before.st_size) if full_digest else None
        after_sample = descriptor_content_version_fingerprint(handle.fileno(), size_bytes=before.st_size)
        if (file_stat_signature(before) != file_stat_signature(os.fstat(handle.fileno()))
                or file_stat_signature(before) != file_stat_signature(path.stat()) or sample != after_sample):
            raise RuntimeError(f"File changed during source observation: {path}")
    return {"path": str(path), "size_bytes": before.st_size, "sample_fingerprint": sample,
            "sha256": digest, "stat": list(file_stat_signature(before)), "strength": "observed"}


def pending_source_operation(connection: DBClient, item_id: int) -> bool:
    return bool(connection.execute(select(exists().where(
        source_transitions.c.library_item_id == item_id,
        source_transitions.c.outcome.in_(("prepared", "unresolved")),
    ))).scalar())


def append_version(connection: DBClient, *, generation_id: str, role: str, identity: Mapping[str, Any],
                   profile: Mapping[str, Any], recorded_at: str, version_id: str | None = None) -> str:
    version = version_id or uuid4().hex
    if connection.execute(select(source_versions.c.version_id).where(source_versions.c.version_id == version)).first():
        return version
    connection.execute(source_versions.insert().values(
        version_id=version, generation_id=generation_id, role=role, sha256=identity.get("sha256"),
        size_bytes=identity.get("size_bytes"), sample_fingerprint=identity.get("sample_fingerprint"),
        video_codec=normalize_codec(profile.get("video_codec")) or None,
        identity_json=json_text(identity), profile_json=json_text(profile), observed_at=recorded_at,
    ))
    return version


def ensure_active_source_version(connection: DBClient, item: Mapping[str, Any] | DBRow) -> str:
    item_id = int(item["id"])
    if pending_source_operation(connection, item_id):
        raise RuntimeError("Source has a pending publication operation")
    active = item.get("active_source_version_id")
    if active:
        version = connection.execute(select(source_versions).where(source_versions.c.version_id == active)).mappings().one()
        identity = json.loads(version["identity_json"])
        if (version["size_bytes"] == item["size_bytes"]
                and ((version["sample_fingerprint"] and version["sample_fingerprint"] == item.get("content_version_fingerprint"))
                     or (not version["sample_fingerprint"] and identity.get("mtime_ns") == item["mtime_ns"]))):
            return str(active)
    generation_id = uuid4().hex
    now = timestamp()
    connection.execute(source_generations.insert().values(generation_id=generation_id, created_at=now))
    version_id = append_version(
        connection, generation_id=generation_id, role="unknown", recorded_at=now,
        identity={"path": item["source_path"], "size_bytes": item["size_bytes"], "mtime_ns": item["mtime_ns"],
                  "sample_fingerprint": item.get("content_version_fingerprint"), "strength": "catalog-observed"},
        profile={key: item.get(key) for key in ("video_codec", "width", "height", "duration_seconds", "audio_summary_json")},
    )
    result = connection.execute(update(library_items).where(
        library_items.c.id == item_id, library_items.c.source_revision == int(item.get("source_revision") or 0),
    ).values(active_source_version_id=version_id, source_revision=library_items.c.source_revision + 1))
    if result.rowcount != 1:
        raise RuntimeError("Source revision changed")
    return version_id


def append_measurement(connection: DBClient, *, version_id: str | None, kind: EvidenceKind, binding: str,
                       summary: Mapping[str, Any], provenance: Mapping[str, Any], measured_at: str | None,
                       recorded_at: str) -> str:
    raw = json_text(summary)
    measurement_id = stable_json_hash({"version": version_id, "kind": kind, "binding": binding,
                                       "summary": summary, "provenance": provenance})
    if not connection.execute(select(source_measurements.c.measurement_id).where(
        source_measurements.c.measurement_id == measurement_id,
    )).first():
        connection.execute(source_measurements.insert().values(
            measurement_id=measurement_id, version_id=version_id, kind=kind, binding=binding,
            summary_json=raw, summary_sha256=hashlib.sha256(raw.encode()).hexdigest(),
            provenance_json=json_text(provenance), measured_at=measured_at, recorded_at=recorded_at,
        ))
    return measurement_id


def record_current_measurement(connection: DBClient, item: Mapping[str, Any] | DBRow, *, kind: EvidenceKind,
                               summary: Mapping[str, Any], provenance: Mapping[str, Any], binding: str) -> str:
    version_id = ensure_active_source_version(connection, item)
    return append_measurement(connection, version_id=version_id, kind=kind, binding=binding, summary=summary,
                              provenance=provenance, measured_at=timestamp(), recorded_at=timestamp())


def record_generated_output(connection: DBClient, item_id: int, *, identity: Mapping[str, Any],
                            profile: Mapping[str, Any], operation_key: Mapping[str, Any], recorded_at: str) -> str:
    item = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
    same_identity = (bool(operation_key.get("input_sample"))
                     and operation_key["input_sample"] == item["content_version_fingerprint"]
                     and operation_key.get("input_size_bytes") == item["size_bytes"])
    if (operation_key.get("input_size_bytes", item["size_bytes"]) != item["size_bytes"]
            or (operation_key.get("input_sample") and item["content_version_fingerprint"]
                and operation_key["input_sample"] != item["content_version_fingerprint"])
            or (operation_key.get("source_revision") is not None and operation_key["source_revision"] != item["source_revision"]
                and not same_identity)):
        raise RuntimeError("Encode input changed before output commit")
    before = ensure_active_source_version(connection, item)
    generation = connection.execute(select(source_versions.c.generation_id).where(source_versions.c.version_id == before)).scalar_one()
    operation_id = stable_json_hash({"kind": "encode", "key": operation_key, "output": identity})
    after = append_version(connection, generation_id=generation, role="output", identity=identity,
                           profile=profile, recorded_at=recorded_at, version_id=f"output_{operation_id}")
    if not connection.execute(select(source_transitions.c.operation_id).where(source_transitions.c.operation_id == operation_id)).first():
        connection.execute(source_transitions.insert().values(
            operation_id=operation_id, library_item_id=item_id, before_version_id=before, after_version_id=after,
            expected_revision=int(item.get("source_revision") or 0), kind="encode", outcome="committed",
            evidence_json=json_text(operation_key), recorded_at=recorded_at,
        ))
    return after


def version_location(connection: DBClient, version: Mapping[str, Any] | DBRow) -> str | None:
    return version_locations(connection, [version])[version["version_id"]]


def version_locations(connection: DBClient, versions: Sequence[Mapping[str, Any] | DBRow]) -> dict[str, str | None]:
    locations = {}
    for version in versions:
        identity = json.loads(version["identity_json"])
        locations[version["version_id"]] = None if identity.get("unavailability") else identity.get("path")
    if not locations:
        return locations
    moves = connection.execute(select(source_moves.c.version_id, source_moves.c.to_path, source_transitions.c.outcome).join(source_transitions)
                               .where(source_moves.c.version_id.in_(locations))
                               .order_by(source_transitions.c.recorded_at.desc(), source_transitions.c.expected_revision.desc(),
                                         source_moves.c.ordinal.desc())).mappings().all()
    resolved = set()
    for move in moves:
        version_id = move["version_id"]
        if version_id in resolved:
            continue
        if move["outcome"] in {"prepared", "unresolved"}:
            locations[version_id] = None
        elif move["outcome"] == "committed":
            locations[version_id] = str(move["to_path"]) or None
        else:
            continue
        resolved.add(version_id)
    return locations


@dataclass(frozen=True, slots=True)
class SourceEvidenceContext:
    conflicting_output_codecs: frozenset[str]
    outputs: tuple[Mapping[str, Any], ...]
    sampled_outputs: Mapping[tuple[str, int], tuple[Mapping[str, Any], ...]]
    unresolved_output_rule: bool
    output_index_hash: str

    def rule_conflicts(self, codec: object) -> bool:
        return self.unresolved_output_rule or self.known_rule_conflicts(codec)

    def known_rule_conflicts(self, codec: object) -> bool:
        # Unrecognized encoder names hide traits on read but never append permanent corrections.
        return normalize_codec(codec) in self.conflicting_output_codecs

    def output_check(self, candidate: Mapping[str, Any] | DBRow) -> str:
        outputs = self.outputs if candidate.get("sha256") else self.sampled_outputs.get(
            (candidate.get("sample_fingerprint"), candidate.get("size_bytes")), (),
        )
        return known_output_check(candidate, outputs)


def _library_output(origin: object) -> bool:
    return str(origin or "").strip().casefold() in {"", "cli", "cli-production", "queue"}


def load_source_evidence_context(connection: DBClient) -> SourceEvidenceContext:
    """One pass-local snapshot of output identities and the codec exclusions they establish."""
    outputs: tuple[Mapping[str, Any], ...] = ()
    conflicts: set[str] = set()
    if inspect(connection).has_table("source_versions"):
        outputs = tuple(connection.execute(select(source_versions).where(source_versions.c.role == "output")).mappings().all())
        library_outputs = set()
        for row in connection.execute(select(source_transitions.c.after_version_id, source_transitions.c.evidence_json).where(
            source_transitions.c.kind.in_(("encode", "publish", "historical-encode", "historical-publish")),
            source_transitions.c.outcome == "committed",
        )).mappings():
            try:
                evidence = json.loads(row["evidence_json"])
            except (ValueError, TypeError):
                continue
            if isinstance(evidence, dict) and _library_output(evidence.get("encode_origin")):
                library_outputs.add(row["after_version_id"])
        conflicts.update(normalize_codec(row["video_codec"]) for row in outputs
                         if row["version_id"] in library_outputs and row["video_codec"])
    stages = connection.execute(select(staged_artifacts).where(staged_artifacts.c.encode_completed_at.is_not(None))).mappings().all()
    commands = [row["encode_command_json"] for row in stages if _library_output(row["encode_origin"])]
    for receipt in connection.execute(select(target_production_outcomes.c.payload_json, target_production_outcomes.c.payload_sha256)).mappings():
        try:
            payload = json.loads(receipt["payload_json"])
            if (isinstance(payload, dict) and stable_json_hash(payload) == receipt["payload_sha256"]
                    and _library_output(payload.get("encode_origin"))):
                commands.append(payload.get("encode_command_json"))
        except (ValueError, TypeError):
            continue
    for raw in commands:
        try:
            command = json.loads(raw or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(command, list):
            continue
        conflicts.update(normalize_codec(command[index + 1]) for index, argument in enumerate(command[:-1])
                         if str(argument) in {"-c:v", "-codec:v", "-vcodec", "-c:v:0"})
    retained = []
    for stage in stages:
        try:
            command = json.loads(stage["encode_command_json"] or "[]")
            codec = next((normalize_codec(command[index + 1]) for index, argument in enumerate(command[:-1])
                          if str(argument) in {"-c:v", "-codec:v", "-vcodec", "-c:v:0"}), "")
        except (ValueError, TypeError):
            codec = ""
        retained.append({"sha256": None, "sample_fingerprint": None, "size_bytes": stage["staging_size_bytes"],
                         "video_codec": codec})
    outputs += tuple(retained)
    known_codecs = set(HISTORICAL_OUTPUT_PROFILES["known_input_codecs"]) | set(HISTORICAL_OUTPUT_PROFILES["codecs"])
    sampled: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for output in outputs:
        if output["sample_fingerprint"] and output["size_bytes"] is not None:
            sampled[(output["sample_fingerprint"], output["size_bytes"])].append(output)
    return SourceEvidenceContext(frozenset(conflicts - set(HISTORICAL_OUTPUT_PROFILES["codecs"])), outputs,
                                 {key: tuple(rows) for key, rows in sampled.items()}, bool(conflicts - known_codecs),
                                 stable_json_hash([dict(row) for row in outputs]))


def known_output_check(candidate: Mapping[str, Any] | DBRow,
                       outputs: Iterable[Mapping[str, Any] | DBRow]) -> Literal["match", "unresolved", "clear", "unavailable"]:
    unresolved = False
    for output in outputs:
        if candidate.get("sha256") and candidate["sha256"] == output.get("sha256"):
            return "match"
        if (candidate.get("size_bytes") is not None and output.get("size_bytes") is not None
                and candidate["size_bytes"] != output["size_bytes"]):
            continue
        if (normalize_codec(candidate.get("video_codec")) and normalize_codec(output.get("video_codec"))
                and normalize_codec(candidate["video_codec"]) != normalize_codec(output["video_codec"])):
            continue
        if candidate.get("sha256"):
            unresolved |= not output.get("sha256")
        elif (candidate.get("sample_fingerprint") and candidate["sample_fingerprint"] == output.get("sample_fingerprint")
              and candidate.get("size_bytes") == output.get("size_bytes")):
            unresolved = True
    if unresolved:
        return "unresolved"
    return "clear" if candidate.get("sha256") else "unavailable"


def load_source_evidence(connection: DBClient, item_id: int, *, context: SourceEvidenceContext | None = None) -> SourceEvidence:
    return load_source_evidence_bulk(connection, [item_id], context=context)[item_id]


def load_source_evidence_bulk(
        connection: DBClient,
        item_ids: Iterable[int],
        *,
        context: SourceEvidenceContext | None = None,
) -> dict[int, SourceEvidence]:
    requested = tuple(dict.fromkeys(int(item_id) for item_id in item_ids))
    if not requested:
        return {}
    items = {int(row["id"]): row for row in connection.execute(
        select(library_items).where(library_items.c.id.in_(requested))
    ).mappings()}
    active_ids = {str(row["active_source_version_id"]) for row in items.values() if row["active_source_version_id"]}
    current_versions = {str(row["version_id"]): row for row in connection.execute(
        select(source_versions).where(source_versions.c.version_id.in_(active_ids))
    ).mappings()} if active_ids else {}
    generations = {str(row["generation_id"]) for row in current_versions.values()}
    superseded = select(source_admissions.c.supersedes_id).where(source_admissions.c.supersedes_id.is_not(None))
    admission_rows = connection.execute(select(source_admissions).where(
        source_admissions.c.generation_id.in_(generations),
        source_admissions.c.admission_id.not_in(superseded),
    )).mappings() if generations else ()
    admissions_by_generation: dict[str, list[DBRow]] = defaultdict(list)
    for row in admission_rows:
        admissions_by_generation[str(row["generation_id"])].append(row)
    original_ids = {str(rows[0]["version_id"]) for rows in admissions_by_generation.values() if len(rows) == 1}
    originals = {str(row["version_id"]): row for row in connection.execute(
        select(source_versions).where(source_versions.c.version_id.in_(original_ids))
    ).mappings()} if original_ids else {}
    pending_items = {int(row["library_item_id"]) for row in connection.execute(
        select(source_transitions.c.library_item_id).where(
            source_transitions.c.library_item_id.in_(requested),
            source_transitions.c.outcome.in_(("prepared", "unresolved")),
        )
    )}
    readable_measurement_ids = {
        str(measurement_id)
        for rows in admissions_by_generation.values() for row in rows
        if len(rows) == 1 and row["basis"] == "readable-source"
        for measurement_id in json.loads(row["checks_json"]).get("measurement_ids", [])
    }
    measurement_rows = list(connection.execute(select(source_measurements).where(or_(
        source_measurements.c.version_id.in_(original_ids),
        source_measurements.c.measurement_id.in_(readable_measurement_ids),
    )).order_by(literal_column("source_measurements.rowid").desc())).mappings()) if original_ids else []
    measurements = _MeasurementRows(measurement_rows)
    locations = version_locations(connection, list(originals.values()))
    if originals and context is None:
        context = load_source_evidence_context(connection)
    return {
        item_id: _classify_source_evidence(
            connection, item_id, items.get(item_id), current_versions, admissions_by_generation,
            originals, measurements, pending_items, locations, context,
        )
        for item_id in requested
    }


class _MeasurementRows:
    def __init__(self, rows: Sequence[DBRow]) -> None:
        self._by_version: dict[tuple[str, str], list[DBRow]] = defaultdict(list)
        self._by_id = {str(row["measurement_id"]): row for row in rows}
        self._order = {str(row["measurement_id"]): index for index, row in enumerate(rows)}
        for row in rows:
            self._by_version[str(row["version_id"]), str(row["kind"])].append(row)

    def for_original(self, version_id: str, kind: str, allowed_ids: set[str]) -> list[DBRow]:
        rows = {str(row["measurement_id"]): row for row in self._by_version.get((version_id, kind), ())}
        rows.update((measurement_id, row) for measurement_id in allowed_ids
                    if (row := self._by_id.get(measurement_id)) is not None and row["kind"] == kind)
        return sorted(rows.values(), key=lambda row: self._order[str(row["measurement_id"])])


def _classify_source_evidence(
        connection: DBClient,
        item_id: int,
        item: DBRow | None,
        current_versions: Mapping[str, DBRow],
        admissions_by_generation: Mapping[str, Sequence[DBRow]],
        originals: Mapping[str, DBRow],
        measurements: _MeasurementRows,
        pending_items: set[int],
        locations: Mapping[str, str | None],
        context: SourceEvidenceContext | None,
) -> SourceEvidence:
    active = str((item or {}).get("active_source_version_id") or "") or None
    traits: dict[EvidenceKind, SourceTrait] = {kind: SourceTrait(kind) for kind in EVIDENCE_KINDS}
    empty = SourceEvidence(None, active, None, "unknown", None, {}, traits)
    if not active or item is None:
        return empty
    current = current_versions[active]
    generation = str(current["generation_id"])
    empty = SourceEvidence(generation, active, None, "unknown", None, {}, traits)
    if (item_id in pending_items or current["size_bytes"] != item["size_bytes"]
            or (current["sample_fingerprint"] and current["sample_fingerprint"] != item["content_version_fingerprint"])):
        return SourceEvidence(generation, active, None, "unknown", None, {},
                              {kind: SourceTrait(kind, reason="source_attachment_unresolved") for kind in EVIDENCE_KINDS})
    admissions = admissions_by_generation.get(generation, ())
    if len(admissions) != 1:
        return empty
    admission = admissions[0]
    original = originals[str(admission["version_id"])]
    checks = json.loads(admission["checks_json"])
    historical = (admission["tier"] == "probable" and admission["basis"] == "historical-plan"
                  and original["role"] == "input"
                  and normalize_codec(original["video_codec"]) in HISTORICAL_OUTPUT_PROFILES["known_input_codecs"]
                  and checks.get("codec_rule_hash") == stable_json_hash(HISTORICAL_OUTPUT_PROFILES)
                  and checks.get("encode_link") and checks.get("unavailability"))
    reason = str(checks.get("reason") or "original_not_assessed")
    readable = admission["tier"] == "probable" and admission["basis"] == "readable-source" and original["role"] == "input"
    if readable:
        from mediaforce.library.source_intake import assess_readable_source

        refreshed = assess_readable_source(connection, cast(dict[str, Any], dict(original)), checks.get("raw_probe", {}), context=context)
        reason = refreshed.get("reason") or ""
        if (checks.get("codec_rule_hash") != refreshed.get("codec_rule_hash")
                or checks.get("encoder_rule_hash") != refreshed.get("encoder_rule_hash")):
            reason = "original_rule_changed"
    if historical:
        context = context or load_source_evidence_context(connection)
        reason = "historical_output_conflict" if context.rule_conflicts(original["video_codec"]) else ""
        output_check = context.output_check(original)
        if output_check in {"match", "unresolved"}:
            reason = "known_output_match" if output_check == "match" else "possible_output_match"
    # Protected owner confirmation and strict binding are owned by #999. Application-DB rows alone confer no authority.
    for kind in EVIDENCE_KINDS:
        allowed_ids = set(checks.get("measurement_ids", []) if readable else [])
        rows = measurements.for_original(str(original["version_id"]), kind, allowed_ids)
        eligible = [row for row in rows if not reason and row["binding"] == "historical-plan-sampled"
                    and _historical_measurement_matches(row, checks)]
        if readable and not reason:
            eligible = [row for row in rows if observed_measurement_matches(row, cast(dict[str, Any], dict(original)))][:1]
        summaries = {row["summary_json"] for row in eligible}
        if len(summaries) == 1:
            selected = eligible[0]
            summary = parse_evidence_summary(selected["summary_json"])
            if summary is not None:
                traits[kind] = SourceTrait(kind, "probable", admission["basis"], selected["binding"],
                                           tuple(row["measurement_id"] for row in eligible), summary, None)
                continue
        traits[kind] = SourceTrait(kind, reason=reason or ("conflicting_measurements" if eligible else "missing_source_measurement"))
    location = locations[original["version_id"]]
    availability = "available" if readable and not reason and location else "unavailable" if checks.get("unavailability") or not location else "unknown"
    return SourceEvidence(generation, active, original["version_id"], availability, location,
                          {**json.loads(original["profile_json"]), "source_size_bytes": original["size_bytes"],
                           "content_version_fingerprint": original["sample_fingerprint"]}, traits)


def observed_measurement_matches(row: DBRow, identity: Mapping[str, Any], *, require_stat: bool = False) -> bool:
    provenance = json.loads(row["provenance_json"])
    before, after = provenance.get("before", {}), provenance.get("after", {})
    summary = parse_evidence_summary(row["summary_json"])
    if summary is None:
        return False
    return (row["binding"] == "observed" and bool(before) and before == after
            and before.get("strength") == "observed" and isinstance(before.get("stat"), list)
            and before.get("sample_fingerprint") == identity["sample_fingerprint"]
            and before.get("size_bytes") == identity["size_bytes"]
            and (not require_stat or before.get("stat") == identity.get("stat"))
            and hashlib.sha256(row["summary_json"].encode()).hexdigest() == row["summary_sha256"]
            and (summary.get("decision", {}).get("status") in {"measured", "resolved"}
                                  or (row["kind"] == "cadence_analysis" and cadence_measurement_complete(summary.get("analysis", {})))))


def _historical_measurement_matches(row: DBRow, checks: Mapping[str, Any]) -> bool:
    provenance = json.loads(row["provenance_json"])
    references = checks.get("plans", [])
    return (provenance.get("basis") == "historical-plan" and provenance.get("codec_rule_hash") == checks.get("codec_rule_hash")
            and hashlib.sha256(row["summary_json"].encode()).hexdigest() == row["summary_sha256"]
            and any(all(provenance.get(key) == value for key, value in reference.items()) for reference in references))


def source_trait_coverage(evidence: Sequence[SourceEvidence | Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    records = [record.to_payload() if isinstance(record, SourceEvidence) else record for record in evidence]
    unique = {record.get("generation_id") or f"unknown:{index}": record for index, record in enumerate(records)}
    result: dict[str, dict[str, int]] = {}
    for kind in EVIDENCE_KINDS:
        counts = Counter({"proven": 0, "probable:readable-source": 0, "probable:historical-plan": 0, "unknown": 0})
        for record in unique.values():
            trait = record.get("traits", {}).get(kind, {})
            tier = trait.get("tier", "unknown")
            counts[f"probable:{trait.get('basis')}" if tier == "probable" else tier] += 1
        result[kind] = {"total": len(unique), **counts}
    return result


def original_trait_summary(item: Mapping[str, Any], kind: EvidenceKind) -> dict[str, Any] | None:
    source = item.get("original_source_evidence") or {}
    trait = source.get("traits", {}).get(kind, {})
    return trait.get("summary") if trait.get("tier") in {"proven", "probable"} else None
