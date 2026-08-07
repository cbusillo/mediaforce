from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_REVISED_AT,
    AV1_VALIDATION_V4_VALID_UNTIL,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4_preparation import (
    AV1_VALIDATION_V4_PREPARATION_STATE,
    AV1ValidationV4PreparationError,
    assert_av1_validation_v4_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4_preparation_claim import (
    AV1ValidationV4PreparationClaimError,
    assert_av1_validation_v4_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4_preparation_config import (
    AV1ValidationV4PreparationConfigError,
    assert_av1_validation_v4_effective_config_snapshot,
    serialize_av1_validation_v4_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    AV1ValidationV4PreparationGrantError,
    assert_av1_validation_v4_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4_preparation_measurement import (
    AV1ValidationV4PreparationMeasurementError,
    assert_av1_validation_v4_preparation_measurement,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE,
    AV1ValidationV4RightsError,
    assert_av1_validation_v4_rights_attestation,
)


AV1_VALIDATION_V4_FREEZE_SCHEMA = (
    "mediaforce.av1_cold_start_v4_manifest_freeze"
)
AV1_VALIDATION_V4_FREEZE_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_FREEZE_CONTRACT_VERSION = "av1v4freeze1"
AV1_VALIDATION_V4_FREEZE_STATE = "owner_frozen_materialized"
AV1_VALIDATION_V4_FREEZE_AUTHORITY = "av1_v4_manifest_revision_2_owner_freeze"
AV1_VALIDATION_V4_FREEZE_APPROVAL_FIELD = (
    "manifest_revision_2_owner_freeze_approved"
)
AV1_VALIDATION_V4_FREEZE_MAX_DECISION_LAG_SECONDS = 86_400
AV1_VALIDATION_V4_FREEZE_REGISTRY_KEY = (
    f"{AV1_VALIDATION_V4_MANIFEST_ID}:revision:{AV1_VALIDATION_V4_MANIFEST_REVISION}"
)

_FREEZE_ID_RE = re.compile(r"av1vfreeze4_[0-9a-f]{32}\Z")
_RIGHTS_ID_RE = re.compile(r"av1vrights4_[0-9a-f]{32}\Z")
_GRANT_ID_RE = re.compile(r"av1vprepgrant4_[0-9a-f]{32}\Z")
_CLAIM_ID_RE = re.compile(r"av1vprepclaim4_[0-9a-f]{32}\Z")
_CONFIG_ID_RE = re.compile(r"av1vconfig4_[0-9a-f]{32}\Z")
_PREPARATION_ID_RE = re.compile(r"av1vprep4_[0-9a-f]{32}\Z")
_MEASUREMENT_ID_RE = re.compile(r"av1vprepmeas4_[0-9a-f]{32}\Z")
_RUNTIME_ID_RE = re.compile(r"av1vruntime4_[0-9a-f]{32}\Z")
_PATH_KEY_ID_RE = re.compile(r"av1vpathkey4_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_FREEZE_SCOPE = {
    "activation_authorized": False,
    "dogfood_authorized": False,
    "empirical_authority_conferred": False,
    "evidence_creation_authorized": False,
    "execution_grant_creation_authorized": False,
    "manifest_payload_mutation_authorized": False,
    "media_read_authorized": False,
    "publication_authorized": False,
    "qualification_execution_authorized": False,
    "retry_authorized": False,
    "reviewed_artifact_set": (
        "rights_grant_claim_config_preparation_measurement"
    ),
}
_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "authority",
    "config_canonical_file_sha256",
    "config_id",
    "config_payload_sha256",
    "contract_version",
    "decided_at",
    "discovery_public_sha256",
    "experiment_id",
    "freeze_id",
    "freeze_scope",
    "manifest_id",
    "manifest_payload_sha256",
    "manifest_revision",
    "manifest_revision_2_owner_freeze_approved",
    "manifest_valid_until",
    "materializer_repository",
    "measurement_completed_at",
    "measurement_id",
    "measurement_payload_sha256",
    "measurement_state",
    "owner_principal",
    "path_privacy_key_id",
    "payload_sha256",
    "preparation_claim_id",
    "preparation_claim_payload_sha256",
    "preparation_claimed_at",
    "preparation_effective_config_sha256",
    "preparation_grant_authorized_at",
    "preparation_grant_id",
    "preparation_grant_payload_sha256",
    "preparation_grant_valid_until",
    "preparation_id",
    "preparation_media_bytes_read_count",
    "preparation_payload_sha256",
    "preparation_state",
    "prepared_at",
    "protocol_version",
    "registry_key",
    "reviewed_repository",
    "rights_attestation_id",
    "rights_attestation_payload_sha256",
    "rights_attested_at",
    "runtime_compatibility_id",
    "schema",
    "schema_version",
    "state",
}) | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS


class AV1ValidationV4FreezeError(ValueError):
    pass


def build_av1_validation_v4_manifest_freeze(
    *,
    rights_attestation: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    preparation_measurement: Mapping[str, Any],
    owner_principal: str,
    decided_at: str,
    materializer_repository_commit: str,
    materializer_repository_tree: str,
) -> dict[str, Any]:
    rights = object_dict(rights_attestation)
    grant = object_dict(preparation_grant)
    claim = object_dict(preparation_claim)
    config = object_dict(effective_config)
    record = object_dict(preparation)
    measurement = object_dict(preparation_measurement)
    _assert_reviewed_bundle(
        rights=rights,
        grant=grant,
        claim=claim,
        config=config,
        preparation=record,
        measurement=measurement,
        owner_principal=owner_principal,
        decided_at=decided_at,
    )
    _assert_repository_identity(
        {
            "commit": materializer_repository_commit,
            "tree": materializer_repository_tree,
        },
        "materializer repository",
    )
    reviewed_repository = object_dict(record["repository"])
    config_file_sha256 = _sha256_bytes(
        serialize_av1_validation_v4_effective_config_snapshot(config)
    )
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_FREEZE_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_FREEZE_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_FREEZE_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_FREEZE_STATE,
        "authority": AV1_VALIDATION_V4_FREEZE_AUTHORITY,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "manifest_valid_until": AV1_VALIDATION_V4_VALID_UNTIL,
        "registry_key": AV1_VALIDATION_V4_FREEZE_REGISTRY_KEY,
        "owner_principal": owner_principal,
        "decided_at": decided_at,
        "reviewed_repository": reviewed_repository,
        "materializer_repository": {
            "commit": materializer_repository_commit,
            "tree": materializer_repository_tree,
        },
        "rights_attestation_id": rights["attestation_id"],
        "rights_attestation_payload_sha256": rights["payload_sha256"],
        "rights_attested_at": rights["attested_at"],
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "preparation_grant_authorized_at": grant["authorized_at"],
        "preparation_grant_valid_until": grant["valid_until"],
        "preparation_claim_id": claim["claim_id"],
        "preparation_claim_payload_sha256": claim["payload_sha256"],
        "preparation_claimed_at": claim["claimed_at"],
        "config_id": config["config_id"],
        "config_payload_sha256": config["payload_sha256"],
        "config_canonical_file_sha256": config_file_sha256,
        "preparation_id": record["preparation_id"],
        "preparation_payload_sha256": record["payload_sha256"],
        "preparation_state": record["state"],
        "prepared_at": record["prepared_at"],
        "preparation_media_bytes_read_count": record["media_bytes_read_count"],
        "preparation_effective_config_sha256": record["effective_config_sha256"],
        "runtime_compatibility_id": record["runtime_compatibility_id"],
        "path_privacy_key_id": record["path_privacy_key_id"],
        "measurement_id": measurement["measurement_id"],
        "measurement_payload_sha256": measurement["payload_sha256"],
        "measurement_state": measurement["state"],
        "measurement_completed_at": measurement["completed_at"],
        AV1_VALIDATION_V4_FREEZE_APPROVAL_FIELD: True,
        "freeze_scope": _FREEZE_SCOPE,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_identity(payload)
    assert_av1_validation_v4_manifest_freeze(bound)
    return bound


def assert_av1_validation_v4_manifest_freeze(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if not materialized or set(materialized) != _ALLOWED_TOP_LEVEL_KEYS:
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze shape is invalid"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_VALIDATION_V4_FREEZE_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_FREEZE_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_FREEZE_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_FREEZE_STATE,
        "authority": AV1_VALIDATION_V4_FREEZE_AUTHORITY,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "manifest_valid_until": AV1_VALIDATION_V4_VALID_UNTIL,
        "registry_key": AV1_VALIDATION_V4_FREEZE_REGISTRY_KEY,
        AV1_VALIDATION_V4_FREEZE_APPROVAL_FIELD: True,
        "freeze_scope": _FREEZE_SCOPE,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1ValidationV4FreezeError(
                f"AV1 v4 manifest freeze cannot authorize {field}"
            )
    for field in (
        "manifest_payload_sha256",
        "discovery_public_sha256",
        "rights_attestation_payload_sha256",
        "preparation_grant_payload_sha256",
        "preparation_claim_payload_sha256",
        "config_payload_sha256",
        "config_canonical_file_sha256",
        "preparation_payload_sha256",
        "preparation_effective_config_sha256",
        "measurement_payload_sha256",
        "payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(materialized.get(field) or "")):
            raise AV1ValidationV4FreezeError(
                f"AV1 v4 manifest freeze {field} is invalid"
            )
    _assert_repository_identity(
        object_dict(materialized.get("reviewed_repository")),
        "reviewed repository",
    )
    _assert_repository_identity(
        object_dict(materialized.get("materializer_repository")),
        "materializer repository",
    )
    id_patterns = {
        "rights_attestation_id": _RIGHTS_ID_RE,
        "preparation_grant_id": _GRANT_ID_RE,
        "preparation_claim_id": _CLAIM_ID_RE,
        "config_id": _CONFIG_ID_RE,
        "preparation_id": _PREPARATION_ID_RE,
        "measurement_id": _MEASUREMENT_ID_RE,
        "runtime_compatibility_id": _RUNTIME_ID_RE,
        "path_privacy_key_id": _PATH_KEY_ID_RE,
    }
    for field, pattern in id_patterns.items():
        if not pattern.fullmatch(str(materialized.get(field) or "")):
            raise AV1ValidationV4FreezeError(
                f"AV1 v4 manifest freeze {field} is invalid"
            )
    owner_principal = materialized.get("owner_principal")
    if (
        not isinstance(owner_principal, str)
        or not owner_principal.strip()
        or len(owner_principal) > 192
    ):
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze owner principal is invalid"
        )
    decided_at = _parse_timestamp(materialized.get("decided_at"), "decided_at")
    rights_at = _parse_timestamp(
        materialized.get("rights_attested_at"),
        "rights_attested_at",
    )
    grant_at = _parse_timestamp(
        materialized.get("preparation_grant_authorized_at"),
        "preparation_grant_authorized_at",
    )
    grant_valid_until = _parse_timestamp(
        materialized.get("preparation_grant_valid_until"),
        "preparation_grant_valid_until",
    )
    claimed_at = _parse_timestamp(
        materialized.get("preparation_claimed_at"),
        "preparation_claimed_at",
    )
    prepared_at = _parse_timestamp(materialized.get("prepared_at"), "prepared_at")
    completed_at = _parse_timestamp(
        materialized.get("measurement_completed_at"),
        "measurement_completed_at",
    )
    valid_until = _parse_timestamp(AV1_VALIDATION_V4_VALID_UNTIL, "valid_until")
    revised_at = _parse_timestamp(AV1_VALIDATION_V4_REVISED_AT, "revised_at")
    if (
        not revised_at
        <= rights_at
        <= grant_at
        <= claimed_at
        <= prepared_at
        <= completed_at
        <= decided_at
        or not grant_at <= claimed_at <= prepared_at < grant_valid_until
        or decided_at >= valid_until
        or decided_at - prepared_at
        > timedelta(seconds=AV1_VALIDATION_V4_FREEZE_MAX_DECISION_LAG_SECONDS)
    ):
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze decision timeline is invalid"
        )
    if (
        materialized.get("preparation_state")
        != AV1_VALIDATION_V4_PREPARATION_STATE
        or isinstance(materialized.get("preparation_media_bytes_read_count"), bool)
        or materialized.get("preparation_media_bytes_read_count") != 0
        or materialized.get("measurement_state") != "measured_success"
        or materialized.get("config_canonical_file_sha256")
        != materialized.get("preparation_effective_config_sha256")
    ):
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze reviewed state is invalid"
        )
    freeze_id = str(materialized.get("freeze_id") or "")
    if not _FREEZE_ID_RE.fullmatch(freeze_id):
        raise AV1ValidationV4FreezeError("AV1 v4 manifest freeze ID is invalid")
    if freeze_id != av1_validation_v4_manifest_freeze_id(materialized):
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze ID does not match"
        )
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze payload digest does not match"
        )


def assert_av1_validation_v4_manifest_freeze_binds_bundle(
    freeze: Mapping[str, Any],
    *,
    rights_attestation: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    preparation_measurement: Mapping[str, Any],
) -> None:
    materialized = object_dict(freeze)
    assert_av1_validation_v4_manifest_freeze(materialized)
    materializer = object_dict(materialized["materializer_repository"])
    expected = build_av1_validation_v4_manifest_freeze(
        rights_attestation=rights_attestation,
        preparation_grant=preparation_grant,
        preparation_claim=preparation_claim,
        effective_config=effective_config,
        preparation=preparation,
        preparation_measurement=preparation_measurement,
        owner_principal=str(materialized["owner_principal"]),
        decided_at=str(materialized["decided_at"]),
        materializer_repository_commit=str(materializer["commit"]),
        materializer_repository_tree=str(materializer["tree"]),
    )
    if materialized != expected:
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze does not bind the reviewed bundle"
        )


def serialize_av1_validation_v4_manifest_freeze(
    freeze: Mapping[str, Any],
    *,
    rights_attestation: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    preparation_measurement: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(freeze))
    assert_av1_validation_v4_manifest_freeze_binds_bundle(
        materialized,
        rights_attestation=rights_attestation,
        preparation_grant=preparation_grant,
        preparation_claim=preparation_claim,
        effective_config=effective_config,
        preparation=preparation,
        preparation_measurement=preparation_measurement,
    )
    return canonical_json_bytes(materialized) + b"\n"


def load_av1_validation_v4_manifest_freeze(
    path: Path,
    *,
    rights_attestation: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    preparation_measurement: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze is unreadable"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze bytes are not canonical"
        )
    assert_av1_validation_v4_manifest_freeze_binds_bundle(
        payload,
        rights_attestation=rights_attestation,
        preparation_grant=preparation_grant,
        preparation_claim=preparation_claim,
        effective_config=effective_config,
        preparation=preparation,
        preparation_measurement=preparation_measurement,
    )
    return payload


def av1_validation_v4_manifest_freeze_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"freeze_id", "payload_sha256"}
    }
    return f"av1vfreeze4_{stable_json_hash(semantic)[:32]}"


def av1_validation_v4_manifest_revision_2_is_frozen(
    payload: Mapping[str, Any],
    *,
    rights_attestation: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    preparation_measurement: Mapping[str, Any],
) -> bool:
    assert_av1_validation_v4_manifest_freeze_binds_bundle(
        payload,
        rights_attestation=rights_attestation,
        preparation_grant=preparation_grant,
        preparation_claim=preparation_claim,
        effective_config=effective_config,
        preparation=preparation,
        preparation_measurement=preparation_measurement,
    )
    return payload.get(AV1_VALIDATION_V4_FREEZE_APPROVAL_FIELD) is True


def _assert_reviewed_bundle(
    *,
    rights: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    measurement: Mapping[str, Any],
    owner_principal: str,
    decided_at: str,
) -> None:
    try:
        assert_av1_validation_v4_rights_attestation(rights)
        assert_av1_validation_v4_preparation_grant(grant)
        assert_av1_validation_v4_preparation_claim(claim)
        assert_av1_validation_v4_effective_config_snapshot(config)
        assert_av1_validation_v4_preparation_bundle(
            preparation,
            rights,
            grant,
            claim,
        )
        assert_av1_validation_v4_preparation_measurement(measurement)
    except (
        AV1ValidationV4RightsError,
        AV1ValidationV4PreparationGrantError,
        AV1ValidationV4PreparationClaimError,
        AV1ValidationV4PreparationConfigError,
        AV1ValidationV4PreparationError,
        AV1ValidationV4PreparationMeasurementError,
    ) as exc:
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze reviewed bundle is invalid"
        ) from exc
    if rights.get("state") != AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE:
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze requires owner-attested rights"
        )
    if (
        owner_principal != rights.get("owner_principal")
        or owner_principal != grant.get("owner_principal")
        or owner_principal != claim.get("owner_principal")
    ):
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze owner binding is invalid"
        )
    repository = object_dict(preparation.get("repository"))
    if any(
        object_dict(artifact.get("repository")) != repository
        for artifact in (grant, claim, config)
    ):
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze repository binding is invalid"
        )
    config_file_sha256 = _sha256_bytes(
        serialize_av1_validation_v4_effective_config_snapshot(config)
    )
    if (
        preparation.get("effective_config_sha256") != config_file_sha256
        or measurement.get("preparation_id") != preparation.get("preparation_id")
        or measurement.get("preparation_payload_sha256")
        != preparation.get("payload_sha256")
        or measurement.get("claim_id") != claim.get("claim_id")
        or measurement.get("claim_payload_sha256") != claim.get("payload_sha256")
        or measurement.get("preparation_grant_id") != grant.get("grant_id")
        or measurement.get("preparation_grant_payload_sha256")
        != grant.get("payload_sha256")
        or object_dict(measurement.get("method")).get("config_digest_algorithm")
        != "sha256_canonical_file_bytes"
        or measurement.get("state") != "measured_success"
        or preparation.get("state") != AV1_VALIDATION_V4_PREPARATION_STATE
        or isinstance(preparation.get("media_bytes_read_count"), bool)
        or preparation.get("media_bytes_read_count") != 0
    ):
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze chain binding is invalid"
        )
    revised_at = _parse_timestamp(AV1_VALIDATION_V4_REVISED_AT, "revised_at")
    rights_at = _parse_timestamp(rights.get("attested_at"), "rights_attested_at")
    grant_at = _parse_timestamp(grant.get("authorized_at"), "grant_authorized_at")
    claim_at = _parse_timestamp(claim.get("claimed_at"), "claim_at")
    prepared_at = _parse_timestamp(preparation.get("prepared_at"), "prepared_at")
    completed_at = _parse_timestamp(measurement.get("completed_at"), "completed_at")
    decision_at = _parse_timestamp(decided_at, "decided_at")
    valid_until = _parse_timestamp(AV1_VALIDATION_V4_VALID_UNTIL, "valid_until")
    if not (
        revised_at
        <= rights_at
        <= grant_at
        <= claim_at
        <= prepared_at
        <= completed_at
        <= decision_at
        < valid_until
    ) or decision_at - prepared_at > timedelta(
        seconds=AV1_VALIDATION_V4_FREEZE_MAX_DECISION_LAG_SECONDS
    ):
        raise AV1ValidationV4FreezeError(
            "AV1 v4 manifest freeze timeline is invalid"
        )


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["freeze_id"] = av1_validation_v4_manifest_freeze_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_repository_identity(repository: Mapping[str, Any], label: str) -> None:
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1ValidationV4FreezeError(
            f"AV1 v4 manifest freeze {label} is invalid"
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
            raise AV1ValidationV4FreezeError(
                "AV1 v4 manifest freeze exposes machine-local text"
            )

    visit(payload)


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1ValidationV4FreezeError(
            f"AV1 v4 manifest freeze {label} is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV4FreezeError(
            f"AV1 v4 manifest freeze {label} is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond != 0 or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1ValidationV4FreezeError(
            f"AV1 v4 manifest freeze {label} must be canonical UTC seconds"
        )
    return normalized


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


if AV1_VALIDATION_V4_FREEZE_APPROVAL_FIELD in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
    raise RuntimeError("AV1 v4 freeze approval collides with frozen authority")

_REQUIRED_FALSE_FREEZE_AUTHORITIES = frozenset({
    "manifest_freeze_authorized",
    "media_read_authorized",
    "qualification_execution_authorized",
})
if not _REQUIRED_FALSE_FREEZE_AUTHORITIES <= AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
    raise RuntimeError("AV1 v4 freeze requires broad authorities to remain false")
