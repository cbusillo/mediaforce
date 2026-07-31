from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from statistics import median
from typing import Any, Callable, Iterator, Literal, Mapping, Protocol, Sequence, cast

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.file_integrity import (
    FileIntegrityError,
    ensure_owner_only_directory,
    fsync_durable_file,
    open_stable_directory,
    rename_exclusive,
    stable_absolute_path,
)
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1_COLD_START_VALIDATION_MAXIMUM_CANDIDATE_CRF_SPAN,
    AV1_COLD_START_VALIDATION_MAXIMUM_DERIVATION_AGE_DAYS,
    AV1_COLD_START_VALIDATION_MINIMUM_DERIVATION_SOURCE_COUNT,
    AV1_COLD_START_VALIDATION_REQUIRED_DERIVATION_EVIDENCE_COUNT,
    AV1ColdStartValidationCandidateLockV1,
    _candidate_lock_from_payload,
    build_av1_cold_start_validation_candidate_lock,
)
from mediaforce.tuning.av1_validation_partition import (
    AV1_VALIDATION_DERIVATION_RESERVATION_COUNT,
    AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT,
    AV1ValidationPartitionError,
    AV1ValidationPartitionAssignment,
    AV1ValidationPartitionSource,
    AV1ValidationPrivatePartition,
)
from mediaforce.tuning.av1_validation_v2 import (
    AV1ValidationManifestV2,
    AV1ValidationV2DerivationAuthorization,
    assert_preregistered_av1_validation_manifest_v2,
)
from mediaforce.tuning.content_intent_observations import (
    AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON,
    BOUNDARY_ASSESSMENT_CONTRACT,
    ContentIntentBoundaryObservation,
    content_intent_boundary_compatibility_from_payload,
    content_intent_boundary_observation_integrity_valid,
)
from mediaforce.web.runtime_lock import assert_mediaforce_runtime_lock_held


AV1_VALIDATION_DERIVATION_PLAN_SCHEMA = "mediaforce.av1_cold_start_derivation_plan"
AV1_VALIDATION_DERIVATION_SOURCE_COMMITMENT_SCHEMA = (
    "mediaforce.av1_cold_start_derivation_source_commitment"
)
AV1_VALIDATION_DERIVATION_ATTEMPT_SCHEMA = "mediaforce.av1_cold_start_derivation_attempt"
AV1_VALIDATION_DERIVATION_TERMINAL_SCHEMA = "mediaforce.av1_cold_start_derivation_terminal_record"
AV1_VALIDATION_DERIVATION_VERDICT_INTENT_SCHEMA = (
    "mediaforce.av1_cold_start_derivation_verdict_intent"
)
AV1_VALIDATION_DERIVATION_VERDICT_CLAIM_SCHEMA = (
    "mediaforce.av1_cold_start_derivation_verdict_claim"
)
AV1_VALIDATION_DERIVATION_PROPOSAL_SCHEMA = "mediaforce.av1_cold_start_derivation_candidate_proposal"
AV1_VALIDATION_DERIVATION_REVIEW_SCHEMA = "mediaforce.av1_cold_start_derivation_candidate_review"
AV1_VALIDATION_DERIVATION_REVIEW_ENVELOPE_SCHEMA = (
    "mediaforce.av1_cold_start_derivation_review_envelope"
)
AV1_VALIDATION_DERIVATION_REVIEW_CLAIM_SCHEMA = (
    "mediaforce.av1_cold_start_derivation_review_claim"
)
AV1_VALIDATION_DERIVATION_LOCK_ENVELOPE_SCHEMA = (
    "mediaforce.av1_cold_start_derivation_candidate_lock_envelope"
)
AV1_VALIDATION_DERIVATION_DIRECTORY_BINDING_SCHEMA = (
    "mediaforce.av1_cold_start_derivation_directory_binding"
)
AV1_VALIDATION_DERIVATION_SCHEMA_VERSION = 2
AV1_VALIDATION_DERIVATION_CONTRACT_VERSION = "av1vdw2"
AV1_VALIDATION_DERIVATION_EXECUTION_SCOPE = "reserved_derivation_sources_only"
AV1_VALIDATION_DERIVATION_SEARCH_MODE = "unchanged_measured_full_search"
AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY = "av1-validation-derivation"
AV1_VALIDATION_DERIVATION_MAXIMUM_CRF_MAD = 2.0
AV1_VALIDATION_DERIVATION_HIGH_CONFIDENCE_RELATIVE_MAD = 0.10
AV1_VALIDATION_DERIVATION_MODERATE_CONFIDENCE_RELATIVE_MAD = 0.25
AV1_VALIDATION_DERIVATION_AGENT_REVIEW_MARKER = "MEDIAFORCE_AV1_REVIEW_V2 "

AV1ValidationDerivationTerminalStatus = Literal[
    "observed",
    "failed",
    "excluded",
    "stopped",
]
AV1ValidationDerivationAttemptStatus = Literal[
    "review_pending",
    "failed",
    "excluded",
    "stopped",
]
AV1ValidationDerivationReviewLane = Literal[
    "architecture",
    "statistical_model_contract",
    "privacy_security",
    "experimental_design",
    "adversarial",
]
AV1ValidationDerivationReviewDecision = Literal["approved", "rejected"]

AV1_VALIDATION_DERIVATION_REVIEW_LANES: tuple[AV1ValidationDerivationReviewLane, ...] = (
    "architecture",
    "statistical_model_contract",
    "privacy_security",
    "experimental_design",
    "adversarial",
)
AV1_VALIDATION_DERIVATION_REASON_CODES = frozenset({
    "authorization_expired",
    "compatibility_drift",
    "content_intent_observation_excluded",
    "interrupted_claim",
    "media_unavailable",
    "metrics_incomplete",
    "operator_stop",
    "quality_floor_miss",
    "runtime_failure",
    "safety_stop",
    "storage_stop",
    "timeout",
    "toolchain_drift",
})

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}\Z")
_AGENT_REVIEWER_RE = re.compile(
    r"agent:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)

if (
    AV1_VALIDATION_DERIVATION_RESERVATION_COUNT
    != AV1_COLD_START_VALIDATION_REQUIRED_DERIVATION_EVIDENCE_COUNT
):
    raise RuntimeError("AV1 derivation contract requires exactly 12 reservations per candidate")


class AV1ValidationDerivationError(ValueError):
    pass


class AV1ValidationDerivationPublicationDeadlineError(
        AV1ValidationDerivationError,
):
    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        super().__init__(f"AV1 {label} was published after authorization expired")


class AV1ValidationDerivationVerdictRetryMismatchError(
        AV1ValidationDerivationError,
):
    def __init__(self, frozen_intent: Mapping[str, Any]) -> None:
        self.frozen_intent = dict(frozen_intent)
        retry_payload = {
            "concern_tags": self.frozen_intent.get("concern_tags"),
            "evidence_ids": self.frozen_intent.get("evidence_ids"),
            "moment_indexes": self.frozen_intent.get("moment_indexes"),
            "verdict": self.frozen_intent.get("verdict"),
        }
        super().__init__(
            "AV1 derivation verdict retry does not match its immutable intent; "
            "retry with "
            f"{canonical_json_bytes(retry_payload).decode('utf-8')}"
        )


class _AV1ValidationDerivationArtifactAlreadyExists(
        AV1ValidationDerivationError,
):
    pass


@dataclass(frozen=True, slots=True)
class _RetainedOwnerOnlyDirectory:
    path: Path
    descriptor: int
    label: str

    def assert_current(self) -> None:
        _assert_owner_only_directory_descriptor_binding(
            self.path,
            self.descriptor,
            self.label,
        )


@contextmanager
def retain_av1_validation_derivation_publication_directories(
        specifications: Sequence[tuple[Path, str, str, str]],
) -> Iterator[Callable[[], None]]:
    retained: list[_RetainedOwnerOnlyDirectory] = []
    active_error: BaseException | None = None
    try:
        seen_paths: dict[Path, tuple[str, str, str]] = {}
        for path, kind, binding_id, binding_digest in specifications:
            normalized_path = stable_absolute_path(path)
            specification = kind, binding_id, binding_digest
            existing_specification = seen_paths.get(normalized_path)
            if (
                existing_specification is not None
                and existing_specification != specification
            ):
                raise AV1ValidationDerivationError(
                    "AV1 derivation publication directory has conflicting bindings"
                )
            if existing_specification is not None:
                continue
            seen_paths[normalized_path] = specification
            _bind_owner_only_directory(
                normalized_path,
                kind=kind,
                binding_id=binding_id,
                binding_digest=binding_digest,
            )
            canonical_path, descriptor = open_stable_directory(
                normalized_path,
                require_owner_only=True,
            )
            try:
                binding = _load_owner_only_directory_binding(
                    canonical_path,
                    expected_kind=kind,
                )
                if (
                    binding["binding_id"] != binding_id
                    or binding["binding_digest"] != binding_digest
                ):
                    raise AV1ValidationDerivationError(
                        "AV1 derivation publication directory binding drifted"
                    )
            except BaseException:
                os.close(descriptor)
                raise
            retained.append(_RetainedOwnerOnlyDirectory(
                path=canonical_path,
                descriptor=descriptor,
                label="derivation publication",
            ))

        def assert_current() -> None:
            for binding in retained:
                binding.assert_current()

        assert_current()
        yield assert_current
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        cleanup_errors: list[OSError] = []
        for binding in reversed(retained):
            try:
                os.close(binding.descriptor)
            except OSError as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            if active_error is not None:
                for cleanup_error in cleanup_errors:
                    active_error.add_note(
                        "AV1 publication directory cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            else:
                raise AV1ValidationDerivationError(
                    "AV1 derivation publication directory cleanup failed"
                ) from cleanup_errors[0]


def av1_validation_derivation_statistics_contract_sha256(
        manifest: AV1ValidationManifestV2,
) -> str:
    return _payload_sha256({
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "maximum_crf_mad": AV1_VALIDATION_DERIVATION_MAXIMUM_CRF_MAD,
        "maximum_candidate_crf_span": manifest.criteria.maximum_candidate_crf_span,
        "high_confidence_relative_mad_maximum": (
            AV1_VALIDATION_DERIVATION_HIGH_CONFIDENCE_RELATIVE_MAD
        ),
        "moderate_confidence_relative_mad_maximum": (
            AV1_VALIDATION_DERIVATION_MODERATE_CONFIDENCE_RELATIVE_MAD
        ),
        "minimum_confidence_level": manifest.criteria.confidence_level,
        "minimum_confidence_score": manifest.criteria.confidence_score,
        "maximum_derivation_age_days": manifest.criteria.maximum_derivation_age_days,
    })


class AV1ValidationDerivationSourceResolver(Protocol):
    def __call__(self, source: AV1ValidationPartitionSource) -> str: ...

    def source_size_bytes(self, source: AV1ValidationPartitionSource) -> int: ...


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationSourceCommitment:
    assignment_id: str
    local_item_id: int
    source_identity: str
    source_sha256: str
    source_size_bytes: int
    evidence_summary_sha256: str

    def __post_init__(self) -> None:
        if not _SAFE_TOKEN_RE.fullmatch(self.assignment_id):
            raise AV1ValidationDerivationError(
                "AV1 derivation source commitment assignment is invalid"
            )
        if self.local_item_id <= 0 or not self.source_identity.strip():
            raise AV1ValidationDerivationError(
                "AV1 derivation source commitment identity is invalid"
            )
        if self.source_size_bytes <= 0:
            raise AV1ValidationDerivationError(
                "AV1 derivation source commitment size is invalid"
            )
        _require_sha256(self.source_sha256, "source commitment digest")
        _require_sha256(
            self.evidence_summary_sha256,
            "source commitment evidence digest",
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_DERIVATION_SOURCE_COMMITMENT_SCHEMA,
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "assignment_id": self.assignment_id,
            "local_item_id": self.local_item_id,
            "source_identity": self.source_identity,
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "evidence_summary_sha256": self.evidence_summary_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationPlan:
    plan_id: str
    manifest_id: str
    manifest_payload_sha256: str
    partition_id: str
    partition_payload_sha256: str
    selection_lock_sha256: str
    derivation_partition_sha256: str
    runtime_context_sha256: str
    execution_environment_sha256: str
    statistics_contract_sha256: str
    review_runner_canonical_path_sha256: str
    review_runner_binary_sha256: str
    repository_commit: str
    repository_tree: str
    authorization: AV1ValidationV2DerivationAuthorization
    assignments: tuple[AV1ValidationPartitionAssignment, ...]
    source_commitments: tuple[AV1ValidationDerivationSourceCommitment, ...]
    source_commitment_sha256: str
    payload_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.manifest_payload_sha256, "manifest digest"),
            (self.partition_payload_sha256, "partition digest"),
            (self.selection_lock_sha256, "selection-lock digest"),
            (self.derivation_partition_sha256, "derivation-partition digest"),
            (self.runtime_context_sha256, "runtime-context digest"),
            (self.execution_environment_sha256, "execution-environment digest"),
            (self.statistics_contract_sha256, "statistics-contract digest"),
            (
                self.review_runner_canonical_path_sha256,
                "review-runner canonical-path digest",
            ),
            (self.review_runner_binary_sha256, "review-runner binary digest"),
            (self.source_commitment_sha256, "source-commitment digest"),
            (self.payload_sha256, "plan digest"),
        ):
            _require_sha256(value, label)
        _require_git_object_id(
            self.repository_commit,
            "plan repository commit",
        )
        _require_git_object_id(
            self.repository_tree,
            "plan repository tree",
        )
        if not self.manifest_id.startswith("av1vmanifest2_"):
            raise AV1ValidationDerivationError("AV1 derivation plan manifest is invalid")
        if not self.partition_id.startswith("av1vpartition1_"):
            raise AV1ValidationDerivationError("AV1 derivation plan partition is invalid")
        if self.authorization.manifest_id != self.manifest_id:
            raise AV1ValidationDerivationError("AV1 derivation authorization manifest drifted")
        if self.authorization.manifest_payload_sha256 != self.manifest_payload_sha256:
            raise AV1ValidationDerivationError("AV1 derivation authorization manifest digest drifted")
        if self.authorization.selection_lock_sha256 != self.selection_lock_sha256:
            raise AV1ValidationDerivationError("AV1 derivation authorization selection lock drifted")
        if self.authorization.derivation_partition_sha256 != self.derivation_partition_sha256:
            raise AV1ValidationDerivationError("AV1 derivation authorization partition drifted")
        if len(self.assignments) != 2 * AV1_VALIDATION_DERIVATION_RESERVATION_COUNT:
            raise AV1ValidationDerivationError("AV1 derivation plan must contain exactly 24 assignments")
        if any(assignment.role != "derivation" for assignment in self.assignments):
            raise AV1ValidationDerivationError("AV1 derivation plan contains a non-derivation assignment")
        assignment_ids = [assignment.assignment_id for assignment in self.assignments]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise AV1ValidationDerivationError("AV1 derivation plan repeats an assignment")
        counts = Counter(assignment.cell_plan_id for assignment in self.assignments)
        if len(counts) != 2 or set(counts.values()) != {AV1_VALIDATION_DERIVATION_RESERVATION_COUNT}:
            raise AV1ValidationDerivationError("AV1 derivation plan candidate reservations are invalid")
        if self.assignments != tuple(sorted(self.assignments, key=_assignment_sort_key)):
            raise AV1ValidationDerivationError("AV1 derivation plan assignments are not canonical")
        _validate_plan_source_commitments(
            assignments=self.assignments,
            commitments=self.source_commitments,
            source_commitment_sha256=self.source_commitment_sha256,
        )
        semantic_payload = self.semantic_payload()
        if self.plan_id != _derivation_id("plan", semantic_payload):
            raise AV1ValidationDerivationError("AV1 derivation plan ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({"plan_id": self.plan_id, **semantic_payload}):
            raise AV1ValidationDerivationError("AV1 derivation plan digest does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_DERIVATION_PLAN_SCHEMA,
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "manifest_id": self.manifest_id,
            "manifest_payload_sha256": self.manifest_payload_sha256,
            "partition_id": self.partition_id,
            "partition_payload_sha256": self.partition_payload_sha256,
            "selection_lock_sha256": self.selection_lock_sha256,
            "derivation_partition_sha256": self.derivation_partition_sha256,
            "runtime_context_sha256": self.runtime_context_sha256,
            "execution_environment_sha256": self.execution_environment_sha256,
            "statistics_contract_sha256": self.statistics_contract_sha256,
            "review_runner_canonical_path_sha256": (
                self.review_runner_canonical_path_sha256
            ),
            "review_runner_binary_sha256": self.review_runner_binary_sha256,
            "repository_commit": self.repository_commit,
            "repository_tree": self.repository_tree,
            "authorization": self.authorization.to_payload(),
            "execution_scope": AV1_VALIDATION_DERIVATION_EXECUTION_SCOPE,
            "search_mode": AV1_VALIDATION_DERIVATION_SEARCH_MODE,
            "derivation_execution_authorized": True,
            "cold_start_warm_start_allowed": False,
            "validation_harness_allowed": False,
            "guided_probe_allowed": False,
            "holdout_execution_authorized": False,
            "retry_substitution_backfill_allowed": False,
            "public_bundle_activation_allowed": False,
            "assignments": [assignment.to_payload() for assignment in self.assignments],
            "source_commitments": [
                commitment.to_payload()
                for commitment in self.source_commitments
            ],
            "source_commitment_sha256": self.source_commitment_sha256,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


def av1_validation_derivation_source_commitment_sha256(
        commitments: Sequence[AV1ValidationDerivationSourceCommitment],
) -> str:
    return _payload_sha256({
        "schema": AV1_VALIDATION_DERIVATION_SOURCE_COMMITMENT_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "commitments": [
            commitment.to_payload()
            for commitment in commitments
        ],
    })


def build_av1_validation_derivation_source_commitments(
        *,
        partition: AV1ValidationPrivatePartition,
        assignments: Sequence[AV1ValidationPartitionAssignment],
        resolver: AV1ValidationDerivationSourceResolver,
) -> tuple[AV1ValidationDerivationSourceCommitment, ...]:
    sources_by_item_id = {
        source.local_item_id: source
        for source in partition.inventory_sources
    }
    commitments: list[AV1ValidationDerivationSourceCommitment] = []
    try:
        for assignment in assignments:
            source = sources_by_item_id.get(assignment.local_item_id)
            if source is None:
                raise AV1ValidationDerivationError(
                    "AV1 derivation source commitment is absent from the partition"
                )
            commitments.append(AV1ValidationDerivationSourceCommitment(
                assignment_id=assignment.assignment_id,
                local_item_id=assignment.local_item_id,
                source_identity=source.source_identity,
                source_sha256=resolver(source),
                source_size_bytes=resolver.source_size_bytes(source),
                evidence_summary_sha256=assignment.evidence_summary_sha256,
            ))
    except AV1ValidationPartitionError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation source commitments could not be resolved"
        ) from exc
    return tuple(sorted(commitments, key=_source_commitment_sort_key))


def av1_validation_derivation_plan_source_commitment(
        plan: AV1ValidationDerivationPlan,
        assignment_id: str,
) -> AV1ValidationDerivationSourceCommitment:
    commitment = next(
        (
            item
            for item in plan.source_commitments
            if item.assignment_id == assignment_id
        ),
        None,
    )
    if commitment is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation plan source commitment is unavailable"
        )
    return commitment


def assert_av1_validation_derivation_source_commitments(
        plan: AV1ValidationDerivationPlan,
        *,
        resolver: AV1ValidationDerivationSourceResolver,
) -> None:
    commitments: list[AV1ValidationDerivationSourceCommitment] = []
    try:
        for assignment in plan.assignments:
            expected = av1_validation_derivation_plan_source_commitment(
                plan,
                assignment.assignment_id,
            )
            source = _source_for_commitment(assignment, expected)
            commitments.append(AV1ValidationDerivationSourceCommitment(
                assignment_id=assignment.assignment_id,
                local_item_id=assignment.local_item_id,
                source_identity=expected.source_identity,
                source_sha256=resolver(source),
                source_size_bytes=resolver.source_size_bytes(source),
                evidence_summary_sha256=assignment.evidence_summary_sha256,
            ))
    except AV1ValidationPartitionError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation source commitments could not be revalidated"
        ) from exc
    rebuilt = tuple(sorted(commitments, key=_source_commitment_sort_key))
    if (
        rebuilt != plan.source_commitments
        or av1_validation_derivation_source_commitment_sha256(rebuilt)
        != plan.source_commitment_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation source commitments drifted from the immutable plan"
        )


def _validate_plan_source_commitments(
        *,
        assignments: Sequence[AV1ValidationPartitionAssignment],
        commitments: Sequence[AV1ValidationDerivationSourceCommitment],
        source_commitment_sha256: str,
) -> None:
    if len(commitments) != len(assignments):
        raise AV1ValidationDerivationError(
            "AV1 derivation plan source commitments are incomplete"
        )
    canonical = tuple(sorted(commitments, key=_source_commitment_sort_key))
    if tuple(commitments) != canonical:
        raise AV1ValidationDerivationError(
            "AV1 derivation plan source commitments are not canonical"
        )
    assignments_by_id = {
        assignment.assignment_id: assignment
        for assignment in assignments
    }
    commitment_ids = [commitment.assignment_id for commitment in commitments]
    if (
        len(commitment_ids) != len(set(commitment_ids))
        or set(commitment_ids) != set(assignments_by_id)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation plan source commitments do not cover its assignments"
        )
    for commitment in commitments:
        assignment = assignments_by_id[commitment.assignment_id]
        if (
            commitment.local_item_id != assignment.local_item_id
            or commitment.evidence_summary_sha256
            != assignment.evidence_summary_sha256
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation source commitment does not match its assignment"
            )
    if (
        av1_validation_derivation_source_commitment_sha256(commitments)
        != source_commitment_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation source-commitment digest does not match its payload"
        )


def _source_for_commitment(
        assignment: AV1ValidationPartitionAssignment,
        commitment: AV1ValidationDerivationSourceCommitment,
) -> AV1ValidationPartitionSource:
    return AV1ValidationPartitionSource(
        local_item_id=commitment.local_item_id,
        source_identity=commitment.source_identity,
        title_identity=assignment.title_token,
        series_identity=assignment.series_token,
        source_group_identity=assignment.source_group_token,
        traits=assignment.traits,
        compatibility_signature=assignment.compatibility_signature,
        base_policy_signature=assignment.policy_signature,
        target_video_bitrate_bps=assignment.target_video_bitrate_bps,
        quality_metric=assignment.quality_metric,
        quality_target=assignment.quality_target,
        minimum_quality_score=assignment.minimum_quality_score,
        evidence_summary_sha256=commitment.evidence_summary_sha256,
    )


def _source_commitment_sort_key(
        commitment: AV1ValidationDerivationSourceCommitment,
) -> str:
    return commitment.assignment_id


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationObservationProjection:
    observation_id: str
    observation_payload_sha256: str
    local_item_id: int
    source_token: str
    title_token: str
    series_token: str
    source_group_token: str
    traits: tuple[str, ...]
    intent_level: str
    compatibility_signature: str
    policy_signature: str
    observation_compatibility_key: str
    observation_policy_hash: str
    verdict: Literal["acceptable", "unacceptable"]
    chosen_crf: float
    boundary_bitrate_bps: int
    quality_metric: str
    quality_target: float
    minimum_quality_score: float
    measured_quality_score: float
    quality_floor_met: bool
    recorded_at: str

    def __post_init__(self) -> None:
        if not self.observation_id.startswith("cibo1_"):
            raise AV1ValidationDerivationError("AV1 derivation observation ID is invalid")
        _require_sha256(self.observation_payload_sha256, "observation digest")
        if self.local_item_id <= 0:
            raise AV1ValidationDerivationError("AV1 derivation observation item is invalid")
        for token in (
            self.source_token,
            self.title_token,
            self.series_token,
            self.source_group_token,
            self.compatibility_signature,
            self.policy_signature,
            self.observation_compatibility_key,
            self.observation_policy_hash,
        ):
            if not _SAFE_TOKEN_RE.fullmatch(token):
                raise AV1ValidationDerivationError("AV1 derivation observation token is invalid")
        if self.traits != tuple(sorted(set(self.traits))) or not self.traits:
            raise AV1ValidationDerivationError("AV1 derivation observation traits are invalid")
        if self.verdict not in {"acceptable", "unacceptable"}:
            raise AV1ValidationDerivationError("AV1 derivation observation verdict is invalid")
        if not math.isfinite(self.chosen_crf) or not 0 <= self.chosen_crf <= 63:
            raise AV1ValidationDerivationError("AV1 derivation observation CRF is invalid")
        if self.boundary_bitrate_bps <= 0:
            raise AV1ValidationDerivationError("AV1 derivation observation bitrate is invalid")
        if not all(
            math.isfinite(value) and value >= 0
            for value in (
                self.quality_target,
                self.minimum_quality_score,
                self.measured_quality_score,
            )
        ):
            raise AV1ValidationDerivationError("AV1 derivation observation quality values are invalid")
        if self.minimum_quality_score > self.quality_target:
            raise AV1ValidationDerivationError("AV1 derivation observation quality floor is invalid")
        if self.quality_floor_met != (
            self.measured_quality_score >= self.minimum_quality_score
        ):
            raise AV1ValidationDerivationError("AV1 derivation observation quality flag is inconsistent")
        _parse_timestamp(self.recorded_at, "observation timestamp")

    def to_payload(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "observation_payload_sha256": self.observation_payload_sha256,
            "local_item_id": self.local_item_id,
            "source_token": self.source_token,
            "title_token": self.title_token,
            "series_token": self.series_token,
            "source_group_token": self.source_group_token,
            "traits": list(self.traits),
            "intent_level": self.intent_level,
            "compatibility_signature": self.compatibility_signature,
            "policy_signature": self.policy_signature,
            "observation_compatibility_key": self.observation_compatibility_key,
            "observation_policy_hash": self.observation_policy_hash,
            "verdict": self.verdict,
            "chosen_crf": self.chosen_crf,
            "boundary_bitrate_bps": self.boundary_bitrate_bps,
            "quality_metric": self.quality_metric,
            "quality_target": self.quality_target,
            "minimum_quality_score": self.minimum_quality_score,
            "measured_quality_score": self.measured_quality_score,
            "quality_floor_met": self.quality_floor_met,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationAttempt:
    attempt_id: str
    plan_id: str
    authorization_id: str
    assignment_id: str
    cell_plan_id: str
    ordinal: int
    started_at: str
    completed_at: str
    status: AV1ValidationDerivationAttemptStatus
    reason_code: str | None
    calibration_payload_json: str | None
    calibration_payload_sha256: str | None
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.plan_id.startswith("av1vdplan1_"):
            raise AV1ValidationDerivationError("AV1 derivation attempt plan is invalid")
        if not self.authorization_id.startswith("av1vderivation1_"):
            raise AV1ValidationDerivationError("AV1 derivation attempt authorization is invalid")
        if not _SAFE_TOKEN_RE.fullmatch(self.assignment_id):
            raise AV1ValidationDerivationError("AV1 derivation attempt assignment is invalid")
        if not _SAFE_TOKEN_RE.fullmatch(self.cell_plan_id) or self.ordinal <= 0:
            raise AV1ValidationDerivationError("AV1 derivation attempt slot is invalid")
        started = _parse_timestamp(self.started_at, "attempt start")
        completed = _parse_timestamp(self.completed_at, "attempt completion")
        if completed < started:
            raise AV1ValidationDerivationError("AV1 derivation attempt chronology is invalid")
        if self.status == "review_pending":
            if (
                self.reason_code is not None
                or self.calibration_payload_json is None
                or self.calibration_payload_sha256 is None
            ):
                raise AV1ValidationDerivationError("AV1 review-pending attempt is incomplete")
            calibration_payload = self.calibration_payload()
            _validate_calibration_execution(calibration_payload)
            if self.calibration_payload_json != canonical_json_bytes(calibration_payload).decode("utf-8"):
                raise AV1ValidationDerivationError("AV1 derivation calibration payload is not canonical")
            if self.calibration_payload_sha256 != _payload_sha256(calibration_payload):
                raise AV1ValidationDerivationError("AV1 derivation calibration digest is invalid")
        elif (
            self.status not in {"failed", "excluded", "stopped"}
            or self.reason_code not in AV1_VALIDATION_DERIVATION_REASON_CODES
            or self.calibration_payload_json is not None
            or self.calibration_payload_sha256 is not None
        ):
            raise AV1ValidationDerivationError("AV1 non-review attempt is invalid")
        _require_sha256(self.payload_sha256, "attempt digest")
        semantic_payload = self.semantic_payload()
        if self.attempt_id != _derivation_id("attempt", semantic_payload):
            raise AV1ValidationDerivationError("AV1 derivation attempt ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({"attempt_id": self.attempt_id, **semantic_payload}):
            raise AV1ValidationDerivationError("AV1 derivation attempt digest does not match its payload")

    def calibration_payload(self) -> dict[str, Any]:
        if self.calibration_payload_json is None:
            return {}
        try:
            return object_dict(json.loads(self.calibration_payload_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AV1ValidationDerivationError("AV1 derivation calibration payload is invalid") from exc

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_DERIVATION_ATTEMPT_SCHEMA,
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "plan_id": self.plan_id,
            "authorization_id": self.authorization_id,
            "assignment_id": self.assignment_id,
            "cell_plan_id": self.cell_plan_id,
            "ordinal": self.ordinal,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "reason_code": self.reason_code,
            "calibration_payload": self.calibration_payload() if self.calibration_payload_json is not None else None,
            "calibration_payload_sha256": self.calibration_payload_sha256,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationTerminalRecord:
    record_id: str
    plan_id: str
    authorization_id: str
    attempt_id: str
    attempt_payload_sha256: str
    assignment_id: str
    cell_plan_id: str
    ordinal: int
    started_at: str
    completed_at: str
    status: AV1ValidationDerivationTerminalStatus
    reason_code: str | None
    observation: AV1ValidationDerivationObservationProjection | None
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.plan_id.startswith("av1vdplan1_"):
            raise AV1ValidationDerivationError("AV1 derivation terminal plan is invalid")
        if not self.authorization_id.startswith("av1vderivation1_"):
            raise AV1ValidationDerivationError("AV1 derivation terminal authorization is invalid")
        if not self.attempt_id.startswith("av1vdattempt1_"):
            raise AV1ValidationDerivationError("AV1 derivation terminal attempt is invalid")
        _require_sha256(self.attempt_payload_sha256, "attempt digest")
        if not _SAFE_TOKEN_RE.fullmatch(self.assignment_id):
            raise AV1ValidationDerivationError("AV1 derivation terminal assignment is invalid")
        if not _SAFE_TOKEN_RE.fullmatch(self.cell_plan_id) or self.ordinal <= 0:
            raise AV1ValidationDerivationError("AV1 derivation terminal slot is invalid")
        started = _parse_timestamp(self.started_at, "attempt start")
        completed = _parse_timestamp(self.completed_at, "attempt completion")
        if completed < started:
            raise AV1ValidationDerivationError("AV1 derivation terminal chronology is invalid")
        if self.status == "observed":
            if self.reason_code is not None or self.observation is None:
                raise AV1ValidationDerivationError("AV1 observed terminal record is incomplete")
        elif (
            self.status not in {"failed", "excluded", "stopped"}
            or self.reason_code not in AV1_VALIDATION_DERIVATION_REASON_CODES
            or self.observation is not None
        ):
            raise AV1ValidationDerivationError("AV1 non-observed terminal record is invalid")
        semantic_payload = self.semantic_payload()
        if self.record_id != _derivation_id("terminal", semantic_payload):
            raise AV1ValidationDerivationError("AV1 derivation terminal ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({"record_id": self.record_id, **semantic_payload}):
            raise AV1ValidationDerivationError("AV1 derivation terminal digest does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_DERIVATION_TERMINAL_SCHEMA,
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "plan_id": self.plan_id,
            "authorization_id": self.authorization_id,
            "attempt_id": self.attempt_id,
            "attempt_payload_sha256": self.attempt_payload_sha256,
            "assignment_id": self.assignment_id,
            "cell_plan_id": self.cell_plan_id,
            "ordinal": self.ordinal,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "reason_code": self.reason_code,
            "observation": self.observation.to_payload() if self.observation is not None else None,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationCandidateProposal:
    proposal_id: str
    plan_id: str
    manifest_id: str
    cell_plan_id: str
    exact_traits: tuple[str, ...]
    crf_lower: float
    crf_center: float
    crf_upper: float
    crf_mad: float
    bitrate_relative_mad: float
    statistics_contract_sha256: str
    minimum_derivation_source_count: int
    maximum_derivation_age_days: int
    maximum_candidate_crf_span: float
    compatibility_signature: str
    policy_signature: str
    target_video_bitrate_min_bps: int
    target_video_bitrate_max_bps: int
    minimum_quality_score: float
    confidence_level: Literal["moderate", "high"]
    confidence_score: float
    derivation_evidence_count: int
    derivation_source_count: int
    derivation_source_tokens: tuple[str, ...]
    derivation_title_tokens: tuple[str, ...]
    derivation_series_tokens: tuple[str, ...]
    derivation_source_group_tokens: tuple[str, ...]
    derivation_source_group_observation_tokens: tuple[str, ...]
    derivation_oldest_recorded_at: str
    derivation_newest_recorded_at: str
    derivation_conflict_count: int
    derivation_snapshot_sha256: str
    selection_lock_sha256: str
    proposed_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.plan_id.startswith("av1vdplan1_"):
            raise AV1ValidationDerivationError("AV1 derivation proposal plan is invalid")
        if not self.manifest_id.startswith("av1vmanifest2_"):
            raise AV1ValidationDerivationError("AV1 derivation proposal manifest is invalid")
        if not _SAFE_TOKEN_RE.fullmatch(self.cell_plan_id):
            raise AV1ValidationDerivationError("AV1 derivation proposal cell plan is invalid")
        if self.exact_traits != tuple(sorted(set(self.exact_traits))) or not self.exact_traits:
            raise AV1ValidationDerivationError("AV1 derivation proposal traits are invalid")
        if not all(
            math.isfinite(value)
            for value in (self.crf_lower, self.crf_center, self.crf_upper)
        ) or not 0 <= self.crf_lower <= self.crf_center <= self.crf_upper <= 63:
            raise AV1ValidationDerivationError("AV1 derivation proposal CRF range is invalid")
        if math.ceil(self.crf_lower) > math.floor(self.crf_upper):
            raise AV1ValidationDerivationError("AV1 derivation proposal has no executable CRF")
        if not (
            math.isfinite(self.maximum_candidate_crf_span)
            and 0
            < self.maximum_candidate_crf_span
            <= AV1_COLD_START_VALIDATION_MAXIMUM_CANDIDATE_CRF_SPAN
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation proposal CRF-span contract is invalid"
            )
        if self.crf_upper - self.crf_lower > self.maximum_candidate_crf_span:
            raise AV1ValidationDerivationError("AV1 derivation proposal CRF span is too wide")
        if (
            not math.isfinite(self.crf_mad)
            or self.crf_mad < 0
            or self.crf_mad > AV1_VALIDATION_DERIVATION_MAXIMUM_CRF_MAD
        ):
            raise AV1ValidationDerivationError("AV1 derivation proposal CRF dispersion is invalid")
        if (
            not math.isfinite(self.bitrate_relative_mad)
            or not 0 <= self.bitrate_relative_mad
            <= AV1_VALIDATION_DERIVATION_MODERATE_CONFIDENCE_RELATIVE_MAD
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation proposal bitrate dispersion is invalid"
            )
        for token in (self.compatibility_signature, self.policy_signature):
            if not _SAFE_TOKEN_RE.fullmatch(token):
                raise AV1ValidationDerivationError("AV1 derivation proposal signature is invalid")
        if not 0 < self.target_video_bitrate_min_bps <= self.target_video_bitrate_max_bps:
            raise AV1ValidationDerivationError("AV1 derivation proposal bitrate range is invalid")
        if not math.isfinite(self.minimum_quality_score) or self.minimum_quality_score <= 0:
            raise AV1ValidationDerivationError("AV1 derivation proposal quality floor is invalid")
        if self.derivation_evidence_count != AV1_VALIDATION_DERIVATION_RESERVATION_COUNT:
            raise AV1ValidationDerivationError("AV1 derivation proposal evidence count is invalid")
        if not (
            AV1_COLD_START_VALIDATION_MINIMUM_DERIVATION_SOURCE_COUNT
            <= self.minimum_derivation_source_count
            <= self.derivation_evidence_count
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation proposal source-count contract is invalid"
            )
        if not (
            1
            <= self.maximum_derivation_age_days
            <= AV1_COLD_START_VALIDATION_MAXIMUM_DERIVATION_AGE_DAYS
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation proposal freshness contract is invalid"
            )
        if self.derivation_conflict_count != 0:
            raise AV1ValidationDerivationError("AV1 derivation proposal evidence is conflicting")
        if self.derivation_source_count < self.minimum_derivation_source_count:
            raise AV1ValidationDerivationError("AV1 derivation proposal source count is invalid")
        if self.confidence_level not in {"moderate", "high"} or self.confidence_score < 0.7:
            raise AV1ValidationDerivationError("AV1 derivation proposal confidence is insufficient")
        for digest in (
            self.derivation_snapshot_sha256,
            self.selection_lock_sha256,
            self.statistics_contract_sha256,
            self.payload_sha256,
        ):
            _require_sha256(digest, "proposal digest")
        for tokens, label in (
            (self.derivation_source_tokens, "source"),
            (self.derivation_title_tokens, "title"),
            (self.derivation_series_tokens, "series"),
            (self.derivation_source_group_tokens, "source-group"),
        ):
            if tokens != tuple(sorted(set(tokens))) or not tokens:
                raise AV1ValidationDerivationError(
                    f"AV1 derivation proposal {label} tokens are invalid"
                )
            if any(not _SAFE_TOKEN_RE.fullmatch(token) for token in tokens):
                raise AV1ValidationDerivationError(
                    f"AV1 derivation proposal {label} token is invalid"
                )
        if (
            self.derivation_source_group_observation_tokens
            != tuple(sorted(self.derivation_source_group_observation_tokens))
            or len(self.derivation_source_group_observation_tokens)
            != self.derivation_evidence_count
            or any(
                not _SAFE_TOKEN_RE.fullmatch(token)
                for token in self.derivation_source_group_observation_tokens
            )
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation proposal source-group observations are invalid"
            )
        source_group_counts = Counter(self.derivation_source_group_observation_tokens)
        if tuple(sorted(source_group_counts)) != self.derivation_source_group_tokens:
            raise AV1ValidationDerivationError(
                "AV1 derivation proposal source-group observations are incomplete"
            )
        if len(source_group_counts) < AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT:
            raise AV1ValidationDerivationError(
                "AV1 derivation proposal has insufficient source groups"
            )
        if any(
            count > AV1_VALIDATION_DERIVATION_RESERVATION_COUNT // 3
            for count in source_group_counts.values()
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation proposal source-group concentration is too high"
            )
        if len(self.derivation_source_tokens) != self.derivation_source_count:
            raise AV1ValidationDerivationError("AV1 derivation proposal source tokens are incomplete")
        if (
            self.derivation_source_count != self.derivation_evidence_count
            or len(self.derivation_title_tokens) != self.derivation_evidence_count
            or len(self.derivation_series_tokens) != self.derivation_evidence_count
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation proposal does not preserve unique source, title, and series reservations"
            )
        oldest = _parse_timestamp(self.derivation_oldest_recorded_at, "oldest observation")
        newest = _parse_timestamp(self.derivation_newest_recorded_at, "newest observation")
        proposed = _parse_timestamp(self.proposed_at, "proposal timestamp")
        if not oldest <= newest <= proposed:
            raise AV1ValidationDerivationError("AV1 derivation proposal chronology is invalid")
        if oldest < proposed - timedelta(days=self.maximum_derivation_age_days):
            raise AV1ValidationDerivationError("AV1 derivation proposal evidence is stale")
        semantic_payload = self.semantic_payload()
        if self.proposal_id != _derivation_id("proposal", semantic_payload):
            raise AV1ValidationDerivationError("AV1 derivation proposal ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({"proposal_id": self.proposal_id, **semantic_payload}):
            raise AV1ValidationDerivationError("AV1 derivation proposal digest does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_DERIVATION_PROPOSAL_SCHEMA,
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "plan_id": self.plan_id,
            "manifest_id": self.manifest_id,
            "cell_plan_id": self.cell_plan_id,
            "exact_traits": list(self.exact_traits),
            "crf_lower": self.crf_lower,
            "crf_center": self.crf_center,
            "crf_upper": self.crf_upper,
            "crf_mad": self.crf_mad,
            "bitrate_relative_mad": self.bitrate_relative_mad,
            "statistics_contract_sha256": self.statistics_contract_sha256,
            "minimum_derivation_source_count": self.minimum_derivation_source_count,
            "maximum_derivation_age_days": self.maximum_derivation_age_days,
            "maximum_candidate_crf_span": self.maximum_candidate_crf_span,
            "compatibility_signature": self.compatibility_signature,
            "policy_signature": self.policy_signature,
            "target_video_bitrate_min_bps": self.target_video_bitrate_min_bps,
            "target_video_bitrate_max_bps": self.target_video_bitrate_max_bps,
            "minimum_quality_score": self.minimum_quality_score,
            "confidence_level": self.confidence_level,
            "confidence_score": self.confidence_score,
            "derivation_evidence_count": self.derivation_evidence_count,
            "derivation_source_count": self.derivation_source_count,
            "derivation_source_tokens": list(self.derivation_source_tokens),
            "derivation_title_tokens": list(self.derivation_title_tokens),
            "derivation_series_tokens": list(self.derivation_series_tokens),
            "derivation_source_group_tokens": list(self.derivation_source_group_tokens),
            "derivation_source_group_observation_tokens": list(
                self.derivation_source_group_observation_tokens
            ),
            "derivation_oldest_recorded_at": self.derivation_oldest_recorded_at,
            "derivation_newest_recorded_at": self.derivation_newest_recorded_at,
            "derivation_conflict_count": self.derivation_conflict_count,
            "derivation_snapshot_sha256": self.derivation_snapshot_sha256,
            "selection_lock_sha256": self.selection_lock_sha256,
            "proposed_at": self.proposed_at,
            "review_state": "pending_independent_review",
            "holdout_execution_authorized": False,
            "public_bundle_activation_allowed": False,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationReviewClaim:
    claim_id: str
    plan_id: str
    authorization_id: str
    proposal_id: str
    proposal_payload_sha256: str
    repository_commit: str
    repository_tree: str
    lane: AV1ValidationDerivationReviewLane
    review_run_id: str
    reviewer_token: str
    review_runner_canonical_path_sha256: str
    review_runner_binary_sha256: str
    claimed_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.plan_id.startswith("av1vdplan1_"):
            raise AV1ValidationDerivationError("AV1 derivation review claim plan is invalid")
        if not self.authorization_id.startswith("av1vderivation1_"):
            raise AV1ValidationDerivationError(
                "AV1 derivation review claim authorization is invalid"
            )
        if not self.proposal_id.startswith("av1vdproposal1_"):
            raise AV1ValidationDerivationError(
                "AV1 derivation review claim proposal is invalid"
            )
        if self.lane not in AV1_VALIDATION_DERIVATION_REVIEW_LANES:
            raise AV1ValidationDerivationError("AV1 derivation review claim lane is invalid")
        if (
            not _AGENT_REVIEWER_RE.fullmatch(self.reviewer_token)
            or self.reviewer_token != f"agent:{self.review_run_id}"
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review claim must identify one Every Code agent run"
            )
        for digest, label in (
            (self.proposal_payload_sha256, "proposal digest"),
            (
                self.review_runner_canonical_path_sha256,
                "review-runner canonical-path digest",
            ),
            (self.review_runner_binary_sha256, "review-runner binary digest"),
            (self.payload_sha256, "review-claim digest"),
        ):
            _require_sha256(digest, label)
        _require_git_object_id(
            self.repository_commit,
            "review repository commit",
        )
        _require_git_object_id(
            self.repository_tree,
            "review repository tree",
        )
        _parse_timestamp(self.claimed_at, "review-claim timestamp")
        semantic_payload = self.semantic_payload()
        if self.claim_id != _derivation_id("review_claim", semantic_payload):
            raise AV1ValidationDerivationError(
                "AV1 derivation review claim ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256({
            "claim_id": self.claim_id,
            **semantic_payload,
        }):
            raise AV1ValidationDerivationError(
                "AV1 derivation review claim digest does not match its payload"
            )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_DERIVATION_REVIEW_CLAIM_SCHEMA,
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "plan_id": self.plan_id,
            "authorization_id": self.authorization_id,
            "proposal_id": self.proposal_id,
            "proposal_payload_sha256": self.proposal_payload_sha256,
            "repository_commit": self.repository_commit,
            "repository_tree": self.repository_tree,
            "lane": self.lane,
            "review_run_id": self.review_run_id,
            "reviewer_token": self.reviewer_token,
            "review_runner_canonical_path_sha256": (
                self.review_runner_canonical_path_sha256
            ),
            "review_runner_binary_sha256": self.review_runner_binary_sha256,
            "claimed_at": self.claimed_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationReviewAttestation:
    attestation_id: str
    proposal_id: str
    proposal_payload_sha256: str
    review_claim_id: str
    review_claim_payload_sha256: str
    lane: AV1ValidationDerivationReviewLane
    reviewer_token: str
    review_runner_canonical_path_sha256: str
    review_runner_binary_sha256: str
    review_evidence_sha256: str
    decision: AV1ValidationDerivationReviewDecision
    reviewed_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if self.lane not in AV1_VALIDATION_DERIVATION_REVIEW_LANES:
            raise AV1ValidationDerivationError("AV1 derivation review lane is invalid")
        if not _AGENT_REVIEWER_RE.fullmatch(self.reviewer_token):
            raise AV1ValidationDerivationError(
                "AV1 derivation reviewer token must identify one Every Code agent run"
            )
        if not self.review_claim_id.startswith("av1vdreviewclaim1_"):
            raise AV1ValidationDerivationError(
                "AV1 derivation review claim reference is invalid"
            )
        for digest, label in (
            (self.review_claim_payload_sha256, "review-claim digest"),
            (
                self.review_runner_canonical_path_sha256,
                "review-runner canonical-path digest",
            ),
            (self.review_runner_binary_sha256, "review-runner binary digest"),
            (self.review_evidence_sha256, "review evidence digest"),
        ):
            _require_sha256(digest, label)
        if self.decision not in {"approved", "rejected"}:
            raise AV1ValidationDerivationError("AV1 derivation review decision is invalid")
        _require_sha256(self.proposal_payload_sha256, "proposal digest")
        _require_sha256(self.payload_sha256, "review digest")
        _parse_timestamp(self.reviewed_at, "review timestamp")
        semantic_payload = self.semantic_payload()
        if self.attestation_id != _derivation_id("review", semantic_payload):
            raise AV1ValidationDerivationError("AV1 derivation review ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({"attestation_id": self.attestation_id, **semantic_payload}):
            raise AV1ValidationDerivationError("AV1 derivation review digest does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_DERIVATION_REVIEW_SCHEMA,
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "proposal_id": self.proposal_id,
            "proposal_payload_sha256": self.proposal_payload_sha256,
            "review_claim_id": self.review_claim_id,
            "review_claim_payload_sha256": self.review_claim_payload_sha256,
            "lane": self.lane,
            "reviewer_token": self.reviewer_token,
            "review_runner_canonical_path_sha256": (
                self.review_runner_canonical_path_sha256
            ),
            "review_runner_binary_sha256": self.review_runner_binary_sha256,
            "review_evidence_sha256": self.review_evidence_sha256,
            "decision": self.decision,
            "reviewed_at": self.reviewed_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationReviewEnvelope:
    envelope_id: str
    review: AV1ValidationDerivationReviewAttestation
    review_run_payload_json: str
    payload_sha256: str

    def __post_init__(self) -> None:
        try:
            evidence_payload = object_dict(json.loads(self.review_run_payload_json))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review envelope evidence is invalid"
            ) from exc
        evidence = canonical_json_bytes(evidence_payload)
        if evidence.decode("utf-8") != self.review_run_payload_json:
            raise AV1ValidationDerivationError(
                "AV1 derivation review envelope evidence is not canonical"
            )
        validate_av1_validation_derivation_review_run_evidence(
            evidence,
            review=self.review,
        )
        semantic_payload = self.semantic_payload()
        if self.envelope_id != _derivation_id("review_envelope", semantic_payload):
            raise AV1ValidationDerivationError(
                "AV1 derivation review envelope ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256({
            "envelope_id": self.envelope_id,
            **semantic_payload,
        }):
            raise AV1ValidationDerivationError(
                "AV1 derivation review envelope digest does not match its payload"
            )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_DERIVATION_REVIEW_ENVELOPE_SCHEMA,
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "review": self.review.to_payload(),
            "review_run_evidence": object_dict(json.loads(self.review_run_payload_json)),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationCandidateLockEnvelope:
    envelope_id: str
    plan_id: str
    plan_payload_sha256: str
    authorization_id: str
    authorization_payload_sha256: str
    proposal_id: str
    proposal_payload_sha256: str
    review_set_sha256: str
    artifact_root_binding_sha256: str
    candidate_lock: AV1ColdStartValidationCandidateLockV1
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.plan_id.startswith("av1vdplan1_"):
            raise AV1ValidationDerivationError(
                "AV1 derivation candidate-lock envelope plan is invalid"
            )
        if not self.authorization_id.startswith("av1vderivation1_"):
            raise AV1ValidationDerivationError(
                "AV1 derivation candidate-lock envelope authorization is invalid"
            )
        if not self.proposal_id.startswith("av1vdproposal1_"):
            raise AV1ValidationDerivationError(
                "AV1 derivation candidate-lock envelope proposal is invalid"
            )
        for digest in (
            self.plan_payload_sha256,
            self.authorization_payload_sha256,
            self.proposal_payload_sha256,
            self.review_set_sha256,
            self.artifact_root_binding_sha256,
            self.payload_sha256,
        ):
            _require_sha256(digest, "candidate-lock envelope digest")
        semantic_payload = self.semantic_payload()
        if self.envelope_id != _derivation_id("lock_envelope", semantic_payload):
            raise AV1ValidationDerivationError(
                "AV1 derivation candidate-lock envelope ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256({
            "envelope_id": self.envelope_id,
            **semantic_payload,
        }):
            raise AV1ValidationDerivationError(
                "AV1 derivation candidate-lock envelope digest does not match its payload"
            )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_DERIVATION_LOCK_ENVELOPE_SCHEMA,
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "plan_id": self.plan_id,
            "plan_payload_sha256": self.plan_payload_sha256,
            "authorization_id": self.authorization_id,
            "authorization_payload_sha256": self.authorization_payload_sha256,
            "proposal_id": self.proposal_id,
            "proposal_payload_sha256": self.proposal_payload_sha256,
            "review_set_sha256": self.review_set_sha256,
            "artifact_root_binding_sha256": self.artifact_root_binding_sha256,
            "candidate_lock": self.candidate_lock.to_payload(),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationDerivationCandidateEvaluation:
    cell_plan_id: str
    derivation_snapshot_sha256: str
    derivation_conflict_count: int
    blockers: tuple[str, ...]
    proposal: AV1ValidationDerivationCandidateProposal | None


def build_av1_validation_derivation_plan(
        *,
        manifest: AV1ValidationManifestV2,
        partition: AV1ValidationPrivatePartition,
        authorization: AV1ValidationV2DerivationAuthorization,
        runtime_context_sha256: str,
        execution_environment_sha256: str,
        statistics_contract_sha256: str,
        review_runner_canonical_path_sha256: str,
        review_runner_binary_sha256: str,
        repository_commit: str,
        repository_tree: str,
        source_commitments: Sequence[AV1ValidationDerivationSourceCommitment],
) -> AV1ValidationDerivationPlan:
    assert_preregistered_av1_validation_manifest_v2(manifest)
    if (
        manifest.criteria.minimum_derivation_evidence_count
        != AV1_COLD_START_VALIDATION_REQUIRED_DERIVATION_EVIDENCE_COUNT
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation preregistration must require exactly twelve observations"
        )
    if partition.manifest_id != manifest.manifest_id or partition.manifest_payload_sha256 != manifest.payload_sha256:
        raise AV1ValidationDerivationError("AV1 derivation partition does not match the manifest")
    if authorization.manifest_id != manifest.manifest_id or authorization.manifest_payload_sha256 != manifest.payload_sha256:
        raise AV1ValidationDerivationError("AV1 derivation authorization does not match the manifest")
    if authorization.selection_lock_sha256 != partition.selection_lock_sha256:
        raise AV1ValidationDerivationError("AV1 derivation authorization selection lock does not match")
    if authorization.derivation_partition_sha256 != partition.derivation_partition_sha256:
        raise AV1ValidationDerivationError("AV1 derivation authorization partition does not match")
    if (
        statistics_contract_sha256
        != av1_validation_derivation_statistics_contract_sha256(manifest)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation plan statistics contract does not match"
        )
    selected_at = _parse_timestamp(partition.selected_at, "partition selection")
    authorized_at = _parse_timestamp(authorization.authorized_at, "derivation authorization")
    valid_until = _parse_timestamp(authorization.valid_until, "derivation authorization expiration")
    manifest_valid_until = _parse_timestamp(manifest.valid_until, "manifest expiration")
    if authorized_at < selected_at or valid_until > manifest_valid_until:
        raise AV1ValidationDerivationError("AV1 derivation authorization chronology is invalid")
    publication_plan_ids = {
        plan.cell_plan_id
        for plan in manifest.cell_plans
        if plan.mode == "publication_candidate"
    }
    assignments = tuple(sorted(
        (
            assignment
            for assignment in partition.assignments
            if assignment.role == "derivation"
        ),
        key=_assignment_sort_key,
    ))
    if {assignment.cell_plan_id for assignment in assignments} != publication_plan_ids:
        raise AV1ValidationDerivationError("AV1 derivation worklist does not cover the candidate plans")
    if any(assignment.minimum_quality_score <= 0 for assignment in assignments):
        raise AV1ValidationDerivationError(
            "AV1 derivation worklist requires a positive quality floor"
        )
    canonical_source_commitments = tuple(sorted(
        source_commitments,
        key=_source_commitment_sort_key,
    ))
    source_commitment_sha256 = (
        av1_validation_derivation_source_commitment_sha256(
            canonical_source_commitments
        )
    )
    _validate_plan_source_commitments(
        assignments=assignments,
        commitments=canonical_source_commitments,
        source_commitment_sha256=source_commitment_sha256,
    )
    source_identity_by_item_id = {
        source.local_item_id: source.source_identity
        for source in partition.inventory_sources
    }
    if any(
        source_identity_by_item_id.get(commitment.local_item_id)
        != commitment.source_identity
        for commitment in canonical_source_commitments
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation source commitment does not match its partition source"
        )
    semantic_payload = _plan_semantic_payload(
        manifest=manifest,
        partition=partition,
        authorization=authorization,
        runtime_context_sha256=runtime_context_sha256,
        execution_environment_sha256=execution_environment_sha256,
        statistics_contract_sha256=statistics_contract_sha256,
        review_runner_canonical_path_sha256=(
            review_runner_canonical_path_sha256
        ),
        review_runner_binary_sha256=review_runner_binary_sha256,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        assignments=assignments,
        source_commitments=canonical_source_commitments,
        source_commitment_sha256=source_commitment_sha256,
    )
    plan_id = _derivation_id("plan", semantic_payload)
    return AV1ValidationDerivationPlan(
        plan_id=plan_id,
        manifest_id=manifest.manifest_id,
        manifest_payload_sha256=manifest.payload_sha256,
        partition_id=partition.partition_id,
        partition_payload_sha256=partition.payload_sha256,
        selection_lock_sha256=partition.selection_lock_sha256,
        derivation_partition_sha256=partition.derivation_partition_sha256,
        runtime_context_sha256=runtime_context_sha256,
        execution_environment_sha256=execution_environment_sha256,
        statistics_contract_sha256=statistics_contract_sha256,
        review_runner_canonical_path_sha256=(
            review_runner_canonical_path_sha256
        ),
        review_runner_binary_sha256=review_runner_binary_sha256,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        authorization=authorization,
        assignments=assignments,
        source_commitments=canonical_source_commitments,
        source_commitment_sha256=source_commitment_sha256,
        payload_sha256=_payload_sha256({"plan_id": plan_id, **semantic_payload}),
    )


def build_av1_validation_derivation_attempt(
        *,
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
        assignment_id: str,
        started_at: str,
        completed_at: str,
        status: AV1ValidationDerivationAttemptStatus,
        reason_code: str | None = None,
        calibration_payload: Mapping[str, Any] | None = None,
) -> AV1ValidationDerivationAttempt:
    _validate_plan_partition(plan, partition)
    assignment = _assignment_by_id(plan, assignment_id)
    source_identity = _partition_source_identity(partition, assignment.local_item_id)
    started = _parse_timestamp(started_at, "attempt start")
    completed = _parse_timestamp(completed_at, "attempt completion")
    authorized_at = _parse_timestamp(plan.authorization.authorized_at, "derivation authorization")
    valid_until = _parse_timestamp(plan.authorization.valid_until, "derivation authorization expiration")
    if started < authorized_at or started >= valid_until or completed < started:
        raise AV1ValidationDerivationError("AV1 derivation attempt is outside its authorization window")
    if completed >= valid_until and not (
        status == "failed" and reason_code == "authorization_expired"
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation attempt crossed its authorization expiration"
        )
    calibration = object_dict(calibration_payload)
    calibration_json = None
    calibration_sha256 = None
    if status == "review_pending":
        source_commitment = av1_validation_derivation_plan_source_commitment(
            plan,
            assignment.assignment_id,
        )
        _validate_calibration_payload(
            assignment,
            calibration,
            source_identity=source_identity,
            expected_source_sha256=source_commitment.source_sha256,
            expected_source_size_bytes=source_commitment.source_size_bytes,
        )
        calibration_json = canonical_json_bytes(calibration).decode("utf-8")
        calibration_sha256 = _payload_sha256(calibration)
    semantic_payload = _attempt_semantic_payload(
        plan=plan,
        assignment=assignment,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        reason_code=reason_code,
        calibration_payload=calibration if calibration_json is not None else None,
        calibration_payload_sha256=calibration_sha256,
    )
    attempt_id = _derivation_id("attempt", semantic_payload)
    return AV1ValidationDerivationAttempt(
        attempt_id=attempt_id,
        plan_id=plan.plan_id,
        authorization_id=plan.authorization.authorization_id,
        assignment_id=assignment.assignment_id,
        cell_plan_id=assignment.cell_plan_id,
        ordinal=assignment.ordinal,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        reason_code=reason_code,
        calibration_payload_json=calibration_json,
        calibration_payload_sha256=calibration_sha256,
        payload_sha256=_payload_sha256({"attempt_id": attempt_id, **semantic_payload}),
    )


def assert_av1_validation_derivation_repository_identity(
        plan: AV1ValidationDerivationPlan,
        *,
        repository_commit: str,
        repository_tree: str,
) -> None:
    _require_git_object_id(repository_commit, "current repository commit")
    _require_git_object_id(repository_tree, "current repository tree")
    if (
        repository_commit != plan.repository_commit
        or repository_tree != plan.repository_tree
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation repository snapshot drifted from the immutable plan"
        )


def build_av1_validation_derivation_terminal_record(
        *,
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
        attempt: AV1ValidationDerivationAttempt,
        observation: ContentIntentBoundaryObservation | None = None,
        observation_exclusion_reason: str | None = None,
        review_failure_reason_code: str | None = None,
) -> AV1ValidationDerivationTerminalRecord:
    validate_av1_validation_derivation_attempt_binding(
        plan=plan,
        partition=partition,
        attempt=attempt,
    )
    assignment = _assignment_by_id(plan, attempt.assignment_id)
    projection = None
    status: AV1ValidationDerivationTerminalStatus
    reason_code = attempt.reason_code
    if attempt.status == "review_pending":
        if observation is not None and observation_exclusion_reason is None:
            status = "observed"
            reason_code = None
            projection = _observation_projection(
                plan=plan,
                partition=partition,
                assignment=assignment,
                attempt=attempt,
                observation=observation,
            )
        elif observation is None and observation_exclusion_reason == "content_intent_observation_excluded":
            status = "excluded"
            reason_code = observation_exclusion_reason
        elif (
                observation is None
                and observation_exclusion_reason is None
                and review_failure_reason_code == "safety_stop"
        ):
            status = "stopped"
            reason_code = review_failure_reason_code
        else:
            raise AV1ValidationDerivationError(
                "AV1 review-pending attempt requires one observation outcome"
            )
    else:
        if observation is not None:
            raise AV1ValidationDerivationError("AV1 non-observed attempt cannot carry an observation")
        status = cast(AV1ValidationDerivationTerminalStatus, attempt.status)
    semantic_payload = _terminal_semantic_payload(
        plan=plan,
        assignment=assignment,
        attempt=attempt,
        status=status,
        reason_code=reason_code,
        observation=projection,
    )
    record_id = _derivation_id("terminal", semantic_payload)
    return AV1ValidationDerivationTerminalRecord(
        record_id=record_id,
        plan_id=plan.plan_id,
        authorization_id=plan.authorization.authorization_id,
        attempt_id=attempt.attempt_id,
        attempt_payload_sha256=attempt.payload_sha256,
        assignment_id=assignment.assignment_id,
        cell_plan_id=assignment.cell_plan_id,
        ordinal=assignment.ordinal,
        started_at=attempt.started_at,
        completed_at=attempt.completed_at,
        status=status,
        reason_code=reason_code,
        observation=projection,
        payload_sha256=_payload_sha256({"record_id": record_id, **semantic_payload}),
    )


def validate_av1_validation_derivation_attempt_binding(
        *,
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
        attempt: AV1ValidationDerivationAttempt,
) -> None:
    _validate_plan_partition(plan, partition)
    assignment = _assignment_by_id(plan, attempt.assignment_id)
    if (
        attempt.plan_id != plan.plan_id
        or attempt.authorization_id != plan.authorization.authorization_id
        or attempt.cell_plan_id != assignment.cell_plan_id
        or attempt.ordinal != assignment.ordinal
    ):
        raise AV1ValidationDerivationError("AV1 derivation attempt is bound to another work item")
    if attempt.status == "review_pending":
        source_commitment = av1_validation_derivation_plan_source_commitment(
            plan,
            assignment.assignment_id,
        )
        _validate_calibration_payload(
            assignment,
            attempt.calibration_payload(),
            source_identity=_partition_source_identity(
                partition,
                assignment.local_item_id,
            ),
            expected_source_sha256=source_commitment.source_sha256,
            expected_source_size_bytes=source_commitment.source_size_bytes,
        )


def validate_av1_validation_derivation_plan_binding(
        *,
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
) -> None:
    _validate_plan_partition(plan, partition)


def assert_av1_validation_derivation_authorization_active(
        plan: AV1ValidationDerivationPlan,
        *,
        at: str,
) -> None:
    timestamp = _parse_timestamp(at, "derivation execution timestamp")
    if not (
        _parse_timestamp(
            plan.authorization.authorized_at,
            "derivation authorization",
        )
        <= timestamp
        < _parse_timestamp(
            plan.authorization.valid_until,
            "derivation authorization expiration",
        )
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation assignment cannot start outside its authorization window"
        )


def evaluate_av1_validation_derivation_candidate(
        *,
        manifest: AV1ValidationManifestV2,
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
        cell_plan_id: str,
        attempts: Sequence[AV1ValidationDerivationAttempt],
        records: Sequence[AV1ValidationDerivationTerminalRecord],
        current_observations: Mapping[str, ContentIntentBoundaryObservation],
        proposed_at: str,
) -> AV1ValidationDerivationCandidateEvaluation:
    assert_preregistered_av1_validation_manifest_v2(manifest)
    _validate_plan_partition(plan, partition)
    if manifest.manifest_id != plan.manifest_id or manifest.payload_sha256 != plan.manifest_payload_sha256:
        raise AV1ValidationDerivationError("AV1 derivation proposal manifest drifted")
    if (
        plan.statistics_contract_sha256
        != av1_validation_derivation_statistics_contract_sha256(manifest)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation statistics contract drifted after authorization"
        )
    proposed = _parse_timestamp(proposed_at, "proposal timestamp")
    authorized = _parse_timestamp(
        plan.authorization.authorized_at,
        "derivation authorization",
    )
    if (
        proposed < authorized
        or proposed >= _parse_timestamp(
            plan.authorization.valid_until,
            "authorization expiration",
        )
    ):
        raise AV1ValidationDerivationError("AV1 derivation proposal is outside its authorization window")
    plan_by_id = {item.assignment_id: item for item in plan.assignments}
    expected = tuple(item for item in plan.assignments if item.cell_plan_id == cell_plan_id)
    if len(expected) != AV1_VALIDATION_DERIVATION_RESERVATION_COUNT:
        raise AV1ValidationDerivationError("AV1 derivation candidate plan is not reserved")
    attempts_by_id: dict[str, AV1ValidationDerivationAttempt] = {}
    for attempt in attempts:
        if attempt.plan_id != plan.plan_id or attempt.authorization_id != plan.authorization.authorization_id:
            raise AV1ValidationDerivationError("AV1 derivation attempt is bound to another plan")
        assignment = plan_by_id.get(attempt.assignment_id)
        if assignment is None or attempt.cell_plan_id != assignment.cell_plan_id or attempt.ordinal != assignment.ordinal:
            raise AV1ValidationDerivationError("AV1 derivation attempt slot drifted")
        if attempt.assignment_id in attempts_by_id:
            raise AV1ValidationDerivationError("AV1 derivation assignment has multiple attempts")
        attempts_by_id[attempt.assignment_id] = attempt
    records_by_id: dict[str, AV1ValidationDerivationTerminalRecord] = {}
    blockers: list[str] = []
    for record in records:
        if record.plan_id != plan.plan_id or record.authorization_id != plan.authorization.authorization_id:
            raise AV1ValidationDerivationError("AV1 derivation terminal record is bound to another plan")
        assignment = plan_by_id.get(record.assignment_id)
        if assignment is None or record.cell_plan_id != assignment.cell_plan_id or record.ordinal != assignment.ordinal:
            raise AV1ValidationDerivationError("AV1 derivation terminal record slot drifted")
        if record.assignment_id in records_by_id:
            raise AV1ValidationDerivationError("AV1 derivation assignment has multiple terminal records")
        attempt = attempts_by_id.get(record.assignment_id)
        if (
            attempt is None
            or record.attempt_id != attempt.attempt_id
            or record.attempt_payload_sha256 != attempt.payload_sha256
        ):
            raise AV1ValidationDerivationError("AV1 derivation terminal record does not match its attempt")
        records_by_id[record.assignment_id] = record
    snapshot_payload = [
        {
            "assignment_id": assignment.assignment_id,
            "attempt_sha256": (
                attempts_by_id[assignment.assignment_id].payload_sha256
                if assignment.assignment_id in attempts_by_id
                else None
            ),
            "terminal_record_sha256": (
                records_by_id[assignment.assignment_id].payload_sha256
                if assignment.assignment_id in records_by_id
                else None
            ),
        }
        for assignment in expected
    ]
    snapshot_sha256 = _payload_sha256({
        "plan_id": plan.plan_id,
        "cell_plan_id": cell_plan_id,
        "terminal_records": snapshot_payload,
    })
    observations: list[AV1ValidationDerivationObservationProjection] = []
    conflicting_assignment_ids: set[str] = set()
    for assignment in expected:
        if assignment.assignment_id not in attempts_by_id:
            blockers.append("missing_attempt")
            continue
        record = records_by_id.get(assignment.assignment_id)
        if record is None:
            blockers.append("missing_terminal_record")
            continue
        if record.status != "observed" or record.observation is None:
            blockers.append(f"terminal_{record.status}")
            continue
        current_observation = current_observations.get(assignment.assignment_id)
        if current_observation is None:
            blockers.append("observation_not_current")
            continue
        try:
            current_record = build_av1_validation_derivation_terminal_record(
                plan=plan,
                partition=partition,
                attempt=attempts_by_id[assignment.assignment_id],
                observation=current_observation,
            )
        except AV1ValidationDerivationError:
            blockers.append("observation_current_contract_invalid")
            conflicting_assignment_ids.add(assignment.source_token)
            continue
        if current_record != record:
            blockers.append("terminal_projection_mismatch")
            conflicting_assignment_ids.add(assignment.source_token)
            continue
        observation = current_record.observation
        if observation is None:
            blockers.append("observation_current_contract_invalid")
            conflicting_assignment_ids.add(assignment.source_token)
            continue
        if observation.verdict != "acceptable":
            blockers.append("observation_unacceptable")
            conflicting_assignment_ids.add(assignment.source_token)
        if not observation.quality_floor_met:
            blockers.append("quality_floor_miss")
            conflicting_assignment_ids.add(assignment.source_token)
        observation_timestamp = _parse_timestamp(
            observation.recorded_at,
            "observation timestamp",
        )
        if observation_timestamp > proposed:
            blockers.append("observation_after_proposal")
        if observation_timestamp < proposed - timedelta(
            days=manifest.criteria.maximum_derivation_age_days
        ):
            blockers.append("observation_stale")
        observations.append(observation)
    if len(observations) != AV1_VALIDATION_DERIVATION_RESERVATION_COUNT:
        blockers.append("insufficient_eligible_observations")
    source_tokens = tuple(sorted({item.source_token for item in observations}))
    title_tokens = tuple(sorted({item.title_token for item in observations}))
    series_tokens = tuple(sorted({item.series_token for item in observations}))
    group_tokens = tuple(sorted({item.source_group_token for item in observations}))
    group_observation_tokens = tuple(sorted(item.source_group_token for item in observations))
    if len(source_tokens) < manifest.criteria.minimum_derivation_source_count:
        blockers.append("insufficient_independent_sources")
    if len(source_tokens) != AV1_VALIDATION_DERIVATION_RESERVATION_COUNT:
        blockers.append("derivation_source_reuse")
    if len(title_tokens) != AV1_VALIDATION_DERIVATION_RESERVATION_COUNT:
        blockers.append("derivation_title_reuse")
    if len(series_tokens) != AV1_VALIDATION_DERIVATION_RESERVATION_COUNT:
        blockers.append("derivation_series_reuse")
    group_counts = Counter(item.source_group_token for item in observations)
    if len(group_tokens) < AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT:
        blockers.append("insufficient_source_groups")
    if any(count > AV1_VALIDATION_DERIVATION_RESERVATION_COUNT // 3 for count in group_counts.values()):
        blockers.append("source_group_concentration")
    for attribute, blocker in (
        ("traits", "trait_drift"),
        ("intent_level", "intent_drift"),
        ("compatibility_signature", "compatibility_drift"),
        ("policy_signature", "policy_drift"),
        ("observation_compatibility_key", "observation_compatibility_conflict"),
        ("observation_policy_hash", "observation_policy_conflict"),
        ("quality_metric", "quality_contract_drift"),
        ("quality_target", "quality_contract_drift"),
        ("minimum_quality_score", "quality_contract_drift"),
    ):
        values = {getattr(item, attribute) for item in observations}
        if len(values) > 1:
            blockers.append(blocker)
            expected_value = getattr(observations[0], attribute)
            conflicting_assignment_ids.update(
                item.source_token
                for item in observations
                if getattr(item, attribute) != expected_value
            )
    derivation_conflict_count = len(conflicting_assignment_ids)
    if blockers:
        return AV1ValidationDerivationCandidateEvaluation(
            cell_plan_id=cell_plan_id,
            derivation_snapshot_sha256=snapshot_sha256,
            derivation_conflict_count=derivation_conflict_count,
            blockers=tuple(sorted(set(blockers))),
            proposal=None,
        )
    crfs = sorted(item.chosen_crf for item in observations)
    crf_center = round(float(median(crfs)), 3)
    crf_mad = round(float(median(abs(value - crf_center) for value in crfs)), 3)
    crf_lower = round(crfs[0], 3)
    crf_upper = round(crfs[-1], 3)
    if crf_mad > AV1_VALIDATION_DERIVATION_MAXIMUM_CRF_MAD:
        blockers.append("crf_dispersion_too_wide")
    if crf_upper - crf_lower > manifest.criteria.maximum_candidate_crf_span:
        blockers.append("crf_span_too_wide")
    if math.ceil(crf_lower) > math.floor(crf_upper):
        blockers.append("crf_range_has_no_executable_integer")
    bitrates = sorted(item.boundary_bitrate_bps for item in observations)
    bitrate_center = round(median(bitrates))
    bitrate_mad = round(median(abs(value - bitrate_center) for value in bitrates))
    relative_mad = bitrate_mad / bitrate_center if bitrate_center > 0 else math.inf
    confidence_level = (
        "high"
        if relative_mad <= AV1_VALIDATION_DERIVATION_HIGH_CONFIDENCE_RELATIVE_MAD
        else "moderate"
        if relative_mad <= AV1_VALIDATION_DERIVATION_MODERATE_CONFIDENCE_RELATIVE_MAD
        else "limited"
    )
    confidence_score = round(max(0.0, min(1.0, 1 - relative_mad)), 3)
    required_confidence_levels = (
        {"high"}
        if manifest.criteria.confidence_level == "high"
        else {"moderate", "high"}
    )
    if (
        confidence_level not in required_confidence_levels
        or confidence_score < manifest.criteria.confidence_score
    ):
        blockers.append("confidence_insufficient")
    if blockers:
        return AV1ValidationDerivationCandidateEvaluation(
            cell_plan_id=cell_plan_id,
            derivation_snapshot_sha256=snapshot_sha256,
            derivation_conflict_count=derivation_conflict_count,
            blockers=tuple(sorted(set(blockers))),
            proposal=None,
        )
    target_bitrates = sorted(
        assignment.target_video_bitrate_bps
        for assignment in expected
    )
    proposed_payload = _proposal_semantic_payload(
        plan=plan,
        cell_plan_id=cell_plan_id,
        observations=observations,
        crf_lower=crf_lower,
        crf_center=crf_center,
        crf_upper=crf_upper,
        crf_mad=crf_mad,
        bitrate_relative_mad=relative_mad,
        statistics_contract_sha256=plan.statistics_contract_sha256,
        minimum_derivation_source_count=(
            manifest.criteria.minimum_derivation_source_count
        ),
        maximum_derivation_age_days=manifest.criteria.maximum_derivation_age_days,
        maximum_candidate_crf_span=manifest.criteria.maximum_candidate_crf_span,
        target_video_bitrate_min_bps=target_bitrates[0],
        target_video_bitrate_max_bps=target_bitrates[-1],
        confidence_level=cast(Literal["moderate", "high"], confidence_level),
        confidence_score=confidence_score,
        derivation_conflict_count=derivation_conflict_count,
        snapshot_sha256=snapshot_sha256,
        proposed_at=proposed_at,
    )
    proposal_id = _derivation_id("proposal", proposed_payload)
    proposal = AV1ValidationDerivationCandidateProposal(
        proposal_id=proposal_id,
        plan_id=plan.plan_id,
        manifest_id=plan.manifest_id,
        cell_plan_id=cell_plan_id,
        exact_traits=observations[0].traits,
        crf_lower=crf_lower,
        crf_center=crf_center,
        crf_upper=crf_upper,
        crf_mad=crf_mad,
        bitrate_relative_mad=relative_mad,
        statistics_contract_sha256=plan.statistics_contract_sha256,
        minimum_derivation_source_count=(
            manifest.criteria.minimum_derivation_source_count
        ),
        maximum_derivation_age_days=manifest.criteria.maximum_derivation_age_days,
        maximum_candidate_crf_span=manifest.criteria.maximum_candidate_crf_span,
        compatibility_signature=observations[0].compatibility_signature,
        policy_signature=observations[0].policy_signature,
        target_video_bitrate_min_bps=target_bitrates[0],
        target_video_bitrate_max_bps=target_bitrates[-1],
        minimum_quality_score=observations[0].minimum_quality_score,
        confidence_level=cast(Literal["moderate", "high"], confidence_level),
        confidence_score=confidence_score,
        derivation_evidence_count=len(observations),
        derivation_source_count=len(source_tokens),
        derivation_source_tokens=source_tokens,
        derivation_title_tokens=title_tokens,
        derivation_series_tokens=series_tokens,
        derivation_source_group_tokens=group_tokens,
        derivation_source_group_observation_tokens=group_observation_tokens,
        derivation_oldest_recorded_at=min(item.recorded_at for item in observations),
        derivation_newest_recorded_at=max(item.recorded_at for item in observations),
        derivation_conflict_count=derivation_conflict_count,
        derivation_snapshot_sha256=snapshot_sha256,
        selection_lock_sha256=plan.selection_lock_sha256,
        proposed_at=proposed_at,
        payload_sha256=_payload_sha256({"proposal_id": proposal_id, **proposed_payload}),
    )
    return AV1ValidationDerivationCandidateEvaluation(
        cell_plan_id=cell_plan_id,
        derivation_snapshot_sha256=snapshot_sha256,
        derivation_conflict_count=derivation_conflict_count,
        blockers=(),
        proposal=proposal,
    )


def build_av1_validation_derivation_review_claim(
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        repository_commit: str,
        repository_tree: str,
        lane: AV1ValidationDerivationReviewLane,
        review_run_id: str,
        review_runner_canonical_path_sha256: str,
        review_runner_binary_sha256: str,
        claimed_at: str,
) -> AV1ValidationDerivationReviewClaim:
    claimed = _parse_timestamp(claimed_at, "review-claim timestamp")
    if (
        proposal.plan_id != plan.plan_id
        or proposal.manifest_id != plan.manifest_id
        or proposal.selection_lock_sha256 != plan.selection_lock_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review claim is bound to another plan"
        )
    if (
        review_runner_canonical_path_sha256
        != plan.review_runner_canonical_path_sha256
        or review_runner_binary_sha256
        != plan.review_runner_binary_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review runner does not match the authorization"
        )
    assert_av1_validation_derivation_repository_identity(
        plan,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
    )
    if not (
        _parse_timestamp(proposal.proposed_at, "proposal timestamp")
        <= claimed
        < _parse_timestamp(
            plan.authorization.valid_until,
            "derivation authorization expiration",
        )
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review claim is outside its authorization window"
        )
    reviewer_token = f"agent:{review_run_id}"
    semantic_payload = {
        "schema": AV1_VALIDATION_DERIVATION_REVIEW_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "plan_id": plan.plan_id,
        "authorization_id": plan.authorization.authorization_id,
        "proposal_id": proposal.proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "repository_commit": repository_commit,
        "repository_tree": repository_tree,
        "lane": lane,
        "review_run_id": review_run_id,
        "reviewer_token": reviewer_token,
        "review_runner_canonical_path_sha256": (
            review_runner_canonical_path_sha256
        ),
        "review_runner_binary_sha256": review_runner_binary_sha256,
        "claimed_at": _utc_timestamp(claimed),
    }
    claim_id = _derivation_id("review_claim", semantic_payload)
    return AV1ValidationDerivationReviewClaim(
        claim_id=claim_id,
        plan_id=plan.plan_id,
        authorization_id=plan.authorization.authorization_id,
        proposal_id=proposal.proposal_id,
        proposal_payload_sha256=proposal.payload_sha256,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        lane=lane,
        review_run_id=review_run_id,
        reviewer_token=reviewer_token,
        review_runner_canonical_path_sha256=(
            review_runner_canonical_path_sha256
        ),
        review_runner_binary_sha256=review_runner_binary_sha256,
        claimed_at=_utc_timestamp(claimed),
        payload_sha256=_payload_sha256({
            "claim_id": claim_id,
            **semantic_payload,
        }),
    )


def build_av1_validation_derivation_review_prompt(
        *,
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
) -> str:
    if (
        claim.plan_id != proposal.plan_id
        or claim.proposal_id != proposal.proposal_id
        or claim.proposal_payload_sha256 != proposal.payload_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review prompt inputs are not bound"
        )
    marker = {
        "decision": "approved|rejected",
        "lane": claim.lane,
        "proposal_id": proposal.proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "repository_commit": claim.repository_commit,
        "repository_tree": claim.repository_tree,
        "review_claim_id": claim.claim_id,
        "review_claim_payload_sha256": claim.payload_sha256,
        "review_run_id": claim.review_run_id,
    }
    proposal_json = canonical_json_bytes(proposal.to_payload()).decode("utf-8")
    marker_json = canonical_json_bytes(marker).decode("utf-8")
    return (
        "Perform one independent, read-only AV1 derivation candidate review. "
        "Do not modify files, invoke another agent, reveal opaque tokens, or infer private media identity. "
        f"Review lane: {claim.lane}. Review the repository implementation and this canonical proposal payload:\n"
        f"Review only repository commit {claim.repository_commit} with tree {claim.repository_tree}; ignore uncommitted worktree state.\n"
        f"Immutable review claim: {claim.claim_id} ({claim.payload_sha256}).\n"
        f"{proposal_json}\n"
        "Reject on any actionable gate failure; otherwise approve. Explain findings concisely. "
        "End with exactly one final marker line using valid JSON and replace the decision placeholder:\n"
        f"{AV1_VALIDATION_DERIVATION_AGENT_REVIEW_MARKER}{marker_json}"
    )


def build_av1_validation_derivation_review_attestation(
        *,
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
        review_evidence_sha256: str,
        decision: AV1ValidationDerivationReviewDecision,
        reviewed_at: str,
) -> AV1ValidationDerivationReviewAttestation:
    reviewed = _parse_timestamp(reviewed_at, "review timestamp")
    if (
        claim.proposal_id != proposal.proposal_id
        or claim.proposal_payload_sha256 != proposal.payload_sha256
        or claim.plan_id != proposal.plan_id
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review claim is bound to another proposal"
        )
    if reviewed < _parse_timestamp(claim.claimed_at, "review-claim timestamp"):
        raise AV1ValidationDerivationError(
            "AV1 derivation review predates its immutable claim"
        )
    normalized_reviewed_at = _utc_timestamp(reviewed)
    semantic_payload = {
        "schema": AV1_VALIDATION_DERIVATION_REVIEW_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "proposal_id": proposal.proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "review_claim_id": claim.claim_id,
        "review_claim_payload_sha256": claim.payload_sha256,
        "lane": claim.lane,
        "reviewer_token": claim.reviewer_token,
        "review_runner_canonical_path_sha256": (
            claim.review_runner_canonical_path_sha256
        ),
        "review_runner_binary_sha256": claim.review_runner_binary_sha256,
        "review_evidence_sha256": review_evidence_sha256,
        "decision": decision,
        "reviewed_at": normalized_reviewed_at,
    }
    attestation_id = _derivation_id("review", semantic_payload)
    return AV1ValidationDerivationReviewAttestation(
        attestation_id=attestation_id,
        proposal_id=proposal.proposal_id,
        proposal_payload_sha256=proposal.payload_sha256,
        review_claim_id=claim.claim_id,
        review_claim_payload_sha256=claim.payload_sha256,
        lane=claim.lane,
        reviewer_token=claim.reviewer_token,
        review_runner_canonical_path_sha256=(
            claim.review_runner_canonical_path_sha256
        ),
        review_runner_binary_sha256=claim.review_runner_binary_sha256,
        review_evidence_sha256=review_evidence_sha256,
        decision=decision,
        reviewed_at=normalized_reviewed_at,
        payload_sha256=_payload_sha256({"attestation_id": attestation_id, **semantic_payload}),
    )


def build_av1_validation_derivation_review_envelope(
        *,
        review: AV1ValidationDerivationReviewAttestation,
        evidence: bytes,
) -> AV1ValidationDerivationReviewEnvelope:
    validate_av1_validation_derivation_review_run_evidence(
        evidence,
        review=review,
    )
    try:
        evidence_payload = object_dict(json.loads(evidence.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review run evidence is invalid"
        ) from exc
    semantic_payload = {
        "schema": AV1_VALIDATION_DERIVATION_REVIEW_ENVELOPE_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "review": review.to_payload(),
        "review_run_evidence": evidence_payload,
    }
    envelope_id = _derivation_id("review_envelope", semantic_payload)
    return AV1ValidationDerivationReviewEnvelope(
        envelope_id=envelope_id,
        review=review,
        review_run_payload_json=canonical_json_bytes(evidence_payload).decode("utf-8"),
        payload_sha256=_payload_sha256({
            "envelope_id": envelope_id,
            **semantic_payload,
        }),
    )


def _av1_validation_derivation_review_set_sha256(
        plan: AV1ValidationDerivationPlan,
        claims: Sequence[AV1ValidationDerivationReviewClaim],
        envelopes: Sequence[AV1ValidationDerivationReviewEnvelope],
) -> str:
    reviews = tuple(envelope.review for envelope in envelopes)
    claims_by_lane = {claim.lane: claim for claim in claims}
    if (
        len(claims) != len(AV1_VALIDATION_DERIVATION_REVIEW_LANES)
        or set(claims_by_lane) != set(AV1_VALIDATION_DERIVATION_REVIEW_LANES)
        or len({claim.review_run_id for claim in claims}) != len(claims)
        or len(envelopes) != len(AV1_VALIDATION_DERIVATION_REVIEW_LANES)
        or {review.lane for review in reviews}
        != set(AV1_VALIDATION_DERIVATION_REVIEW_LANES)
        or len({review.reviewer_token for review in reviews}) != len(reviews)
        or len({review.review_evidence_sha256 for review in reviews}) != len(reviews)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review set is incomplete or not independent"
        )
    repository_commit, repository_tree = (
        _av1_validation_derivation_review_repository_identity(claims)
    )
    assert_av1_validation_derivation_repository_identity(
        plan,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
    )
    if any(
        not _av1_validation_derivation_review_matches_claim(
            review,
            claims_by_lane[review.lane],
        )
        for review in reviews
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review set does not resolve its immutable claims"
        )
    return _payload_sha256({
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "plan_id": plan.plan_id,
        "plan_payload_sha256": plan.payload_sha256,
        "repository_commit": repository_commit,
        "repository_tree": repository_tree,
        "reviews": [
            {
                "lane": envelope.review.lane,
                "claim_id": claims_by_lane[envelope.review.lane].claim_id,
                "claim_payload_sha256": (
                    claims_by_lane[envelope.review.lane].payload_sha256
                ),
                "envelope_id": envelope.envelope_id,
                "envelope_payload_sha256": envelope.payload_sha256,
            }
            for envelope in sorted(envelopes, key=lambda item: item.review.lane)
        ],
    })


def _av1_validation_derivation_review_repository_identity(
        claims: Sequence[AV1ValidationDerivationReviewClaim],
) -> tuple[str, str]:
    repository_identities = {
        (claim.repository_commit, claim.repository_tree)
        for claim in claims
    }
    if len(repository_identities) != 1:
        raise AV1ValidationDerivationError(
            "AV1 derivation review claims must share one repository commit and tree"
        )
    return next(iter(repository_identities))


def _av1_validation_derivation_review_matches_claim(
        review: AV1ValidationDerivationReviewAttestation,
        claim: AV1ValidationDerivationReviewClaim,
) -> bool:
    return (
        review.review_claim_id == claim.claim_id
        and review.review_claim_payload_sha256 == claim.payload_sha256
        and review.proposal_id == claim.proposal_id
        and review.proposal_payload_sha256 == claim.proposal_payload_sha256
        and review.lane == claim.lane
        and review.reviewer_token == claim.reviewer_token
        and review.review_runner_canonical_path_sha256
        == claim.review_runner_canonical_path_sha256
        and review.review_runner_binary_sha256
        == claim.review_runner_binary_sha256
        and _parse_timestamp(review.reviewed_at, "review timestamp")
        >= _parse_timestamp(claim.claimed_at, "review-claim timestamp")
    )


def finalize_av1_validation_derivation_candidate_lock(
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        review_claims: Sequence[AV1ValidationDerivationReviewClaim],
        reviews: Sequence[AV1ValidationDerivationReviewAttestation],
        current_evaluation: AV1ValidationDerivationCandidateEvaluation,
        locked_at: str,
        repository_commit: str,
        repository_tree: str,
) -> AV1ColdStartValidationCandidateLockV1:
    assert_av1_validation_derivation_repository_identity(
        plan,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
    )
    current_proposal = current_evaluation.proposal
    locked = _parse_timestamp(locked_at, "candidate lock timestamp")
    if (
        current_evaluation.blockers
        or current_proposal is None
        or proposal.plan_id != plan.plan_id
        or proposal.manifest_id != plan.manifest_id
        or proposal.selection_lock_sha256 != plan.selection_lock_sha256
        or current_evaluation.cell_plan_id != proposal.cell_plan_id
        or current_evaluation.derivation_snapshot_sha256
        != proposal.derivation_snapshot_sha256
        or _proposal_lock_inputs(current_proposal) != _proposal_lock_inputs(proposal)
        or _parse_timestamp(
            current_proposal.proposed_at,
            "current proposal timestamp",
        )
        != locked
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation candidate is no longer current at lock time"
        )
    if _parse_timestamp(
        current_proposal.derivation_oldest_recorded_at,
        "oldest current observation",
    ) < locked - timedelta(days=current_proposal.maximum_derivation_age_days):
        raise AV1ValidationDerivationError(
            "AV1 derivation candidate evidence is stale at lock time"
        )
    claims_by_lane = {claim.lane: claim for claim in review_claims}
    if (
        len(review_claims) != len(AV1_VALIDATION_DERIVATION_REVIEW_LANES)
        or set(claims_by_lane) != set(AV1_VALIDATION_DERIVATION_REVIEW_LANES)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation candidate requires all five immutable review claims"
        )
    review_repository_commit, review_repository_tree = (
        _av1_validation_derivation_review_repository_identity(review_claims)
    )
    assert_av1_validation_derivation_repository_identity(
        plan,
        repository_commit=review_repository_commit,
        repository_tree=review_repository_tree,
    )
    if {review.lane for review in reviews} != set(AV1_VALIDATION_DERIVATION_REVIEW_LANES):
        raise AV1ValidationDerivationError("AV1 derivation candidate requires all five review lanes")
    if len(reviews) != len(AV1_VALIDATION_DERIVATION_REVIEW_LANES):
        raise AV1ValidationDerivationError("AV1 derivation candidate has duplicate review lanes")
    if len({review.reviewer_token for review in reviews}) != len(reviews):
        raise AV1ValidationDerivationError("AV1 derivation reviewers must be independent")
    if len({review.review_evidence_sha256 for review in reviews}) != len(reviews):
        raise AV1ValidationDerivationError(
            "AV1 derivation review evidence must come from independent agent runs"
        )
    for review in reviews:
        reviewed = _parse_timestamp(review.reviewed_at, "review timestamp")
        claim = claims_by_lane[review.lane]
        if (
            claim.plan_id != proposal.plan_id
            or claim.proposal_id != proposal.proposal_id
            or claim.proposal_payload_sha256 != proposal.payload_sha256
            or not _av1_validation_derivation_review_matches_claim(review, claim)
            or review.proposal_id != proposal.proposal_id
            or review.proposal_payload_sha256 != proposal.payload_sha256
            or review.decision != "approved"
            or reviewed < _parse_timestamp(
                proposal.proposed_at,
                "proposal timestamp",
            )
            or reviewed > locked
        ):
            raise AV1ValidationDerivationError("AV1 derivation candidate review did not approve the proposal")
    normalized_locked_at = _utc_timestamp(locked)
    return build_av1_cold_start_validation_candidate_lock(
        manifest_id=current_proposal.manifest_id,
        cell_plan_id=current_proposal.cell_plan_id,
        exact_traits=current_proposal.exact_traits,
        crf_lower=current_proposal.crf_lower,
        crf_center=current_proposal.crf_center,
        crf_upper=current_proposal.crf_upper,
        compatibility_signature=current_proposal.compatibility_signature,
        policy_signature=current_proposal.policy_signature,
        target_video_bitrate_min_bps=current_proposal.target_video_bitrate_min_bps,
        target_video_bitrate_max_bps=current_proposal.target_video_bitrate_max_bps,
        minimum_quality_score=current_proposal.minimum_quality_score,
        confidence_level=current_proposal.confidence_level,
        confidence_score=current_proposal.confidence_score,
        derivation_evidence_count=current_proposal.derivation_evidence_count,
        derivation_source_count=current_proposal.derivation_source_count,
        derivation_source_tokens=current_proposal.derivation_source_tokens,
        derivation_series_tokens=current_proposal.derivation_series_tokens,
        derivation_source_group_tokens=current_proposal.derivation_source_group_tokens,
        derivation_oldest_recorded_at=current_proposal.derivation_oldest_recorded_at,
        derivation_newest_recorded_at=current_proposal.derivation_newest_recorded_at,
        derivation_conflict_count=current_proposal.derivation_conflict_count,
        derivation_snapshot_sha256=current_proposal.derivation_snapshot_sha256,
        selection_lock_sha256=current_proposal.selection_lock_sha256,
        locked_at=normalized_locked_at,
        reviewed_at=normalized_locked_at,
    )


def _finalize_and_write_av1_validation_derivation_candidate_lock(
        artifact_root: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        review_claims: Sequence[AV1ValidationDerivationReviewClaim],
        review_envelopes: Sequence[AV1ValidationDerivationReviewEnvelope],
        current_evaluation: AV1ValidationDerivationCandidateEvaluation,
        locked_at: str,
        repository_commit: str,
        repository_tree: str,
        before_publish: Callable[[], None] | None = None,
) -> AV1ValidationDerivationCandidateLockEnvelope:
    reviews = tuple(envelope.review for envelope in review_envelopes)
    candidate_lock = finalize_av1_validation_derivation_candidate_lock(
        plan=plan,
        proposal=proposal,
        review_claims=review_claims,
        reviews=reviews,
        current_evaluation=current_evaluation,
        locked_at=locked_at,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
    )
    root = _bind_av1_validation_derivation_artifact_root(artifact_root, plan)
    if (
        candidate_lock.manifest_id != plan.manifest_id
        or candidate_lock.selection_lock_sha256 != plan.selection_lock_sha256
        or candidate_lock.cell_plan_id not in {
            assignment.cell_plan_id for assignment in plan.assignments
        }
        or any(
            claim.plan_id != plan.plan_id
            or claim.authorization_id != plan.authorization.authorization_id
            or claim.review_runner_canonical_path_sha256
            != plan.review_runner_canonical_path_sha256
            or claim.review_runner_binary_sha256
            != plan.review_runner_binary_sha256
            for claim in review_claims
        )
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation candidate lock is bound to another plan"
        )
    directory = root / "candidate-locks"
    _bind_owner_only_directory(
        directory,
        kind="candidate_locks",
        binding_id=plan.plan_id,
        binding_digest=plan.authorization.authorization_id,
    )
    root_binding = _read_owner_only_bytes(
        root / ".binding",
        "derivation artifact-root binding",
    )
    semantic_payload = {
        "schema": AV1_VALIDATION_DERIVATION_LOCK_ENVELOPE_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "plan_id": plan.plan_id,
        "plan_payload_sha256": plan.payload_sha256,
        "authorization_id": plan.authorization.authorization_id,
        "authorization_payload_sha256": plan.authorization.payload_sha256,
        "proposal_id": proposal.proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "review_set_sha256": _av1_validation_derivation_review_set_sha256(
            plan,
            review_claims,
            review_envelopes
        ),
        "artifact_root_binding_sha256": (
            f"sha256:{hashlib.sha256(root_binding).hexdigest()}"
        ),
        "candidate_lock": candidate_lock.to_payload(),
    }
    envelope_id = _derivation_id("lock_envelope", semantic_payload)
    envelope = AV1ValidationDerivationCandidateLockEnvelope(
        envelope_id=envelope_id,
        plan_id=plan.plan_id,
        plan_payload_sha256=plan.payload_sha256,
        authorization_id=plan.authorization.authorization_id,
        authorization_payload_sha256=plan.authorization.payload_sha256,
        proposal_id=proposal.proposal_id,
        proposal_payload_sha256=proposal.payload_sha256,
        review_set_sha256=semantic_payload["review_set_sha256"],
        artifact_root_binding_sha256=semantic_payload[
            "artifact_root_binding_sha256"
        ],
        candidate_lock=candidate_lock,
        payload_sha256=_payload_sha256({
            "envelope_id": envelope_id,
            **semantic_payload,
        }),
    )
    path = directory / f"{candidate_lock.cell_plan_id}.json"
    try:
        _write_owner_only(
            path,
            canonical_json_bytes(envelope.to_payload()),
            before_publish=before_publish,
            published_before=plan.authorization.valid_until,
        )
    except _AV1ValidationDerivationArtifactAlreadyExists:
        existing = _load_av1_validation_derivation_candidate_lock_envelope(
            root,
            plan=plan,
            cell_plan_id=candidate_lock.cell_plan_id,
        )
        if existing != envelope:
            raise AV1ValidationDerivationError(
                "AV1 derivation candidate lock conflicts with an immutable existing lock"
            )
        _fsync_owner_only_parent(path, "derivation candidate-lock envelope")
        return existing
    return envelope


def _load_av1_validation_derivation_candidate_lock_envelope(
        artifact_root: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        cell_plan_id: str,
) -> AV1ValidationDerivationCandidateLockEnvelope:
    if cell_plan_id not in {
        assignment.cell_plan_id for assignment in plan.assignments
    }:
        raise AV1ValidationDerivationError(
            "AV1 derivation candidate-lock cell is not authorized"
        )
    root = _assert_av1_validation_derivation_artifact_root_binding(
        artifact_root,
        plan,
    )
    directory = root / "candidate-locks"
    binding = _load_owner_only_directory_binding(
        directory,
        expected_kind="candidate_locks",
    )
    if (
        binding["binding_id"] != plan.plan_id
        or binding["binding_digest"] != plan.authorization.authorization_id
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation candidate-lock directory binding drifted"
        )
    payload, raw = _load_owner_only_json(
        directory / f"{cell_plan_id}.json",
        "derivation candidate-lock envelope",
        published_before=plan.authorization.valid_until,
    )
    envelope = av1_validation_derivation_candidate_lock_envelope_from_payload(
        payload,
        raw=raw,
    )
    return envelope


def _load_verified_av1_validation_derivation_candidate_lock(
        artifact_root: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        review_claims: Sequence[AV1ValidationDerivationReviewClaim],
        review_envelopes: Sequence[AV1ValidationDerivationReviewEnvelope],
        current_evaluation: AV1ValidationDerivationCandidateEvaluation,
        cell_plan_id: str,
        repository_commit: str,
        repository_tree: str,
) -> AV1ValidationDerivationCandidateLockEnvelope:
    root = _assert_av1_validation_derivation_artifact_root_binding(
        artifact_root,
        plan,
    )
    envelope = _load_av1_validation_derivation_candidate_lock_envelope(
        root,
        plan=plan,
        cell_plan_id=cell_plan_id,
    )
    reviews = tuple(item.review for item in review_envelopes)
    expected_lock = finalize_av1_validation_derivation_candidate_lock(
        plan=plan,
        proposal=proposal,
        review_claims=review_claims,
        reviews=reviews,
        current_evaluation=current_evaluation,
        locked_at=envelope.candidate_lock.locked_at,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
    )
    root_binding = _read_owner_only_bytes(
        root / ".binding",
        "derivation artifact-root binding",
    )
    expected_values = {
        "plan_id": plan.plan_id,
        "plan_payload_sha256": plan.payload_sha256,
        "authorization_id": plan.authorization.authorization_id,
        "authorization_payload_sha256": plan.authorization.payload_sha256,
        "proposal_id": proposal.proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "review_set_sha256": _av1_validation_derivation_review_set_sha256(
            plan,
            review_claims,
            review_envelopes
        ),
        "artifact_root_binding_sha256": (
            f"sha256:{hashlib.sha256(root_binding).hexdigest()}"
        ),
    }
    if (
        envelope.candidate_lock != expected_lock
        or any(getattr(envelope, key) != value for key, value in expected_values.items())
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation candidate-lock provenance chain is invalid"
        )
    return envelope


def _av1_validation_derivation_artifact_root_path(
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
) -> Path:
    root = Path(os.path.abspath(os.fspath(artifact_root.expanduser())))
    if (
        root.name != plan.partition_id
        or root.parent.name != AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation artifacts must use the partition-global canonical root"
        )
    return root


def _bind_av1_validation_derivation_artifact_root(
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
) -> Path:
    root = _av1_validation_derivation_artifact_root_path(artifact_root, plan)
    _bind_owner_only_directory(
        root,
        kind="artifact_root",
        binding_id=plan.plan_id,
        binding_digest=_av1_validation_derivation_artifact_root_binding_digest(
            root,
            plan,
        ),
    )
    return stable_absolute_path(root)


def _assert_av1_validation_derivation_artifact_root_binding(
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
) -> Path:
    root = _av1_validation_derivation_artifact_root_path(artifact_root, plan)
    binding = _load_owner_only_directory_binding(
        root,
        expected_kind="artifact_root",
    )
    if (
        binding["binding_id"] != plan.plan_id
        or binding["binding_digest"]
        != _av1_validation_derivation_artifact_root_binding_digest(root, plan)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation artifact-root binding drifted"
        )
    return stable_absolute_path(root)


def _av1_validation_derivation_artifact_root_binding_digest(
        root: Path,
        plan: AV1ValidationDerivationPlan,
) -> str:
    canonical_root = stable_absolute_path(root)
    canonical_root_sha256 = (
        "sha256:"
        + hashlib.sha256(
            os.fsencode(os.fspath(canonical_root)),
        ).hexdigest()
    )
    return _payload_sha256({
        "kind": "artifact_root",
        "plan_id": plan.plan_id,
        "authorization_id": plan.authorization.authorization_id,
        "canonical_root_sha256": canonical_root_sha256,
    })


def validate_av1_validation_derivation_artifact_root_binding(
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
) -> None:
    _assert_av1_validation_derivation_artifact_root_binding(
        artifact_root,
        plan,
    )


def write_av1_validation_derivation_plan(
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
        *,
        before_publish: Callable[[], None] | None = None,
) -> Path:
    root = _av1_validation_derivation_artifact_root_path(artifact_root, plan)
    path = root / "plan.json"
    try:
        _write_owner_only(
            path,
            canonical_json_bytes(plan.to_payload()),
            before_publish=before_publish,
        )
    except _AV1ValidationDerivationArtifactAlreadyExists:
        existing = load_av1_validation_derivation_plan(path)
        if existing != plan:
            raise AV1ValidationDerivationError(
                "AV1 derivation plan conflicts with an immutable existing plan"
            )
        _fsync_owner_only_parent(path, "derivation plan")
    bound_root = _bind_av1_validation_derivation_artifact_root(root, plan)
    return bound_root / "plan.json"


def load_av1_validation_derivation_plan(path: Path) -> AV1ValidationDerivationPlan:
    payload, raw = _load_owner_only_json(path, "derivation plan")
    return av1_validation_derivation_plan_from_payload(payload, raw=raw)


def write_av1_validation_derivation_attempt(
        directory: Path,
        attempt: AV1ValidationDerivationAttempt,
) -> Path:
    _bind_owner_only_directory(
        directory,
        kind="attempts",
        binding_id=attempt.plan_id,
        binding_digest=attempt.authorization_id,
    )
    path = directory / f"{attempt.assignment_id}.json"
    try:
        _write_owner_only(path, canonical_json_bytes(attempt.to_payload()))
    except _AV1ValidationDerivationArtifactAlreadyExists:
        payload, raw = _load_owner_only_json(path, "derivation attempt")
        existing = av1_validation_derivation_attempt_from_payload(
            payload,
            raw=raw,
        )
        if existing != attempt:
            raise AV1ValidationDerivationError(
                "AV1 derivation attempt conflicts with an immutable existing attempt"
            )
        _fsync_owner_only_parent(path, "derivation attempt")
    return path


def load_av1_validation_derivation_attempts(
        directory: Path,
) -> tuple[AV1ValidationDerivationAttempt, ...]:
    binding = _load_owner_only_directory_binding(directory, expected_kind="attempts")
    attempts_list: list[AV1ValidationDerivationAttempt] = []
    for path in sorted(directory.glob("*.json")):
        payload, raw = _load_owner_only_json(path, "derivation attempt")
        attempts_list.append(
            av1_validation_derivation_attempt_from_payload(payload, raw=raw)
        )
    attempts = tuple(attempts_list)
    assignment_ids = [attempt.assignment_id for attempt in attempts]
    if len(assignment_ids) != len(set(assignment_ids)):
        raise AV1ValidationDerivationError("AV1 derivation attempt directory repeats an assignment")
    if any(
        attempt.plan_id != binding["binding_id"]
        or attempt.authorization_id != binding["binding_digest"]
        for attempt in attempts
    ):
        raise AV1ValidationDerivationError("AV1 derivation attempt directory binding drifted")
    return attempts


def write_av1_validation_derivation_terminal_record(
        directory: Path,
        record: AV1ValidationDerivationTerminalRecord,
        *,
        before_publish: Callable[[], None] | None = None,
) -> Path:
    _bind_owner_only_directory(
        directory,
        kind="terminal_records",
        binding_id=record.plan_id,
        binding_digest=record.authorization_id,
    )
    path = directory / f"{record.assignment_id}.json"
    try:
        _write_owner_only(
            path,
            canonical_json_bytes(record.to_payload()),
            before_publish=before_publish,
        )
    except _AV1ValidationDerivationArtifactAlreadyExists:
        payload, raw = _load_owner_only_json(path, "derivation terminal record")
        existing = av1_validation_derivation_terminal_record_from_payload(
            payload,
            raw=raw,
        )
        if existing != record:
            raise AV1ValidationDerivationError(
                "AV1 derivation terminal record conflicts with the immutable artifact"
            )
        _fsync_owner_only_parent(path, "derivation terminal record")
    return path


def ensure_av1_validation_derivation_terminal_intent(
        directory: Path,
        record: AV1ValidationDerivationTerminalRecord,
        *,
        before_publish: Callable[[], None] | None = None,
) -> Path:
    return _ensure_av1_validation_derivation_terminal_artifact(
        directory,
        kind="terminal_intents",
        label="terminal intent",
        record=record,
        before_publish=before_publish,
    )


def ensure_av1_validation_derivation_verdict_claim(
        directory: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        attempt: AV1ValidationDerivationAttempt,
        claimed_at: str,
        before_publish: Callable[[], None] | None = None,
) -> bool:
    if attempt.plan_id != plan.plan_id or attempt.status != "review_pending":
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict claim is bound to another attempt"
        )
    _bind_owner_only_directory(
        directory,
        kind="verdict_claims",
        binding_id=plan.plan_id,
        binding_digest=plan.authorization.authorization_id,
    )
    path = directory / f"{attempt.assignment_id}.json"
    if path.exists() or path.is_symlink():
        existing, raw = _load_owner_only_json(
            path,
            "derivation verdict claim",
            published_before=plan.authorization.valid_until,
        )
        _validate_av1_validation_derivation_verdict_claim(
            existing,
            raw=raw,
            plan=plan,
            attempt=attempt,
        )
        _fsync_owner_only_parent(path, "derivation verdict claim")
        return False
    timestamp = _parse_timestamp(claimed_at, "verdict claim timestamp")
    payload = {
        "schema": AV1_VALIDATION_DERIVATION_VERDICT_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "plan_id": plan.plan_id,
        "authorization_id": plan.authorization.authorization_id,
        "attempt_id": attempt.attempt_id,
        "attempt_payload_sha256": attempt.payload_sha256,
        "assignment_id": attempt.assignment_id,
        "claimed_at": _utc_timestamp(timestamp),
    }
    _validate_av1_validation_derivation_verdict_claim(
        payload,
        raw=canonical_json_bytes(payload),
        plan=plan,
        attempt=attempt,
    )
    try:
        _write_owner_only(
            path,
            canonical_json_bytes(payload),
            before_publish=before_publish,
            published_before=plan.authorization.valid_until,
        )
        return True
    except _AV1ValidationDerivationArtifactAlreadyExists:
        if not path.exists():
            raise
    existing, raw = _load_owner_only_json(
        path,
        "derivation verdict claim",
        published_before=plan.authorization.valid_until,
    )
    _validate_av1_validation_derivation_verdict_claim(
        existing,
        raw=raw,
        plan=plan,
        attempt=attempt,
    )
    _fsync_owner_only_parent(path, "derivation verdict claim")
    return False


def load_av1_validation_derivation_verdict_claims(
        directory: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        attempts: Sequence[AV1ValidationDerivationAttempt],
) -> tuple[dict[str, Any], ...]:
    if not directory.exists():
        return ()
    binding = _load_owner_only_directory_binding(
        directory,
        expected_kind="verdict_claims",
    )
    attempts_by_assignment = {
        attempt.assignment_id: attempt
        for attempt in attempts
    }
    claims: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        payload, raw = _load_owner_only_json(
            path,
            "derivation verdict claim",
            published_before=plan.authorization.valid_until,
        )
        assignment_id = _required_text(
            payload.get("assignment_id"),
            "assignment ID",
        )
        attempt = attempts_by_assignment.get(assignment_id)
        if attempt is None:
            raise AV1ValidationDerivationError(
                "AV1 derivation verdict claim has no matching attempt"
            )
        _validate_av1_validation_derivation_verdict_claim(
            payload,
            raw=raw,
            plan=plan,
            attempt=attempt,
        )
        claims.append(payload)
    if (
        binding["binding_id"] != plan.plan_id
        or binding["binding_digest"] != plan.authorization.authorization_id
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict-claim directory binding drifted"
        )
    return tuple(claims)


def load_av1_validation_derivation_verdict_intent(
        directory: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        attempt: AV1ValidationDerivationAttempt,
) -> dict[str, Any] | None:
    if not directory.exists():
        return None
    binding = _load_owner_only_directory_binding(
        directory,
        expected_kind="verdict_intents",
    )
    if (
        binding["binding_id"] != plan.plan_id
        or binding["binding_digest"] != plan.authorization.authorization_id
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict-intent directory binding drifted"
        )
    path = directory / f"{attempt.assignment_id}.json"
    if not path.exists() and not path.is_symlink():
        return None
    payload, raw = _load_owner_only_json(
        path,
        "derivation verdict intent",
        published_before=plan.authorization.valid_until,
    )
    _validate_av1_validation_derivation_verdict_intent(
        payload,
        raw=raw,
        plan=plan,
        attempt=attempt,
    )
    return payload


def resolve_av1_validation_derivation_verdict_intent(
        directory: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        attempt: AV1ValidationDerivationAttempt,
        verdict: str,
        concern_tags: Sequence[str],
        evidence_ids: Sequence[str],
        moment_indexes: Sequence[int],
        recorded_at: str,
        before_publish: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if attempt.plan_id != plan.plan_id or attempt.status != "review_pending":
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict intent is bound to another attempt"
        )
    if verdict not in {"approved", "rejected"}:
        raise AV1ValidationDerivationError("AV1 derivation verdict intent is invalid")
    normalized_concerns = sorted({
        str(value).strip().lower()
        for value in concern_tags
        if str(value).strip()
    })
    normalized_evidence_ids = sorted({
        str(value).strip()
        for value in evidence_ids
        if str(value).strip()
    })
    try:
        parsed_moments = [int(value) for value in moment_indexes]
    except (TypeError, ValueError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict moment indexes are invalid"
        ) from exc
    if any(value <= 0 for value in parsed_moments):
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict moment indexes are invalid"
        )
    normalized_moments = sorted(set(parsed_moments))
    timestamp = _parse_timestamp(recorded_at, "verdict timestamp")
    semantic_payload = {
        "schema": AV1_VALIDATION_DERIVATION_VERDICT_INTENT_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "plan_id": plan.plan_id,
        "authorization_id": plan.authorization.authorization_id,
        "attempt_id": attempt.attempt_id,
        "attempt_payload_sha256": attempt.payload_sha256,
        "assignment_id": attempt.assignment_id,
        "verdict": verdict,
        "concern_tags": normalized_concerns,
        "evidence_ids": normalized_evidence_ids,
        "moment_indexes": normalized_moments,
        "recorded_at": _utc_timestamp(timestamp),
    }
    payload = {
        **semantic_payload,
        "payload_sha256": _payload_sha256(semantic_payload),
    }
    _bind_owner_only_directory(
        directory,
        kind="verdict_intents",
        binding_id=plan.plan_id,
        binding_digest=plan.authorization.authorization_id,
    )
    path = directory / f"{attempt.assignment_id}.json"
    if not path.exists() and not path.is_symlink():
        if not (
            _parse_timestamp(attempt.completed_at, "attempt completion")
            <= timestamp
            < _parse_timestamp(
                plan.authorization.valid_until,
                "derivation authorization expiration",
            )
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation verdict intent is outside its authorization window"
            )
        try:
            _write_owner_only(
                path,
                canonical_json_bytes(payload),
                before_publish=before_publish,
                published_before=plan.authorization.valid_until,
            )
            return payload
        except _AV1ValidationDerivationArtifactAlreadyExists:
            if not path.exists():
                raise
    existing, raw = _load_owner_only_json(
        path,
        "derivation verdict intent",
        published_before=plan.authorization.valid_until,
    )
    _validate_av1_validation_derivation_verdict_intent(
        existing,
        raw=raw,
        plan=plan,
        attempt=attempt,
    )
    stable_keys = set(payload) - {"recorded_at", "payload_sha256"}
    if any(existing.get(key) != payload.get(key) for key in stable_keys):
        raise AV1ValidationDerivationVerdictRetryMismatchError(existing)
    _fsync_owner_only_parent(path, "derivation verdict intent")
    return existing


def _validate_av1_validation_derivation_verdict_claim(
        payload: Mapping[str, Any],
        *,
        raw: bytes,
        plan: AV1ValidationDerivationPlan,
        attempt: AV1ValidationDerivationAttempt,
) -> None:
    _require_exact_keys(payload, {
        "schema", "schema_version", "contract_version", "plan_id",
        "authorization_id", "attempt_id", "attempt_payload_sha256",
        "assignment_id", "claimed_at",
    }, "derivation verdict claim")
    claimed_at = _parse_timestamp(
        str(payload.get("claimed_at") or ""),
        "verdict claim timestamp",
    )
    if (
        payload.get("schema") != AV1_VALIDATION_DERIVATION_VERDICT_CLAIM_SCHEMA
        or int_value(payload.get("schema_version"))
        != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or payload.get("contract_version")
        != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
        or payload.get("plan_id") != plan.plan_id
        or payload.get("authorization_id")
        != plan.authorization.authorization_id
        or payload.get("attempt_id") != attempt.attempt_id
        or payload.get("attempt_payload_sha256") != attempt.payload_sha256
        or payload.get("assignment_id") != attempt.assignment_id
        or raw != canonical_json_bytes(payload)
        or claimed_at
        < _parse_timestamp(attempt.completed_at, "attempt completion")
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict claim integrity is invalid"
        )


def _validate_av1_validation_derivation_verdict_intent(
        payload: Mapping[str, Any],
        *,
        raw: bytes,
        plan: AV1ValidationDerivationPlan,
        attempt: AV1ValidationDerivationAttempt,
) -> None:
    _require_exact_keys(payload, {
        "schema", "schema_version", "contract_version", "plan_id",
        "authorization_id", "attempt_id", "attempt_payload_sha256",
        "assignment_id", "verdict", "concern_tags", "evidence_ids",
        "moment_indexes", "recorded_at", "payload_sha256",
    }, "derivation verdict intent")
    timestamp = _parse_timestamp(
        str(payload.get("recorded_at") or ""),
        "verdict timestamp",
    )
    normalized_concerns = sorted({
        str(value).strip().lower()
        for value in object_list(payload.get("concern_tags"))
        if str(value).strip()
    })
    normalized_evidence_ids = sorted({
        str(value).strip()
        for value in object_list(payload.get("evidence_ids"))
        if str(value).strip()
    })
    try:
        normalized_moments = sorted({
            int(value)
            for value in object_list(payload.get("moment_indexes"))
            if int(value) > 0
        })
    except (TypeError, ValueError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict intent moment indexes are invalid"
        ) from exc
    if (
        payload.get("schema") != AV1_VALIDATION_DERIVATION_VERDICT_INTENT_SCHEMA
        or int_value(payload.get("schema_version"))
        != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or payload.get("contract_version")
        != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
        or payload.get("plan_id") != plan.plan_id
        or payload.get("authorization_id")
        != plan.authorization.authorization_id
        or payload.get("attempt_id") != attempt.attempt_id
        or payload.get("attempt_payload_sha256") != attempt.payload_sha256
        or payload.get("assignment_id") != attempt.assignment_id
        or payload.get("verdict") not in {"approved", "rejected"}
        or payload.get("concern_tags") != normalized_concerns
        or payload.get("evidence_ids") != normalized_evidence_ids
        or payload.get("moment_indexes") != normalized_moments
        or payload.get("payload_sha256")
        != _payload_sha256({
            key: value
            for key, value in payload.items()
            if key != "payload_sha256"
        })
        or raw != canonical_json_bytes(payload)
        or not (
            _parse_timestamp(attempt.completed_at, "attempt completion")
            <= timestamp
            < _parse_timestamp(
                plan.authorization.valid_until,
                "derivation authorization expiration",
            )
        )
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict intent integrity is invalid"
        )


def ensure_av1_validation_derivation_terminal_record(
        directory: Path,
        record: AV1ValidationDerivationTerminalRecord,
        *,
        before_publish: Callable[[], None] | None = None,
) -> Path:
    return _ensure_av1_validation_derivation_terminal_artifact(
        directory,
        kind="terminal_records",
        label="terminal record",
        record=record,
        before_publish=before_publish,
    )


def write_av1_validation_derivation_assignment_claim(
        directory: Path,
        *,
        assignment_id: str,
        plan_id: str,
        authorization_id: str,
        claimed_at: str,
        before_publish: Callable[[], None] | None = None,
        published_before: str | None = None,
) -> Path:
    bind_av1_validation_derivation_attempt_directory(
        directory,
        plan_id=plan_id,
        authorization_id=authorization_id,
    )
    path = directory / f"{assignment_id}.claim"
    _write_owner_only(
        path,
        canonical_json_bytes({
            "schema": "mediaforce.av1_cold_start_derivation_assignment_claim",
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "plan_id": plan_id,
            "authorization_id": authorization_id,
            "assignment_id": assignment_id,
            "claimed_at": claimed_at,
        }),
        before_publish=before_publish,
        published_before=published_before,
    )
    return path


def load_av1_validation_derivation_assignment_claims(
        directory: Path,
        *,
        plan: AV1ValidationDerivationPlan,
) -> tuple[dict[str, Any], ...]:
    if not directory.exists():
        return ()
    claims: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.claim")):
        published_after_deadline = False
        try:
            payload, raw = _load_owner_only_json(
                path,
                "derivation assignment claim",
                published_before=plan.authorization.valid_until,
            )
        except AV1ValidationDerivationPublicationDeadlineError:
            payload, raw = _load_owner_only_json(
                path,
                "derivation assignment claim",
            )
            published_after_deadline = True
        _require_exact_keys(payload, {
            "schema", "schema_version", "contract_version", "plan_id",
            "authorization_id", "assignment_id", "claimed_at",
        }, "derivation assignment claim")
        claim = {
            "schema": "mediaforce.av1_cold_start_derivation_assignment_claim",
            "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
            "plan_id": _required_text(payload.get("plan_id"), "plan ID"),
            "authorization_id": _required_text(
                payload.get("authorization_id"), "authorization ID"
            ),
            "assignment_id": _required_text(
                payload.get("assignment_id"), "assignment ID"
            ),
            "claimed_at": _required_text(
                payload.get("claimed_at"), "claim timestamp"
            ),
        }
        if (
            payload.get("schema") != claim["schema"]
            or int_value(payload.get("schema_version"))
            != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
            or payload.get("contract_version")
            != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
            or claim["plan_id"] != plan.plan_id
            or claim["authorization_id"]
            != plan.authorization.authorization_id
            or raw != canonical_json_bytes(claim)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation assignment claim is invalid"
            )
        claim_timestamp = _parse_timestamp(
            str(claim["claimed_at"]),
            "claim timestamp",
        )
        if not (
            _parse_timestamp(
                plan.authorization.authorized_at,
                "derivation authorization start",
            )
            <= claim_timestamp
            < _parse_timestamp(
                plan.authorization.valid_until,
                "derivation authorization expiration",
            )
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation assignment claim is outside its authorization window"
            )
        claim["published_after_deadline"] = published_after_deadline
        claims.append(claim)
    return tuple(claims)


def load_av1_validation_derivation_terminal_records(
        directory: Path,
) -> tuple[AV1ValidationDerivationTerminalRecord, ...]:
    binding = _load_owner_only_directory_binding(
        directory,
        expected_kind="terminal_records",
    )
    records_list: list[AV1ValidationDerivationTerminalRecord] = []
    for path in sorted(directory.glob("*.json")):
        payload, raw = _load_owner_only_json(path, "derivation terminal record")
        records_list.append(
            av1_validation_derivation_terminal_record_from_payload(payload, raw=raw)
        )
    records = tuple(records_list)
    assignment_ids = [record.assignment_id for record in records]
    if len(assignment_ids) != len(set(assignment_ids)):
        raise AV1ValidationDerivationError("AV1 derivation terminal directory repeats an assignment")
    if any(
        record.plan_id != binding["binding_id"]
        or record.authorization_id != binding["binding_digest"]
        for record in records
    ):
        raise AV1ValidationDerivationError("AV1 derivation terminal directory binding drifted")
    return records


def load_av1_validation_derivation_terminal_intents(
        directory: Path,
) -> tuple[AV1ValidationDerivationTerminalRecord, ...]:
    binding = _load_owner_only_directory_binding(
        directory,
        expected_kind="terminal_intents",
    )
    intents_list: list[AV1ValidationDerivationTerminalRecord] = []
    for path in sorted(directory.glob("*.json")):
        payload, raw = _load_owner_only_json(path, "derivation terminal intent")
        intents_list.append(
            av1_validation_derivation_terminal_record_from_payload(payload, raw=raw)
        )
    intents = tuple(intents_list)
    assignment_ids = [intent.assignment_id for intent in intents]
    if len(assignment_ids) != len(set(assignment_ids)):
        raise AV1ValidationDerivationError(
            "AV1 derivation terminal-intent directory repeats an assignment"
        )
    if any(
        intent.plan_id != binding["binding_id"]
        or intent.authorization_id != binding["binding_digest"]
        for intent in intents
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation terminal-intent directory binding drifted"
        )
    return intents


def write_av1_validation_derivation_candidate_proposal(
        artifact_root: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        before_publish: Callable[[], None] | None = None,
) -> Path:
    root = _bind_av1_validation_derivation_artifact_root(artifact_root, plan)
    if (
        proposal.plan_id != plan.plan_id
        or proposal.manifest_id != plan.manifest_id
        or proposal.selection_lock_sha256 != plan.selection_lock_sha256
        or proposal.cell_plan_id not in {
            assignment.cell_plan_id
            for assignment in plan.assignments
        }
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation proposal is bound to another plan"
        )
    directory = root / "proposals"
    _bind_owner_only_directory(
        directory,
        kind="candidate_proposals",
        binding_id=plan.plan_id,
        binding_digest=plan.authorization.authorization_id,
    )
    path = directory / f"{proposal.cell_plan_id}.json"
    try:
        _write_owner_only(
            path,
            canonical_json_bytes(proposal.to_payload()),
            before_publish=before_publish,
            published_before=plan.authorization.valid_until,
        )
    except _AV1ValidationDerivationArtifactAlreadyExists:
        existing = load_av1_validation_derivation_candidate_proposal(
            root,
            plan=plan,
            cell_plan_id=proposal.cell_plan_id,
        )
        if existing != proposal:
            raise AV1ValidationDerivationError(
                "AV1 derivation candidate proposal conflicts with an immutable existing proposal"
            )
        _fsync_owner_only_parent(path, "derivation candidate proposal")
    return path


def load_av1_validation_derivation_candidate_proposal(
        artifact_root: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        cell_plan_id: str,
) -> AV1ValidationDerivationCandidateProposal:
    if not _SAFE_TOKEN_RE.fullmatch(cell_plan_id) or cell_plan_id not in {
        assignment.cell_plan_id for assignment in plan.assignments
    }:
        raise AV1ValidationDerivationError(
            "AV1 derivation proposal cell plan is not authorized"
        )
    root = _assert_av1_validation_derivation_artifact_root_binding(
        artifact_root,
        plan,
    )
    directory = root / "proposals"
    binding = _load_owner_only_directory_binding(
        directory,
        expected_kind="candidate_proposals",
    )
    if (
        binding["binding_id"] != plan.plan_id
        or binding["binding_digest"] != plan.authorization.authorization_id
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation proposal directory binding drifted"
        )
    path = directory / f"{cell_plan_id}.json"
    payload, raw = _load_owner_only_json(
        path,
        "derivation candidate proposal",
        published_before=plan.authorization.valid_until,
    )
    proposal = av1_validation_derivation_candidate_proposal_from_payload(
        payload,
        raw=raw,
    )
    if (
        proposal.plan_id != plan.plan_id
        or proposal.manifest_id != plan.manifest_id
        or proposal.selection_lock_sha256 != plan.selection_lock_sha256
        or proposal.cell_plan_id != cell_plan_id
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation proposal is bound to another plan"
        )
    return proposal


def write_av1_validation_derivation_review_claim(
        artifact_root: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
        before_publish: Callable[[], None] | None = None,
) -> Path:
    root = _bind_av1_validation_derivation_artifact_root(artifact_root, plan)
    assert_av1_validation_derivation_repository_identity(
        plan,
        repository_commit=claim.repository_commit,
        repository_tree=claim.repository_tree,
    )
    if (
        proposal.plan_id != plan.plan_id
        or proposal.manifest_id != plan.manifest_id
        or claim.plan_id != plan.plan_id
        or claim.authorization_id != plan.authorization.authorization_id
        or claim.proposal_id != proposal.proposal_id
        or claim.proposal_payload_sha256 != proposal.payload_sha256
        or claim.review_runner_canonical_path_sha256
        != plan.review_runner_canonical_path_sha256
        or claim.review_runner_binary_sha256
        != plan.review_runner_binary_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review claim is bound to another run or proposal"
        )
    directory = root / "review-claims" / proposal.proposal_id
    _bind_owner_only_directory(
        directory,
        kind="review_claims",
        binding_id=proposal.proposal_id,
        binding_digest=proposal.payload_sha256,
    )
    path = directory / f"{claim.lane}.json"
    _write_owner_only(
        path,
        canonical_json_bytes(claim.to_payload()),
        before_publish=before_publish,
        published_before=plan.authorization.valid_until,
    )
    return path


def load_av1_validation_derivation_review_claims(
        artifact_root: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
) -> tuple[AV1ValidationDerivationReviewClaim, ...]:
    root = _assert_av1_validation_derivation_artifact_root_binding(
        artifact_root,
        plan,
    )
    if proposal.plan_id != plan.plan_id or proposal.manifest_id != plan.manifest_id:
        raise AV1ValidationDerivationError(
            "AV1 derivation proposal is bound to another plan"
        )
    directory = root / "review-claims" / proposal.proposal_id
    binding = _load_owner_only_directory_binding(
        directory,
        expected_kind="review_claims",
    )
    if (
        binding["binding_id"] != proposal.proposal_id
        or binding["binding_digest"] != proposal.payload_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review-claim directory binding drifted"
        )
    claims: list[AV1ValidationDerivationReviewClaim] = []
    for path in sorted(directory.glob("*.json")):
        if path.stem not in AV1_VALIDATION_DERIVATION_REVIEW_LANES:
            raise AV1ValidationDerivationError(
                "AV1 derivation review-claim directory contains an invalid lane"
            )
        payload, raw = _load_owner_only_json(
            path,
            "derivation review claim",
            published_before=plan.authorization.valid_until,
        )
        claim = av1_validation_derivation_review_claim_from_payload(
            payload,
            raw=raw,
        )
        assert_av1_validation_derivation_repository_identity(
            plan,
            repository_commit=claim.repository_commit,
            repository_tree=claim.repository_tree,
        )
        if (
            claim.plan_id != plan.plan_id
            or claim.authorization_id != plan.authorization.authorization_id
            or claim.proposal_id != proposal.proposal_id
            or claim.proposal_payload_sha256 != proposal.payload_sha256
            or claim.lane != path.stem
            or claim.review_runner_canonical_path_sha256
            != plan.review_runner_canonical_path_sha256
            or claim.review_runner_binary_sha256
            != plan.review_runner_binary_sha256
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review claim is bound to another run or proposal"
            )
        claims.append(claim)
    lanes = [claim.lane for claim in claims]
    if len(lanes) != len(set(lanes)):
        raise AV1ValidationDerivationError(
            "AV1 derivation review-claim directory repeats a lane"
        )
    return tuple(sorted(claims, key=lambda item: item.lane))


def write_av1_validation_derivation_review_envelope(
        artifact_root: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
        envelope: AV1ValidationDerivationReviewEnvelope,
        before_publish: Callable[[], None] | None = None,
) -> Path:
    review = envelope.review
    root = _bind_av1_validation_derivation_artifact_root(artifact_root, plan)
    assert_av1_validation_derivation_repository_identity(
        plan,
        repository_commit=claim.repository_commit,
        repository_tree=claim.repository_tree,
    )
    if (
        proposal.plan_id != plan.plan_id
        or proposal.manifest_id != plan.manifest_id
        or review.proposal_id != proposal.proposal_id
        or review.proposal_payload_sha256 != proposal.payload_sha256
        or not _av1_validation_derivation_review_matches_claim(review, claim)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review envelope is bound to another run or proposal"
        )
    directory = root / "reviews" / proposal.proposal_id
    _bind_owner_only_directory(
        directory,
        kind="reviews",
        binding_id=review.proposal_id,
        binding_digest=review.proposal_payload_sha256,
    )
    path = directory / f"{review.lane}.json"
    try:
        _write_owner_only(
            path,
            canonical_json_bytes(envelope.to_payload()),
            before_publish=before_publish,
            published_before=plan.authorization.valid_until,
        )
    except _AV1ValidationDerivationArtifactAlreadyExists:
        existing = load_av1_validation_derivation_review_envelope(
            root,
            plan=plan,
            proposal=proposal,
            claim=claim,
        )
        if existing != envelope:
            raise AV1ValidationDerivationError(
                "AV1 derivation review envelope conflicts with an immutable existing review"
            )
        _fsync_owner_only_parent(path, "derivation review envelope")
    return path


def load_av1_validation_derivation_review_envelope(
        artifact_root: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
) -> AV1ValidationDerivationReviewEnvelope:
    root = _assert_av1_validation_derivation_artifact_root_binding(
        artifact_root,
        plan,
    )
    assert_av1_validation_derivation_repository_identity(
        plan,
        repository_commit=claim.repository_commit,
        repository_tree=claim.repository_tree,
    )
    if (
        proposal.plan_id != plan.plan_id
        or proposal.manifest_id != plan.manifest_id
        or claim.plan_id != plan.plan_id
        or claim.authorization_id != plan.authorization.authorization_id
        or claim.proposal_id != proposal.proposal_id
        or claim.proposal_payload_sha256 != proposal.payload_sha256
        or claim.review_runner_canonical_path_sha256
        != plan.review_runner_canonical_path_sha256
        or claim.review_runner_binary_sha256
        != plan.review_runner_binary_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review claim is bound to another run or proposal"
        )
    directory = root / "reviews" / proposal.proposal_id
    binding = _load_owner_only_directory_binding(
        directory,
        expected_kind="reviews",
    )
    payload, raw = _load_owner_only_json(
        directory / f"{claim.lane}.json",
        "derivation review envelope",
        published_before=plan.authorization.valid_until,
    )
    envelope = av1_validation_derivation_review_envelope_from_payload(
        payload,
        raw=raw,
    )
    review = envelope.review
    if (
        binding["binding_id"] != proposal.proposal_id
        or binding["binding_digest"] != proposal.payload_sha256
        or review.lane != claim.lane
        or review.proposal_id != proposal.proposal_id
        or review.proposal_payload_sha256 != proposal.payload_sha256
        or not _av1_validation_derivation_review_matches_claim(review, claim)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review envelope is bound to another run or proposal"
        )
    return envelope


def load_av1_validation_derivation_review_envelopes(
        artifact_root: Path,
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        claims: Sequence[AV1ValidationDerivationReviewClaim],
) -> tuple[AV1ValidationDerivationReviewEnvelope, ...]:
    root = _assert_av1_validation_derivation_artifact_root_binding(
        artifact_root,
        plan,
    )
    if proposal.plan_id != plan.plan_id or proposal.manifest_id != plan.manifest_id:
        raise AV1ValidationDerivationError(
            "AV1 derivation proposal is bound to another plan"
        )
    directory = root / "reviews" / proposal.proposal_id
    binding = _load_owner_only_directory_binding(directory, expected_kind="reviews")
    expected_paths = {
        f"{lane}.json" for lane in AV1_VALIDATION_DERIVATION_REVIEW_LANES
    }
    actual_paths = {path.name for path in directory.glob("*.json")}
    if actual_paths != expected_paths:
        raise AV1ValidationDerivationError(
            "AV1 derivation review directory must contain exactly five lane envelopes"
        )
    envelopes_list: list[AV1ValidationDerivationReviewEnvelope] = []
    for lane in AV1_VALIDATION_DERIVATION_REVIEW_LANES:
        payload, raw = _load_owner_only_json(
            directory / f"{lane}.json",
            "derivation review envelope",
            published_before=plan.authorization.valid_until,
        )
        envelopes_list.append(
            av1_validation_derivation_review_envelope_from_payload(
                payload,
                raw=raw,
            )
        )
    envelopes = tuple(envelopes_list)
    reviews = tuple(envelope.review for envelope in envelopes)
    claims_by_lane = {claim.lane: claim for claim in claims}
    lanes = [review.lane for review in reviews]
    if len(lanes) != len(set(lanes)):
        raise AV1ValidationDerivationError("AV1 derivation review directory repeats a lane")
    if any(
        review.proposal_id != binding["binding_id"]
        or review.proposal_payload_sha256 != binding["binding_digest"]
        or review.lane not in claims_by_lane
        or not _av1_validation_derivation_review_matches_claim(
            review,
            claims_by_lane[review.lane],
        )
        for review in reviews
    ):
        raise AV1ValidationDerivationError("AV1 derivation review directory binding drifted")
    _av1_validation_derivation_review_set_sha256(plan, claims, envelopes)
    return envelopes


def validate_av1_validation_derivation_review_run_evidence(
        evidence: bytes,
        *,
        review: AV1ValidationDerivationReviewAttestation,
) -> None:
    try:
        payload = object_dict(json.loads(evidence.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review run evidence is invalid"
        ) from exc
    _require_exact_keys(payload, {
        "schema", "schema_version", "review_run_id", "reviewer_token",
        "proposal_id", "proposal_payload_sha256", "review_claim_id",
        "review_claim_payload_sha256", "lane", "decision",
        "repository_commit", "repository_tree",
        "review_runner_canonical_path_sha256", "review_runner_binary_sha256",
        "proposal", "review_claim", "prompt_sha256", "stdout", "stderr",
        "returncode",
    }, "derivation review run evidence")
    try:
        proposal_payload = object_dict(payload.get("proposal"))
        claim_payload = object_dict(payload.get("review_claim"))
        proposal = av1_validation_derivation_candidate_proposal_from_payload(
            proposal_payload,
            raw=canonical_json_bytes(proposal_payload),
        )
        claim = av1_validation_derivation_review_claim_from_payload(
            claim_payload,
            raw=canonical_json_bytes(claim_payload),
        )
    except (TypeError, ValueError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review run evidence bindings are invalid"
        ) from exc
    review_run_id = _required_text(payload.get("review_run_id"), "review run ID")
    repository_commit = _required_text(
        payload.get("repository_commit"),
        "review repository commit",
    )
    repository_tree = _required_text(
        payload.get("repository_tree"),
        "review repository tree",
    )
    _require_git_object_id(repository_commit, "review repository commit")
    _require_git_object_id(repository_tree, "review repository tree")
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    returncode = payload.get("returncode")
    if (
        payload.get("schema") != "mediaforce.av1_derivation_agent_review_run"
        or int_value(payload.get("schema_version")) != 1
        or payload.get("reviewer_token") != review.reviewer_token
        or review.reviewer_token != f"agent:{review_run_id}"
        or payload.get("proposal_id") != review.proposal_id
        or payload.get("proposal_payload_sha256") != review.proposal_payload_sha256
        or payload.get("review_claim_id") != review.review_claim_id
        or payload.get("review_claim_payload_sha256")
        != review.review_claim_payload_sha256
        or payload.get("repository_commit") != claim.repository_commit
        or payload.get("repository_tree") != claim.repository_tree
        or payload.get("lane") != review.lane
        or payload.get("decision") != review.decision
        or payload.get("review_runner_canonical_path_sha256")
        != review.review_runner_canonical_path_sha256
        or payload.get("review_runner_binary_sha256")
        != review.review_runner_binary_sha256
        or proposal.proposal_id != review.proposal_id
        or proposal.payload_sha256 != review.proposal_payload_sha256
        or claim.review_run_id != review_run_id
        or claim.plan_id != proposal.plan_id
        or not _av1_validation_derivation_review_matches_claim(review, claim)
        or type(returncode) is not int
        or returncode != 0
        or not isinstance(stdout, str)
        or not isinstance(stderr, str)
        or evidence != canonical_json_bytes(payload)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review run evidence does not match its attestation"
        )
    _require_sha256(
        _required_text(
            payload.get("review_runner_canonical_path_sha256"),
            "review-runner canonical-path digest",
        ),
        "review-runner canonical-path digest",
    )
    _require_sha256(
        _required_text(
            payload.get("review_runner_binary_sha256"),
            "review-runner binary digest",
        ),
        "review-runner binary digest",
    )
    canonical_evidence = canonical_json_bytes(payload)
    if review.review_evidence_sha256 != (
        f"sha256:{hashlib.sha256(canonical_evidence).hexdigest()}"
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review evidence digest does not match its canonical evidence"
        )
    prompt_sha256 = _required_text(payload.get("prompt_sha256"), "review prompt digest")
    _require_sha256(prompt_sha256, "review prompt digest")
    final_message, prompt = _completed_code_review_message(stdout)
    expected_prompt = build_av1_validation_derivation_review_prompt(
        proposal=proposal,
        claim=claim,
    )
    if prompt != expected_prompt:
        raise AV1ValidationDerivationError(
            "AV1 derivation review prompt does not match its frozen inputs"
        )
    if prompt_sha256 != f"sha256:{hashlib.sha256(expected_prompt.encode('utf-8')).hexdigest()}":
        raise AV1ValidationDerivationError(
            "AV1 derivation review prompt digest does not match its completed run"
        )
    marker = _code_review_marker(final_message)
    if marker != {
        "decision": review.decision,
        "lane": review.lane,
        "proposal_id": review.proposal_id,
        "proposal_payload_sha256": review.proposal_payload_sha256,
        "repository_commit": repository_commit,
        "repository_tree": repository_tree,
        "review_claim_id": review.review_claim_id,
        "review_claim_payload_sha256": review.review_claim_payload_sha256,
        "review_run_id": review_run_id,
    }:
        raise AV1ValidationDerivationError(
            "AV1 derivation review completion marker does not match its attestation"
        )


def _review_transcript_json_object(
        pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AV1ValidationDerivationError(
                "AV1 derivation review JSON contains duplicate JSON keys"
            )
        value[key] = item
    return value


def _completed_code_review_message(stdout: str) -> tuple[str, str]:
    stage = "config"
    prompt: str | None = None
    messages: list[str] = []
    completed_message: str | None = None
    config_keys = {"provider", "model", "workdir", "approval", "sandbox"}
    reserved_event_keys = {"prompt", "msg", "id", "event_seq", "order"}
    for line in stdout.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if not line.strip():
            continue
        if completed_message is not None:
            raise AV1ValidationDerivationError(
                "AV1 derivation review transcript has events after completion"
            )
        try:
            event = object_dict(json.loads(
                line,
                object_pairs_hook=_review_transcript_json_object,
            ))
        except (json.JSONDecodeError, TypeError) as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review transcript is not canonical JSONL"
            ) from exc
        has_config_fields = bool(config_keys.intersection(event))
        is_config = config_keys.issubset(event)
        is_prompt = set(event) == {"prompt"}
        if stage == "config":
            if (
                not is_config
                or any(
                    not isinstance(event.get(key), str)
                    or not str(event[key]).strip()
                    for key in ("provider", "model", "workdir")
                )
                or event.get("approval") != "never"
                or event.get("sandbox") != "read-only"
                or reserved_event_keys.intersection(event)
            ):
                raise AV1ValidationDerivationError(
                    "AV1 derivation review transcript configuration is invalid"
                )
            stage = "prompt"
            continue
        if has_config_fields:
            raise AV1ValidationDerivationError(
                "AV1 derivation review transcript configuration is duplicated or out of order"
            )
        if stage == "prompt":
            if (
                not is_prompt
                or not isinstance(event.get("prompt"), str)
                or not event["prompt"]
            ):
                raise AV1ValidationDerivationError(
                    "AV1 derivation review transcript prompt is missing or out of order"
                )
            prompt = event["prompt"]
            stage = "events"
            continue
        if "prompt" in event:
            raise AV1ValidationDerivationError(
                "AV1 derivation review transcript prompt is duplicated or out of order"
            )
        message = event.get("msg")
        if not isinstance(message, dict):
            continue
        if message.get("type") == "agent_message" and isinstance(
            message.get("message"), str
        ):
            messages.append(message["message"])
        if (
            message.get("type") == "task_lifecycle"
            and message.get("phase") == "quiescent"
        ):
            last_agent_message = message.get("last_agent_message")
            if (
                not isinstance(last_agent_message, str)
                or not messages
                or last_agent_message != messages[-1]
            ):
                raise AV1ValidationDerivationError(
                    "AV1 derivation review transcript completion is invalid"
                )
            completed_message = last_agent_message
    if (
        stage != "events"
        or prompt is None
        or not messages
        or completed_message != messages[-1]
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review transcript lacks a completed Code run"
        )
    return messages[-1], prompt


def _code_review_marker(message: str) -> dict[str, Any]:
    nonempty_lines = [
        line.removesuffix("\r").strip()
        for line in message.split("\n")
        if line.removesuffix("\r").strip()
    ]
    marker_lines = [
        line
        for line in nonempty_lines
        if line.startswith(AV1_VALIDATION_DERIVATION_AGENT_REVIEW_MARKER)
    ]
    if (
        not nonempty_lines
        or len(marker_lines) != 1
        or marker_lines[0] != nonempty_lines[-1]
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review transcript lacks one terminal marker"
        )
    try:
        marker = object_dict(json.loads(
            marker_lines[0][len(AV1_VALIDATION_DERIVATION_AGENT_REVIEW_MARKER):],
            object_pairs_hook=_review_transcript_json_object,
        ))
    except (json.JSONDecodeError, TypeError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review completion marker is invalid"
        ) from exc
    _require_exact_keys(marker, {
        "decision", "lane", "proposal_id", "proposal_payload_sha256",
        "repository_commit", "repository_tree",
        "review_claim_id", "review_claim_payload_sha256", "review_run_id",
    }, "derivation review completion marker")
    return marker


def av1_validation_derivation_plan_public_summary(
        plan: AV1ValidationDerivationPlan,
) -> dict[str, Any]:
    payload = {
        "schema": "mediaforce.av1_cold_start_derivation_plan_summary",
        "schema_version": 1,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "plan_id": plan.plan_id,
        "manifest_id": plan.manifest_id,
        "selection_lock_sha256": plan.selection_lock_sha256,
        "derivation_partition_sha256": plan.derivation_partition_sha256,
        "runtime_context_bound": True,
        "review_runner_identity_bound": True,
        "repository_snapshot_bound": True,
        "authorization_id": plan.authorization.authorization_id,
        "derivation_assignment_count": len(plan.assignments),
        "candidate_count": len({assignment.cell_plan_id for assignment in plan.assignments}),
        "derivation_execution_authorized": True,
        "holdout_execution_authorized": False,
        "guided_probe_allowed": False,
        "public_bundle_activation_allowed": False,
    }
    assert_av1_cold_start_public_payload_safe(payload)
    return payload


def av1_validation_derivation_candidate_evaluation_public_summary(
        evaluation: AV1ValidationDerivationCandidateEvaluation,
) -> dict[str, Any]:
    payload = {
        "schema": "mediaforce.av1_cold_start_derivation_candidate_summary",
        "schema_version": 1,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "cell_plan_id": evaluation.cell_plan_id,
        "derivation_snapshot_sha256": evaluation.derivation_snapshot_sha256,
        "blockers": list(evaluation.blockers),
        "candidate_proposal_id": (
            evaluation.proposal.proposal_id
            if evaluation.proposal is not None
            else None
        ),
        "candidate_proposal_sha256": (
            evaluation.proposal.payload_sha256
            if evaluation.proposal is not None
            else None
        ),
        "candidate_lock_created": False,
        "holdout_execution_authorized": False,
        "public_bundle_activation_allowed": False,
    }
    assert_av1_cold_start_public_payload_safe(payload)
    return payload


def av1_validation_derivation_plan_from_payload(
        payload: Mapping[str, Any],
        *,
        raw: bytes | None = None,
) -> AV1ValidationDerivationPlan:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "plan_id", "schema", "schema_version", "contract_version", "manifest_id",
        "manifest_payload_sha256", "partition_id", "partition_payload_sha256",
        "selection_lock_sha256", "derivation_partition_sha256", "runtime_context_sha256",
        "execution_environment_sha256", "statistics_contract_sha256",
        "review_runner_canonical_path_sha256", "review_runner_binary_sha256",
        "repository_commit", "repository_tree",
        "authorization",
        "execution_scope", "search_mode", "derivation_execution_authorized",
        "cold_start_warm_start_allowed", "validation_harness_allowed", "guided_probe_allowed",
        "holdout_execution_authorized", "retry_substitution_backfill_allowed",
        "public_bundle_activation_allowed", "assignments", "source_commitments",
        "source_commitment_sha256", "payload_sha256",
    }, "derivation plan")
    if (
        value.get("schema") != AV1_VALIDATION_DERIVATION_PLAN_SCHEMA
        or int_value(value.get("schema_version")) != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or value.get("contract_version") != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
        or value.get("execution_scope") != AV1_VALIDATION_DERIVATION_EXECUTION_SCOPE
        or value.get("search_mode") != AV1_VALIDATION_DERIVATION_SEARCH_MODE
        or value.get("derivation_execution_authorized") is not True
        or value.get("cold_start_warm_start_allowed") is not False
        or value.get("validation_harness_allowed") is not False
        or value.get("guided_probe_allowed") is not False
        or value.get("holdout_execution_authorized") is not False
        or value.get("retry_substitution_backfill_allowed") is not False
        or value.get("public_bundle_activation_allowed") is not False
    ):
        raise AV1ValidationDerivationError("AV1 derivation plan authority is invalid")
    from mediaforce.tuning.av1_validation_v2 import av1_validation_v2_derivation_authorization_from_payload

    plan = AV1ValidationDerivationPlan(
        plan_id=_required_text(value.get("plan_id"), "plan ID"),
        manifest_id=_required_text(value.get("manifest_id"), "manifest ID"),
        manifest_payload_sha256=_required_text(value.get("manifest_payload_sha256"), "manifest digest"),
        partition_id=_required_text(value.get("partition_id"), "partition ID"),
        partition_payload_sha256=_required_text(value.get("partition_payload_sha256"), "partition digest"),
        selection_lock_sha256=_required_text(value.get("selection_lock_sha256"), "selection-lock digest"),
        derivation_partition_sha256=_required_text(value.get("derivation_partition_sha256"), "derivation-partition digest"),
        runtime_context_sha256=_required_text(
            value.get("runtime_context_sha256"),
            "runtime-context digest",
        ),
        execution_environment_sha256=_required_text(
            value.get("execution_environment_sha256"),
            "execution-environment digest",
        ),
        statistics_contract_sha256=_required_text(
            value.get("statistics_contract_sha256"),
            "statistics-contract digest",
        ),
        review_runner_canonical_path_sha256=_required_text(
            value.get("review_runner_canonical_path_sha256"),
            "review-runner canonical-path digest",
        ),
        review_runner_binary_sha256=_required_text(
            value.get("review_runner_binary_sha256"),
            "review-runner binary digest",
        ),
        repository_commit=_required_text(
            value.get("repository_commit"),
            "plan repository commit",
        ),
        repository_tree=_required_text(
            value.get("repository_tree"),
            "plan repository tree",
        ),
        authorization=av1_validation_v2_derivation_authorization_from_payload(object_dict(value.get("authorization"))),
        assignments=tuple(_assignment_from_payload(object_dict(item)) for item in object_list(value.get("assignments"))),
        source_commitments=tuple(
            _source_commitment_from_payload(object_dict(item))
            for item in object_list(value.get("source_commitments"))
        ),
        source_commitment_sha256=_required_text(
            value.get("source_commitment_sha256"),
            "source-commitment digest",
        ),
        payload_sha256=_required_text(value.get("payload_sha256"), "plan digest"),
    )
    if raw is not None and raw != canonical_json_bytes(plan.to_payload()):
        raise AV1ValidationDerivationError("AV1 derivation plan JSON is not canonical")
    return plan


def av1_validation_derivation_attempt_from_payload(
        payload: Mapping[str, Any],
        *,
        raw: bytes | None = None,
) -> AV1ValidationDerivationAttempt:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "attempt_id", "schema", "schema_version", "contract_version", "plan_id",
        "authorization_id", "assignment_id", "cell_plan_id", "ordinal", "started_at",
        "completed_at", "status", "reason_code", "calibration_payload",
        "calibration_payload_sha256", "payload_sha256",
    }, "derivation attempt")
    if (
        value.get("schema") != AV1_VALIDATION_DERIVATION_ATTEMPT_SCHEMA
        or int_value(value.get("schema_version")) != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or value.get("contract_version") != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
    ):
        raise AV1ValidationDerivationError("AV1 derivation attempt contract is invalid")
    calibration_payload = value.get("calibration_payload")
    attempt = AV1ValidationDerivationAttempt(
        attempt_id=_required_text(value.get("attempt_id"), "attempt ID"),
        plan_id=_required_text(value.get("plan_id"), "plan ID"),
        authorization_id=_required_text(value.get("authorization_id"), "authorization ID"),
        assignment_id=_required_text(value.get("assignment_id"), "assignment ID"),
        cell_plan_id=_required_text(value.get("cell_plan_id"), "cell-plan ID"),
        ordinal=int_value(value.get("ordinal")),
        started_at=_required_text(value.get("started_at"), "attempt start"),
        completed_at=_required_text(value.get("completed_at"), "attempt completion"),
        status=cast(AV1ValidationDerivationAttemptStatus, _required_text(value.get("status"), "status")),
        reason_code=(str(value.get("reason_code")) if value.get("reason_code") is not None else None),
        calibration_payload_json=(
            canonical_json_bytes(object_dict(calibration_payload)).decode("utf-8")
            if calibration_payload is not None
            else None
        ),
        calibration_payload_sha256=(
            str(value.get("calibration_payload_sha256"))
            if value.get("calibration_payload_sha256") is not None
            else None
        ),
        payload_sha256=_required_text(value.get("payload_sha256"), "attempt digest"),
    )
    if raw is not None and raw != canonical_json_bytes(attempt.to_payload()):
        raise AV1ValidationDerivationError("AV1 derivation attempt JSON is not canonical")
    return attempt


def av1_validation_derivation_terminal_record_from_payload(
        payload: Mapping[str, Any],
        *,
        raw: bytes | None = None,
) -> AV1ValidationDerivationTerminalRecord:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "record_id", "schema", "schema_version", "contract_version", "plan_id",
        "authorization_id", "attempt_id", "attempt_payload_sha256", "assignment_id",
        "cell_plan_id", "ordinal", "started_at", "completed_at", "status",
        "reason_code", "observation", "payload_sha256",
    }, "derivation terminal record")
    if (
        value.get("schema") != AV1_VALIDATION_DERIVATION_TERMINAL_SCHEMA
        or int_value(value.get("schema_version")) != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or value.get("contract_version") != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
    ):
        raise AV1ValidationDerivationError("AV1 derivation terminal contract is invalid")
    observation_payload = value.get("observation")
    record = AV1ValidationDerivationTerminalRecord(
        record_id=_required_text(value.get("record_id"), "record ID"),
        plan_id=_required_text(value.get("plan_id"), "plan ID"),
        authorization_id=_required_text(value.get("authorization_id"), "authorization ID"),
        attempt_id=_required_text(value.get("attempt_id"), "attempt ID"),
        attempt_payload_sha256=_required_text(value.get("attempt_payload_sha256"), "attempt digest"),
        assignment_id=_required_text(value.get("assignment_id"), "assignment ID"),
        cell_plan_id=_required_text(value.get("cell_plan_id"), "cell-plan ID"),
        ordinal=int_value(value.get("ordinal")),
        started_at=_required_text(value.get("started_at"), "attempt start"),
        completed_at=_required_text(value.get("completed_at"), "attempt completion"),
        status=cast(AV1ValidationDerivationTerminalStatus, _required_text(value.get("status"), "status")),
        reason_code=(str(value.get("reason_code")) if value.get("reason_code") is not None else None),
        observation=(
            _observation_projection_from_payload(object_dict(observation_payload))
            if observation_payload is not None
            else None
        ),
        payload_sha256=_required_text(value.get("payload_sha256"), "record digest"),
    )
    if raw is not None and raw != canonical_json_bytes(record.to_payload()):
        raise AV1ValidationDerivationError("AV1 derivation terminal JSON is not canonical")
    return record


def av1_validation_derivation_candidate_proposal_from_payload(
        payload: Mapping[str, Any],
        *,
        raw: bytes | None = None,
) -> AV1ValidationDerivationCandidateProposal:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "proposal_id", "schema", "schema_version", "contract_version", "plan_id",
        "manifest_id", "cell_plan_id", "exact_traits", "crf_lower", "crf_center",
        "crf_upper", "crf_mad", "bitrate_relative_mad",
        "statistics_contract_sha256", "minimum_derivation_source_count",
        "maximum_derivation_age_days", "maximum_candidate_crf_span",
        "compatibility_signature", "policy_signature",
        "target_video_bitrate_min_bps", "target_video_bitrate_max_bps",
        "minimum_quality_score", "confidence_level", "confidence_score",
        "derivation_evidence_count", "derivation_source_count", "derivation_source_tokens",
        "derivation_title_tokens", "derivation_series_tokens",
        "derivation_source_group_tokens",
        "derivation_source_group_observation_tokens",
        "derivation_oldest_recorded_at", "derivation_newest_recorded_at",
        "derivation_conflict_count",
        "derivation_snapshot_sha256", "selection_lock_sha256", "proposed_at",
        "review_state", "holdout_execution_authorized", "public_bundle_activation_allowed",
        "payload_sha256",
    }, "derivation candidate proposal")
    if (
        value.get("schema") != AV1_VALIDATION_DERIVATION_PROPOSAL_SCHEMA
        or int_value(value.get("schema_version")) != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or value.get("contract_version") != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
        or value.get("review_state") != "pending_independent_review"
        or value.get("holdout_execution_authorized") is not False
        or value.get("public_bundle_activation_allowed") is not False
    ):
        raise AV1ValidationDerivationError("AV1 derivation candidate proposal contract is invalid")
    proposal = AV1ValidationDerivationCandidateProposal(
        proposal_id=_required_text(value.get("proposal_id"), "proposal ID"),
        plan_id=_required_text(value.get("plan_id"), "plan ID"),
        manifest_id=_required_text(value.get("manifest_id"), "manifest ID"),
        cell_plan_id=_required_text(value.get("cell_plan_id"), "cell-plan ID"),
        exact_traits=tuple(str(item) for item in object_list(value.get("exact_traits"))),
        crf_lower=float_value(value.get("crf_lower")),
        crf_center=float_value(value.get("crf_center")),
        crf_upper=float_value(value.get("crf_upper")),
        crf_mad=float_value(value.get("crf_mad")),
        bitrate_relative_mad=float_value(value.get("bitrate_relative_mad")),
        statistics_contract_sha256=_required_text(
            value.get("statistics_contract_sha256"),
            "statistics contract digest",
        ),
        minimum_derivation_source_count=int_value(
            value.get("minimum_derivation_source_count")
        ),
        maximum_derivation_age_days=int_value(
            value.get("maximum_derivation_age_days")
        ),
        maximum_candidate_crf_span=float_value(
            value.get("maximum_candidate_crf_span")
        ),
        compatibility_signature=_required_text(value.get("compatibility_signature"), "compatibility signature"),
        policy_signature=_required_text(value.get("policy_signature"), "policy signature"),
        target_video_bitrate_min_bps=int_value(value.get("target_video_bitrate_min_bps")),
        target_video_bitrate_max_bps=int_value(value.get("target_video_bitrate_max_bps")),
        minimum_quality_score=float_value(value.get("minimum_quality_score")),
        confidence_level=cast(Literal["moderate", "high"], _required_text(value.get("confidence_level"), "confidence")),
        confidence_score=float_value(value.get("confidence_score")),
        derivation_evidence_count=int_value(value.get("derivation_evidence_count")),
        derivation_source_count=int_value(value.get("derivation_source_count")),
        derivation_source_tokens=tuple(str(item) for item in object_list(value.get("derivation_source_tokens"))),
        derivation_title_tokens=tuple(
            str(item)
            for item in object_list(value.get("derivation_title_tokens"))
        ),
        derivation_series_tokens=tuple(str(item) for item in object_list(value.get("derivation_series_tokens"))),
        derivation_source_group_tokens=tuple(str(item) for item in object_list(value.get("derivation_source_group_tokens"))),
        derivation_source_group_observation_tokens=tuple(
            str(item)
            for item in object_list(value.get("derivation_source_group_observation_tokens"))
        ),
        derivation_oldest_recorded_at=_required_text(value.get("derivation_oldest_recorded_at"), "oldest observation"),
        derivation_newest_recorded_at=_required_text(value.get("derivation_newest_recorded_at"), "newest observation"),
        derivation_conflict_count=int_value(value.get("derivation_conflict_count")),
        derivation_snapshot_sha256=_required_text(value.get("derivation_snapshot_sha256"), "derivation snapshot"),
        selection_lock_sha256=_required_text(value.get("selection_lock_sha256"), "selection-lock digest"),
        proposed_at=_required_text(value.get("proposed_at"), "proposal timestamp"),
        payload_sha256=_required_text(value.get("payload_sha256"), "proposal digest"),
    )
    if raw is not None and raw != canonical_json_bytes(proposal.to_payload()):
        raise AV1ValidationDerivationError("AV1 derivation candidate proposal JSON is not canonical")
    return proposal


def av1_validation_derivation_review_claim_from_payload(
        payload: Mapping[str, Any],
        *,
        raw: bytes | None = None,
) -> AV1ValidationDerivationReviewClaim:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "claim_id", "schema", "schema_version", "contract_version", "plan_id",
        "authorization_id", "proposal_id", "proposal_payload_sha256", "lane",
        "repository_commit", "repository_tree",
        "review_run_id", "reviewer_token",
        "review_runner_canonical_path_sha256", "review_runner_binary_sha256",
        "claimed_at", "payload_sha256",
    }, "derivation review claim")
    if (
        value.get("schema") != AV1_VALIDATION_DERIVATION_REVIEW_CLAIM_SCHEMA
        or int_value(value.get("schema_version"))
        != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or value.get("contract_version")
        != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review-claim contract is invalid"
        )
    claim = AV1ValidationDerivationReviewClaim(
        claim_id=_required_text(value.get("claim_id"), "review-claim ID"),
        plan_id=_required_text(value.get("plan_id"), "plan ID"),
        authorization_id=_required_text(
            value.get("authorization_id"),
            "authorization ID",
        ),
        proposal_id=_required_text(value.get("proposal_id"), "proposal ID"),
        proposal_payload_sha256=_required_text(
            value.get("proposal_payload_sha256"),
            "proposal digest",
        ),
        repository_commit=_required_text(
            value.get("repository_commit"),
            "review repository commit",
        ),
        repository_tree=_required_text(
            value.get("repository_tree"),
            "review repository tree",
        ),
        lane=cast(
            AV1ValidationDerivationReviewLane,
            _required_text(value.get("lane"), "review lane"),
        ),
        review_run_id=_required_text(value.get("review_run_id"), "review run ID"),
        reviewer_token=_required_text(value.get("reviewer_token"), "reviewer token"),
        review_runner_canonical_path_sha256=_required_text(
            value.get("review_runner_canonical_path_sha256"),
            "review-runner canonical-path digest",
        ),
        review_runner_binary_sha256=_required_text(
            value.get("review_runner_binary_sha256"),
            "review-runner binary digest",
        ),
        claimed_at=_required_text(value.get("claimed_at"), "review-claim timestamp"),
        payload_sha256=_required_text(
            value.get("payload_sha256"),
            "review-claim digest",
        ),
    )
    if raw is not None and raw != canonical_json_bytes(claim.to_payload()):
        raise AV1ValidationDerivationError(
            "AV1 derivation review-claim JSON is not canonical"
        )
    return claim


def av1_validation_derivation_review_attestation_from_payload(
        payload: Mapping[str, Any],
        *,
        raw: bytes | None = None,
) -> AV1ValidationDerivationReviewAttestation:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "attestation_id", "schema", "schema_version", "contract_version", "proposal_id",
        "proposal_payload_sha256", "review_claim_id", "review_claim_payload_sha256",
        "lane", "reviewer_token", "review_runner_canonical_path_sha256",
        "review_runner_binary_sha256", "review_evidence_sha256",
        "decision", "reviewed_at", "payload_sha256",
    }, "derivation review attestation")
    if (
        value.get("schema") != AV1_VALIDATION_DERIVATION_REVIEW_SCHEMA
        or int_value(value.get("schema_version")) != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or value.get("contract_version") != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
    ):
        raise AV1ValidationDerivationError("AV1 derivation review contract is invalid")
    review = AV1ValidationDerivationReviewAttestation(
        attestation_id=_required_text(value.get("attestation_id"), "attestation ID"),
        proposal_id=_required_text(value.get("proposal_id"), "proposal ID"),
        proposal_payload_sha256=_required_text(value.get("proposal_payload_sha256"), "proposal digest"),
        review_claim_id=_required_text(
            value.get("review_claim_id"),
            "review-claim ID",
        ),
        review_claim_payload_sha256=_required_text(
            value.get("review_claim_payload_sha256"),
            "review-claim digest",
        ),
        lane=cast(AV1ValidationDerivationReviewLane, _required_text(value.get("lane"), "review lane")),
        reviewer_token=_required_text(value.get("reviewer_token"), "reviewer token"),
        review_runner_canonical_path_sha256=_required_text(
            value.get("review_runner_canonical_path_sha256"),
            "review-runner canonical-path digest",
        ),
        review_runner_binary_sha256=_required_text(
            value.get("review_runner_binary_sha256"),
            "review-runner binary digest",
        ),
        review_evidence_sha256=_required_text(
            value.get("review_evidence_sha256"),
            "review evidence digest",
        ),
        decision=cast(AV1ValidationDerivationReviewDecision, _required_text(value.get("decision"), "decision")),
        reviewed_at=_required_text(value.get("reviewed_at"), "review timestamp"),
        payload_sha256=_required_text(value.get("payload_sha256"), "review digest"),
    )
    if raw is not None and raw != canonical_json_bytes(review.to_payload()):
        raise AV1ValidationDerivationError("AV1 derivation review JSON is not canonical")
    return review


def av1_validation_derivation_review_envelope_from_payload(
        payload: Mapping[str, Any],
        *,
        raw: bytes | None = None,
) -> AV1ValidationDerivationReviewEnvelope:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "envelope_id",
        "schema",
        "schema_version",
        "contract_version",
        "review",
        "review_run_evidence",
        "payload_sha256",
    }, "derivation review envelope")
    if (
        value.get("schema") != AV1_VALIDATION_DERIVATION_REVIEW_ENVELOPE_SCHEMA
        or int_value(value.get("schema_version"))
        != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or value.get("contract_version")
        != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review envelope contract is invalid"
        )
    review_payload = object_dict(value.get("review"))
    evidence_payload = object_dict(value.get("review_run_evidence"))
    envelope = AV1ValidationDerivationReviewEnvelope(
        envelope_id=_required_text(value.get("envelope_id"), "review envelope ID"),
        review=av1_validation_derivation_review_attestation_from_payload(
            review_payload,
            raw=canonical_json_bytes(review_payload),
        ),
        review_run_payload_json=canonical_json_bytes(evidence_payload).decode("utf-8"),
        payload_sha256=_required_text(
            value.get("payload_sha256"),
            "review envelope digest",
        ),
    )
    if raw is not None and raw != canonical_json_bytes(envelope.to_payload()):
        raise AV1ValidationDerivationError(
            "AV1 derivation review envelope JSON is not canonical"
        )
    return envelope


def av1_validation_derivation_candidate_lock_envelope_from_payload(
        payload: Mapping[str, Any],
        *,
        raw: bytes | None = None,
) -> AV1ValidationDerivationCandidateLockEnvelope:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "envelope_id",
        "schema",
        "schema_version",
        "contract_version",
        "plan_id",
        "plan_payload_sha256",
        "authorization_id",
        "authorization_payload_sha256",
        "proposal_id",
        "proposal_payload_sha256",
        "review_set_sha256",
        "artifact_root_binding_sha256",
        "candidate_lock",
        "payload_sha256",
    }, "derivation candidate-lock envelope")
    if (
        value.get("schema") != AV1_VALIDATION_DERIVATION_LOCK_ENVELOPE_SCHEMA
        or int_value(value.get("schema_version"))
        != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or value.get("contract_version")
        != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation candidate-lock envelope contract is invalid"
        )
    envelope = AV1ValidationDerivationCandidateLockEnvelope(
        envelope_id=_required_text(value.get("envelope_id"), "lock envelope ID"),
        plan_id=_required_text(value.get("plan_id"), "plan ID"),
        plan_payload_sha256=_required_text(
            value.get("plan_payload_sha256"),
            "plan digest",
        ),
        authorization_id=_required_text(
            value.get("authorization_id"),
            "authorization ID",
        ),
        authorization_payload_sha256=_required_text(
            value.get("authorization_payload_sha256"),
            "authorization digest",
        ),
        proposal_id=_required_text(value.get("proposal_id"), "proposal ID"),
        proposal_payload_sha256=_required_text(
            value.get("proposal_payload_sha256"),
            "proposal digest",
        ),
        review_set_sha256=_required_text(
            value.get("review_set_sha256"),
            "review-set digest",
        ),
        artifact_root_binding_sha256=_required_text(
            value.get("artifact_root_binding_sha256"),
            "artifact-root binding digest",
        ),
        candidate_lock=_candidate_lock_from_payload(
            object_dict(value.get("candidate_lock"))
        ),
        payload_sha256=_required_text(
            value.get("payload_sha256"),
            "lock envelope digest",
        ),
    )
    if raw is not None and raw != canonical_json_bytes(envelope.to_payload()):
        raise AV1ValidationDerivationError(
            "AV1 derivation candidate-lock envelope JSON is not canonical"
        )
    return envelope


def _plan_semantic_payload(
        *,
        manifest: AV1ValidationManifestV2,
        partition: AV1ValidationPrivatePartition,
        authorization: AV1ValidationV2DerivationAuthorization,
        runtime_context_sha256: str,
        execution_environment_sha256: str,
        statistics_contract_sha256: str,
        review_runner_canonical_path_sha256: str,
        review_runner_binary_sha256: str,
        repository_commit: str,
        repository_tree: str,
        assignments: Sequence[AV1ValidationPartitionAssignment],
        source_commitments: Sequence[AV1ValidationDerivationSourceCommitment],
        source_commitment_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_DERIVATION_PLAN_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "manifest_id": manifest.manifest_id,
        "manifest_payload_sha256": manifest.payload_sha256,
        "partition_id": partition.partition_id,
        "partition_payload_sha256": partition.payload_sha256,
        "selection_lock_sha256": partition.selection_lock_sha256,
        "derivation_partition_sha256": partition.derivation_partition_sha256,
        "runtime_context_sha256": runtime_context_sha256,
        "execution_environment_sha256": execution_environment_sha256,
        "statistics_contract_sha256": statistics_contract_sha256,
        "review_runner_canonical_path_sha256": (
            review_runner_canonical_path_sha256
        ),
        "review_runner_binary_sha256": review_runner_binary_sha256,
        "repository_commit": repository_commit,
        "repository_tree": repository_tree,
        "authorization": authorization.to_payload(),
        "execution_scope": AV1_VALIDATION_DERIVATION_EXECUTION_SCOPE,
        "search_mode": AV1_VALIDATION_DERIVATION_SEARCH_MODE,
        "derivation_execution_authorized": True,
        "cold_start_warm_start_allowed": False,
        "validation_harness_allowed": False,
        "guided_probe_allowed": False,
        "holdout_execution_authorized": False,
        "retry_substitution_backfill_allowed": False,
        "public_bundle_activation_allowed": False,
        "assignments": [assignment.to_payload() for assignment in assignments],
        "source_commitments": [
            commitment.to_payload()
            for commitment in source_commitments
        ],
        "source_commitment_sha256": source_commitment_sha256,
    }


def _terminal_semantic_payload(
        *,
        plan: AV1ValidationDerivationPlan,
        assignment: AV1ValidationPartitionAssignment,
        attempt: AV1ValidationDerivationAttempt,
        status: AV1ValidationDerivationTerminalStatus,
        reason_code: str | None,
        observation: AV1ValidationDerivationObservationProjection | None,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_DERIVATION_TERMINAL_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "plan_id": plan.plan_id,
        "authorization_id": plan.authorization.authorization_id,
        "attempt_id": attempt.attempt_id,
        "attempt_payload_sha256": attempt.payload_sha256,
        "assignment_id": assignment.assignment_id,
        "cell_plan_id": assignment.cell_plan_id,
        "ordinal": assignment.ordinal,
        "started_at": attempt.started_at,
        "completed_at": attempt.completed_at,
        "status": status,
        "reason_code": reason_code,
        "observation": observation.to_payload() if observation is not None else None,
    }


def _attempt_semantic_payload(
        *,
        plan: AV1ValidationDerivationPlan,
        assignment: AV1ValidationPartitionAssignment,
        started_at: str,
        completed_at: str,
        status: AV1ValidationDerivationAttemptStatus,
        reason_code: str | None,
        calibration_payload: Mapping[str, Any] | None,
        calibration_payload_sha256: str | None,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_DERIVATION_ATTEMPT_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "plan_id": plan.plan_id,
        "authorization_id": plan.authorization.authorization_id,
        "assignment_id": assignment.assignment_id,
        "cell_plan_id": assignment.cell_plan_id,
        "ordinal": assignment.ordinal,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "reason_code": reason_code,
        "calibration_payload": dict(calibration_payload) if calibration_payload is not None else None,
        "calibration_payload_sha256": calibration_payload_sha256,
    }


def _proposal_semantic_payload(
        *,
        plan: AV1ValidationDerivationPlan,
        cell_plan_id: str,
        observations: Sequence[AV1ValidationDerivationObservationProjection],
        crf_lower: float,
        crf_center: float,
        crf_upper: float,
        crf_mad: float,
        bitrate_relative_mad: float,
        statistics_contract_sha256: str,
        minimum_derivation_source_count: int,
        maximum_derivation_age_days: int,
        maximum_candidate_crf_span: float,
        target_video_bitrate_min_bps: int,
        target_video_bitrate_max_bps: int,
        confidence_level: Literal["moderate", "high"],
        confidence_score: float,
        derivation_conflict_count: int,
        snapshot_sha256: str,
        proposed_at: str,
) -> dict[str, Any]:
    source_tokens = sorted({item.source_token for item in observations})
    title_tokens = sorted({item.title_token for item in observations})
    return {
        "schema": AV1_VALIDATION_DERIVATION_PROPOSAL_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "plan_id": plan.plan_id,
        "manifest_id": plan.manifest_id,
        "cell_plan_id": cell_plan_id,
        "exact_traits": list(observations[0].traits),
        "crf_lower": crf_lower,
        "crf_center": crf_center,
        "crf_upper": crf_upper,
        "crf_mad": crf_mad,
        "bitrate_relative_mad": bitrate_relative_mad,
        "statistics_contract_sha256": statistics_contract_sha256,
        "minimum_derivation_source_count": minimum_derivation_source_count,
        "maximum_derivation_age_days": maximum_derivation_age_days,
        "maximum_candidate_crf_span": maximum_candidate_crf_span,
        "compatibility_signature": observations[0].compatibility_signature,
        "policy_signature": observations[0].policy_signature,
        "target_video_bitrate_min_bps": target_video_bitrate_min_bps,
        "target_video_bitrate_max_bps": target_video_bitrate_max_bps,
        "minimum_quality_score": observations[0].minimum_quality_score,
        "confidence_level": confidence_level,
        "confidence_score": confidence_score,
        "derivation_evidence_count": len(observations),
        "derivation_source_count": len(source_tokens),
        "derivation_source_tokens": source_tokens,
        "derivation_title_tokens": title_tokens,
        "derivation_series_tokens": sorted({item.series_token for item in observations}),
        "derivation_source_group_tokens": sorted({item.source_group_token for item in observations}),
        "derivation_source_group_observation_tokens": sorted(
            item.source_group_token for item in observations
        ),
        "derivation_oldest_recorded_at": min(item.recorded_at for item in observations),
        "derivation_newest_recorded_at": max(item.recorded_at for item in observations),
        "derivation_conflict_count": derivation_conflict_count,
        "derivation_snapshot_sha256": snapshot_sha256,
        "selection_lock_sha256": plan.selection_lock_sha256,
        "proposed_at": proposed_at,
        "review_state": "pending_independent_review",
        "holdout_execution_authorized": False,
        "public_bundle_activation_allowed": False,
    }


def _proposal_lock_inputs(
        proposal: AV1ValidationDerivationCandidateProposal,
) -> dict[str, Any]:
    return {
        "plan_id": proposal.plan_id,
        "manifest_id": proposal.manifest_id,
        "cell_plan_id": proposal.cell_plan_id,
        "exact_traits": list(proposal.exact_traits),
        "crf_lower": proposal.crf_lower,
        "crf_center": proposal.crf_center,
        "crf_upper": proposal.crf_upper,
        "crf_mad": proposal.crf_mad,
        "bitrate_relative_mad": proposal.bitrate_relative_mad,
        "statistics_contract_sha256": proposal.statistics_contract_sha256,
        "minimum_derivation_source_count": (
            proposal.minimum_derivation_source_count
        ),
        "maximum_derivation_age_days": proposal.maximum_derivation_age_days,
        "maximum_candidate_crf_span": proposal.maximum_candidate_crf_span,
        "compatibility_signature": proposal.compatibility_signature,
        "policy_signature": proposal.policy_signature,
        "target_video_bitrate_min_bps": proposal.target_video_bitrate_min_bps,
        "target_video_bitrate_max_bps": proposal.target_video_bitrate_max_bps,
        "minimum_quality_score": proposal.minimum_quality_score,
        "confidence_level": proposal.confidence_level,
        "confidence_score": proposal.confidence_score,
        "derivation_evidence_count": proposal.derivation_evidence_count,
        "derivation_source_count": proposal.derivation_source_count,
        "derivation_source_tokens": list(proposal.derivation_source_tokens),
        "derivation_title_tokens": list(proposal.derivation_title_tokens),
        "derivation_series_tokens": list(proposal.derivation_series_tokens),
        "derivation_source_group_tokens": list(
            proposal.derivation_source_group_tokens
        ),
        "derivation_source_group_observation_tokens": list(
            proposal.derivation_source_group_observation_tokens
        ),
        "derivation_oldest_recorded_at": proposal.derivation_oldest_recorded_at,
        "derivation_newest_recorded_at": proposal.derivation_newest_recorded_at,
        "derivation_conflict_count": proposal.derivation_conflict_count,
        "derivation_snapshot_sha256": proposal.derivation_snapshot_sha256,
        "selection_lock_sha256": proposal.selection_lock_sha256,
    }


def _observation_projection(
        *,
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
        assignment: AV1ValidationPartitionAssignment,
        attempt: AV1ValidationDerivationAttempt,
        observation: ContentIntentBoundaryObservation,
) -> AV1ValidationDerivationObservationProjection:
    if not content_intent_boundary_observation_integrity_valid(observation):
        raise AV1ValidationDerivationError("AV1 derivation observation integrity is invalid")
    if (
        observation.personalization_eligible
        or observation.exclusion_reason
        != AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation observation is not quarantined from personalization"
        )
    source = next(
        (item for item in partition.inventory_sources if item.local_item_id == assignment.local_item_id),
        None,
    )
    if source is None:
        raise AV1ValidationDerivationError("AV1 derivation assignment source is missing")
    try:
        traits = tuple(sorted(str(item) for item in json.loads(observation.content_traits_json)))
        assessment = object_dict(json.loads(observation.assessment_json))
        compatibility = object_dict(json.loads(observation.compatibility_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AV1ValidationDerivationError("AV1 derivation observation payload is invalid") from exc
    chosen_crf = float_value(assessment.get("chosen_crf"))
    calibration = attempt.calibration_payload()
    calibration_sample_item = object_dict(calibration.get("sample_item"))
    calibration_sample_result = object_dict(calibration.get("sample_result"))
    calibration_quality_score = float_value(
        calibration_sample_result.get("quality_score")
    )
    calibration_chosen_crf = float_value(
        calibration_sample_result.get("chosen_crf")
    )
    calibration_duration_seconds = float_value(
        calibration_sample_item.get("duration_seconds")
    )
    calibration_predicted_video_bytes = int_value(
        calibration_sample_result.get("predicted_video_size_bytes")
    )
    calibration_boundary_bitrate_bps = (
        round(
            (calibration_predicted_video_bytes * 8)
            / calibration_duration_seconds
        )
        if calibration_duration_seconds > 0
        and calibration_predicted_video_bytes > 0
        else 0
    )
    calibration_compatibility = object_dict(
        calibration_sample_result.get("content_intent_compatibility")
    )
    try:
        calibration_compatibility_key = (
            content_intent_boundary_compatibility_from_payload(
                calibration_compatibility
            ).compatibility_key
        )
    except ValueError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation calibration compatibility is invalid"
        ) from exc
    recorded_at = _parse_timestamp(observation.recorded_at, "observation timestamp")
    authorized_at = _parse_timestamp(plan.authorization.authorized_at, "derivation authorization")
    valid_until = _parse_timestamp(plan.authorization.valid_until, "derivation authorization expiration")
    if (
        not authorized_at <= recorded_at < valid_until
        or recorded_at < _parse_timestamp(
            attempt.completed_at,
            "attempt completion",
        )
    ):
        raise AV1ValidationDerivationError("AV1 derivation observation is outside its authorization window")
    expected_kind = "visual_approval" if observation.verdict == "acceptable" else "visual_rejection"
    if (
        observation.revision != 0
            or observation.supersedes_observation_id is not None
            or observation.authority != "runtime_native"
            or observation.disposition != "active"
            or observation.personalization_eligible is not False
            or observation.exclusion_reason
            != AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON
        or observation.source_event_kind != "post_test_review"
        or observation.observation_kind != expected_kind
        or observation.library_item_id != assignment.local_item_id
        or observation.content_fingerprint != source.source_identity
        or observation.job_id != str(calibration.get("job_id") or "")
        or observation.artifact_fingerprint
        != str(calibration.get("review_artifact_fingerprint") or "")
        or observation.compatibility_key
        != calibration_compatibility_key
        or int_value(calibration_sample_item.get("library_item_id")) != assignment.local_item_id
        or traits != assignment.traits
        or observation.intent_level != assignment.intent_level
        or observation.quality_metric.casefold() != assignment.quality_metric.casefold()
        or not math.isclose(
            chosen_crf,
            calibration_chosen_crf,
            rel_tol=0.0,
            abs_tol=0.001,
        )
        or observation.boundary_bitrate_bps
        != calibration_boundary_bitrate_bps
        or not math.isclose(
            observation.measured_quality_score,
            calibration_quality_score,
            rel_tol=0.0,
            abs_tol=0.001,
        )
        or observation.quality_floor_met
        != (
            calibration_quality_score
            >= assignment.minimum_quality_score
        )
        or not math.isclose(observation.quality_target, assignment.quality_target, rel_tol=0.0, abs_tol=0.001)
        or not math.isclose(
            observation.minimum_quality_score,
            assignment.minimum_quality_score,
            rel_tol=0.0,
            abs_tol=0.001,
        )
        or compatibility.get("measurement_basis") != "sample_projection"
        or compatibility.get("assessment_contract") != BOUNDARY_ASSESSMENT_CONTRACT
        or not math.isfinite(chosen_crf)
        or not 0 <= chosen_crf <= 63
    ):
        raise AV1ValidationDerivationError("AV1 derivation observation does not match its reserved assignment")
    return AV1ValidationDerivationObservationProjection(
        observation_id=observation.observation_id,
        observation_payload_sha256=f"sha256:{observation.payload_sha256}",
        local_item_id=assignment.local_item_id,
        source_token=assignment.source_token,
        title_token=assignment.title_token,
        series_token=assignment.series_token,
        source_group_token=assignment.source_group_token,
        traits=assignment.traits,
        intent_level=assignment.intent_level,
        compatibility_signature=assignment.compatibility_signature,
        policy_signature=assignment.policy_signature,
        observation_compatibility_key=observation.compatibility_key,
        observation_policy_hash=observation.policy_hash,
        verdict=observation.verdict,
        chosen_crf=round(chosen_crf, 3),
        boundary_bitrate_bps=observation.boundary_bitrate_bps,
        quality_metric=assignment.quality_metric,
        quality_target=assignment.quality_target,
        minimum_quality_score=assignment.minimum_quality_score,
        measured_quality_score=observation.measured_quality_score,
        quality_floor_met=observation.quality_floor_met,
        recorded_at=_utc_timestamp(recorded_at),
    )


def _validate_calibration_payload(
        assignment: AV1ValidationPartitionAssignment,
        calibration: Mapping[str, Any],
        *,
        source_identity: str,
        expected_source_sha256: str,
        expected_source_size_bytes: int,
) -> None:
    _validate_calibration_execution(calibration)
    sample_item = object_dict(calibration.get("sample_item"))
    stream_budget_ledger = object_dict(sample_item.get("stream_budget_ledger"))
    stream_budget_totals = object_dict(stream_budget_ledger.get("totals"))
    sample_result = object_dict(calibration.get("sample_result"))
    target_trace = object_dict(sample_result.get("target_size_trace"))
    quality_floor = object_dict(target_trace.get("quality_floor"))
    selected_candidate = object_dict(target_trace.get("selected_candidate"))
    quality_target = float_value(sample_result.get("quality_target"))
    quality_score = float_value(sample_result.get("quality_score"))
    chosen_crf = float_value(sample_result.get("chosen_crf"))
    duration_seconds = float_value(sample_item.get("duration_seconds"))
    source_size_bytes = int_value(sample_item.get("source_size_bytes"))
    source_snapshot_size_bytes = int_value(
        sample_item.get("source_snapshot_size_bytes")
    )
    source_snapshot_sha256 = _required_text(
        sample_item.get("source_snapshot_sha256"),
        "source snapshot digest",
    )
    _require_sha256(source_snapshot_sha256, "source snapshot digest")
    predicted_video_size_bytes = int_value(
        sample_result.get("predicted_video_size_bytes")
    )
    if (
        int_value(sample_item.get("library_item_id")) != assignment.local_item_id
        or str(sample_item.get("content_version_fingerprint") or "")
        != source_identity
        or str(
            sample_item.get("source_snapshot_content_version_fingerprint") or ""
        ) != source_identity
        or source_snapshot_sha256 != expected_source_sha256
        or source_size_bytes != expected_source_size_bytes
        or source_snapshot_size_bytes != source_size_bytes
        or int_value(stream_budget_totals.get("remaining_video_bitrate_bps"))
        != assignment.target_video_bitrate_bps
        or str(sample_result.get("quality_metric") or "").casefold()
        != assignment.quality_metric.casefold()
        or not math.isclose(quality_target, assignment.quality_target, rel_tol=0.0, abs_tol=0.001)
        or not math.isfinite(quality_score)
        or duration_seconds <= 0
        or predicted_video_size_bytes <= 0
        or str(quality_floor.get("metric") or "").casefold()
        != assignment.quality_metric.casefold()
        or not math.isclose(
            float_value(quality_floor.get("target")),
            assignment.quality_target,
            rel_tol=0.0,
            abs_tol=0.001,
        )
        or not math.isclose(
            float_value(quality_floor.get("minimum")),
            assignment.minimum_quality_score,
            rel_tol=0.0,
            abs_tol=0.001,
        )
        or not math.isclose(
            float_value(selected_candidate.get("crf")),
            chosen_crf,
            rel_tol=0.0,
            abs_tol=0.001,
        )
        or not math.isclose(
            float_value(selected_candidate.get("metric_score")),
            quality_score,
            rel_tol=0.0,
            abs_tol=0.001,
        )
        or str(selected_candidate.get("metric") or "").casefold()
        != assignment.quality_metric.casefold()
        or not math.isclose(
            float_value(selected_candidate.get("metric_target")),
            assignment.quality_target,
            rel_tol=0.0,
            abs_tol=0.001,
        )
        or not math.isclose(
            float_value(selected_candidate.get("min_metric_score")),
            assignment.minimum_quality_score,
            rel_tol=0.0,
            abs_tol=0.001,
        )
        or selected_candidate.get("quality_floor_met")
        is not (quality_score >= assignment.minimum_quality_score)
        or int_value(selected_candidate.get("predicted_video_bytes"))
        != predicted_video_size_bytes
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation calibration does not prove unchanged measured full search"
        )


def _validate_calibration_execution(calibration: Mapping[str, Any]) -> None:
    sample_result = object_dict(calibration.get("sample_result"))
    cold_start = object_dict(sample_result.get("av1_cold_start_prior"))
    target_trace = object_dict(sample_result.get("target_size_trace"))
    compatibility = object_dict(sample_result.get("content_intent_compatibility"))
    chosen_crf = float_value(sample_result.get("chosen_crf"))
    host = object_dict(calibration.get("host"))
    candidates = [object_dict(value) for value in object_list(target_trace.get("candidates"))]
    selected_candidate = object_dict(target_trace.get("selected_candidate"))
    curve = object_dict(target_trace.get("curve"))
    retry_policy = object_dict(target_trace.get("retry_policy"))
    candidate_attempts = [int_value(candidate.get("attempt")) for candidate in candidates]
    candidate_roles = [str(candidate.get("role") or "") for candidate in candidates]
    required_candidate_fields = {
        "attempt",
        "role",
        "crf",
        "metric",
        "metric_score",
        "min_metric_score",
        "quality_floor_met",
        "sampled_clip_bytes",
        "predicted_video_bytes",
        "predicted_whole_episode_bytes",
        "within_sample_band",
        "violates_source_cap",
    }
    review_fingerprint = str(calibration.get("review_artifact_fingerprint") or "").strip()
    if (
        calibration.get("mode") != "sample"
        or calibration.get("action") != "av1_derivation"
        or host.get("mode") != "local"
        or host.get("media_access") != "direct"
        or cold_start.get("status") != "unavailable"
        or cold_start.get("reason") != "cold_start_planner_unavailable"
        or cold_start.get("execution") not in (None, {})
        or not target_trace
        or int_value(target_trace.get("schema_version")) != 1
        or target_trace.get("status") != "selected"
        or target_trace.get("warm_start") not in (None, {})
        or not candidates
        or candidate_attempts != list(range(1, len(candidates) + 1))
        or candidate_roles[0] != "target_seed"
        or any(
            role not in {
                "target_seed",
                "compression_floor",
                "expanded_bound",
                "refine",
            }
            for role in candidate_roles
        )
        or any(
            not required_candidate_fields.issubset(candidate)
            for candidate in candidates
        )
        or int_value(curve.get("candidate_count")) != len(candidates)
        or str(curve.get("shape") or "") not in {
            "single_point",
            "monotonic",
            "non_monotonic",
        }
        or int_value(curve.get("max_candidates")) < len(candidates)
        or int_value(retry_policy.get("max_final_output_retries")) != 1
        or not selected_candidate
        or selected_candidate not in candidates
        or not compatibility
        or compatibility.get("measurement_basis") != "sample_projection"
        or compatibility.get("assessment_contract") != BOUNDARY_ASSESSMENT_CONTRACT
        or not math.isfinite(chosen_crf)
        or not 0 <= chosen_crf <= 63
        or not review_fingerprint.startswith("cira3_")
        or calibration.get("current_review_artifact_fingerprint") != review_fingerprint
        or calibration.get("review_media_ready") is not True
        or calibration.get("boundary_review_media_ready") is not True
        or not str(calibration.get("job_id") or "").strip()
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation calibration does not prove unchanged measured full search"
        )


def _validate_plan_partition(
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
) -> None:
    derivation_assignments = tuple(sorted(
        (
            assignment
            for assignment in partition.assignments
            if assignment.role == "derivation"
        ),
        key=_assignment_sort_key,
    ))
    source_identity_by_item_id = {
        source.local_item_id: source.source_identity
        for source in partition.inventory_sources
    }
    commitment_source_identities_match = all(
        source_identity_by_item_id.get(commitment.local_item_id)
        == commitment.source_identity
        for commitment in plan.source_commitments
    )
    if (
        plan.partition_id != partition.partition_id
        or plan.partition_payload_sha256 != partition.payload_sha256
        or plan.selection_lock_sha256 != partition.selection_lock_sha256
        or plan.derivation_partition_sha256 != partition.derivation_partition_sha256
        or plan.assignments != derivation_assignments
        or not commitment_source_identities_match
    ):
        raise AV1ValidationDerivationError("AV1 derivation plan does not match the private partition")


def _assignment_by_id(
        plan: AV1ValidationDerivationPlan,
        assignment_id: str,
) -> AV1ValidationPartitionAssignment:
    assignment = next((item for item in plan.assignments if item.assignment_id == assignment_id), None)
    if assignment is None:
        raise AV1ValidationDerivationError("AV1 derivation assignment is not in the worklist")
    return assignment


def _partition_source_identity(
        partition: AV1ValidationPrivatePartition,
        local_item_id: int,
) -> str:
    source = next(
        (
            item
            for item in partition.inventory_sources
            if item.local_item_id == local_item_id
        ),
        None,
    )
    if source is None:
        raise AV1ValidationDerivationError("AV1 derivation assignment source is missing")
    return source.source_identity


def _assignment_sort_key(assignment: AV1ValidationPartitionAssignment) -> tuple[object, ...]:
    return assignment.cell_plan_id, assignment.ordinal, assignment.assignment_id


def _source_commitment_from_payload(
        payload: Mapping[str, Any],
) -> AV1ValidationDerivationSourceCommitment:
    _require_exact_keys(payload, {
        "schema", "schema_version", "contract_version", "assignment_id",
        "local_item_id", "source_identity", "source_sha256", "source_size_bytes",
        "evidence_summary_sha256",
    }, "derivation source commitment")
    if (
        payload.get("schema")
        != AV1_VALIDATION_DERIVATION_SOURCE_COMMITMENT_SCHEMA
        or int_value(payload.get("schema_version"))
        != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or payload.get("contract_version")
        != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation source commitment contract is invalid"
        )
    return AV1ValidationDerivationSourceCommitment(
        assignment_id=_required_text(
            payload.get("assignment_id"),
            "assignment ID",
        ),
        local_item_id=int_value(payload.get("local_item_id")),
        source_identity=_required_text(
            payload.get("source_identity"),
            "source identity",
        ),
        source_sha256=_required_text(
            payload.get("source_sha256"),
            "source digest",
        ),
        source_size_bytes=int_value(payload.get("source_size_bytes")),
        evidence_summary_sha256=_required_text(
            payload.get("evidence_summary_sha256"),
            "evidence digest",
        ),
    )


def _assignment_from_payload(payload: Mapping[str, Any]) -> AV1ValidationPartitionAssignment:
    _require_exact_keys(payload, {
        "assignment_id", "role", "cell_plan_id", "ordinal", "local_item_id", "traits",
        "intent_level", "source_token", "title_token", "series_token", "source_group_token",
        "compatibility_signature", "policy_signature", "target_video_bitrate_bps",
        "quality_metric", "quality_target", "minimum_quality_score", "evidence_summary_sha256",
    }, "derivation assignment")
    return AV1ValidationPartitionAssignment(
        assignment_id=_required_text(payload.get("assignment_id"), "assignment ID"),
        role=cast(Literal["holdout", "derivation"], _required_text(payload.get("role"), "role")),
        cell_plan_id=_required_text(payload.get("cell_plan_id"), "cell-plan ID"),
        ordinal=int_value(payload.get("ordinal")),
        local_item_id=int_value(payload.get("local_item_id")),
        traits=tuple(str(item) for item in object_list(payload.get("traits"))),
        intent_level=_required_text(payload.get("intent_level"), "intent"),
        source_token=_required_text(payload.get("source_token"), "source token"),
        title_token=_required_text(payload.get("title_token"), "title token"),
        series_token=_required_text(payload.get("series_token"), "series token"),
        source_group_token=_required_text(payload.get("source_group_token"), "source-group token"),
        compatibility_signature=_required_text(payload.get("compatibility_signature"), "compatibility signature"),
        policy_signature=_required_text(payload.get("policy_signature"), "policy signature"),
        target_video_bitrate_bps=int_value(payload.get("target_video_bitrate_bps")),
        quality_metric=_required_text(payload.get("quality_metric"), "quality metric"),
        quality_target=float_value(payload.get("quality_target")),
        minimum_quality_score=float_value(payload.get("minimum_quality_score")),
        evidence_summary_sha256=_required_text(payload.get("evidence_summary_sha256"), "evidence digest"),
    )


def _observation_projection_from_payload(
        payload: Mapping[str, Any],
) -> AV1ValidationDerivationObservationProjection:
    _require_exact_keys(payload, {
        "observation_id", "observation_payload_sha256", "local_item_id", "source_token",
        "title_token", "series_token", "source_group_token", "traits", "intent_level",
        "compatibility_signature", "policy_signature", "observation_compatibility_key",
        "observation_policy_hash", "verdict", "chosen_crf", "boundary_bitrate_bps",
        "quality_metric", "quality_target", "minimum_quality_score", "measured_quality_score",
        "quality_floor_met", "recorded_at",
    }, "derivation observation projection")
    return AV1ValidationDerivationObservationProjection(
        observation_id=_required_text(payload.get("observation_id"), "observation ID"),
        observation_payload_sha256=_required_text(payload.get("observation_payload_sha256"), "observation digest"),
        local_item_id=int_value(payload.get("local_item_id")),
        source_token=_required_text(payload.get("source_token"), "source token"),
        title_token=_required_text(payload.get("title_token"), "title token"),
        series_token=_required_text(payload.get("series_token"), "series token"),
        source_group_token=_required_text(payload.get("source_group_token"), "source-group token"),
        traits=tuple(str(item) for item in object_list(payload.get("traits"))),
        intent_level=_required_text(payload.get("intent_level"), "intent"),
        compatibility_signature=_required_text(payload.get("compatibility_signature"), "compatibility signature"),
        policy_signature=_required_text(payload.get("policy_signature"), "policy signature"),
        observation_compatibility_key=_required_text(payload.get("observation_compatibility_key"), "observation compatibility"),
        observation_policy_hash=_required_text(payload.get("observation_policy_hash"), "observation policy"),
        verdict=cast(Literal["acceptable", "unacceptable"], _required_text(payload.get("verdict"), "verdict")),
        chosen_crf=float_value(payload.get("chosen_crf")),
        boundary_bitrate_bps=int_value(payload.get("boundary_bitrate_bps")),
        quality_metric=_required_text(payload.get("quality_metric"), "quality metric"),
        quality_target=float_value(payload.get("quality_target")),
        minimum_quality_score=float_value(payload.get("minimum_quality_score")),
        measured_quality_score=float_value(payload.get("measured_quality_score")),
        quality_floor_met=payload.get("quality_floor_met") is True,
        recorded_at=_required_text(payload.get("recorded_at"), "recorded timestamp"),
    )


def _load_owner_only_json(
        path: Path,
        label: str,
        *,
        published_before: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    raw = _read_owner_only_bytes(
        path,
        label,
        published_before=published_before,
    )
    try:
        return object_dict(json.loads(raw.decode("utf-8"))), raw
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise AV1ValidationDerivationError(f"AV1 {label} is not valid JSON") from exc


def _write_owner_only(
        path: Path,
        data: bytes,
        *,
        before_publish: Callable[[], None] | None = None,
        published_before: str | None = None,
) -> None:
    assert_mediaforce_runtime_lock_held()
    if not path.name or path.name in {".", ".."}:
        raise AV1ValidationDerivationError(
            "AV1 private derivation artifact name is invalid"
        )
    parent_descriptor = -1
    temporary_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    temporary_created = False
    published = False
    try:
        canonical_parent, parent_descriptor = ensure_owner_only_directory(
            path.parent
        )
        _assert_owner_only_directory_descriptor_binding(
            canonical_parent,
            parent_descriptor,
            "private derivation artifact",
        )
        descriptor = os.open(
            temporary_name,
            flags,
            0o400,
            dir_fd=parent_descriptor,
        )
        temporary_created = True
        initial_info = os.fstat(descriptor)
        initial_path_info = os.stat(
            temporary_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(initial_info.st_mode)
            or initial_info.st_uid != os.getuid()
            or stat.S_IMODE(initial_info.st_mode) != 0o400
            or initial_info.st_nlink != 1
            or (initial_info.st_dev, initial_info.st_ino)
            != (initial_path_info.st_dev, initial_path_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                "AV1 private derivation artifact could not be created immutably"
            )
        offset = 0
        while offset < len(data):
            count = os.write(descriptor, data[offset:])
            if count <= 0:
                raise OSError("AV1 private derivation artifact write did not progress")
            offset += count
        _fsync_owner_only_artifact(descriptor)
        completed_info = os.fstat(descriptor)
        completed_path_info = os.stat(
            temporary_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            completed_info.st_uid != os.getuid()
            or stat.S_IMODE(completed_info.st_mode) != 0o400
            or completed_info.st_nlink != 1
            or completed_info.st_size != len(data)
            or (completed_info.st_dev, completed_info.st_ino)
            != (initial_info.st_dev, initial_info.st_ino)
            or (completed_path_info.st_dev, completed_path_info.st_ino)
            != (completed_info.st_dev, completed_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                "AV1 private derivation artifact changed during publication"
            )
        if before_publish is not None:
            before_publish()
        _assert_owner_only_directory_descriptor_binding(
            canonical_parent,
            parent_descriptor,
            "private derivation artifact",
        )
        try:
            _rename_owner_only_exclusive(
                parent_descriptor=parent_descriptor,
                source_name=temporary_name,
                destination_name=path.name,
            )
        except FileExistsError as exc:
            raise _AV1ValidationDerivationArtifactAlreadyExists(
                "AV1 private derivation artifact already exists"
            ) from exc
        published = True
        final_info = os.fstat(descriptor)
        final_path_info = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            final_info.st_uid != os.getuid()
            or stat.S_IMODE(final_info.st_mode) != 0o400
            or final_info.st_nlink != 1
            or final_info.st_size != len(data)
            or (final_info.st_dev, final_info.st_ino)
            != (completed_info.st_dev, completed_info.st_ino)
            or (final_path_info.st_dev, final_path_info.st_ino)
            != (final_info.st_dev, final_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                "AV1 private derivation artifact changed during publication"
            )
        if published_before is not None:
            _assert_owner_only_publication_before(
                path,
                "private derivation artifact",
                info=final_info,
                published_before=published_before,
            )
        try:
            _assert_owner_only_directory_descriptor_binding(
                canonical_parent,
                parent_descriptor,
                "private derivation artifact",
            )
        except AV1ValidationDerivationError:
            os.unlink(path.name, dir_fd=parent_descriptor)
            published = False
            os.fsync(parent_descriptor)
            raise
        os.fsync(parent_descriptor)
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            "AV1 private derivation artifact directory is unsafe"
        ) from exc
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 private derivation artifact could not be written safely"
        ) from exc
    finally:
        cleanup_error: OSError | None = None
        temporary_unlinked = False
        if parent_descriptor >= 0 and temporary_created and not published:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
                temporary_unlinked = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_error = exc
        if parent_descriptor >= 0 and temporary_unlinked:
            try:
                os.fsync(parent_descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if parent_descriptor >= 0:
            try:
                os.close(parent_descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise AV1ValidationDerivationError(
                "AV1 private derivation artifact cleanup failed"
            ) from cleanup_error


def _rename_owner_only_exclusive(
        *,
        parent_descriptor: int,
        source_name: str,
        destination_name: str,
) -> None:
    assert_mediaforce_runtime_lock_held()
    rename_exclusive(
        source_directory_descriptor=parent_descriptor,
        source_name=source_name,
        destination_directory_descriptor=parent_descriptor,
        destination_name=destination_name,
    )


def _fsync_owner_only_artifact(descriptor: int) -> None:
    assert_mediaforce_runtime_lock_held()
    fsync_durable_file(descriptor)


def _assert_owner_only_directory_descriptor_binding(
        path: Path,
        descriptor: int,
        label: str,
) -> None:
    try:
        descriptor_info = os.fstat(descriptor)
        path_info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise AV1ValidationDerivationError(
            f"AV1 {label} directory binding drifted"
        ) from exc
    if (
        not stat.S_ISDIR(descriptor_info.st_mode)
        or not stat.S_ISDIR(path_info.st_mode)
        or descriptor_info.st_uid != os.getuid()
        or path_info.st_uid != os.getuid()
        or stat.S_IMODE(descriptor_info.st_mode) & 0o077
        or stat.S_IMODE(path_info.st_mode) & 0o077
        or (descriptor_info.st_dev, descriptor_info.st_ino)
        != (path_info.st_dev, path_info.st_ino)
    ):
        raise AV1ValidationDerivationError(
            f"AV1 {label} directory binding drifted"
        )


def _fsync_owner_only_parent(path: Path, label: str) -> None:
    assert_mediaforce_runtime_lock_held()
    parent_descriptor = -1
    completed = False
    try:
        canonical_parent, parent_descriptor = open_stable_directory(
            path.parent,
            require_owner_only=True,
        )
        _assert_owner_only_directory_descriptor_binding(
            canonical_parent,
            parent_descriptor,
            label,
        )
        os.fsync(parent_descriptor)
        completed = True
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            f"AV1 {label} directory is unsafe"
        ) from exc
    except OSError as exc:
        raise AV1ValidationDerivationError(
            f"AV1 {label} directory could not be synced safely"
        ) from exc
    finally:
        if parent_descriptor >= 0:
            try:
                os.close(parent_descriptor)
            except OSError as exc:
                if completed:
                    raise AV1ValidationDerivationError(
                        f"AV1 {label} directory cleanup failed"
                    ) from exc


def _ensure_owner_only_directory(path: Path) -> None:
    assert_mediaforce_runtime_lock_held()
    try:
        _, descriptor = ensure_owner_only_directory(path)
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            "AV1 private derivation directory could not be created safely"
        ) from exc
    os.close(descriptor)


def bind_av1_validation_derivation_attempt_directory(
        directory: Path,
        *,
        plan_id: str,
        authorization_id: str,
) -> None:
    _bind_owner_only_directory(
        directory,
        kind="attempts",
        binding_id=plan_id,
        binding_digest=authorization_id,
    )


def _bind_owner_only_directory(
        path: Path,
        *,
        kind: str,
        binding_id: str,
        binding_digest: str,
) -> None:
    assert_mediaforce_runtime_lock_held()
    _ensure_owner_only_directory(path)
    payload = {
        "schema": AV1_VALIDATION_DERIVATION_DIRECTORY_BINDING_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_DERIVATION_CONTRACT_VERSION,
        "kind": kind,
        "binding_id": binding_id,
        "binding_digest": binding_digest,
    }
    binding_path = path / ".binding"
    try:
        _write_owner_only(binding_path, canonical_json_bytes(payload))
        return
    except _AV1ValidationDerivationArtifactAlreadyExists as write_error:
        try:
            current, raw = _load_owner_only_json(
                binding_path,
                "derivation directory binding",
            )
        except AV1ValidationDerivationError:
            raise write_error
    if current != payload or raw != canonical_json_bytes(payload):
        raise AV1ValidationDerivationError("AV1 derivation directory is bound to another artifact set")
    _fsync_owner_only_parent(binding_path, "derivation directory binding")


def _load_owner_only_directory_binding(
        path: Path,
        *,
        expected_kind: str,
) -> dict[str, Any]:
    _assert_owner_only_directory(path)
    binding_path = path / ".binding"
    payload, raw = _load_owner_only_json(binding_path, "derivation directory binding")
    _require_exact_keys(payload, {
        "schema",
        "schema_version",
        "contract_version",
        "kind",
        "binding_id",
        "binding_digest",
    }, "derivation directory binding")
    if (
        payload.get("schema") != AV1_VALIDATION_DERIVATION_DIRECTORY_BINDING_SCHEMA
        or int_value(payload.get("schema_version")) != AV1_VALIDATION_DERIVATION_SCHEMA_VERSION
        or payload.get("contract_version") != AV1_VALIDATION_DERIVATION_CONTRACT_VERSION
        or payload.get("kind") != expected_kind
        or not _SAFE_TOKEN_RE.fullmatch(str(payload.get("binding_id") or ""))
        or not _SAFE_TOKEN_RE.fullmatch(str(payload.get("binding_digest") or ""))
        or raw != canonical_json_bytes(payload)
    ):
        raise AV1ValidationDerivationError("AV1 derivation directory binding is invalid")
    return payload


def _assert_owner_only_directory(path: Path) -> None:
    try:
        _, descriptor = open_stable_directory(
            path,
            require_owner_only=True,
        )
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            "AV1 private derivation directory must be owner-only and stable"
        ) from exc
    os.close(descriptor)


def _read_owner_only_bytes(
        path: Path,
        label: str,
        *,
        published_before: str | None = None,
) -> bytes:
    parent_descriptor = -1
    descriptor = -1
    completed = False
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        _, parent_descriptor = open_stable_directory(
            path.parent,
            require_owner_only=True,
        )
        descriptor = os.open(
            path.name,
            flags,
            dir_fd=parent_descriptor,
        )
        info = os.fstat(descriptor)
        path_info = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
                not stat.S_ISREG(info.st_mode)
                or not stat.S_ISREG(path_info.st_mode)
                or (info.st_dev, info.st_ino)
                != (path_info.st_dev, path_info.st_ino)
        ):
            raise AV1ValidationDerivationError(f"AV1 {label} must be a regular file")
        if (
            info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o400
            or info.st_nlink != 1
        ):
            raise AV1ValidationDerivationError(
                f"AV1 {label} must be immutable and owner-only"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        final_info = os.fstat(descriptor)
        final_path_info = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            final_info.st_dev != info.st_dev
            or final_info.st_ino != info.st_ino
            or final_info.st_size != info.st_size
            or final_info.st_mtime_ns != info.st_mtime_ns
            or final_info.st_ctime_ns != info.st_ctime_ns
            or stat.S_IMODE(final_info.st_mode) != 0o400
            or final_info.st_nlink != 1
            or (final_path_info.st_dev, final_path_info.st_ino)
            != (final_info.st_dev, final_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                f"AV1 {label} changed while it was being read"
            )
        if published_before is not None:
            _assert_owner_only_publication_before(
                path,
                label,
                info=final_info,
                published_before=published_before,
            )
        result = b"".join(chunks)
        completed = True
        return result
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(f"AV1 {label} is unavailable") from exc
    except OSError as exc:
        raise AV1ValidationDerivationError(f"AV1 {label} is unavailable") from exc
    finally:
        cleanup_error: OSError | None = None
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = exc
        if parent_descriptor >= 0:
            try:
                os.close(parent_descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None and completed:
            raise AV1ValidationDerivationError(
                f"AV1 {label} cleanup failed"
            ) from cleanup_error


def _assert_owner_only_publication_before(
        path: Path,
        label: str,
        *,
        info: os.stat_result,
        published_before: str,
) -> None:
    deadline = _parse_timestamp(
        published_before,
        "derivation publication deadline",
    )
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = deadline - epoch
    deadline_ns = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )
    if _owner_only_publication_time_ns(info) >= deadline_ns:
        raise AV1ValidationDerivationPublicationDeadlineError(path, label)


def _owner_only_publication_time_ns(info: os.stat_result) -> int:
    return info.st_ctime_ns


def _ensure_av1_validation_derivation_terminal_artifact(
        directory: Path,
        *,
        kind: str,
        label: str,
        record: AV1ValidationDerivationTerminalRecord,
        before_publish: Callable[[], None] | None = None,
) -> Path:
    _bind_owner_only_directory(
        directory,
        kind=kind,
        binding_id=record.plan_id,
        binding_digest=record.authorization_id,
    )
    path = directory / f"{record.assignment_id}.json"
    if path.exists() or path.is_symlink():
        payload, raw = _load_owner_only_json(path, f"derivation {label}")
        existing = av1_validation_derivation_terminal_record_from_payload(
            payload,
            raw=raw,
        )
        if existing != record:
            raise AV1ValidationDerivationError(
                f"AV1 derivation {label} conflicts with the immutable artifact"
            )
        _fsync_owner_only_parent(path, f"derivation {label}")
        return path
    try:
        _write_owner_only(
            path,
            canonical_json_bytes(record.to_payload()),
            before_publish=before_publish,
        )
        return path
    except _AV1ValidationDerivationArtifactAlreadyExists:
        payload, raw = _load_owner_only_json(path, f"derivation {label}")
        existing = av1_validation_derivation_terminal_record_from_payload(
            payload,
            raw=raw,
        )
        if existing != record:
            raise AV1ValidationDerivationError(
                f"AV1 derivation {label} conflicts with the immutable artifact"
            )
        _fsync_owner_only_parent(path, f"derivation {label}")
        return path

def _derivation_id(kind: str, payload: Mapping[str, Any]) -> str:
    prefixes = {
        "plan": "av1vdplan1",
        "attempt": "av1vdattempt1",
        "terminal": "av1vdterminal1",
        "proposal": "av1vdproposal1",
        "review_claim": "av1vdreviewclaim1",
        "review": "av1vdreview1",
        "review_envelope": "av1vdreviewenv1",
        "lock_envelope": "av1vdlockenv1",
    }
    return f"{prefixes[kind]}_{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:32]}"


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationDerivationError(f"AV1 {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise AV1ValidationDerivationError(f"AV1 {label} must include a UTC offset")
    return parsed.astimezone(UTC)


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise AV1ValidationDerivationError(f"AV1 {label} is invalid")


def _require_git_object_id(value: str, label: str) -> None:
    if not _GIT_OBJECT_ID_RE.fullmatch(value):
        raise AV1ValidationDerivationError(f"AV1 {label} is invalid")


def _required_text(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise AV1ValidationDerivationError(f"AV1 {label} is required")
    return normalized


def _require_exact_keys(
        payload: Mapping[str, Any],
        expected: set[str],
        label: str,
) -> None:
    if set(payload) != expected:
        raise AV1ValidationDerivationError(f"AV1 {label} fields are invalid")
