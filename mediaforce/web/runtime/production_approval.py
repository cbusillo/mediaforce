"""Which sample approval governs a file, and the contract a production run records from it.

The rule, used by queueing, the run's saved plan, automatic publishing, manual publishing and the
folder page alike:

- A file is governed by the nearest folder holding an approval: the file itself, then its season,
  then its show, then the library.
- When no folder on that path holds an approval, a TV season's kind of content may govern it: one
  approval covering every season that measures the same (#1047). Unapproved old samples do not block it.
- A production run records the contract of the approval it was queued under. Queueing leaves out
  every file whose governing approval has a different contract, such as a season with its own
  sample inside a show run, so each file a run makes is governed by the approval the run recorded.
- A finished file publishes only while the approval governing it is current and its contract equals
  the one the run recorded. A changed or re-sampled approval means the file is made again.

Queueing and publishing both ask `governed_by`, so a run never records a contract that publishing
would reject for one of its files. Production actions find their approval with
`production_calibration`, so a season the page shows as following its show's approval queues
under it.

An approval governs the settings its sample tested: video, subtitles and the audio its tuning may
change. The show's sound choice and planning, including the series lifecycle mode, stay the owner's:
the sample records them but production uses the owner's current ones (`policy_with_owner_settings`).
Changing them leaves the approval and its contract as they are, so finished files still publish.
"""

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol, runtime_checkable

from mediaforce.core.audio_presets import PRESET_AUDIO_KEYS, owner_sound_keys
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import int_value, object_dict
from mediaforce.tuning.size_goals import operator_intent_from_policy
from mediaforce.web.runtime.folder_tuning_advice import calibration_policy_hash, review_gate
from mediaforce.web.runtime.production_holds import approval_identity

LoadCalibrationState = Callable[[MediaforceConfig, str], dict[str, Any] | None]

PRODUCTION_APPROVAL_CONTRACT_SCHEMA_VERSION = 1
APPROVAL_NEEDS_FRESH_SAMPLE_CODE = "approval_needs_fresh_sample"
NEARER_APPROVAL_CODE = "nearer_approval"
_APPROVAL_FIELDS = ("accepted_at", "accepted_sample_job_id", "accepted_policy_hash")


@dataclass(frozen=True, slots=True)
class EffectiveApproval:
    """The approval governing a file, with the contract a run queued under it records."""

    prefix: str
    calibration: dict[str, Any]
    contract: dict[str, Any] | None
    # The kind of content whose approval this is, or None for a folder's own approval.
    kind: str | None = None

    @property
    def source(self) -> str:
        return "kind" if self.kind else "folder"

    @property
    def current(self) -> bool:
        """Still approved, for the sample it was approved on, with the policy unchanged since."""
        accepted_hash = str(self.calibration.get("accepted_policy_hash") or "")
        return bool(
            review_gate(self.calibration).get("can_confirm_full")
            and accepted_hash
            and accepted_hash == calibration_policy_hash(self.calibration)
        )


@runtime_checkable
class KindApprovalSource(Protocol):
    """A calibration loader that also knows the kind approval covering a TV season, when one does."""

    def __call__(self, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None: ...

    def kind_approval(self, config: MediaforceConfig, rel_path: str) -> EffectiveApproval | None: ...


def _kind_approval(config: MediaforceConfig, rel_path: str, load: LoadCalibrationState) -> EffectiveApproval | None:
    return load.kind_approval(config, rel_path) if isinstance(load, KindApprovalSource) else None


@runtime_checkable
class KindSampleSource(Protocol):
    def following_kind(self, config: MediaforceConfig, prefix: str) -> str | None: ...


def following_kind(config: MediaforceConfig, prefix: str, load: LoadCalibrationState) -> str | None:
    return load.following_kind(config, prefix) if isinstance(load, KindSampleSource) else None


def approval_prefixes(rel_path: str) -> list[str]:
    """The folders that could govern a file, nearest first."""
    path = PurePosixPath(str(rel_path or "").strip().strip("/"))
    if not path.parts:
        return []
    return [path.as_posix(), *(parent.as_posix() for parent in path.parents if parent.as_posix() != ".")]


def holds_approval(calibration: Mapping[str, Any] | None) -> bool:
    """True once a sample here was approved, even when the approval is old or later re-sampled."""
    return any(str(object_dict(calibration).get(field) or "").strip() for field in _APPROVAL_FIELDS)


def effective_production_approval(
        config: MediaforceConfig,
        rel_path: str,
        load_calibration_state: LoadCalibrationState,
) -> EffectiveApproval | None:
    """The approval governing this file or folder, or None when nothing above it holds one."""
    for prefix in approval_prefixes(rel_path):
        calibration = load_calibration_state(config, prefix)
        if holds_approval(calibration):
            payload = object_dict(calibration)
            return EffectiveApproval(prefix, payload, production_approval_contract(payload))
    return _kind_approval(config, rel_path, load_calibration_state)


def production_calibration(
        config: MediaforceConfig,
        prefix: str,
        load_calibration_state: LoadCalibrationState,
) -> tuple[str, dict[str, Any] | None]:
    """The saved sample a production action here runs under, and the folder it was saved at.

    A kind's approval replaces unapproved samples. Otherwise, the nearest saved sample decides,
    including a sample awaiting review. The caller's review gate judges it.
    """
    approval = effective_production_approval(config, prefix, load_calibration_state)
    if approval is not None and approval.kind is not None:
        return approval.prefix, approval.calibration
    # A whole-library scope has no folder path, so only its own saved sample can govern it.
    for candidate in approval_prefixes(prefix) or [prefix]:
        calibration = load_calibration_state(config, candidate)
        if calibration is not None:
            return candidate, calibration
    return prefix, None


def cached_calibration_loader(load_calibration_state: LoadCalibrationState) -> LoadCalibrationState:
    """Read each folder's calibration once while one action checks many files."""
    return _CachedCalibrationLoader(load_calibration_state)


class _CachedCalibrationLoader:
    def __init__(self, load_calibration_state: LoadCalibrationState) -> None:
        self._load = load_calibration_state
        self._cache: dict[str, dict[str, Any] | None] = {}

    def __call__(self, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
        if prefix not in self._cache:
            self._cache[prefix] = self._load(config, prefix)
        return self._cache[prefix]

    def kind_approval(self, config: MediaforceConfig, rel_path: str) -> EffectiveApproval | None:
        return _kind_approval(config, rel_path, self._load)

    def following_kind(self, config: MediaforceConfig, prefix: str) -> str | None:
        return following_kind(config, prefix, self._load)


def production_approval_contract(calibration: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a production run records about its approval; None for an approval too old to say."""
    sample_job_id = str(calibration.get("accepted_sample_job_id") or "").strip()
    policy_hash = str(calibration.get("accepted_policy_hash") or "").strip()
    sample_item = object_dict(calibration.get("sample_item"))
    operator_intent = object_dict(sample_item.get("resolved_operator_intent"))
    request = object_dict(operator_intent.get("request"))
    if not request:
        policy = object_dict(calibration.get("policy"))
        intent = operator_intent_from_policy(
            object_dict(policy.get("video")),
            audio_policy=object_dict(policy.get("audio")),
            subtitle_policy=object_dict(policy.get("subtitle")),
        )
        if intent.requires_confirmation:
            return None
        request = intent.request_payload()
    if not sample_job_id or not policy_hash or not request:
        return None
    # A show's sound choice is part of what the owner asked for, so a new choice counts as a changed request.
    audio_preset = str(object_dict(object_dict(calibration.get("policy")).get("audio")).get("preset") or "")
    if audio_preset:
        request = {**request, "audio_preset": audio_preset}
    return {
        "schema_version": PRODUCTION_APPROVAL_CONTRACT_SCHEMA_VERSION,
        "sample_job_id": sample_job_id,
        "policy_hash": policy_hash,
        "operator_intent_hash": f"sha256:{stable_json_hash(request)}",
        "operator_intent": request,
    }


def valid_production_approval_contract(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    contract = object_dict(payload)
    request = object_dict(contract.get("operator_intent"))
    if (
            int_value(contract.get("schema_version")) != PRODUCTION_APPROVAL_CONTRACT_SCHEMA_VERSION
            or not str(contract.get("sample_job_id") or "").strip()
            or not str(contract.get("policy_hash") or "").strip()
            or not request
    ):
        return None
    expected_hash = f"sha256:{stable_json_hash(request)}"
    if str(contract.get("operator_intent_hash") or "").strip() != expected_hash:
        return None
    return contract


def governed_by(approval: EffectiveApproval | None, contract: Mapping[str, Any] | None) -> bool:
    """True when the approval governing a file is the one that recorded this contract."""
    return approval is not None and approval.contract is not None and approval.contract == contract


def production_approval_matches(
        approval: EffectiveApproval | None,
        manifest: Mapping[str, Any],
        item: Mapping[str, Any],
) -> bool:
    """True when the approval governing the file now is the one its run was queued under.

    A run saved before runs recorded their approval matches only when the file's policy is the
    approved one.
    """
    if approval is None or not approval.current:
        return False
    recorded = object_dict(manifest.get("selection")).get("production_approval_contract")
    if recorded:
        return governed_by(approval, valid_production_approval_contract(object_dict(recorded)))
    policy = object_dict(item.get("resolved_policy"))
    accepted_hash = str(approval.calibration.get("accepted_policy_hash") or "")
    return bool(policy) and calibration_policy_hash({"policy": policy}) == accepted_hash


def policy_with_owner_settings(
        sample_policy: Mapping[str, Any],
        config: MediaforceConfig,
        prefix: str,
) -> dict[str, Any]:
    """The part of an approved sample's policy that the sample owns, to apply over this folder's settings.

    Planning and the sound-choice audio keys are left out. Applied as the folder's top override, the
    owner's current sound and lifecycle still come through from the folder, show and library below
    it; saved, it leaves the folder's own sound and planning as they were and inherits the rest.
    """
    sample_audio = object_dict(sample_policy.get("audio"))
    # Under a sound choice every preset key is the owner's, even where the sample saw none.
    owner_keys = owner_sound_keys(sample_audio) | (
        frozenset(PRESET_AUDIO_KEYS) if config.has_sound_choice(prefix) else frozenset()
    )
    audio = {key: value for key, value in sample_audio.items() if key not in owner_keys}
    policy = {section: values for section, values in sample_policy.items() if section not in {"planning", "audio"}}
    if audio:
        policy["audio"] = audio
    return copy.deepcopy(policy)


def production_approval_identity(calibration: Mapping[str, Any]) -> str:
    """The approval a production run is queued under, as recorded on the files it holds back."""
    payload = object_dict(calibration)
    return approval_identity(payload, production_approval_contract(payload))


def approval_needs_fresh_sample_response() -> dict[str, Any]:
    return {
        "ok": False,
        "code": APPROVAL_NEEDS_FRESH_SAMPLE_CODE,
        "message": (
            "This approval was saved by an older version of Mediaforce and does not record what was "
            "approved, so nothing was queued. Make a fresh sample here and approve it, then start again."
        ),
    }


def nearer_approval_reason(approval: EffectiveApproval, rel_path: str) -> str:
    """Why a run leaves out a file that a nearer approval governs."""
    if approval.prefix == str(rel_path or "").strip().strip("/"):
        return "It has its own approved settings. Start it on its own."
    name = PurePosixPath(approval.prefix).name
    return f"{name} has its own approved settings. Start {name} on its own."
