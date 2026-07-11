import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from mediaforce.core.type_defs import object_dict, object_list

EVIDENCE_SCHEMA = "mediaforce.evidence"
EVIDENCE_VERSION = 1


def stable_json_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def stable_policy_hash(policy: Mapping[str, Any] | None) -> str:
    return f"sha256:{stable_json_hash(object_dict(policy))}"


def stable_source_id(item: Mapping[str, Any]) -> str:
    library_item_id = item.get("library_item_id") or item.get("id")
    rel_path = str(item.get("rel_path") or "").strip().strip("/").replace("\\", "/")
    if library_item_id not in (None, ""):
        identity: dict[str, Any] = {"library_item_id": str(library_item_id)}
    elif rel_path:
        identity = {"rel_path": rel_path}
    else:
        identity = {
            "fingerprint": _optional_text(item.get("source_fingerprint", item.get("fingerprint"))),
            "file_name": str(item.get("file_name") or ""),
        }
    return f"src1_{stable_json_hash(identity)[:24]}"


def build_evidence_envelope(
        *,
        kind: str,
        sources: Sequence[Mapping[str, Any]],
        policy_hash: str,
        tool_name: str,
        tool_version: str,
        subject: Mapping[str, Any] | None = None,
        measurement: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_kind = kind.strip()
    normalized_tool_name = tool_name.strip()
    normalized_tool_version = tool_version.strip()
    if not normalized_kind:
        raise ValueError("Evidence kind is required")
    if not normalized_tool_name or not normalized_tool_version:
        raise ValueError("Evidence tool name and version are required")

    payload = {
        "schema": EVIDENCE_SCHEMA,
        "version": EVIDENCE_VERSION,
        "kind": normalized_kind,
        "subject": copy.deepcopy(object_dict(subject)),
        "inputs": {
            "sources": _normalized_sources(sources),
            "policy_hash": policy_hash.strip(),
            "tool": {
                "name": normalized_tool_name,
                "version": normalized_tool_version,
            },
        },
        "measurement": copy.deepcopy(object_dict(measurement)),
        "result": copy.deepcopy(object_dict(result)),
    }
    return {
        "evidence_id": f"ev1_{stable_json_hash(payload)[:32]}",
        **payload,
    }


def evidence_envelope_valid(envelope: Mapping[str, Any] | None) -> bool:
    payload = dict(object_dict(envelope))
    evidence_id = str(payload.pop("evidence_id", ""))
    if payload.get("schema") != EVIDENCE_SCHEMA or payload.get("version") != EVIDENCE_VERSION:
        return False
    return evidence_id == f"ev1_{stable_json_hash(payload)[:32]}"


def evidence_staleness(
        envelope: Mapping[str, Any],
        *,
        sources: Sequence[Mapping[str, Any]],
        policy_hash: str,
        tool_name: str,
        tool_version: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    if envelope.get("schema") != EVIDENCE_SCHEMA or envelope.get("version") != EVIDENCE_VERSION:
        reasons.append("schema_changed")

    inputs = object_dict(envelope.get("inputs"))
    recorded_sources = _normalized_sources(
        [object_dict(source) for source in object_list(inputs.get("sources"))]
    )
    current_sources = _normalized_sources(sources)
    recorded_by_id = {str(source["source_id"]): source.get("fingerprint") for source in recorded_sources}
    current_by_id = {str(source["source_id"]): source.get("fingerprint") for source in current_sources}
    if recorded_by_id.keys() != current_by_id.keys():
        reasons.append("source_set_changed")
    if any(
            recorded_by_id[source_id] != current_by_id[source_id]
            for source_id in recorded_by_id.keys() & current_by_id.keys()
    ):
        reasons.append("source_fingerprint_changed")
    if str(inputs.get("policy_hash") or "") != policy_hash.strip():
        reasons.append("policy_changed")

    recorded_tool = object_dict(inputs.get("tool"))
    if (
            str(recorded_tool.get("name") or "") != tool_name.strip()
            or str(recorded_tool.get("version") or "") != tool_version.strip()
    ):
        reasons.append("tool_changed")
    return {"stale": bool(reasons), "reasons": reasons}


def _normalized_sources(sources: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [
        {
            "source_id": str(source.get("source_id") or "").strip(),
            "fingerprint": _optional_text(source.get("fingerprint", source.get("source_fingerprint"))),
        }
        for source in sources
    ]
    if any(not source["source_id"] for source in normalized):
        raise ValueError("Evidence sources require stable source IDs")
    return sorted(normalized, key=lambda source: (str(source["source_id"]), str(source.get("fingerprint") or "")))


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
