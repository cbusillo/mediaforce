from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Iterator

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_CONFIGURATIONS,
    AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_REVISED_AT,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_SOURCE_LAYOUT,
    AV1_VALIDATION_V4_TRAVERSAL_COUNT,
    AV1_VALIDATION_V4_VALID_UNTIL,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4_freeze import (
    AV1ValidationV4FreezeError,
    assert_av1_validation_v4_manifest_freeze,
)


AV1_VALIDATION_V4_QUALIFICATION_REQUEST_SCHEMA = (
    "mediaforce.av1_cold_start_v4_qualification_request"
)
AV1_VALIDATION_V4_QUALIFICATION_REQUEST_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_QUALIFICATION_REQUEST_CONTRACT_VERSION = "av1v4qreq1"
AV1_VALIDATION_V4_QUALIFICATION_REQUEST_STATE = "owner_submitted"

AV1_VALIDATION_V4_QUALIFICATION_GRANT_SCHEMA = (
    "mediaforce.av1_cold_start_v4_qualification_grant"
)
AV1_VALIDATION_V4_QUALIFICATION_GRANT_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_QUALIFICATION_GRANT_CONTRACT_VERSION = "av1v4qgrant1"
AV1_VALIDATION_V4_QUALIFICATION_GRANT_STATE = "owner_authorized"
AV1_VALIDATION_V4_QUALIFICATION_GRANT_AUTHORITY = "av1_v4_qualification_execution"
AV1_VALIDATION_V4_QUALIFICATION_GRANT_MAX_SECONDS = 14_400

AV1_VALIDATION_V4_QUALIFICATION_CLAIM_SCHEMA = (
    "mediaforce.av1_cold_start_v4_qualification_claim"
)
AV1_VALIDATION_V4_QUALIFICATION_CLAIM_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_QUALIFICATION_CLAIM_CONTRACT_VERSION = "av1v4qclaim1"
AV1_VALIDATION_V4_QUALIFICATION_CLAIM_STATE = "grant_consumed"

# Exactly these three fields from AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS are
# True in the grant and only in the grant.
_QUALIFICATION_GRANT_AUTHORIZED_FIELDS = frozenset({
    "media_read_authorized",
    "qualification_execution_authorized",
    "runtime_execution_authorized",
})
if not _QUALIFICATION_GRANT_AUTHORIZED_FIELDS <= AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
    raise RuntimeError(
        "AV1 v4 qualification grant authorized fields are not in the false authority set"
    )
_QUALIFICATION_GRANT_FALSE_FIELDS = (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS - _QUALIFICATION_GRANT_AUTHORIZED_FIELDS
)

_GRANT_SCOPE = {
    "adaptive_continuation_authorized": False,
    "backfill_authorized_scope": False,
    "owner_authorization_confirmed": True,
    "resume_run_authorized": False,
    "run_scope": "one_run_wide",
    "single_use_authorized": True,
    "substitute_source_authorized": False,
    "traversal_count": AV1_VALIDATION_V4_TRAVERSAL_COUNT,
}
if set(_GRANT_SCOPE) & AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
    raise RuntimeError(
        "AV1 v4 qualification grant scope collides with false authority fields"
    )

_REQUEST_ID_RE = re.compile(r"av1vqualreq4_[0-9a-f]{32}\Z")
_GRANT_ID_RE = re.compile(r"av1vqualgrant4_[0-9a-f]{32}\Z")
_CLAIM_ID_RE = re.compile(r"av1vqualclaim4_[0-9a-f]{32}\Z")
_FREEZE_ID_RE = re.compile(r"av1vfreeze4_[0-9a-f]{32}\Z")
_PREPARATION_ID_RE = re.compile(r"av1vprep4_[0-9a-f]{32}\Z")
_RUNTIME_ID_RE = re.compile(r"av1vruntime4_[0-9a-f]{32}\Z")
_PATH_KEY_ID_RE = re.compile(r"av1vpathkey4_[0-9a-f]{32}\Z")
_CONFIG_ID_RE = re.compile(r"av1vconfig4_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_INVOCATION_KEYS = frozenset({
    "asset_id",
    "base_config_sha256",
    "configuration",
    "excluded_stream_indexes",
    "mode",
    "ordinal",
    "sha256",
    "source_path_hmac_id",
    "video_stream_index",
})

_REQUEST_ALLOWED_KEYS = frozenset({
    "config_canonical_file_sha256",
    "config_id",
    "consumption_registry",
    "contract_version",
    "discovery_public_sha256",
    "execution_repository",
    "experiment_id",
    "freeze_id",
    "freeze_payload_sha256",
    "invocations",
    "manifest_id",
    "manifest_payload_sha256",
    "manifest_revision",
    "owner_principal",
    "path_privacy_key_id",
    "payload_sha256",
    "preparation_id",
    "preparation_payload_sha256",
    "preparation_repository",
    "protocol_version",
    "request_id",
    "requested_at",
    "runtime_compatibility_id",
    "schema",
    "schema_version",
    "state",
    "valid_until",
}) | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS

_GRANT_ALLOWED_KEYS = frozenset({
    "authority",
    "authorized_at",
    "contract_version",
    "discovery_public_sha256",
    "experiment_id",
    "grant_id",
    "grant_scope",
    "manifest_id",
    "manifest_payload_sha256",
    "manifest_revision",
    "owner_principal",
    "payload_sha256",
    "protocol_version",
    "request_id",
    "request_payload_sha256",
    "schema",
    "schema_version",
    "state",
    "valid_until",
}) | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS

_CLAIM_ALLOWED_KEYS = frozenset({
    "claim_id",
    "claimed_at",
    "contract_version",
    "discovery_public_sha256",
    "experiment_id",
    "grant_id",
    "grant_payload_sha256",
    "manifest_id",
    "manifest_payload_sha256",
    "manifest_revision",
    "owner_principal",
    "payload_sha256",
    "protocol_version",
    "request_id",
    "request_payload_sha256",
    "schema",
    "schema_version",
    "state",
}) | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS


class AV1ValidationV4QualificationAuthorityError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


def build_av1_validation_v4_qualification_request(
    *,
    freeze: Mapping[str, Any],
    preparation: Mapping[str, Any],
    owner_principal: str,
    execution_repository_commit: str,
    execution_repository_tree: str,
    consumption_registry: str,
    requested_at: str,
    valid_until: str,
) -> dict[str, Any]:
    freeze_payload = object_dict(freeze)
    prep_payload = object_dict(preparation)
    _assert_freeze_precondition(freeze_payload)
    _assert_preparation_bound_by_freeze(prep_payload, freeze_payload)
    _assert_repository_identity(
        {"commit": execution_repository_commit, "tree": execution_repository_tree},
        "execution repository",
    )
    if (
        not isinstance(owner_principal, str)
        or not owner_principal.strip()
        or len(owner_principal) > 192
        or av1_validation_v4_contains_private_text(owner_principal)
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request owner principal is invalid"
        )
    if owner_principal != freeze_payload.get("owner_principal"):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request owner must match the freeze"
        )
    _assert_consumption_registry(consumption_registry)
    invocations = _extract_invocations(prep_payload)
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_QUALIFICATION_REQUEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_QUALIFICATION_REQUEST_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_QUALIFICATION_REQUEST_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_QUALIFICATION_REQUEST_STATE,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "freeze_id": freeze_payload["freeze_id"],
        "freeze_payload_sha256": freeze_payload["payload_sha256"],
        "owner_principal": owner_principal,
        "preparation_repository": dict(
            object_dict(freeze_payload["reviewed_repository"])
        ),
        "execution_repository": {
            "commit": execution_repository_commit,
            "tree": execution_repository_tree,
        },
        "preparation_id": freeze_payload["preparation_id"],
        "preparation_payload_sha256": freeze_payload["preparation_payload_sha256"],
        "runtime_compatibility_id": freeze_payload["runtime_compatibility_id"],
        "path_privacy_key_id": freeze_payload["path_privacy_key_id"],
        "config_id": freeze_payload["config_id"],
        "config_canonical_file_sha256": freeze_payload["config_canonical_file_sha256"],
        "invocations": invocations,
        "consumption_registry": consumption_registry,
        "requested_at": requested_at,
        "valid_until": valid_until,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_request_identity(payload)
    assert_av1_validation_v4_qualification_request(bound)
    return bound


def assert_av1_validation_v4_qualification_request(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request payload is invalid"
        )
    unknown = set(materialized) - _REQUEST_ALLOWED_KEYS
    if unknown:
        raise AV1ValidationV4QualificationAuthorityError(
            f"AV1 v4 qualification request contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text({
        k: v for k, v in materialized.items() if k != "consumption_registry"
    })
    expected = {
        "schema": AV1_VALIDATION_V4_QUALIFICATION_REQUEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_QUALIFICATION_REQUEST_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_QUALIFICATION_REQUEST_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_QUALIFICATION_REQUEST_STATE,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    }
    if any(materialized.get(k) != v for k, v in expected.items()):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1ValidationV4QualificationAuthorityError(
                f"AV1 v4 qualification request cannot authorize {field}"
            )
    if not _FREEZE_ID_RE.fullmatch(str(materialized.get("freeze_id") or "")):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request freeze ID is invalid"
        )
    for field in (
        "freeze_payload_sha256",
        "manifest_payload_sha256",
        "discovery_public_sha256",
        "preparation_payload_sha256",
        "config_canonical_file_sha256",
        "payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(materialized.get(field) or "")):
            raise AV1ValidationV4QualificationAuthorityError(
                f"AV1 v4 qualification request {field} is invalid"
            )
    if not _PREPARATION_ID_RE.fullmatch(
        str(materialized.get("preparation_id") or "")
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request preparation ID is invalid"
        )
    if not _RUNTIME_ID_RE.fullmatch(
        str(materialized.get("runtime_compatibility_id") or "")
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request runtime compatibility ID is invalid"
        )
    if not _PATH_KEY_ID_RE.fullmatch(
        str(materialized.get("path_privacy_key_id") or "")
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request path privacy key ID is invalid"
        )
    if not _CONFIG_ID_RE.fullmatch(str(materialized.get("config_id") or "")):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request config ID is invalid"
        )
    owner = materialized.get("owner_principal")
    if (
        not isinstance(owner, str)
        or not owner.strip()
        or len(owner) > 192
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request owner principal is invalid"
        )
    _assert_repository_identity(
        object_dict(materialized.get("preparation_repository")),
        "preparation repository",
    )
    _assert_repository_identity(
        object_dict(materialized.get("execution_repository")),
        "execution repository",
    )
    _assert_consumption_registry(str(materialized.get("consumption_registry") or ""))
    _assert_request_invocations(materialized)
    _assert_request_window(materialized)
    request_id = str(materialized.get("request_id") or "")
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request ID is invalid"
        )
    if request_id != av1_validation_v4_qualification_request_id(materialized):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request ID does not match its payload"
        )
    without_sha = {k: v for k, v in materialized.items() if k != "payload_sha256"}
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request payload SHA-256 does not match"
        )


def serialize_av1_validation_v4_qualification_request(
    payload: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_qualification_request(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def load_av1_validation_v4_qualification_request(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request is unreadable"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request bytes are not canonical"
        )
    assert_av1_validation_v4_qualification_request(payload)
    return payload


def av1_validation_v4_qualification_request_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        k: v for k, v in payload.items() if k not in {"request_id", "payload_sha256"}
    }
    return f"av1vqualreq4_{stable_json_hash(semantic)[:32]}"


# ---------------------------------------------------------------------------
# Grant
# ---------------------------------------------------------------------------


def build_av1_validation_v4_qualification_grant(
    *,
    request: Mapping[str, Any],
    owner_principal: str,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    req = object_dict(request)
    try:
        assert_av1_validation_v4_qualification_request(req)
    except AV1ValidationV4QualificationAuthorityError as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant request is invalid"
        ) from exc
    if owner_principal != req.get("owner_principal"):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant owner must match the request"
        )
    if av1_validation_v4_contains_private_text(owner_principal):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant owner principal exposes machine-local text"
        )
    requested_at = _parse_timestamp(req.get("requested_at"), "requested_at")
    request_valid_until = _parse_timestamp(req.get("valid_until"), "request valid_until")
    grant_authorized_at = _parse_timestamp(authorized_at, "authorized_at")
    grant_valid_until = _parse_timestamp(valid_until, "valid_until")
    if not requested_at <= grant_authorized_at < grant_valid_until <= request_valid_until:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant window must be contained by the request"
        )
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_QUALIFICATION_GRANT_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_QUALIFICATION_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_QUALIFICATION_GRANT_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_QUALIFICATION_GRANT_STATE,
        "authority": AV1_VALIDATION_V4_QUALIFICATION_GRANT_AUTHORITY,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "request_id": req["request_id"],
        "request_payload_sha256": req["payload_sha256"],
        "owner_principal": owner_principal,
        "authorized_at": authorized_at,
        "valid_until": valid_until,
        "grant_scope": _GRANT_SCOPE,
    }
    payload.update({field: False for field in _QUALIFICATION_GRANT_FALSE_FIELDS})
    payload.update({field: True for field in _QUALIFICATION_GRANT_AUTHORIZED_FIELDS})
    bound = _bind_grant_identity(payload)
    assert_av1_validation_v4_qualification_grant(bound)
    return bound


def assert_av1_validation_v4_qualification_grant(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant payload is invalid"
        )
    unknown = set(materialized) - _GRANT_ALLOWED_KEYS
    if unknown:
        raise AV1ValidationV4QualificationAuthorityError(
            f"AV1 v4 qualification grant contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_VALIDATION_V4_QUALIFICATION_GRANT_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_QUALIFICATION_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_QUALIFICATION_GRANT_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_QUALIFICATION_GRANT_STATE,
        "authority": AV1_VALIDATION_V4_QUALIFICATION_GRANT_AUTHORITY,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "grant_scope": _GRANT_SCOPE,
    }
    if any(materialized.get(k) != v for k, v in expected.items()):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant binding is invalid"
        )
    for field in _QUALIFICATION_GRANT_FALSE_FIELDS:
        if materialized.get(field) is not False:
            raise AV1ValidationV4QualificationAuthorityError(
                f"AV1 v4 qualification grant cannot authorize {field}"
            )
    for field in _QUALIFICATION_GRANT_AUTHORIZED_FIELDS:
        if materialized.get(field) is not True:
            raise AV1ValidationV4QualificationAuthorityError(
                f"AV1 v4 qualification grant must authorize {field}"
            )
    for field in ("request_payload_sha256", "manifest_payload_sha256",
                  "discovery_public_sha256", "payload_sha256"):
        if not _SHA256_RE.fullmatch(str(materialized.get(field) or "")):
            raise AV1ValidationV4QualificationAuthorityError(
                f"AV1 v4 qualification grant {field} is invalid"
            )
    if not _REQUEST_ID_RE.fullmatch(str(materialized.get("request_id") or "")):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant request ID is invalid"
        )
    owner = materialized.get("owner_principal")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 192:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant owner principal is invalid"
        )
    _assert_grant_window(materialized)
    grant_id = str(materialized.get("grant_id") or "")
    if not _GRANT_ID_RE.fullmatch(grant_id):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant ID is invalid"
        )
    if grant_id != av1_validation_v4_qualification_grant_id(materialized):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant ID does not match its payload"
        )
    without_sha = {k: v for k, v in materialized.items() if k != "payload_sha256"}
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant payload SHA-256 does not match"
        )


def assert_av1_validation_v4_qualification_grant_active(
    payload: Mapping[str, Any],
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v4_qualification_grant(payload)
    checked_at = _parse_timestamp(as_of, "activity timestamp")
    authorized_at = _parse_timestamp(
        payload.get("authorized_at"), "authorization timestamp"
    )
    valid_until = _parse_timestamp(payload.get("valid_until"), "expiration timestamp")
    if checked_at < authorized_at or checked_at >= valid_until:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant is not active"
        )


def assert_av1_validation_v4_qualification_grant_for_request(
    *,
    request: Mapping[str, Any],
    grant: Mapping[str, Any],
) -> None:
    req = object_dict(request)
    grt = object_dict(grant)
    assert_av1_validation_v4_qualification_request(req)
    assert_av1_validation_v4_qualification_grant(grt)
    if (
        grt.get("request_id") != req.get("request_id")
        or grt.get("request_payload_sha256") != req.get("payload_sha256")
        or grt.get("owner_principal") != req.get("owner_principal")
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant request binding does not match"
        )
    requested_at = _parse_timestamp(req.get("requested_at"), "requested_at")
    request_valid_until = _parse_timestamp(req.get("valid_until"), "request valid_until")
    authorized_at = _parse_timestamp(grt.get("authorized_at"), "authorized_at")
    valid_until = _parse_timestamp(grt.get("valid_until"), "valid_until")
    if not requested_at <= authorized_at < valid_until <= request_valid_until:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant window is outside the request"
        )


def serialize_av1_validation_v4_qualification_grant(
    payload: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_qualification_grant(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def load_av1_validation_v4_qualification_grant(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant is unreadable"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant bytes are not canonical"
        )
    assert_av1_validation_v4_qualification_grant(payload)
    return payload


def av1_validation_v4_qualification_grant_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        k: v for k, v in payload.items() if k not in {"grant_id", "payload_sha256"}
    }
    return f"av1vqualgrant4_{stable_json_hash(semantic)[:32]}"


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


def build_av1_validation_v4_qualification_claim(
    *,
    request: Mapping[str, Any],
    grant: Mapping[str, Any],
    claimed_at: str,
) -> dict[str, Any]:
    req = object_dict(request)
    grt = object_dict(grant)
    try:
        assert_av1_validation_v4_qualification_grant_active(grt, as_of=claimed_at)
    except AV1ValidationV4QualificationAuthorityError as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim grant is not active"
        ) from exc
    try:
        assert_av1_validation_v4_qualification_request(req)
    except AV1ValidationV4QualificationAuthorityError as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim request is invalid"
        ) from exc
    assert_av1_validation_v4_qualification_grant_for_request(
        request=req,
        grant=grt,
    )
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_QUALIFICATION_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_QUALIFICATION_CLAIM_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_QUALIFICATION_CLAIM_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_QUALIFICATION_CLAIM_STATE,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "request_id": req["request_id"],
        "request_payload_sha256": req["payload_sha256"],
        "grant_id": grt["grant_id"],
        "grant_payload_sha256": grt["payload_sha256"],
        "owner_principal": req["owner_principal"],
        "claimed_at": claimed_at,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_claim_identity(payload)
    assert_av1_validation_v4_qualification_claim(bound)
    return bound


def assert_av1_validation_v4_qualification_claim(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim payload is invalid"
        )
    unknown = set(materialized) - _CLAIM_ALLOWED_KEYS
    if unknown:
        raise AV1ValidationV4QualificationAuthorityError(
            f"AV1 v4 qualification claim contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_VALIDATION_V4_QUALIFICATION_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_QUALIFICATION_CLAIM_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_QUALIFICATION_CLAIM_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_QUALIFICATION_CLAIM_STATE,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    }
    if any(materialized.get(k) != v for k, v in expected.items()):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1ValidationV4QualificationAuthorityError(
                f"AV1 v4 qualification claim cannot authorize {field}"
            )
    for field in (
        "manifest_payload_sha256",
        "discovery_public_sha256",
        "request_payload_sha256",
        "grant_payload_sha256",
        "payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(materialized.get(field) or "")):
            raise AV1ValidationV4QualificationAuthorityError(
                f"AV1 v4 qualification claim {field} is invalid"
            )
    if not _REQUEST_ID_RE.fullmatch(str(materialized.get("request_id") or "")):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim request ID is invalid"
        )
    if not _GRANT_ID_RE.fullmatch(str(materialized.get("grant_id") or "")):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim grant ID is invalid"
        )
    owner = materialized.get("owner_principal")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 192:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim owner principal is invalid"
        )
    _parse_timestamp(materialized.get("claimed_at"), "claimed_at")
    claim_id = str(materialized.get("claim_id") or "")
    if not _CLAIM_ID_RE.fullmatch(claim_id):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim ID is invalid"
        )
    if claim_id != av1_validation_v4_qualification_claim_id(materialized):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim ID does not match its payload"
        )
    without_sha = {k: v for k, v in materialized.items() if k != "payload_sha256"}
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim payload SHA-256 does not match"
        )


def serialize_av1_validation_v4_qualification_claim(
    payload: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_qualification_claim(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def load_av1_validation_v4_qualification_claim(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim is unreadable"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim bytes are not canonical"
        )
    assert_av1_validation_v4_qualification_claim(payload)
    return payload


def av1_validation_v4_qualification_claim_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        k: v for k, v in payload.items() if k not in {"claim_id", "payload_sha256"}
    }
    return f"av1vqualclaim4_{stable_json_hash(semantic)[:32]}"


# ---------------------------------------------------------------------------
# Registry: atomic exclusive claim publication (burn-before-read)
# ---------------------------------------------------------------------------


def consume_av1_validation_v4_qualification_grant(
    *,
    registry: Path,
    request: Mapping[str, Any],
    grant: Mapping[str, Any],
    claimed_at: str,
) -> dict[str, Any]:
    """Write the claim O_EXCL inside the registry lock; a crash burns the grant."""
    if not registry.is_absolute():
        raise AV1ValidationV4QualificationAuthorityError(
            "qualification consumption registry must be absolute"
        )
    request_payload = object_dict(request)
    if request_payload.get("consumption_registry") != str(registry):
        raise AV1ValidationV4QualificationAuthorityError(
            "qualification consumption registry does not bind the request"
        )
    _assert_private_directory(registry)
    grt = object_dict(grant)
    assert_av1_validation_v4_qualification_grant_for_request(
        request=request_payload,
        grant=grt,
    )
    assert_av1_validation_v4_qualification_grant_active(grt, as_of=claimed_at)
    claim = build_av1_validation_v4_qualification_claim(
        request=request_payload,
        grant=grt,
        claimed_at=claimed_at,
    )
    grant_id = str(grt["grant_id"])
    claim_filename = f"{grant_id}.claim.json"
    claim_path = registry / claim_filename
    with _registry_lock(registry):
        _exclusive_write(claim_path, serialize_av1_validation_v4_qualification_claim(claim))
    return claim


def assert_av1_validation_v4_qualification_claim_custody(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim custody verification failed"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification claim custody verification failed"
        )


def begin_av1_validation_v4_qualification_run(
    *,
    registry: Path,
    request: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
) -> Path:
    """Publish the single-use run-start receipt before any traversal callback."""
    req = object_dict(request)
    grt = object_dict(grant)
    clm = object_dict(claim)
    if not registry.is_absolute() or req.get("consumption_registry") != str(registry):
        raise AV1ValidationV4QualificationAuthorityError(
            "qualification run registry does not bind the request"
        )
    _assert_private_directory(registry)
    assert_av1_validation_v4_qualification_grant_for_request(
        request=req,
        grant=grt,
    )
    assert_av1_validation_v4_qualification_grant_active(
        grt,
        as_of=str(clm.get("claimed_at") or ""),
    )
    assert_av1_validation_v4_qualification_claim(clm)
    if (
        grt.get("request_id") != req.get("request_id")
        or grt.get("request_payload_sha256") != req.get("payload_sha256")
        or clm.get("request_id") != req.get("request_id")
        or clm.get("request_payload_sha256") != req.get("payload_sha256")
        or clm.get("grant_id") != grt.get("grant_id")
        or clm.get("grant_payload_sha256") != grt.get("payload_sha256")
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "qualification run request, grant, and claim do not bind"
        )
    receipt: dict[str, Any] = {
        "schema": "mediaforce.av1_cold_start_v4_qualification_run_start",
        "schema_version": 1,
        "state": "run_started_single_use_consumed",
        "request_id": req["request_id"],
        "request_payload_sha256": req["payload_sha256"],
        "grant_id": grt["grant_id"],
        "grant_payload_sha256": grt["payload_sha256"],
        "claim_id": clm["claim_id"],
        "claim_payload_sha256": clm["payload_sha256"],
        "started_at": clm["claimed_at"],
    }
    receipt.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    path = registry / f"{grt['grant_id']}.run-started.json"
    with _registry_lock(registry):
        _exclusive_write(path, canonical_json_bytes(receipt) + b"\n")
    return path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_freeze_precondition(freeze: Mapping[str, Any]) -> None:
    try:
        assert_av1_validation_v4_manifest_freeze(freeze)
    except AV1ValidationV4FreezeError as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request requires a valid manifest freeze"
        ) from exc
    freeze_scope = object_dict(freeze.get("freeze_scope"))
    if freeze_scope.get("execution_grant_creation_authorized") is not False:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request freeze scope must not authorize "
            "execution grant creation"
        )


def _assert_preparation_bound_by_freeze(
    preparation: Mapping[str, Any],
    freeze: Mapping[str, Any],
) -> None:
    prep_id = str(preparation.get("preparation_id") or "")
    if not _PREPARATION_ID_RE.fullmatch(prep_id):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request preparation ID is invalid"
        )
    prep_digest = str(preparation.get("payload_sha256") or "")
    if not _SHA256_RE.fullmatch(prep_digest):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request preparation digest is invalid"
        )
    # Verify self-consistency: preparation_id must match preparation content
    without_id = {k: v for k, v in preparation.items() if k not in {"preparation_id", "payload_sha256"}}
    expected_id = f"av1vprep4_{stable_json_hash(without_id)[:32]}"
    if prep_id != expected_id:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request preparation ID is not self-consistent"
        )
    # Verify payload digest self-consistency
    without_sha = {k: v for k, v in preparation.items() if k != "payload_sha256"}
    if prep_digest != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request preparation payload digest is not self-consistent"
        )
    if prep_id != freeze.get("preparation_id"):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request preparation ID does not match the freeze"
        )
    if prep_digest != freeze.get("preparation_payload_sha256"):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request preparation digest does not match the freeze"
        )


def _extract_invocations(preparation: Mapping[str, Any]) -> list[dict[str, Any]]:
    invocations = [
        object_dict(item) for item in object_list(preparation.get("invocations"))
    ]
    if len(invocations) != AV1_VALIDATION_V4_TRAVERSAL_COUNT:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request invocation count is invalid"
        )
    return [dict(inv) for inv in invocations]


def _assert_request_invocations(payload: Mapping[str, Any]) -> None:
    invocations = [
        object_dict(item) for item in object_list(payload.get("invocations"))
    ]
    if len(invocations) != AV1_VALIDATION_V4_TRAVERSAL_COUNT:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request must bind all traversal invocations"
        )
    seen_ordinals: set[int] = set()
    seen_sha256s: set[str] = set()
    for invocation in invocations:
        if set(invocation) != _INVOCATION_KEYS:
            raise AV1ValidationV4QualificationAuthorityError(
                "AV1 v4 qualification request invocation schema is invalid"
            )
        ordinal = invocation.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise AV1ValidationV4QualificationAuthorityError(
                "AV1 v4 qualification request invocation ordinal is invalid"
            )
        if ordinal in seen_ordinals:
            raise AV1ValidationV4QualificationAuthorityError(
                "AV1 v4 qualification request invocation ordinals must be unique"
            )
        seen_ordinals.add(ordinal)
        if invocation.get("asset_id") not in AV1_VALIDATION_V4_SOURCE_IDS:
            raise AV1ValidationV4QualificationAuthorityError(
                "AV1 v4 qualification request invocation asset ID is invalid"
            )
        if invocation.get("configuration") not in AV1_VALIDATION_V4_CONFIGURATIONS:
            raise AV1ValidationV4QualificationAuthorityError(
                "AV1 v4 qualification request invocation configuration is invalid"
            )
        if invocation.get("mode") not in {"baseline", "guided"}:
            raise AV1ValidationV4QualificationAuthorityError(
                "AV1 v4 qualification request invocation mode is invalid"
            )
        sha = str(invocation.get("sha256") or "")
        if not _SHA256_RE.fullmatch(sha):
            raise AV1ValidationV4QualificationAuthorityError(
                "AV1 v4 qualification request invocation digest is invalid"
            )
        if sha in seen_sha256s:
            raise AV1ValidationV4QualificationAuthorityError(
                "AV1 v4 qualification request invocation digests must be unique"
            )
        seen_sha256s.add(sha)
    if seen_ordinals != set(range(1, AV1_VALIDATION_V4_TRAVERSAL_COUNT + 1)):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request invocation ordinals are not sequential"
        )
    role_rank = {"primary": 0, "confirmation": 1}
    expected_assets = tuple(
        asset_id
        for asset_id, content_class, role in sorted(
            AV1_VALIDATION_V4_SOURCE_LAYOUT,
            key=lambda item: (role_rank[item[2]], item[1]),
        )
    )
    expected = [
        (ordinal, asset_id, configuration, "baseline" if configuration.endswith("baseline") else "guided")
        for ordinal, (asset_id, configuration) in enumerate(
            (
                (asset_id, configuration)
                for asset_id in expected_assets
                for configuration in AV1_VALIDATION_V4_CONFIGURATIONS
            ),
            start=1,
        )
    ]
    actual = [
        (
            invocation["ordinal"],
            invocation["asset_id"],
            invocation["configuration"],
            invocation["mode"],
        )
        for invocation in invocations
    ]
    if actual != expected:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request invocation coverage is invalid"
        )


def _assert_request_window(payload: Mapping[str, Any]) -> None:
    revised_at = _parse_timestamp(AV1_VALIDATION_V4_REVISED_AT, "manifest revised_at")
    manifest_valid_until = _parse_timestamp(
        AV1_VALIDATION_V4_VALID_UNTIL, "manifest valid_until"
    )
    requested_at = _parse_timestamp(payload.get("requested_at"), "requested_at")
    valid_until = _parse_timestamp(payload.get("valid_until"), "valid_until")
    if not revised_at <= requested_at < valid_until <= manifest_valid_until:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request window is invalid"
        )


def _assert_grant_window(payload: Mapping[str, Any]) -> None:
    revised_at = _parse_timestamp(AV1_VALIDATION_V4_REVISED_AT, "manifest revised_at")
    manifest_valid_until = _parse_timestamp(
        AV1_VALIDATION_V4_VALID_UNTIL, "manifest valid_until"
    )
    authorized_at = _parse_timestamp(payload.get("authorized_at"), "authorized_at")
    valid_until = _parse_timestamp(payload.get("valid_until"), "valid_until")
    if not revised_at <= authorized_at < valid_until <= manifest_valid_until:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant window is invalid"
        )
    if valid_until - authorized_at > timedelta(
        seconds=AV1_VALIDATION_V4_QUALIFICATION_GRANT_MAX_SECONDS
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification grant exceeds its maximum duration"
        )


def _assert_repository_identity(
    repository: Mapping[str, Any],
    label: str,
) -> None:
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            f"AV1 v4 qualification authority {label} is invalid"
        )


def _assert_consumption_registry(value: str) -> None:
    if not isinstance(value, str):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification authority consumption registry is invalid"
        )
    path = PurePosixPath(value)
    if (
        not value.startswith("/")
        or value == "/"
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or "//" in value
        or value.endswith("/")
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification authority consumption registry is invalid"
        )


def _assert_no_private_text(payload: Mapping[str, Any]) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
            raise AV1ValidationV4QualificationAuthorityError(
                "AV1 v4 qualification authority artifact exposes machine-local text"
            )

    visit(payload)


def _parse_timestamp(value: Any, label: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1ValidationV4QualificationAuthorityError(
            f"AV1 v4 qualification authority {label} is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            f"AV1 v4 qualification authority {label} is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond != 0 or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1ValidationV4QualificationAuthorityError(
            f"AV1 v4 qualification authority {label} must be canonical UTC seconds"
        )
    return normalized


def _bind_request_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["request_id"] = av1_validation_v4_qualification_request_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _bind_grant_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["grant_id"] = av1_validation_v4_qualification_grant_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _bind_claim_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["claim_id"] = av1_validation_v4_qualification_claim_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            "qualification registry is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AV1ValidationV4QualificationAuthorityError(
            "qualification registry must be an owner-only real directory"
        )


@contextmanager
def _registry_lock(registry: Path) -> Iterator[None]:
    _assert_private_directory(registry)
    lock_path = registry / ".qualification-run.lock"
    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    lock_fd = os.open(str(lock_path), lock_flags, 0o600)
    try:
        lock_meta = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_meta.st_mode)
            or stat.S_IMODE(lock_meta.st_mode) != 0o600
            or lock_meta.st_nlink != 1
        ):
            raise AV1ValidationV4QualificationAuthorityError(
                "qualification registry lock file is invalid"
            )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock_fd)


def _exclusive_write(path: Path, data: bytes) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent_fd = os.open(path.parent, directory_flags)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise AV1ValidationV4QualificationAuthorityError(
                f"qualification artifact already exists: {path.name}"
            ) from exc
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(parent_fd)
        except Exception:
            try:
                os.unlink(path.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(parent_fd)
