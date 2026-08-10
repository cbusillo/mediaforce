"""Owner-only AV1 protocol-v4 revision-4 ordinal registry.

This module is deliberately revision-4-only.  It consumes the pure r4r4
contract, diagnostic, and outcome artifacts, then adds a tiny immutable custody
state machine around them.  It does not read media, grant execution authority,
or interpret machine-local paths.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1_V4R4_ADVANCING_DISPOSITIONS,
    AV1_V4R4_EXPERIMENT_ID,
    AV1_V4R4_MANIFEST_ID,
    AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R4_MANIFEST_REVISION,
    AV1_V4R4_ORDINAL_COUNT,
    AV1_V4R4_PROTOCOL_VERSION,
    av1_v4r4_identity_domain,
    av1_v4r4_ordinal_layout,
)
from mediaforce.tuning.av1_validation_v4r4_outcome import (
    AV1V4R4OutcomeError,
    assert_av1_v4r4_outcome,
    assert_av1_v4r4_terminal,
    build_av1_v4r4_terminal,
)


def _execution_chain_assertion() -> Any:
    from mediaforce.tuning.av1_validation_v4r4_execution_authority import (
        assert_av1_v4r4_execution_chain,
    )

    return assert_av1_v4r4_execution_chain


def _runner_admission_chain_assertion() -> Any:
    from mediaforce.tuning.av1_validation_v4r4_runner_admission import (
        assert_av1_v4r4_runner_admission_chain,
    )

    return assert_av1_v4r4_runner_admission_chain


AV1_V4R4_OR_PLAN_SCHEMA = "mediaforce.av1_cold_start_v4r4_ordinal_registry_plan"
AV1_V4R4_OR_PLAN_SCHEMA_VERSION = 1
AV1_V4R4_OR_PLAN_CONTRACT_VERSION = "av1v4r4ordregplan1"
AV1_V4R4_OR_GRANT_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_ordinal_registry_sequencing_grant"
)
AV1_V4R4_OR_GRANT_SCHEMA_VERSION = 1
AV1_V4R4_OR_GRANT_CONTRACT_VERSION = "av1v4r4ordreggrant1"
AV1_V4R4_OR_GRANT_AUTHORITY = "av1_v4r4_ordinal_registry_sequencing"
AV1_V4R4_OR_CLAIM_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_ordinal_registry_sequencing_claim"
)
AV1_V4R4_OR_CLAIM_SCHEMA_VERSION = 1
AV1_V4R4_OR_CLAIM_CONTRACT_VERSION = "av1v4r4ordregclaim1"
AV1_V4R4_OR_STARTED_SCHEMA = "mediaforce.av1_cold_start_v4r4_ordinal_registry_started"
AV1_V4R4_OR_STARTED_SCHEMA_VERSION = 1
AV1_V4R4_OR_STARTED_CONTRACT_VERSION = "av1v4r4ordregstart1"
AV1_V4R4_OR_OUTCOME_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_ordinal_registry_outcome_publication"
)
AV1_V4R4_OR_OUTCOME_SCHEMA_VERSION = 1
AV1_V4R4_OR_OUTCOME_CONTRACT_VERSION = "av1v4r4ordregout1"
AV1_V4R4_OR_TERMINAL_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_ordinal_registry_terminal_publication"
)
AV1_V4R4_OR_TERMINAL_SCHEMA_VERSION = 1
AV1_V4R4_OR_TERMINAL_CONTRACT_VERSION = "av1v4r4ordregterm1"

_COMMON_KEYS = {
    "schema",
    "schema_version",
    "contract_version",
    "protocol_version",
    "manifest_revision",
    "experiment_id",
    "manifest_id",
    "manifest_payload_sha256",
}
_PLAN_KEYS = _COMMON_KEYS | {
    "plan_id",
    "payload_sha256",
    "registry_id",
    "plan_opens_at",
    "plan_closes_at",
    "ordinal_count",
    "ordinal_targets",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
_GRANT_KEYS = _COMMON_KEYS | {
    "grant_id",
    "payload_sha256",
    "plan_id",
    "plan_payload_sha256",
    "ordinal",
    "asset_id",
    "configuration",
    "target_size_bytes",
    "source_cap_total_bytes",
    "authority",
    "authorized_at",
    "valid_until",
    "admission_opens_at",
    "admission_closes_at",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
_CLAIM_KEYS = _COMMON_KEYS | {
    "claim_id",
    "payload_sha256",
    "plan_id",
    "plan_payload_sha256",
    "grant_id",
    "grant_payload_sha256",
    "ordinal",
    "claimed_at",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
_STARTED_KEYS = _COMMON_KEYS | {
    "started_id",
    "payload_sha256",
    "plan_id",
    "plan_payload_sha256",
    "grant_id",
    "grant_payload_sha256",
    "claim_id",
    "claim_payload_sha256",
    "ordinal",
    "started_at",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
_OUTCOME_KEYS = _COMMON_KEYS | {
    "outcome_publication_id",
    "payload_sha256",
    "plan_id",
    "plan_payload_sha256",
    "started_id",
    "started_payload_sha256",
    "ordinal",
    "outcome_at",
    "outcome",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
_TERMINAL_KEYS = _COMMON_KEYS | {
    "terminal_publication_id",
    "payload_sha256",
    "plan_id",
    "plan_payload_sha256",
    "terminal_at",
    "terminal",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS

_REGISTRY_ID_RE = re.compile(r"av1v4r4ordreg_[0-9a-f]{64}\Z")
_PLAN_ID_RE = re.compile(r"av1v4r4ordplan_[0-9a-f]{32}\Z")
_GRANT_ID_RE = re.compile(r"av1v4r4ordgrant_[0-9a-f]{32}\Z")
_CLAIM_ID_RE = re.compile(r"av1v4r4ordclaim_[0-9a-f]{32}\Z")
_STARTED_ID_RE = re.compile(r"av1v4r4ordstart_[0-9a-f]{32}\Z")
_OUTCOME_PUBLICATION_ID_RE = re.compile(r"av1v4r4ordoutpub_[0-9a-f]{32}\Z")
_TERMINAL_PUBLICATION_ID_RE = re.compile(r"av1v4r4ordtermpub_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

Clock = Callable[[], datetime]

_PROCESS_LOCK = threading.RLock()
_MAX_FILE_BYTES = 128 * 1024
_TEMP_SUFFIX = ".tmp"
_PLAN_NAME = "v4r4-ordinal-registry-plan.json"
_TERMINAL_NAME = "v4r4-ordinal-registry-terminal.json"
_BINDING_TOKEN = object()


class AV1V4R4OrdinalRegistryError(AV1V4R4OutcomeError):
    """Raised when r4r4 registry custody or sequencing is invalid."""


@dataclass(frozen=True, slots=True)
class AV1V4R4OrdinalRegistryBinding:
    """Private registry binding kept out of public custody artifacts."""

    registry: Path
    registry_id: str
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _BINDING_TOKEN:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry binding must be keyed"
            )


@dataclass(frozen=True, slots=True)
class AV1V4R4OrdinalRegistryGrantPublication:
    grant: Mapping[str, Any]
    created: bool


@dataclass(frozen=True, slots=True)
class AV1V4R4OrdinalRegistryOutcomePublication:
    outcome_publication: Mapping[str, Any]
    terminal_publication: Mapping[str, Any] | None
    created: bool


@dataclass(frozen=True, slots=True)
class AV1V4R4OrdinalRegistryAdmissionStartPublication:
    admission: Mapping[str, Any]
    started: Mapping[str, Any]
    created: bool


def av1_v4r4_ordinal_registry_hmac_id(registry: Path, *, key: bytes) -> str:
    """Return a public, path-free registry identifier for an owner directory."""

    if len(key) < 32:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry HMAC key is too short"
        )
    path = _normalize_registry_path(registry)
    digest = hmac.digest(
        key,
        f"mediaforce:av1:v4:r4:ordinal-registry:{path}".encode(),
        "sha256",
    ).hex()
    return f"av1v4r4ordreg_{digest}"


def av1_v4r4_ordinal_registry_binding(
    registry: Path,
    *,
    key: bytes,
) -> AV1V4R4OrdinalRegistryBinding:
    """Return a private registry path plus its public keyed identifier."""

    registry_path = Path(_normalize_registry_path(registry))
    return AV1V4R4OrdinalRegistryBinding(
        registry=registry_path,
        registry_id=av1_v4r4_ordinal_registry_hmac_id(registry_path, key=key),
        _token=_BINDING_TOKEN,
    )


def initialize_av1_v4r4_ordinal_registry(registry: Path) -> None:
    """Create or tighten an owner-only r4r4 registry directory."""

    registry_path = Path(_normalize_registry_path(registry))
    parent_fd = -1
    registry_fd = -1
    try:
        registry_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _assert_owner_safe_ancestor_chain(registry_path.parent)
        parent_fd = os.open(
            registry_path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        _assert_owner_safe_directory_fd(parent_fd)
        try:
            os.mkdir(registry_path.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            registry_fd = os.open(
                registry_path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry custody is invalid"
            ) from exc
        _assert_registry_owner_fd(registry_fd)
        os.fchmod(registry_fd, 0o700)
        os.fsync(registry_fd)
        _assert_registry_fd(registry_fd)
    except OSError as exc:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry initialization failed"
        ) from exc
    finally:
        if registry_fd >= 0:
            os.close(registry_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
    assert_av1_v4r4_ordinal_registry(registry_path)


def assert_av1_v4r4_ordinal_registry(registry: Path) -> None:
    """Verify that *registry* is an owner-only r4r4 artifact directory."""

    with _locked_registry(registry) as ctx:
        ctx.assert_supported_artifacts()


def assert_av1_v4r4_ordinal_registry_file_custody(
    registry: Path,
    filename: str,
) -> None:
    """Verify one supported registry file has owner-owned 0600 custody."""

    with _locked_registry(registry) as ctx:
        ctx.assert_file_custody(filename)


def build_av1_v4r4_ordinal_registry_plan(
    *,
    registry_id: str,
    plan_opens_at: str,
    plan_closes_at: str,
) -> dict[str, Any]:
    if not _REGISTRY_ID_RE.fullmatch(registry_id):
        raise AV1V4R4OrdinalRegistryError("AV1 v4 r4 registry ID is invalid")
    opens = _parse_ts(plan_opens_at)
    closes = _parse_ts(plan_closes_at)
    if closes <= opens:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry plan window is invalid"
        )
    payload: dict[str, Any] = {
        **_common_payload(
            schema=AV1_V4R4_OR_PLAN_SCHEMA,
            schema_version=AV1_V4R4_OR_PLAN_SCHEMA_VERSION,
            contract_version=AV1_V4R4_OR_PLAN_CONTRACT_VERSION,
        ),
        "registry_id": registry_id,
        "plan_opens_at": plan_opens_at,
        "plan_closes_at": plan_closes_at,
        "ordinal_count": AV1_V4R4_ORDINAL_COUNT,
        "ordinal_targets": _ordinal_targets(),
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="plan_id",
        id_prefix="av1v4r4ordplan_",
        domain=av1_v4r4_identity_domain("ordinal-registry-plan"),
    )
    assert_av1_v4r4_ordinal_registry_plan(bound)
    return bound


def assert_av1_v4r4_ordinal_registry_plan(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _PLAN_KEYS:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry plan shape is invalid"
        )
    _assert_common_payload(
        materialized,
        schema=AV1_V4R4_OR_PLAN_SCHEMA,
        schema_version=AV1_V4R4_OR_PLAN_SCHEMA_VERSION,
        contract_version=AV1_V4R4_OR_PLAN_CONTRACT_VERSION,
    )
    if not _REGISTRY_ID_RE.fullmatch(str(materialized.get("registry_id") or "")):
        raise AV1V4R4OrdinalRegistryError("AV1 v4 r4 registry ID is invalid")
    opens = _parse_ts(materialized.get("plan_opens_at"))
    closes = _parse_ts(materialized.get("plan_closes_at"))
    if closes <= opens:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry plan window is invalid"
        )
    if materialized.get("ordinal_count") != AV1_V4R4_ORDINAL_COUNT:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry count is invalid"
        )
    if object_list(materialized.get("ordinal_targets")) != _ordinal_targets():
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry target map is invalid"
        )
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_bound_identity(
        materialized,
        id_field="plan_id",
        id_pattern=_PLAN_ID_RE,
        id_prefix="av1v4r4ordplan_",
        domain=av1_v4r4_identity_domain("ordinal-registry-plan"),
    )


def build_av1_v4r4_ordinal_registry_grant(
    *,
    plan: Mapping[str, Any],
    ordinal: int,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    plan_payload = dict(plan)
    assert_av1_v4r4_ordinal_registry_plan(plan_payload)
    layout = _layout_for_ordinal(ordinal)
    authorized = _parse_ts(authorized_at)
    valid = _parse_ts(valid_until)
    if valid <= authorized or valid > _parse_ts(plan_payload["plan_closes_at"]):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry grant window is invalid"
        )
    _assert_plan_open(plan_payload, authorized)
    payload: dict[str, Any] = {
        **_common_payload(
            schema=AV1_V4R4_OR_GRANT_SCHEMA,
            schema_version=AV1_V4R4_OR_GRANT_SCHEMA_VERSION,
            contract_version=AV1_V4R4_OR_GRANT_CONTRACT_VERSION,
        ),
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "ordinal": ordinal,
        "asset_id": layout["asset_id"],
        "configuration": layout["configuration"],
        "target_size_bytes": layout["target_size_bytes"],
        "source_cap_total_bytes": layout["source_cap_total_bytes"],
        "authority": AV1_V4R4_OR_GRANT_AUTHORITY,
        "authorized_at": authorized_at,
        "valid_until": valid_until,
        "admission_opens_at": authorized_at,
        "admission_closes_at": valid_until,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="grant_id",
        id_prefix="av1v4r4ordgrant_",
        domain=av1_v4r4_identity_domain("ordinal-registry-grant"),
    )
    assert_av1_v4r4_ordinal_registry_grant(bound)
    return bound


def assert_av1_v4r4_ordinal_registry_grant(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _GRANT_KEYS:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry grant shape is invalid"
        )
    _assert_common_payload(
        materialized,
        schema=AV1_V4R4_OR_GRANT_SCHEMA,
        schema_version=AV1_V4R4_OR_GRANT_SCHEMA_VERSION,
        contract_version=AV1_V4R4_OR_GRANT_CONTRACT_VERSION,
    )
    ordinal = materialized.get("ordinal")
    layout = _layout_for_ordinal(ordinal)
    if (
        materialized.get("asset_id") != layout["asset_id"]
        or materialized.get("configuration") != layout["configuration"]
        or materialized.get("target_size_bytes") != layout["target_size_bytes"]
        or materialized.get("source_cap_total_bytes")
        != layout["source_cap_total_bytes"]
        or materialized.get("authority") != AV1_V4R4_OR_GRANT_AUTHORITY
        or not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or ""))
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry grant binding is invalid"
        )
    authorized = _parse_ts(materialized.get("authorized_at"))
    valid = _parse_ts(materialized.get("valid_until"))
    if (
        valid <= authorized
        or materialized.get("admission_opens_at") != materialized.get("authorized_at")
        or materialized.get("admission_closes_at") != materialized.get("valid_until")
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry grant window is invalid"
        )
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_bound_identity(
        materialized,
        id_field="grant_id",
        id_pattern=_GRANT_ID_RE,
        id_prefix="av1v4r4ordgrant_",
        domain=av1_v4r4_identity_domain("ordinal-registry-grant"),
    )


def build_av1_v4r4_ordinal_registry_claim(
    *,
    plan: Mapping[str, Any],
    grant: Mapping[str, Any],
    claimed_at: str,
) -> dict[str, Any]:
    plan_payload = dict(plan)
    grant_payload = dict(grant)
    assert_av1_v4r4_ordinal_registry_plan(plan_payload)
    assert_av1_v4r4_ordinal_registry_grant(grant_payload)
    _assert_record_binds_plan(grant_payload, plan_payload, "grant")
    claimed = _parse_ts(claimed_at)
    _assert_grant_open(grant_payload, claimed)
    payload: dict[str, Any] = {
        **_common_payload(
            schema=AV1_V4R4_OR_CLAIM_SCHEMA,
            schema_version=AV1_V4R4_OR_CLAIM_SCHEMA_VERSION,
            contract_version=AV1_V4R4_OR_CLAIM_CONTRACT_VERSION,
        ),
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "grant_id": grant_payload["grant_id"],
        "grant_payload_sha256": grant_payload["payload_sha256"],
        "ordinal": grant_payload["ordinal"],
        "claimed_at": claimed_at,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="claim_id",
        id_prefix="av1v4r4ordclaim_",
        domain=av1_v4r4_identity_domain("ordinal-registry-claim"),
    )
    assert_av1_v4r4_ordinal_registry_claim(bound)
    return bound


def assert_av1_v4r4_ordinal_registry_claim(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _CLAIM_KEYS:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry claim shape is invalid"
        )
    _assert_common_payload(
        materialized,
        schema=AV1_V4R4_OR_CLAIM_SCHEMA,
        schema_version=AV1_V4R4_OR_CLAIM_SCHEMA_VERSION,
        contract_version=AV1_V4R4_OR_CLAIM_CONTRACT_VERSION,
    )
    _layout_for_ordinal(materialized.get("ordinal"))
    if (
        not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or ""))
        or not _GRANT_ID_RE.fullmatch(str(materialized.get("grant_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("grant_payload_sha256") or ""))
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry claim binding is invalid"
        )
    _parse_ts(materialized.get("claimed_at"))
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_bound_identity(
        materialized,
        id_field="claim_id",
        id_pattern=_CLAIM_ID_RE,
        id_prefix="av1v4r4ordclaim_",
        domain=av1_v4r4_identity_domain("ordinal-registry-claim"),
    )


def build_av1_v4r4_ordinal_registry_started(
    *,
    plan: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    started_at: str,
) -> dict[str, Any]:
    plan_payload = dict(plan)
    grant_payload = dict(grant)
    claim_payload = dict(claim)
    assert_av1_v4r4_ordinal_registry_plan(plan_payload)
    assert_av1_v4r4_ordinal_registry_grant(grant_payload)
    assert_av1_v4r4_ordinal_registry_claim(claim_payload)
    _assert_record_binds_plan(grant_payload, plan_payload, "grant")
    _assert_record_binds_plan(claim_payload, plan_payload, "claim")
    _assert_record_binds_grant(claim_payload, grant_payload, "claim")
    started = _parse_ts(started_at)
    if started < _parse_ts(claim_payload["claimed_at"]):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry started time regressed"
        )
    _assert_grant_open(grant_payload, started)
    payload: dict[str, Any] = {
        **_common_payload(
            schema=AV1_V4R4_OR_STARTED_SCHEMA,
            schema_version=AV1_V4R4_OR_STARTED_SCHEMA_VERSION,
            contract_version=AV1_V4R4_OR_STARTED_CONTRACT_VERSION,
        ),
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "grant_id": grant_payload["grant_id"],
        "grant_payload_sha256": grant_payload["payload_sha256"],
        "claim_id": claim_payload["claim_id"],
        "claim_payload_sha256": claim_payload["payload_sha256"],
        "ordinal": claim_payload["ordinal"],
        "started_at": started_at,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="started_id",
        id_prefix="av1v4r4ordstart_",
        domain=av1_v4r4_identity_domain("ordinal-registry-started"),
    )
    assert_av1_v4r4_ordinal_registry_started(bound)
    return bound


def assert_av1_v4r4_ordinal_registry_started(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _STARTED_KEYS:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry started shape is invalid"
        )
    _assert_common_payload(
        materialized,
        schema=AV1_V4R4_OR_STARTED_SCHEMA,
        schema_version=AV1_V4R4_OR_STARTED_SCHEMA_VERSION,
        contract_version=AV1_V4R4_OR_STARTED_CONTRACT_VERSION,
    )
    _layout_for_ordinal(materialized.get("ordinal"))
    if (
        not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or ""))
        or not _GRANT_ID_RE.fullmatch(str(materialized.get("grant_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("grant_payload_sha256") or ""))
        or not _CLAIM_ID_RE.fullmatch(str(materialized.get("claim_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("claim_payload_sha256") or ""))
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry started binding is invalid"
        )
    _parse_ts(materialized.get("started_at"))
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_bound_identity(
        materialized,
        id_field="started_id",
        id_pattern=_STARTED_ID_RE,
        id_prefix="av1v4r4ordstart_",
        domain=av1_v4r4_identity_domain("ordinal-registry-started"),
    )


def build_av1_v4r4_ordinal_registry_outcome_publication(
    *,
    plan: Mapping[str, Any],
    started: Mapping[str, Any],
    outcome: Mapping[str, Any],
    outcome_at: str,
) -> dict[str, Any]:
    plan_payload = dict(plan)
    started_payload = dict(started)
    outcome_payload = dict(outcome)
    assert_av1_v4r4_ordinal_registry_plan(plan_payload)
    assert_av1_v4r4_ordinal_registry_started(started_payload)
    assert_av1_v4r4_outcome(outcome_payload)
    _assert_record_binds_plan(started_payload, plan_payload, "started")
    if outcome_payload["ordinal"] != started_payload["ordinal"]:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry outcome binding is invalid"
        )
    completed = _parse_ts(outcome_at)
    if completed < _parse_ts(started_payload["started_at"]):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry outcome time regressed"
        )
    payload: dict[str, Any] = {
        **_common_payload(
            schema=AV1_V4R4_OR_OUTCOME_SCHEMA,
            schema_version=AV1_V4R4_OR_OUTCOME_SCHEMA_VERSION,
            contract_version=AV1_V4R4_OR_OUTCOME_CONTRACT_VERSION,
        ),
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "started_id": started_payload["started_id"],
        "started_payload_sha256": started_payload["payload_sha256"],
        "ordinal": outcome_payload["ordinal"],
        "outcome_at": outcome_at,
        "outcome": outcome_payload,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="outcome_publication_id",
        id_prefix="av1v4r4ordoutpub_",
        domain=av1_v4r4_identity_domain("ordinal-registry-outcome"),
    )
    assert_av1_v4r4_ordinal_registry_outcome_publication(bound)
    return bound


def assert_av1_v4r4_ordinal_registry_outcome_publication(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _OUTCOME_KEYS:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry outcome publication shape is invalid"
        )
    _assert_common_payload(
        materialized,
        schema=AV1_V4R4_OR_OUTCOME_SCHEMA,
        schema_version=AV1_V4R4_OR_OUTCOME_SCHEMA_VERSION,
        contract_version=AV1_V4R4_OR_OUTCOME_CONTRACT_VERSION,
    )
    _layout_for_ordinal(materialized.get("ordinal"))
    outcome = object_dict(materialized.get("outcome"))
    try:
        assert_av1_v4r4_outcome(outcome)
    except AV1V4R4OutcomeError as exc:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry outcome binding is invalid"
        ) from exc
    if outcome.get("ordinal") != materialized.get("ordinal"):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry outcome binding is invalid"
        )
    if (
        not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or ""))
        or not _STARTED_ID_RE.fullmatch(str(materialized.get("started_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("started_payload_sha256") or ""))
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry outcome binding is invalid"
        )
    _parse_ts(materialized.get("outcome_at"))
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_bound_identity(
        materialized,
        id_field="outcome_publication_id",
        id_pattern=_OUTCOME_PUBLICATION_ID_RE,
        id_prefix="av1v4r4ordoutpub_",
        domain=av1_v4r4_identity_domain("ordinal-registry-outcome"),
    )


def build_av1_v4r4_ordinal_registry_terminal_publication(
    *,
    plan: Mapping[str, Any],
    terminal: Mapping[str, Any],
    terminal_at: str,
) -> dict[str, Any]:
    plan_payload = dict(plan)
    terminal_payload = dict(terminal)
    assert_av1_v4r4_ordinal_registry_plan(plan_payload)
    assert_av1_v4r4_terminal(terminal_payload)
    _parse_ts(terminal_at)
    payload: dict[str, Any] = {
        **_common_payload(
            schema=AV1_V4R4_OR_TERMINAL_SCHEMA,
            schema_version=AV1_V4R4_OR_TERMINAL_SCHEMA_VERSION,
            contract_version=AV1_V4R4_OR_TERMINAL_CONTRACT_VERSION,
        ),
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "terminal_at": terminal_at,
        "terminal": terminal_payload,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="terminal_publication_id",
        id_prefix="av1v4r4ordtermpub_",
        domain=av1_v4r4_identity_domain("ordinal-registry-terminal"),
    )
    assert_av1_v4r4_ordinal_registry_terminal_publication(bound)
    return bound


def assert_av1_v4r4_ordinal_registry_terminal_publication(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _TERMINAL_KEYS:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry terminal publication shape is invalid"
        )
    _assert_common_payload(
        materialized,
        schema=AV1_V4R4_OR_TERMINAL_SCHEMA,
        schema_version=AV1_V4R4_OR_TERMINAL_SCHEMA_VERSION,
        contract_version=AV1_V4R4_OR_TERMINAL_CONTRACT_VERSION,
    )
    terminal = object_dict(materialized.get("terminal"))
    try:
        assert_av1_v4r4_terminal(terminal)
    except AV1V4R4OutcomeError as exc:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry terminal binding is invalid"
        ) from exc
    if (
        not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or ""))
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry terminal binding is invalid"
        )
    _parse_ts(materialized.get("terminal_at"))
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_bound_identity(
        materialized,
        id_field="terminal_publication_id",
        id_pattern=_TERMINAL_PUBLICATION_ID_RE,
        id_prefix="av1v4r4ordtermpub_",
        domain=av1_v4r4_identity_domain("ordinal-registry-terminal"),
    )


def serialize_av1_v4r4_ordinal_registry_plan(payload: Mapping[str, Any]) -> bytes:
    return _serialize_artifact(payload, assert_av1_v4r4_ordinal_registry_plan)


def deserialize_av1_v4r4_ordinal_registry_plan(data: bytes) -> dict[str, Any]:
    return _deserialize_artifact(data, assert_av1_v4r4_ordinal_registry_plan, "plan")


def serialize_av1_v4r4_ordinal_registry_grant(payload: Mapping[str, Any]) -> bytes:
    return _serialize_artifact(payload, assert_av1_v4r4_ordinal_registry_grant)


def deserialize_av1_v4r4_ordinal_registry_grant(data: bytes) -> dict[str, Any]:
    return _deserialize_artifact(data, assert_av1_v4r4_ordinal_registry_grant, "grant")


def serialize_av1_v4r4_ordinal_registry_claim(payload: Mapping[str, Any]) -> bytes:
    return _serialize_artifact(payload, assert_av1_v4r4_ordinal_registry_claim)


def deserialize_av1_v4r4_ordinal_registry_claim(data: bytes) -> dict[str, Any]:
    return _deserialize_artifact(data, assert_av1_v4r4_ordinal_registry_claim, "claim")


def serialize_av1_v4r4_ordinal_registry_started(payload: Mapping[str, Any]) -> bytes:
    return _serialize_artifact(payload, assert_av1_v4r4_ordinal_registry_started)


def deserialize_av1_v4r4_ordinal_registry_started(data: bytes) -> dict[str, Any]:
    return _deserialize_artifact(data, assert_av1_v4r4_ordinal_registry_started, "started")


def serialize_av1_v4r4_ordinal_registry_outcome_publication(
    payload: Mapping[str, Any],
) -> bytes:
    return _serialize_artifact(
        payload,
        assert_av1_v4r4_ordinal_registry_outcome_publication,
    )


def deserialize_av1_v4r4_ordinal_registry_outcome_publication(
    data: bytes,
) -> dict[str, Any]:
    return _deserialize_artifact(
        data,
        assert_av1_v4r4_ordinal_registry_outcome_publication,
        "outcome publication",
    )


def serialize_av1_v4r4_ordinal_registry_terminal_publication(
    payload: Mapping[str, Any],
) -> bytes:
    return _serialize_artifact(
        payload,
        assert_av1_v4r4_ordinal_registry_terminal_publication,
    )


def deserialize_av1_v4r4_ordinal_registry_terminal_publication(
    data: bytes,
) -> dict[str, Any]:
    return _deserialize_artifact(
        data,
        assert_av1_v4r4_ordinal_registry_terminal_publication,
        "terminal publication",
    )


def serialize_av1_v4r4_ordinal_registry_runner_admission(
    payload: Mapping[str, Any],
) -> bytes:
    from mediaforce.tuning.av1_validation_v4r4_runner_admission import (
        serialize_av1_v4r4_runner_admission,
    )

    return serialize_av1_v4r4_runner_admission(payload)


def deserialize_av1_v4r4_ordinal_registry_runner_admission(
    data: bytes,
) -> dict[str, Any]:
    from mediaforce.tuning.av1_validation_v4r4_runner_admission import (
        deserialize_av1_v4r4_runner_admission,
    )

    try:
        return deserialize_av1_v4r4_runner_admission(data)
    except Exception as exc:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry runner admission bytes are invalid"
        ) from exc


def deserialize_av1_v4r4_ordinal_registry_execution_grant(
    data: bytes,
) -> dict[str, Any]:
    from mediaforce.tuning.av1_validation_v4r4_execution_authority import (
        deserialize_av1_v4r4_execution_grant,
    )

    try:
        return deserialize_av1_v4r4_execution_grant(data)
    except Exception as exc:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry execution grant bytes are invalid"
        ) from exc


def deserialize_av1_v4r4_ordinal_registry_execution_claim(
    data: bytes,
) -> dict[str, Any]:
    from mediaforce.tuning.av1_validation_v4r4_execution_authority import (
        deserialize_av1_v4r4_execution_claim,
    )

    try:
        return deserialize_av1_v4r4_execution_claim(data)
    except Exception as exc:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry execution claim bytes are invalid"
        ) from exc


def publish_av1_v4r4_ordinal_registry_plan(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    plan_payload = dict(plan)
    assert_av1_v4r4_ordinal_registry_plan(plan_payload)
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_supported_artifacts()
        _assert_binding_matches_plan(binding, plan_payload)
        if ctx.exists(_TERMINAL_NAME):
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry is sealed"
            )
        if ctx.exists(_PLAN_NAME):
            existing = ctx.load_plan()
            _assert_same_record(existing, plan_payload, "plan")
            return existing
        ctx.write(_PLAN_NAME, serialize_av1_v4r4_ordinal_registry_plan(plan_payload))
        return plan_payload


def publish_av1_v4r4_ordinal_registry_grant_with_status(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: Mapping[str, Any],
    ordinal: int,
    clock: Clock,
    valid_until: str,
) -> AV1V4R4OrdinalRegistryGrantPublication:
    _assert_clock(clock)
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_supported_artifacts()
        plan_payload = ctx.load_matching_plan(binding, plan)
        now = ctx.read_clock(clock)
        ctx.assert_next_ordinal_admissible(plan_payload, ordinal, now)
        filename = _grant_name(ordinal)
        if ctx.exists(filename):
            grant = ctx.load_grant(ordinal)
            _assert_record_binds_plan(grant, plan_payload, "grant")
            _assert_grant_within_plan(grant, plan_payload)
            return AV1V4R4OrdinalRegistryGrantPublication(grant=grant, created=False)
        grant = build_av1_v4r4_ordinal_registry_grant(
            plan=plan_payload,
            ordinal=ordinal,
            authorized_at=_format_ts(now),
            valid_until=valid_until,
        )
        ctx.write(filename, serialize_av1_v4r4_ordinal_registry_grant(grant))
        return AV1V4R4OrdinalRegistryGrantPublication(grant=grant, created=True)


def publish_av1_v4r4_ordinal_registry_grant(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: Mapping[str, Any],
    ordinal: int,
    clock: Clock,
    valid_until: str,
) -> dict[str, Any]:
    return dict(
        publish_av1_v4r4_ordinal_registry_grant_with_status(
            binding=binding,
            plan=plan,
            ordinal=ordinal,
            clock=clock,
            valid_until=valid_until,
        ).grant
    )


def publish_av1_v4r4_ordinal_registry_claim(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: Mapping[str, Any],
    grant: Mapping[str, Any],
    clock: Clock,
) -> dict[str, Any]:
    _assert_clock(clock)
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_supported_artifacts()
        plan_payload = ctx.load_matching_plan(binding, plan)
        grant_payload = ctx.load_matching_grant(plan_payload, grant)
        ordinal = int(grant_payload["ordinal"])
        now = ctx.read_clock(clock)
        ctx.assert_next_ordinal_admissible(plan_payload, ordinal, now)
        if ctx.exists(_claim_name(ordinal)):
            claim = ctx.load_claim(ordinal)
            _assert_record_binds_plan(claim, plan_payload, "claim")
            _assert_record_binds_grant(claim, grant_payload, "claim")
            _assert_record_within_grant(
                _parse_ts(claim["claimed_at"]),
                grant_payload,
                "claim",
            )
            return claim
        _assert_grant_open(grant_payload, now)
        claim = build_av1_v4r4_ordinal_registry_claim(
            plan=plan_payload,
            grant=grant_payload,
            claimed_at=_format_ts(now),
        )
        ctx.write_burn(_claim_name(ordinal), serialize_av1_v4r4_ordinal_registry_claim(claim))
        return claim


def publish_av1_v4r4_ordinal_registry_runner_admission_started(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    sequencing_claim: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    execution_claim: Mapping[str, Any],
    admission: Mapping[str, Any],
    clock: Clock,
) -> AV1V4R4OrdinalRegistryAdmissionStartPublication:
    """Atomically publish non-authorizing admission and started for one ordinal."""

    from mediaforce.tuning.av1_validation_v4r4_runner_admission import (
        serialize_av1_v4r4_runner_admission,
    )

    _assert_clock(clock)
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_supported_artifacts()
        plan_payload = ctx.load_matching_plan(binding, plan)
        seq_grant_hint = dict(sequencing_grant)
        seq_claim_hint = dict(sequencing_claim)
        exec_grant_hint = dict(execution_grant)
        exec_claim_hint = dict(execution_claim)
        admission_payload = dict(admission)

        seq_grant = ctx.load_matching_grant(plan_payload, seq_grant_hint)
        ordinal = int(seq_grant["ordinal"])
        seq_claim = ctx.load_claim(ordinal)
        _assert_same_record(seq_claim, seq_claim_hint, "claim")
        _assert_record_binds_grant(seq_claim, seq_grant, "claim")
        exec_grant = ctx.load_execution_grant(ordinal)
        exec_claim = ctx.load_execution_claim(ordinal)
        _assert_same_record(exec_grant, exec_grant_hint, "execution grant")
        _assert_same_record(exec_claim, exec_claim_hint, "execution claim")

        now = ctx.read_clock(clock)
        ctx.assert_next_ordinal_admissible(plan_payload, ordinal, now)
        if ctx.exists(_admission_name(ordinal)) or ctx.exists(_started_name(ordinal)):
            if ctx.exists(_admission_name(ordinal)) and ctx.exists(_started_name(ordinal)):
                existing_admission = ctx.load_runner_admission(ordinal)
                existing_started = ctx.load_started(ordinal)
                _assert_same_record(existing_admission, admission_payload, "runner admission")
                _assert_record_binds_plan(existing_started, plan_payload, "started")
                _assert_record_binds_grant(existing_started, seq_grant, "started")
                _assert_record_binds_claim(existing_started, seq_claim, "started")
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry runner admission was already used"
                )
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry has an incomplete publication"
            )
        if ctx.exists(_outcome_name(ordinal)):
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry outcome already exists"
            )

        _assert_full_execution_and_admission_chain(
            plan=plan_payload,
            sequencing_grant=seq_grant,
            sequencing_claim=seq_claim,
            execution_grant=exec_grant,
            execution_claim=exec_claim,
            admission=admission_payload,
            now=now,
        )
        if now < _parse_ts(seq_claim["claimed_at"]):
            ctx.publish_terminal(plan_payload, now)
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry clock moved backward"
            )
        _assert_grant_open(seq_grant, now)
        started = build_av1_v4r4_ordinal_registry_started(
            plan=plan_payload,
            grant=seq_grant,
            claim=seq_claim,
            started_at=_format_ts(now),
        )
        ctx.write(_admission_name(ordinal), serialize_av1_v4r4_runner_admission(admission_payload))
        ctx.write(_started_name(ordinal), serialize_av1_v4r4_ordinal_registry_started(started))
        return AV1V4R4OrdinalRegistryAdmissionStartPublication(
            admission=admission_payload,
            started=started,
            created=True,
        )


def publish_av1_v4r4_ordinal_registry_outcome(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: Mapping[str, Any],
    started: Mapping[str, Any],
    outcome: Mapping[str, Any],
    clock: Clock,
) -> AV1V4R4OrdinalRegistryOutcomePublication:
    _assert_clock(clock)
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_supported_artifacts()
        plan_payload = ctx.load_matching_plan(binding, plan)
        started_hint = dict(started)
        try:
            assert_av1_v4r4_ordinal_registry_started(started_hint)
        except Exception as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry started binding is invalid"
            ) from exc
        outcome_payload = dict(outcome)
        try:
            assert_av1_v4r4_outcome(outcome_payload)
        except Exception as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry outcome binding is invalid"
            ) from exc
        ordinal = int(outcome_payload["ordinal"])
        grant_payload = ctx.load_grant(ordinal)
        claim_payload = ctx.load_claim(ordinal)
        execution_grant = ctx.load_execution_grant(ordinal)
        execution_claim = ctx.load_execution_claim(ordinal)
        admission = ctx.load_runner_admission(ordinal)
        started_payload = ctx.load_started(ordinal)
        _assert_same_record(started_payload, started_hint, "started")
        _assert_record_binds_plan(started_payload, plan_payload, "started")
        _assert_record_binds_grant(started_payload, grant_payload, "started")
        _assert_record_binds_claim(started_payload, claim_payload, "started")
        if started_payload["ordinal"] != ordinal:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry outcome binding is invalid"
            )
        _assert_record_within_grant(
            _parse_ts(claim_payload["claimed_at"]),
            grant_payload,
            "claim",
        )
        _assert_record_within_grant(
            _parse_ts(started_payload["started_at"]),
            grant_payload,
            "start",
        )
        now = ctx.read_clock(clock)
        _assert_full_execution_and_admission_chain(
            plan=plan_payload,
            sequencing_grant=grant_payload,
            sequencing_claim=claim_payload,
            execution_grant=execution_grant,
            execution_claim=execution_claim,
            admission=admission,
            now=_parse_ts(started_payload["started_at"]),
        )
        if ctx.exists(_outcome_name(ordinal)):
            existing = ctx.load_outcome_publication(ordinal)
            ctx.assert_chain_binding_and_time(
                plan_payload,
                grant_payload,
                claim_payload,
                execution_grant,
                execution_claim,
                admission,
                started_payload,
                existing,
            )
            if object_dict(existing["outcome"]) != outcome_payload:
                ctx.publish_terminal(plan_payload, now)
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry duplicate outcome is invalid"
                )
            terminal = (
                ctx.load_verified_terminal(plan_payload)
                if ctx.exists(_TERMINAL_NAME)
                else None
            )
            return AV1V4R4OrdinalRegistryOutcomePublication(
                outcome_publication=existing,
                terminal_publication=terminal,
                created=False,
            )
        ctx.assert_next_ordinal_admissible(plan_payload, ordinal, now)
        if now < _parse_ts(started_payload["started_at"]):
            ctx.publish_terminal(plan_payload, now)
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry clock moved backward"
            )
        publication = build_av1_v4r4_ordinal_registry_outcome_publication(
            plan=plan_payload,
            started=started_payload,
            outcome=outcome_payload,
            outcome_at=_format_ts(now),
        )
        ctx.write(
            _outcome_name(ordinal),
            serialize_av1_v4r4_ordinal_registry_outcome_publication(publication),
        )
        terminal = None
        if _outcome_absorbs(outcome_payload) or ordinal == AV1_V4R4_ORDINAL_COUNT:
            terminal = ctx.publish_terminal(plan_payload, now)
        return AV1V4R4OrdinalRegistryOutcomePublication(
            outcome_publication=publication,
            terminal_publication=terminal,
            created=True,
        )


def publish_av1_v4r4_ordinal_registry_terminal(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: Mapping[str, Any],
    clock: Clock,
) -> dict[str, Any]:
    _assert_clock(clock)
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_supported_artifacts()
        plan_payload = ctx.load_matching_plan(binding, plan)
        now = ctx.read_clock(clock)
        return ctx.publish_terminal(plan_payload, now)


def load_av1_v4r4_ordinal_registry_terminal(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
) -> dict[str, Any] | None:
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_supported_artifacts()
        if not ctx.exists(_TERMINAL_NAME):
            return None
        plan_payload = ctx.load_plan()
        _assert_binding_matches_plan(binding, plan_payload)
        return ctx.load_verified_terminal(plan_payload)


def reconcile_av1_v4r4_ordinal_registry(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: Mapping[str, Any],
    clock: Clock,
) -> int:
    """Return the advancing high-water ordinal, sealing on defects."""

    _assert_clock(clock)
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_supported_artifacts()
        plan_payload = ctx.load_matching_plan(binding, plan)
        now = ctx.read_clock(clock)
        return ctx.derive_high_water_or_terminal(plan_payload, now)


@dataclass
class _RegistryContext:
    registry: Path
    dir_fd: int

    @staticmethod
    def read_clock(clock: Clock) -> datetime:
        current = clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry clock must be timezone-aware"
            )
        return current.astimezone(UTC).replace(microsecond=0)

    def assert_supported_artifacts(self) -> None:
        names = os.listdir(self.dir_fd)
        for name in names:
            if _is_temp_artifact_name(name):
                self.cleanup_temp_artifact(name)
        for name in names:
            if _is_temp_artifact_name(name):
                continue
            if not _is_registry_artifact_filename(name):
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry contains an unsupported artifact"
                )
            self.assert_file_custody(name)

    def _unlink_temp(self, filename: str) -> None:
        try:
            os.unlink(filename, dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry temporary cleanup failed"
            ) from exc

    def cleanup_temp_artifact(self, filename: str) -> None:
        try:
            metadata = os.stat(filename, dir_fd=self.dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry temporary custody check failed"
            ) from exc
        if _owner_regular_file(metadata, nlink=1):
            self._unlink_temp(filename)
            return
        if metadata.st_nlink != 2:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry temporary custody is invalid"
            )
        try:
            final_metadata = os.stat(
                _temp_final_name(filename),
                dir_fd=self.dir_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry temporary custody is invalid"
            ) from exc
        if not _owner_regular_file(final_metadata, nlink=2) or not _same_inode(
            metadata,
            final_metadata,
        ):
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry temporary custody is invalid"
            )
        self._unlink_temp(filename)

    def exists(self, filename: str) -> bool:
        _assert_registry_filename(filename)
        try:
            os.stat(filename, dir_fd=self.dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def assert_file_custody(self, filename: str) -> os.stat_result:
        _assert_registry_filename(filename)
        try:
            metadata = os.stat(filename, dir_fd=self.dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry file custody check failed"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry file custody is invalid"
            )
        return metadata

    def read(self, filename: str) -> bytes:
        metadata = self.assert_file_custody(filename)
        if metadata.st_size <= 0 or metadata.st_size > _MAX_FILE_BYTES:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry file size is invalid"
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(filename, flags, dir_fd=self.dir_fd)
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry file open failed"
            ) from exc
        try:
            opened = os.fstat(fd)
            if not _same_inode(metadata, opened):
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry file changed during read"
                )
            data = b""
            while len(data) < metadata.st_size:
                chunk = os.read(fd, metadata.st_size - len(data))
                if not chunk:
                    break
                data += chunk
            after = os.fstat(fd)
            if (
                len(data) != metadata.st_size
                or not _same_inode(metadata, after)
                or after.st_size != metadata.st_size
            ):
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry file read was incomplete"
                )
            return data
        finally:
            os.close(fd)

    def write(self, filename: str, data: bytes) -> None:
        _assert_registry_filename(filename)
        temp_name = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}{_TEMP_SUFFIX}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(temp_name, flags, 0o600, dir_fd=self.dir_fd)
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry temporary artifact creation failed"
            ) from exc
        try:
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.link(
                temp_name,
                filename,
                src_dir_fd=self.dir_fd,
                dst_dir_fd=self.dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry artifact already exists"
            ) from exc
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry artifact publication failed"
            ) from exc
        finally:
            with contextlib_suppress_oserror():
                os.unlink(temp_name, dir_fd=self.dir_fd)
            with contextlib_suppress_oserror():
                os.fsync(self.dir_fd)
        self.assert_file_custody(filename)

    def write_burn(self, filename: str, data: bytes) -> None:
        _assert_registry_filename(filename)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(filename, flags, 0o600, dir_fd=self.dir_fd)
        except FileExistsError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry artifact already exists"
            ) from exc
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry burn artifact creation failed"
            ) from exc
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, data)
            os.fsync(fd)
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_size != len(data)
            ):
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry burn custody is invalid"
                )
        finally:
            os.close(fd)
            try:
                os.fsync(self.dir_fd)
            except OSError as exc:
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry directory sync failed"
                ) from exc
        self.assert_file_custody(filename)

    def load_plan(self) -> dict[str, Any]:
        return deserialize_av1_v4r4_ordinal_registry_plan(self.read(_PLAN_NAME))

    def load_grant(self, ordinal: int) -> dict[str, Any]:
        payload = deserialize_av1_v4r4_ordinal_registry_grant(
            self.read(_grant_name(ordinal))
        )
        _assert_payload_ordinal_matches_slot(payload, ordinal, "grant")
        return payload

    def load_claim(self, ordinal: int) -> dict[str, Any]:
        payload = deserialize_av1_v4r4_ordinal_registry_claim(
            self.read(_claim_name(ordinal))
        )
        _assert_payload_ordinal_matches_slot(payload, ordinal, "claim")
        return payload

    def load_execution_grant(self, ordinal: int) -> dict[str, Any]:
        payload = deserialize_av1_v4r4_ordinal_registry_execution_grant(
            self.read(_execution_grant_name(ordinal))
        )
        _assert_payload_ordinal_matches_slot(payload, ordinal, "execution grant")
        return payload

    def load_execution_claim(self, ordinal: int) -> dict[str, Any]:
        payload = deserialize_av1_v4r4_ordinal_registry_execution_claim(
            self.read(_execution_claim_name(ordinal))
        )
        _assert_payload_ordinal_matches_slot(payload, ordinal, "execution claim")
        return payload

    def load_runner_admission(self, ordinal: int) -> dict[str, Any]:
        payload = deserialize_av1_v4r4_ordinal_registry_runner_admission(
            self.read(_admission_name(ordinal))
        )
        _assert_payload_ordinal_matches_slot(payload, ordinal, "runner admission")
        return payload

    def load_started(self, ordinal: int) -> dict[str, Any]:
        payload = deserialize_av1_v4r4_ordinal_registry_started(
            self.read(_started_name(ordinal))
        )
        _assert_payload_ordinal_matches_slot(payload, ordinal, "started")
        return payload

    def load_outcome_publication(self, ordinal: int) -> dict[str, Any]:
        payload = deserialize_av1_v4r4_ordinal_registry_outcome_publication(
            self.read(_outcome_name(ordinal))
        )
        _assert_payload_ordinal_matches_slot(payload, ordinal, "outcome")
        outcome = object_dict(payload["outcome"])
        _assert_payload_ordinal_matches_slot(outcome, ordinal, "outcome")
        return payload

    def load_terminal(self) -> dict[str, Any]:
        return deserialize_av1_v4r4_ordinal_registry_terminal_publication(
            self.read(_TERMINAL_NAME)
        )

    def load_verified_terminal(
        self,
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        terminal = self.load_terminal()
        _assert_record_binds_plan(terminal, plan, "terminal")
        prefix, last_at = self.validated_prefix_until_defect(plan)
        terminal_at = _parse_ts(terminal["terminal_at"])
        expected_now = last_at if last_at is not None else terminal_at
        expected = self.build_terminal_publication(plan, prefix, last_at, expected_now)
        if dict(terminal) != expected:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry terminal is stale"
            )
        return terminal

    def load_matching_plan(
        self,
        binding: AV1V4R4OrdinalRegistryBinding,
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan_payload = dict(plan)
        assert_av1_v4r4_ordinal_registry_plan(plan_payload)
        _assert_binding_matches_plan(binding, plan_payload)
        if not self.exists(_PLAN_NAME):
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry plan is unavailable"
            )
        canonical = self.load_plan()
        _assert_same_record(canonical, plan_payload, "plan")
        return canonical

    def load_matching_grant(
        self,
        plan: Mapping[str, Any],
        grant: Mapping[str, Any],
    ) -> dict[str, Any]:
        grant_hint = dict(grant)
        assert_av1_v4r4_ordinal_registry_grant(grant_hint)
        _assert_record_binds_plan(grant_hint, plan, "grant")
        ordinal = int(grant_hint["ordinal"])
        canonical = self.load_grant(ordinal)
        _assert_same_record(canonical, grant_hint, "grant")
        _assert_grant_within_plan(canonical, plan)
        return canonical

    def assert_next_ordinal_admissible(
        self,
        plan: Mapping[str, Any],
        ordinal: int,
        now: datetime,
    ) -> None:
        _layout_for_ordinal(ordinal)
        if self.exists(_TERMINAL_NAME):
            self.load_verified_terminal(plan)
            raise AV1V4R4OrdinalRegistryError("AV1 v4 r4 ordinal registry is sealed")
        try:
            high_water = self.derive_prior_high_water(plan, ordinal)
        except AV1V4R4OrdinalRegistryError:
            self.publish_terminal(plan, now)
            raise
        if ordinal != high_water + 1:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry authority is not for the next ordinal"
            )
        if ordinal > 1:
            prior = self.load_outcome_publication(ordinal - 1)
            prior_outcome_at = _parse_ts(prior["outcome_at"])
            if now < prior_outcome_at:
                self.publish_terminal(plan, prior_outcome_at)
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry clock moved backward"
                )
        _assert_plan_open(plan, now)

    def derive_prior_high_water(
        self,
        plan: Mapping[str, Any],
        ordinal: int,
    ) -> int:
        prefix, _last_at = self.validated_prefix(plan, limit=ordinal - 1)
        high_water = len(prefix)
        return high_water

    def derive_high_water_or_terminal(
        self,
        plan: Mapping[str, Any],
        now: datetime,
    ) -> int:
        if self.exists(_TERMINAL_NAME):
            terminal = self.load_verified_terminal(plan)
            return len(object_list(terminal["terminal"]["outcome_bindings"]))
        try:
            prefix, _last_at = self.validated_prefix(plan, require_advancing=False)
        except AV1V4R4OrdinalRegistryError:
            self.publish_terminal(plan, now)
            raise
        if len(prefix) == AV1_V4R4_ORDINAL_COUNT or (
            prefix and _outcome_absorbs(object_dict(prefix[-1]["outcome"]))
        ):
            self.publish_terminal(plan, now)
        return len(prefix)

    def validated_prefix_until_defect(
        self,
        plan: Mapping[str, Any],
    ) -> tuple[list[Mapping[str, Any]], datetime | None]:
        prefix: list[Mapping[str, Any]] = []
        last_at: datetime | None = None
        for ordinal in range(1, AV1_V4R4_ORDINAL_COUNT + 1):
            present = {
                "grant": self.exists(_grant_name(ordinal)),
                "claim": self.exists(_claim_name(ordinal)),
                "execution_grant": self.exists(_execution_grant_name(ordinal)),
                "execution_claim": self.exists(_execution_claim_name(ordinal)),
                "admission": self.exists(_admission_name(ordinal)),
                "started": self.exists(_started_name(ordinal)),
                "outcome": self.exists(_outcome_name(ordinal)),
            }
            if not any(present.values()) or not _admitted_slot_complete(present):
                return prefix, last_at
            try:
                grant = self.load_grant(ordinal)
                claim = self.load_claim(ordinal)
                execution_grant = self.load_execution_grant(ordinal)
                execution_claim = self.load_execution_claim(ordinal)
                admission = self.load_runner_admission(ordinal)
                started = self.load_started(ordinal)
                outcome_publication = self.load_outcome_publication(ordinal)
                outcome_at = self.assert_chain_binding_and_time(
                    plan,
                    grant,
                    claim,
                    execution_grant,
                    execution_claim,
                    admission,
                    started,
                    outcome_publication,
                    not_before=last_at,
                )
            except AV1V4R4OrdinalRegistryError:
                return prefix, last_at
            prefix.append(outcome_publication)
            last_at = outcome_at
            if _outcome_absorbs(object_dict(outcome_publication["outcome"])):
                return prefix, last_at
        return prefix, last_at

    def validated_prefix(
        self,
        plan: Mapping[str, Any],
        *,
        limit: int = AV1_V4R4_ORDINAL_COUNT,
        require_advancing: bool = True,
    ) -> tuple[list[Mapping[str, Any]], datetime | None]:
        prefix: list[Mapping[str, Any]] = []
        last_at: datetime | None = None
        for ordinal in range(1, limit + 1):
            present = {
                "grant": self.exists(_grant_name(ordinal)),
                "claim": self.exists(_claim_name(ordinal)),
                "execution_grant": self.exists(_execution_grant_name(ordinal)),
                "execution_claim": self.exists(_execution_claim_name(ordinal)),
                "admission": self.exists(_admission_name(ordinal)),
                "started": self.exists(_started_name(ordinal)),
                "outcome": self.exists(_outcome_name(ordinal)),
            }
            if not any(present.values()):
                self.assert_no_later_publications(ordinal)
                return prefix, last_at
            if not _admitted_slot_complete(present):
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry has an incomplete publication"
                )
            grant = self.load_grant(ordinal)
            claim = self.load_claim(ordinal)
            execution_grant = self.load_execution_grant(ordinal)
            execution_claim = self.load_execution_claim(ordinal)
            admission = self.load_runner_admission(ordinal)
            started = self.load_started(ordinal)
            outcome_publication = self.load_outcome_publication(ordinal)
            outcome_at = self.assert_chain_binding_and_time(
                plan,
                grant,
                claim,
                execution_grant,
                execution_claim,
                admission,
                started,
                outcome_publication,
                not_before=last_at,
            )
            outcome = object_dict(outcome_publication["outcome"])
            prefix.append(outcome_publication)
            last_at = outcome_at
            absorbs = _outcome_absorbs(outcome)
            if require_advancing:
                self.assert_outcome_advances(outcome)
            if absorbs:
                self.assert_no_later_publications(ordinal + 1)
                if require_advancing:
                    raise AV1V4R4OrdinalRegistryError(
                        "AV1 v4 r4 ordinal registry outcome is terminal"
                    )
                return prefix, last_at
        return prefix, last_at

    def assert_no_later_publications(self, first_ordinal: int) -> None:
        for later in range(first_ordinal, AV1_V4R4_ORDINAL_COUNT + 1):
            if any(
                self.exists(name)
                for name in (
                    _grant_name(later),
                    _claim_name(later),
                    _execution_grant_name(later),
                    _execution_claim_name(later),
                    _admission_name(later),
                    _started_name(later),
                    _outcome_name(later),
                )
            ):
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry has an ordinal gap"
                )

    def assert_chain_binding_and_time(
        self,
        plan: Mapping[str, Any],
        grant: Mapping[str, Any],
        claim: Mapping[str, Any],
        execution_grant: Mapping[str, Any],
        execution_claim: Mapping[str, Any],
        admission: Mapping[str, Any],
        started: Mapping[str, Any],
        outcome_publication: Mapping[str, Any],
        *,
        not_before: datetime | None = None,
    ) -> datetime:
        _assert_record_binds_plan(grant, plan, "grant")
        _assert_record_binds_plan(claim, plan, "claim")
        _assert_record_binds_plan(started, plan, "started")
        _assert_record_binds_plan(outcome_publication, plan, "outcome")
        _assert_record_binds_grant(claim, grant, "claim")
        _assert_record_binds_grant(started, grant, "started")
        _assert_record_binds_claim(started, claim, "started")
        _assert_record_binds_started(outcome_publication, started, "outcome")
        _assert_full_execution_and_admission_chain(
            plan=plan,
            sequencing_grant=grant,
            sequencing_claim=claim,
            execution_grant=execution_grant,
            execution_claim=execution_claim,
            admission=admission,
            now=_parse_ts(started["started_at"]),
        )
        ordinal = grant["ordinal"]
        if (
            claim["ordinal"] != ordinal
            or started["ordinal"] != ordinal
            or outcome_publication["ordinal"] != ordinal
            or object_dict(outcome_publication["outcome"])["ordinal"] != ordinal
        ):
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry chain ordinal is invalid"
            )
        claimed_at = _parse_ts(claim["claimed_at"])
        started_at = _parse_ts(started["started_at"])
        outcome_at = _parse_ts(outcome_publication["outcome_at"])
        authorized_at = _parse_ts(grant["authorized_at"])
        _assert_grant_within_plan(grant, plan)
        _assert_record_within_grant(claimed_at, grant, "claim")
        _assert_record_within_grant(started_at, grant, "start")
        if (
            started_at < claimed_at
            or outcome_at < started_at
            or (
                not_before is not None
                and any(
                    timestamp < not_before
                    for timestamp in (
                        authorized_at,
                        claimed_at,
                        started_at,
                        outcome_at,
                    )
                )
            )
        ):
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry clock moved backward"
            )
        return outcome_at

    def assert_outcome_advances(self, outcome: Mapping[str, Any]) -> None:
        if outcome["disposition"] not in AV1_V4R4_ADVANCING_DISPOSITIONS:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry outcome is terminal"
            )

    def publish_terminal(
        self,
        plan: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        if self.exists(_TERMINAL_NAME):
            return self.load_verified_terminal(plan)
        prefix, last_at = self.validated_prefix_until_defect(plan)
        seal_at = last_at if last_at is not None else now
        return self.publish_terminal_from_prefix(plan, prefix, seal_at)

    def build_terminal_publication(
        self,
        plan: Mapping[str, Any],
        prefix: list[Mapping[str, Any]],
        last_at: datetime | None,
        now: datetime | None,
    ) -> dict[str, Any]:
        if now is None:
            if last_at is None:
                raise AV1V4R4OrdinalRegistryError(
                    "AV1 v4 r4 ordinal registry terminal time is unavailable"
                )
            seal_at = last_at
        else:
            seal_at = last_at if last_at is not None and now < last_at else now
        outcomes = [object_dict(publication["outcome"]) for publication in prefix]
        terminal = build_av1_v4r4_terminal(outcomes)
        return build_av1_v4r4_ordinal_registry_terminal_publication(
            plan=plan,
            terminal=terminal,
            terminal_at=_format_ts(seal_at),
        )

    def publish_terminal_from_prefix(
        self,
        plan: Mapping[str, Any],
        prefix: list[Mapping[str, Any]],
        seal_at: datetime,
    ) -> dict[str, Any]:
        publication = self.build_terminal_publication(plan, prefix, seal_at, seal_at)
        self.write(
            _TERMINAL_NAME,
            serialize_av1_v4r4_ordinal_registry_terminal_publication(publication),
        )
        return publication


@contextmanager
def _locked_registry(registry: Path) -> Iterator[_RegistryContext]:
    registry_path = _normalize_registry_path(registry)
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _PROCESS_LOCK:
        try:
            dir_fd = os.open(registry_path, flags)
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry is unavailable"
            ) from exc
        try:
            _assert_registry_fd(dir_fd)
            fcntl.flock(dir_fd, fcntl.LOCK_EX)
            yield _RegistryContext(registry=Path(registry_path), dir_fd=dir_fd)
        finally:
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
            finally:
                os.close(dir_fd)


@contextmanager
def contextlib_suppress_oserror() -> Iterator[None]:
    try:
        yield
    except OSError:
        return


def _common_payload(
    *,
    schema: str,
    schema_version: int,
    contract_version: str,
) -> dict[str, Any]:
    return {
        "schema": schema,
        "schema_version": schema_version,
        "contract_version": contract_version,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    }


def _assert_common_payload(
    payload: Mapping[str, Any],
    *,
    schema: str,
    schema_version: int,
    contract_version: str,
) -> None:
    expected = _common_payload(
        schema=schema,
        schema_version=schema_version,
        contract_version=contract_version,
    )
    if any(payload.get(key) != value for key, value in expected.items()):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry artifact binding is invalid"
        )


def _false_authority_payload() -> dict[str, bool]:
    return {field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS}


def _ordinal_targets() -> list[dict[str, Any]]:
    return [
        {
            "ordinal": item["ordinal"],
            "asset_id": item["asset_id"],
            "configuration": item["configuration"],
            "target_size_bytes": item["target_size_bytes"],
            "source_cap_total_bytes": item["source_cap_total_bytes"],
        }
        for item in av1_v4r4_ordinal_layout()
    ]


def _layout_for_ordinal(ordinal: Any) -> dict[str, Any]:
    if type(ordinal) is not int or not 1 <= ordinal <= AV1_V4R4_ORDINAL_COUNT:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry ordinal is invalid"
        )
    return av1_v4r4_ordinal_layout()[ordinal - 1]


def _serialize_artifact(payload: Mapping[str, Any], assertion: Any) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assertion(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def _deserialize_artifact(data: bytes, assertion: Any, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1V4R4OrdinalRegistryError(
            f"AV1 v4 r4 ordinal registry {label} bytes are unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R4OrdinalRegistryError(
            f"AV1 v4 r4 ordinal registry {label} bytes are not canonical"
        )
    assertion(payload)
    return payload


def _bind_identity(
    payload: Mapping[str, Any],
    *,
    id_field: str,
    id_prefix: str,
    domain: str,
) -> dict[str, Any]:
    bound = dict(payload)
    bound[id_field] = (
        id_prefix + stable_json_hash({"domain": domain, "payload": bound})[:32]
    )
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_bound_identity(
    payload: Mapping[str, Any],
    *,
    id_field: str,
    id_pattern: re.Pattern[str],
    id_prefix: str,
    domain: str,
) -> None:
    artifact_id = str(payload.get(id_field) or "")
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {id_field, "payload_sha256"}
    }
    expected_id = (
        id_prefix + stable_json_hash({"domain": domain, "payload": semantic})[:32]
    )
    if not id_pattern.fullmatch(artifact_id) or artifact_id != expected_id:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry artifact ID is invalid"
        )
    without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    if payload.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry artifact digest is invalid"
        )


def _assert_false_authorities(payload: Mapping[str, Any]) -> None:
    if any(
        payload.get(field) is not False
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry artifact cannot confer authority"
        )


def _assert_no_private_text(value: Any) -> None:
    if isinstance(value, str):
        if av1_validation_v4_contains_private_text(value):
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry artifact contains machine-local text"
            )
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_private_text(str(key))
            _assert_no_private_text(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_private_text(child)


def _assert_binding_matches_plan(
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: Mapping[str, Any],
) -> None:
    if plan.get("registry_id") != binding.registry_id:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry binding is invalid"
        )


def _assert_record_binds_plan(
    record: Mapping[str, Any],
    plan: Mapping[str, Any],
    label: str,
) -> None:
    if (
        record.get("plan_id") != plan.get("plan_id")
        or record.get("plan_payload_sha256") != plan.get("payload_sha256")
    ):
        raise AV1V4R4OrdinalRegistryError(
            f"AV1 v4 r4 ordinal registry {label} plan binding is invalid"
        )


def _assert_record_binds_grant(
    record: Mapping[str, Any],
    grant: Mapping[str, Any],
    label: str,
) -> None:
    if (
        record.get("grant_id") != grant.get("grant_id")
        or record.get("grant_payload_sha256") != grant.get("payload_sha256")
    ):
        raise AV1V4R4OrdinalRegistryError(
            f"AV1 v4 r4 ordinal registry {label} grant binding is invalid"
        )


def _assert_record_binds_claim(
    record: Mapping[str, Any],
    claim: Mapping[str, Any],
    label: str,
) -> None:
    if (
        record.get("claim_id") != claim.get("claim_id")
        or record.get("claim_payload_sha256") != claim.get("payload_sha256")
    ):
        raise AV1V4R4OrdinalRegistryError(
            f"AV1 v4 r4 ordinal registry {label} claim binding is invalid"
        )


def _assert_record_binds_started(
    record: Mapping[str, Any],
    started: Mapping[str, Any],
    label: str,
) -> None:
    if (
        record.get("started_id") != started.get("started_id")
        or record.get("started_payload_sha256") != started.get("payload_sha256")
    ):
        raise AV1V4R4OrdinalRegistryError(
            f"AV1 v4 r4 ordinal registry {label} started binding is invalid"
        )


def _assert_payload_ordinal_matches_slot(
    payload: Mapping[str, Any],
    ordinal: int,
    label: str,
) -> None:
    if payload.get("ordinal") != ordinal:
        raise AV1V4R4OrdinalRegistryError(
            f"AV1 v4 r4 ordinal registry {label} filename ordinal is invalid"
        )


def _assert_same_record(
    canonical: Mapping[str, Any],
    candidate: Mapping[str, Any],
    label: str,
) -> None:
    if dict(canonical) != dict(candidate):
        raise AV1V4R4OrdinalRegistryError(
            f"AV1 v4 r4 ordinal registry {label} is immutable"
        )


def _assert_grant_within_plan(
    grant: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> None:
    authorized_at = _parse_ts(grant["authorized_at"])
    valid_until = _parse_ts(grant["valid_until"])
    if (
        authorized_at < _parse_ts(plan["plan_opens_at"])
        or valid_until > _parse_ts(plan["plan_closes_at"])
        or valid_until <= authorized_at
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry grant window is invalid"
        )


def _assert_record_within_grant(
    record_at: datetime,
    grant: Mapping[str, Any],
    label: str,
) -> None:
    if not _parse_ts(grant["authorized_at"]) <= record_at < _parse_ts(
        grant["valid_until"]
    ):
        raise AV1V4R4OrdinalRegistryError(
            f"AV1 v4 r4 ordinal registry {label} is outside the grant interval"
        )


def _assert_plan_open(plan: Mapping[str, Any], now: datetime) -> None:
    if now < _parse_ts(plan["plan_opens_at"]) or now >= _parse_ts(
        plan["plan_closes_at"]
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry plan is inactive"
        )


def _assert_grant_open(grant: Mapping[str, Any], now: datetime) -> None:
    if now < _parse_ts(grant["admission_opens_at"]) or now >= _parse_ts(
        grant["admission_closes_at"]
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry admission is outside the grant interval"
        )


def _outcome_absorbs(outcome: Mapping[str, Any]) -> bool:
    return (
        outcome.get("disposition") == "fatal_failure"
        or outcome.get("positive_control_matched") is False
    )


def _admitted_slot_complete(present: Mapping[str, bool]) -> bool:
    return bool(
        present.get("grant")
        and present.get("claim")
        and present.get("execution_grant")
        and present.get("execution_claim")
        and present.get("admission")
        and present.get("started")
        and present.get("outcome")
    )


def _assert_full_execution_and_admission_chain(
    *,
    plan: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    sequencing_claim: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    execution_claim: Mapping[str, Any],
    admission: Mapping[str, Any],
    now: datetime,
) -> None:
    try:
        _execution_chain_assertion()(
            plan=plan,
            sequencing_grant=sequencing_grant,
            sequencing_claim=sequencing_claim,
            execution_grant=execution_grant,
            execution_claim=execution_claim,
            now=now,
        )
        _runner_admission_chain_assertion()(
            admission=admission,
            plan=plan,
            sequencing_grant=sequencing_grant,
            sequencing_claim=sequencing_claim,
            execution_grant=execution_grant,
            execution_claim=execution_claim,
        )
    except Exception as exc:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry execution/admission chain is invalid"
        ) from exc


def _normalize_registry_path(registry: Path) -> str:
    if not isinstance(registry, Path):
        registry = Path(registry)
    if not registry.is_absolute():
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry must be an absolute path"
        )
    value = os.fsdecode(registry)
    if "\x00" in value:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry path is invalid"
        )
    return value


def _assert_owner_safe_ancestor_chain(path: Path) -> None:
    current = Path(_normalize_registry_path(path))
    parts = current.parts
    if not parts:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry path is invalid"
        )
    candidate = Path(parts[0])
    euid = os.geteuid()
    for part in parts[1:]:
        candidate /= part
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry ancestor custody check failed"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry ancestor custody is invalid"
            )
        mode = stat.S_IMODE(metadata.st_mode)
        owner_safe = metadata.st_uid in {0, euid}
        writable_by_others = bool(mode & (stat.S_IWGRP | stat.S_IWOTH))
        sticky = bool(mode & stat.S_ISVTX)
        if not owner_safe or (writable_by_others and not sticky):
            raise AV1V4R4OrdinalRegistryError(
                "AV1 v4 r4 ordinal registry ancestor custody is invalid"
            )


def _assert_registry_fd(dir_fd: int) -> None:
    metadata = os.fstat(dir_fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry custody is invalid"
        )


def _assert_registry_owner_fd(dir_fd: int) -> None:
    metadata = os.fstat(dir_fd)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry custody is invalid"
        )


def _assert_owner_safe_directory_fd(dir_fd: int) -> None:
    metadata = os.fstat(dir_fd)
    mode = stat.S_IMODE(metadata.st_mode)
    writable_by_others = bool(mode & (stat.S_IWGRP | stat.S_IWOTH))
    sticky = bool(mode & stat.S_ISVTX)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or (writable_by_others and not sticky)
    ):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry ancestor custody is invalid"
        )


def _owner_regular_file(metadata: os.stat_result, *, nlink: int) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_uid == os.geteuid()
        and metadata.st_nlink == nlink
    )


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("ordinal registry write made no progress")
        written += count


def _parse_ts(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry timestamp is invalid"
        )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry timestamp is invalid"
        ) from exc
    return parsed.replace(tzinfo=UTC)


def _format_ts(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry timestamp is invalid"
        )
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assert_clock(clock: Clock) -> None:
    if not callable(clock):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry clock is invalid"
        )


def _grant_name(ordinal: int) -> str:
    _layout_for_ordinal(ordinal)
    return f"v4r4-ordinal-{ordinal:02d}-sequencing-grant.json"


def _claim_name(ordinal: int) -> str:
    _layout_for_ordinal(ordinal)
    return f"v4r4-ordinal-{ordinal:02d}-sequencing-claim.json"


def _execution_grant_name(ordinal: int) -> str:
    _layout_for_ordinal(ordinal)
    return f"v4r4-ordinal-{ordinal:02d}-execution-grant.json"


def _execution_claim_name(ordinal: int) -> str:
    _layout_for_ordinal(ordinal)
    return f"v4r4-ordinal-{ordinal:02d}-execution-claim.json"


def _admission_name(ordinal: int) -> str:
    _layout_for_ordinal(ordinal)
    return f"v4r4-ordinal-{ordinal:02d}-runner-admission.json"


def _started_name(ordinal: int) -> str:
    _layout_for_ordinal(ordinal)
    return f"v4r4-ordinal-{ordinal:02d}-started.json"


def _outcome_name(ordinal: int) -> str:
    _layout_for_ordinal(ordinal)
    return f"v4r4-ordinal-{ordinal:02d}-outcome-publication.json"


def _is_registry_artifact_filename(name: str) -> bool:
    if name in {_PLAN_NAME, _TERMINAL_NAME}:
        return True
    return any(
        name == builder(ordinal)
        for ordinal in range(1, AV1_V4R4_ORDINAL_COUNT + 1)
        for builder in (
            _grant_name,
            _claim_name,
            _execution_grant_name,
            _execution_claim_name,
            _admission_name,
            _started_name,
            _outcome_name,
        )
    )


def _is_temp_artifact_name(name: str) -> bool:
    if not name.startswith(".") or not name.endswith(_TEMP_SUFFIX):
        return False
    body = name[1 : -len(_TEMP_SUFFIX)]
    parts = body.rsplit(".", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return False
    try:
        int(parts[2], 16)
    except ValueError:
        return False
    return _is_registry_artifact_filename(parts[0])


def _temp_final_name(name: str) -> str:
    if not isinstance(name, str) or not _is_temp_artifact_name(name):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry temporary filename is invalid"
        )
    body = name[1 : -len(_TEMP_SUFFIX)]
    final_name = body.rsplit(".", 2)[0]
    _assert_registry_filename(final_name)
    return final_name


def _assert_registry_filename(name: str) -> None:
    if not isinstance(name, str) or not _is_registry_artifact_filename(name):
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry filename is invalid"
        )
    if "/" in name or "\x00" in name or name in {".", ".."}:
        raise AV1V4R4OrdinalRegistryError(
            "AV1 v4 r4 ordinal registry filename is invalid"
        )


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


if AV1_V4R4_OR_OUTCOME_SCHEMA == "mediaforce.av1_cold_start_v4r4_ordinal_outcome":
    raise RuntimeError("AV1 v4 r4 registry and pure outcome schemas must differ")
if AV1_V4R4_OR_TERMINAL_SCHEMA == "mediaforce.av1_cold_start_v4r4_terminal":
    raise RuntimeError("AV1 v4 r4 registry and pure terminal schemas must differ")
