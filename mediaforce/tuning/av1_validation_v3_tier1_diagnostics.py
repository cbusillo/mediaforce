from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import av1_validation_v3_id
from mediaforce.tuning.av1_validation_v3_tier1_coverage import (
    AV1_VALIDATION_V3_TIER1_FIXTURE_IDS,
    AV1ValidationV3Tier1CoverageAttestation,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1ValidationV3Tier1FixtureOutcome,
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    AV1ValidationV3Tier1CommandDiagnostic,
)


AV1_VALIDATION_V3_TIER1_DIAGNOSTICS_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_run_diagnostics"
)
AV1_VALIDATION_V3_TIER1_DIAGNOSTICS_SCHEMA_VERSION = 1
_OBSERVATION_KEYS = frozenset({
    "width",
    "height",
    "r_frame_rate",
    "pix_fmt",
    "color_primaries",
    "color_transfer",
    "color_space",
    "color_range",
    "nb_read_frames",
})
_COMMAND_OUTCOMES = frozenset({
    "completed",
    "spawn_failed",
    "stream_limit",
    "stdout_limit",
    "timed_out",
})
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_COMMAND_RECORDS = 3 * len(AV1_VALIDATION_V3_TIER1_FIXTURE_IDS)


class AV1ValidationV3Tier1DiagnosticsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1FixtureDiagnostic:
    fixture_id: str
    observation: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.fixture_id not in AV1_VALIDATION_V3_TIER1_FIXTURE_IDS:
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostic fixture ID is invalid"
            )
        if not set(self.observation) <= _OBSERVATION_KEYS:
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostic observation fields are invalid"
            )
        for value in self.observation.values():
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise AV1ValidationV3Tier1DiagnosticsError(
                    "AV1 v3 Tier 1 diagnostic observation value is invalid"
                )
            if isinstance(value, str) and len(value) > 64:
                raise AV1ValidationV3Tier1DiagnosticsError(
                    "AV1 v3 Tier 1 diagnostic observation value is invalid"
                )

    def to_payload(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "observation": dict(sorted(self.observation.items())),
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CommandRecord:
    program: str
    argv_sha256: str
    outcome: str
    returncode: int
    stdout_bytes: int
    stderr_bytes: int
    stderr_truncated: bool

    def __post_init__(self) -> None:
        if self.program not in {"ffmpeg", "ffprobe"}:
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostic program is invalid"
            )
        if not _SHA256_RE.fullmatch(self.argv_sha256):
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostic command digest is invalid"
            )
        if self.outcome not in _COMMAND_OUTCOMES:
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostic command outcome is invalid"
            )
        for value in (self.returncode, self.stdout_bytes, self.stderr_bytes):
            if isinstance(value, bool) or not isinstance(value, int):
                raise AV1ValidationV3Tier1DiagnosticsError(
                    "AV1 v3 Tier 1 diagnostic command count is invalid"
                )
        if self.stdout_bytes < 0 or self.stderr_bytes < 0:
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostic command count is invalid"
            )
        if not isinstance(self.stderr_truncated, bool):
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostic truncation state is invalid"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "program": self.program,
            "argv_sha256": self.argv_sha256,
            "outcome": self.outcome,
            "returncode": self.returncode,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stderr_truncated": self.stderr_truncated,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1RunDiagnostics:
    diagnostics_id: str
    attestation_id: str
    attestation_payload_sha256: str
    grant_id: str
    fixtures: tuple[AV1ValidationV3Tier1FixtureDiagnostic, ...]
    commands: tuple[AV1ValidationV3Tier1CommandRecord, ...]
    completed_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if (
            not self.attestation_id.startswith("av1vtier1coverage3_")
            or not _SHA256_RE.fullmatch(self.attestation_payload_sha256)
            or not self.grant_id.startswith("av1vtier1grant3_")
            or not _SHA256_RE.fullmatch(self.payload_sha256)
        ):
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostic binding is invalid"
            )
        if tuple(item.fixture_id for item in self.fixtures) != AV1_VALIDATION_V3_TIER1_FIXTURE_IDS:
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostic fixture set is incomplete"
            )
        if len(self.commands) > _MAX_COMMAND_RECORDS:
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostic command set is too large"
            )
        semantic = self.semantic_payload()
        if self.diagnostics_id != av1_validation_v3_id("tier1diagnostics", semantic):
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostics ID is invalid"
            )
        expected = f"sha256:{stable_json_hash({'diagnostics_id': self.diagnostics_id, **semantic})}"
        if self.payload_sha256 != expected:
            raise AV1ValidationV3Tier1DiagnosticsError(
                "AV1 v3 Tier 1 diagnostics digest is invalid"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER1_DIAGNOSTICS_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER1_DIAGNOSTICS_SCHEMA_VERSION,
            "gate": "A0",
            "tier": "tier1",
            "evidence_eligible": False,
            "empirical_authority_conferred": False,
            "attestation_id": self.attestation_id,
            "attestation_payload_sha256": self.attestation_payload_sha256,
            "grant_id": self.grant_id,
            "fixtures": [item.to_payload() for item in self.fixtures],
            "commands": [item.to_payload() for item in self.commands],
            "completed_at": self.completed_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "diagnostics_id": self.diagnostics_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


def build_av1_validation_v3_tier1_run_diagnostics(
    *,
    attestation: AV1ValidationV3Tier1CoverageAttestation,
    outcomes: Sequence[AV1ValidationV3Tier1FixtureOutcome],
    diagnostics: Sequence[AV1ValidationV3Tier1CommandDiagnostic],
) -> AV1ValidationV3Tier1RunDiagnostics:
    fixtures = tuple(
        AV1ValidationV3Tier1FixtureDiagnostic(
            fixture_id=outcome.fixture_id,
            observation=outcome.observation,
        )
        for outcome in sorted(outcomes, key=lambda item: item.fixture_id)
    )
    commands = tuple(
        AV1ValidationV3Tier1CommandRecord(
            program=item.program,
            argv_sha256=item.argv_sha256,
            outcome=item.outcome,
            returncode=item.returncode,
            stdout_bytes=item.stdout_bytes,
            stderr_bytes=item.stderr_bytes,
            stderr_truncated=item.stderr_truncated,
        )
        for item in diagnostics
    )
    semantic = {
        "schema": AV1_VALIDATION_V3_TIER1_DIAGNOSTICS_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_DIAGNOSTICS_SCHEMA_VERSION,
        "gate": "A0",
        "tier": "tier1",
        "evidence_eligible": False,
        "empirical_authority_conferred": False,
        "attestation_id": attestation.attestation_id,
        "attestation_payload_sha256": attestation.payload_sha256,
        "grant_id": attestation.grant_id,
        "fixtures": [item.to_payload() for item in fixtures],
        "commands": [item.to_payload() for item in commands],
        "completed_at": attestation.completed_at,
    }
    diagnostics_id = av1_validation_v3_id("tier1diagnostics", semantic)
    return AV1ValidationV3Tier1RunDiagnostics(
        diagnostics_id=diagnostics_id,
        attestation_id=attestation.attestation_id,
        attestation_payload_sha256=attestation.payload_sha256,
        grant_id=attestation.grant_id,
        fixtures=fixtures,
        commands=commands,
        completed_at=attestation.completed_at,
        payload_sha256=f"sha256:{stable_json_hash({'diagnostics_id': diagnostics_id, **semantic})}",
    )


def serialize_av1_validation_v3_tier1_run_diagnostics(
    diagnostics: AV1ValidationV3Tier1RunDiagnostics,
) -> bytes:
    return canonical_json_bytes(diagnostics.to_payload()) + b"\n"


def load_av1_validation_v3_tier1_run_diagnostics(
    path: Path,
) -> AV1ValidationV3Tier1RunDiagnostics:
    raw = path.read_bytes()
    try:
        diagnostics = av1_validation_v3_tier1_run_diagnostics_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1DiagnosticsError(
            "AV1 v3 Tier 1 run diagnostics are invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_tier1_run_diagnostics(diagnostics):
        raise AV1ValidationV3Tier1DiagnosticsError(
            "AV1 v3 Tier 1 run diagnostics bytes are not canonical"
        )
    return diagnostics


def av1_validation_v3_tier1_run_diagnostics_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier1RunDiagnostics:
    value = object_dict(payload)
    fixtures = tuple(
        AV1ValidationV3Tier1FixtureDiagnostic(
            fixture_id=str(item.get("fixture_id") or ""),
            observation=object_dict(item.get("observation")),
        )
        for item in (object_dict(raw) for raw in object_list(value.get("fixtures")))
    )
    commands = tuple(
        AV1ValidationV3Tier1CommandRecord(
            program=str(item.get("program") or ""),
            argv_sha256=str(item.get("argv_sha256") or ""),
            outcome=str(item.get("outcome") or ""),
            returncode=item.get("returncode") if isinstance(item.get("returncode"), int) else 0,
            stdout_bytes=item.get("stdout_bytes") if isinstance(item.get("stdout_bytes"), int) else -1,
            stderr_bytes=item.get("stderr_bytes") if isinstance(item.get("stderr_bytes"), int) else -1,
            stderr_truncated=item.get("stderr_truncated") is True,
        )
        for item in (object_dict(raw) for raw in object_list(value.get("commands")))
    )
    diagnostics = AV1ValidationV3Tier1RunDiagnostics(
        diagnostics_id=str(value.get("diagnostics_id") or ""),
        attestation_id=str(value.get("attestation_id") or ""),
        attestation_payload_sha256=str(value.get("attestation_payload_sha256") or ""),
        grant_id=str(value.get("grant_id") or ""),
        fixtures=fixtures,
        commands=commands,
        completed_at=str(value.get("completed_at") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != diagnostics.to_payload():
        raise AV1ValidationV3Tier1DiagnosticsError(
            "AV1 v3 Tier 1 run diagnostics fields are invalid"
        )
    return diagnostics
