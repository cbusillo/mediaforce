from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from pathlib import Path
import json
import os
import stat

import pytest

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    assert_av1_v4r3_ordinal_window_terminal,
)
from mediaforce.tuning.av1_validation_v4r4_contract import (
    av1_v4r4_identity_domain,
    av1_v4r4_ordinal_layout,
)
from mediaforce.tuning.av1_validation_v4r4_diagnostics import (
    AV1V4R4ConflictDiagnostic,
)
from mediaforce.tuning.av1_validation_v4r4_outcome import (
    assert_av1_v4r4_terminal,
    build_av1_v4r4_outcome,
)
from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    AV1V4R4OrdinalRegistryBinding,
    AV1V4R4OrdinalRegistryError,
    assert_av1_v4r4_ordinal_registry,
    assert_av1_v4r4_ordinal_registry_terminal_publication,
    av1_v4r4_ordinal_registry_binding,
    av1_v4r4_ordinal_registry_hmac_id,
    build_av1_v4r4_ordinal_registry_plan,
    build_av1_v4r4_ordinal_registry_terminal_publication,
    deserialize_av1_v4r4_ordinal_registry_terminal_publication,
    initialize_av1_v4r4_ordinal_registry,
    load_av1_v4r4_ordinal_registry_preparation,
    serialize_av1_v4r4_ordinal_registry_claim,
    serialize_av1_v4r4_ordinal_registry_grant,
    serialize_av1_v4r4_ordinal_registry_outcome_publication,
    serialize_av1_v4r4_ordinal_registry_started,
    serialize_av1_v4r4_ordinal_registry_terminal_publication,
    publish_av1_v4r4_ordinal_registry_claim,
    publish_av1_v4r4_ordinal_registry_grant,
    publish_av1_v4r4_ordinal_registry_grant_with_status,
    publish_av1_v4r4_ordinal_registry_outcome,
    publish_av1_v4r4_ordinal_registry_plan,
    publish_av1_v4r4_ordinal_registry_runner_admission_started,
    publish_av1_v4r4_ordinal_registry_terminal,
    reconcile_av1_v4r4_ordinal_registry,
)


class TickClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


_GRANT_VALID_UNTIL = "2026-08-10T06:00:00Z"


def _binding(tmp_path: Path) -> AV1V4R4OrdinalRegistryBinding:
    registry = tmp_path / "registry"
    initialize_av1_v4r4_ordinal_registry(registry)
    binding = av1_v4r4_ordinal_registry_binding(registry, key=b"k" * 32)
    assert binding.registry_id == av1_v4r4_ordinal_registry_hmac_id(
        registry,
        key=b"k" * 32,
    )
    return binding


def test_registry_binding_requires_keyed_factory(tmp_path: Path) -> None:
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="must be keyed"):
        AV1V4R4OrdinalRegistryBinding(
            registry=tmp_path / "registry",
            registry_id="av1v4r4ordreg_" + "0" * 64,
        )


def _plan(binding: AV1V4R4OrdinalRegistryBinding) -> dict[str, object]:
    return build_av1_v4r4_ordinal_registry_plan(
        registry_id=binding.registry_id,
        plan_opens_at="2026-08-10T00:00:00Z",
        plan_closes_at="2026-08-11T00:00:00Z",
    )


def _diagnostic(
    *,
    ordinal: int = 3,
    reason: str = "target_band_violates_quality_floor",
    quality: str = "within_relax_step",
    size: str = "beyond_projection_tolerance_within_source_cap",
) -> AV1V4R4ConflictDiagnostic:
    layout = av1_v4r4_ordinal_layout()[ordinal - 1]
    return AV1V4R4ConflictDiagnostic(
        ordinal=ordinal,
        asset_id=str(layout["asset_id"]),
        failure_search_reason=reason,
        conflict_quality_gap_band=quality,
        conflict_size_gap_band=size,
    )


def _selected(ordinal: int) -> dict[str, object]:
    if ordinal == 2:
        return build_av1_v4r4_outcome(
            ordinal=ordinal,
            disposition="selected_success",
            guided_probe_status="size_band_miss",
            guided_fallback_selected=True,
        )
    return build_av1_v4r4_outcome(ordinal=ordinal, disposition="selected_success")


def _conflict(ordinal: int = 3) -> dict[str, object]:
    return build_av1_v4r4_outcome(
        ordinal=ordinal,
        disposition="bounded_quality_conflict",
        conflict_diagnostic=_diagnostic(ordinal=ordinal),
    )


def _fatal(ordinal: int) -> dict[str, object]:
    return build_av1_v4r4_outcome(ordinal=ordinal, disposition="fatal_failure")


def _rebind_for_test(
    payload: dict[str, object],
    *,
    id_field: str,
    id_prefix: str,
    domain: str,
) -> dict[str, object]:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {id_field, "payload_sha256"}
    }
    payload[id_field] = id_prefix + stable_json_hash(
        {"domain": domain, "payload": semantic}
    )[:32]
    without_sha = {key: value for key, value in payload.items() if key != "payload_sha256"}
    payload["payload_sha256"] = f"sha256:{stable_json_hash(without_sha)}"
    return json.loads(canonical_json_bytes(payload))


def _publish_plan(
    tmp_path: Path,
) -> tuple[AV1V4R4OrdinalRegistryBinding, dict[str, object], TickClock]:
    from tests.test_av1_validation_v4r4_execution_authority import _prepared_chain

    prepared = _prepared_chain(tmp_path)
    return prepared.binding, prepared.plan, prepared.clock


def _write_private_execution_grant(
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: dict[str, object],
    grant: dict[str, object],
) -> dict[str, object]:
    from tests.test_av1_validation_v4r4_execution_authority import (
        _execution_grant,
        _private_canonical_bytes,
    )

    request, preflight = load_av1_v4r4_ordinal_registry_preparation(
        binding=binding,
        plan=plan,
    )
    execution_grant = _execution_grant(
        plan,
        grant,
        qualification_request=request,
        execution_preflight=preflight,
    )
    path = binding.registry / f"v4r4-ordinal-{grant['ordinal']:02d}-execution-grant.json"
    if not path.exists():
        path.write_bytes(_private_canonical_bytes(execution_grant))
        path.chmod(0o600)
    return execution_grant


def _execution_and_admission_for_test(
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: dict[str, object],
    grant: dict[str, object],
    claim: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    from tests.test_av1_validation_v4r4_execution_authority import (
        _execution_claim,
        _execution_grant,
    )
    from tests.test_av1_validation_v4r4_runner_admission import _admission

    request, preflight = load_av1_v4r4_ordinal_registry_preparation(
        binding=binding,
        plan=plan,
    )
    exec_grant = _execution_grant(
        plan,
        grant,
        qualification_request=request,
        execution_preflight=preflight,
    )
    exec_claim = _execution_claim(plan, claim, exec_grant)
    admission = _admission(plan, grant, claim, exec_grant, exec_claim)
    return exec_grant, exec_claim, admission


def _write_private_execution_artifacts(
    binding: AV1V4R4OrdinalRegistryBinding,
    ordinal: int,
    exec_grant: dict[str, object],
    exec_claim: dict[str, object],
) -> None:
    from tests.test_av1_validation_v4r4_execution_authority import _private_canonical_bytes

    for filename, payload in (
        (f"v4r4-ordinal-{ordinal:02d}-execution-grant.json", exec_grant),
        (f"v4r4-ordinal-{ordinal:02d}-execution-claim.json", exec_claim),
    ):
        path = binding.registry / filename
        path.write_bytes(_private_canonical_bytes(payload))
        path.chmod(0o600)


def _publish_admitted_started(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: dict[str, object],
    grant: dict[str, object],
    claim: dict[str, object],
    clock: TickClock,
) -> dict[str, object]:
    ordinal = int(grant["ordinal"])
    exec_grant, exec_claim, admission = _execution_and_admission_for_test(
        binding,
        plan,
        grant,
        claim,
    )
    _write_private_execution_artifacts(binding, ordinal, exec_grant, exec_claim)
    return publish_av1_v4r4_ordinal_registry_runner_admission_started(
        binding=binding,
        plan=plan,
        sequencing_grant=grant,
        sequencing_claim=claim,
        execution_grant=exec_grant,
        execution_claim=exec_claim,
        admission=admission,
        clock=clock,
    ).started


def _run_ordinal(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    plan: dict[str, object],
    clock: TickClock,
    ordinal: int,
    outcome: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object], object]:
    grant = publish_av1_v4r4_ordinal_registry_grant(
        binding=binding,
        plan=plan,
        ordinal=ordinal,
        clock=clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    _write_private_execution_grant(binding, plan, grant)
    claim = publish_av1_v4r4_ordinal_registry_claim(
        binding=binding,
        plan=plan,
        grant=grant,
        clock=clock,
    )
    started = _publish_admitted_started(
        binding=binding,
        plan=plan,
        grant=grant,
        claim=claim,
        clock=clock,
    )
    publication = publish_av1_v4r4_ordinal_registry_outcome(
        binding=binding,
        plan=plan,
        started=started,
        outcome=outcome,
        clock=clock,
    )
    return grant, claim, started, publication


def test_ordinal_four_eligible_after_ordinal_three_bounded_conflict_requires_fresh_grant(
    tmp_path: Path,
) -> None:
    binding, plan, clock = _publish_plan(tmp_path)

    grant1, claim1, _started1, _publication1 = _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=1,
        outcome=_selected(1),
    )
    _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=2,
        outcome=_selected(2),
    )
    _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=3,
        outcome=_conflict(3),
    )

    assert publish_av1_v4r4_ordinal_registry_claim(
        binding=binding,
        plan=plan,
        grant=grant1,
        clock=clock,
    ) == claim1

    grant4 = publish_av1_v4r4_ordinal_registry_grant(
        binding=binding,
        plan=plan,
        ordinal=4,
        clock=clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    _write_private_execution_grant(binding, plan, grant4)
    claim4 = publish_av1_v4r4_ordinal_registry_claim(
        binding=binding,
        plan=plan,
        grant=grant4,
        clock=clock,
    )

    assert grant4["ordinal"] == 4
    assert claim4["ordinal"] == 4
    assert grant4["grant_id"] != grant1["grant_id"]
    assert claim4["claim_id"] != claim1["claim_id"]
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="immutable"):
        _publish_admitted_started(
            binding=binding,
            plan=plan,
            grant=grant4,
            claim=claim1,
            clock=clock,
        )
    started4 = _publish_admitted_started(
        binding=binding,
        plan=plan,
        grant=grant4,
        claim=claim4,
        clock=clock,
    )
    publish_av1_v4r4_ordinal_registry_outcome(
        binding=binding,
        plan=plan,
        started=started4,
        outcome=_selected(4),
        clock=clock,
    )
    assert reconcile_av1_v4r4_ordinal_registry(
        binding=binding,
        plan=plan,
        clock=clock,
    ) == 4


def test_complete_terminal_sums_counts_and_rejects_revision_three_reader(
    tmp_path: Path,
) -> None:
    binding, plan, clock = _publish_plan(tmp_path)

    for ordinal in range(1, 9):
        _run_ordinal(
            binding=binding,
            plan=plan,
            clock=clock,
            ordinal=ordinal,
            outcome=_conflict(3) if ordinal == 3 else _selected(ordinal),
        )

    terminal_publication = publish_av1_v4r4_ordinal_registry_terminal(
        binding=binding,
        plan=plan,
        clock=clock,
    )
    terminal = terminal_publication["terminal"]

    assert terminal["termination_kind"] == "traversal_complete"
    assert terminal["verdict"] == "policy_conflicted"
    assert terminal["selected_count"] == 7
    assert terminal["conflict_count"] == 1
    assert terminal["traversed_count"] == 8
    assert terminal["conflict_reason_counts"] == {
        "all_candidates_violate_quality_floor": 0,
        "target_band_violates_quality_floor": 1,
        "target_requires_crossing_quality_floor": 0,
    }
    assert terminal["quality_gap_band_counts"]["within_relax_step"] == 1
    assert terminal["size_gap_band_counts"][
        "beyond_projection_tolerance_within_source_cap"
    ] == 1
    assert terminal["map_result"] == "content_class_target_normalization"
    assert terminal["hypothesis_h1"] == "supported"
    assert_av1_v4r4_terminal(terminal)
    assert_av1_v4r4_ordinal_registry_terminal_publication(terminal_publication)
    with pytest.raises(Exception):
        assert_av1_v4r3_ordinal_window_terminal(terminal_publication)


def test_fatal_failure_absorbs_and_blocks_later_ordinal(tmp_path: Path) -> None:
    binding, plan, clock = _publish_plan(tmp_path)

    _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=1,
        outcome=_selected(1),
    )
    publication = _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=2,
        outcome=_fatal(2),
    )[3]
    terminal = publication.terminal_publication["terminal"]

    assert terminal["termination_kind"] == "fatal_failure"
    assert terminal["traversed_count"] == 1
    assert terminal["verdict"] == "indeterminate"
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="sealed"):
        publish_av1_v4r4_ordinal_registry_grant(
            binding=binding,
            plan=plan,
            ordinal=3,
            clock=clock,
            valid_until=_GRANT_VALID_UNTIL,
        )


def test_same_second_started_outcome_is_valid_and_reconciles(tmp_path: Path) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    instant = datetime(2026, 8, 10, 4, 0, 5, tzinfo=UTC)
    clock.current = instant
    grant = publish_av1_v4r4_ordinal_registry_grant(
        binding=binding,
        plan=plan,
        ordinal=1,
        clock=clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    _write_private_execution_grant(binding, plan, grant)
    clock.current = instant
    claim = publish_av1_v4r4_ordinal_registry_claim(
        binding=binding,
        plan=plan,
        grant=grant,
        clock=clock,
    )
    clock.current = instant
    started = _publish_admitted_started(
        binding=binding,
        plan=plan,
        grant=grant,
        claim=claim,
        clock=clock,
    )
    clock.current = instant

    publication = publish_av1_v4r4_ordinal_registry_outcome(
        binding=binding,
        plan=plan,
        started=started,
        outcome=_selected(1),
        clock=clock,
    )

    assert publication.outcome_publication["outcome_at"] == started["started_at"]
    assert reconcile_av1_v4r4_ordinal_registry(
        binding=binding,
        plan=plan,
        clock=clock,
    ) == 1


def test_absorbing_outcome_replay_is_idempotent_after_terminal(
    tmp_path: Path,
) -> None:
    binding, plan, clock = _publish_plan(tmp_path)

    _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=1,
        outcome=_selected(1),
    )
    _grant2, _claim2, started2, first = _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=2,
        outcome=_fatal(2),
    )
    assert first.created is True
    assert first.terminal_publication is not None

    replay = publish_av1_v4r4_ordinal_registry_outcome(
        binding=binding,
        plan=plan,
        started=started2,
        outcome=_fatal(2),
        clock=clock,
    )

    assert replay.created is False
    assert replay.outcome_publication == first.outcome_publication
    assert replay.terminal_publication == first.terminal_publication
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="duplicate outcome"):
        publish_av1_v4r4_ordinal_registry_outcome(
            binding=binding,
            plan=plan,
            started=started2,
            outcome=_selected(2),
            clock=clock,
        )


def test_reconcile_accepts_absorbing_current_but_later_admission_rejects(
    tmp_path: Path,
) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=1,
        outcome=_fatal(1),
    )

    assert reconcile_av1_v4r4_ordinal_registry(
        binding=binding,
        plan=plan,
        clock=clock,
    ) == 1
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="sealed"):
        publish_av1_v4r4_ordinal_registry_grant(
            binding=binding,
            plan=plan,
            ordinal=2,
            clock=clock,
            valid_until=_GRANT_VALID_UNTIL,
        )


def test_incomplete_publication_gap_duplicate_and_clock_regression_seal(
    tmp_path: Path,
) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    grant1 = publish_av1_v4r4_ordinal_registry_grant(
        binding=binding,
        plan=plan,
        ordinal=1,
        clock=clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    _write_private_execution_grant(binding, plan, grant1)
    claim1 = publish_av1_v4r4_ordinal_registry_claim(
        binding=binding,
        plan=plan,
        grant=grant1,
        clock=clock,
    )

    assert publish_av1_v4r4_ordinal_registry_claim(
        binding=binding,
        plan=plan,
        grant=grant1,
        clock=clock,
    ) == claim1
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="incomplete publication"):
        reconcile_av1_v4r4_ordinal_registry(binding=binding, plan=plan, clock=clock)
    terminal = publish_av1_v4r4_ordinal_registry_terminal(
        binding=binding,
        plan=plan,
        clock=clock,
    )
    assert terminal["terminal"]["termination_kind"] == "incomplete_seal"
    assert terminal["terminal"]["outcome_count"] == 0

    gap_binding, gap_plan, gap_clock = _publish_plan(tmp_path / "gap")
    gap_registry = gap_binding.registry
    _run_ordinal(
        binding=gap_binding,
        plan=gap_plan,
        clock=gap_clock,
        ordinal=1,
        outcome=_selected(1),
    )
    ordinal3_claim = gap_registry / "v4r4-ordinal-03-sequencing-claim.json"
    ordinal3_claim.write_text("{}\n")
    ordinal3_claim.chmod(0o600)
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="ordinal gap"):
        reconcile_av1_v4r4_ordinal_registry(
            binding=gap_binding,
            plan=gap_plan,
            clock=gap_clock,
        )

    dup_binding, dup_plan, dup_clock = _publish_plan(tmp_path / "dup")
    _grant2, _claim2, started2, _publication2 = _run_ordinal(
        binding=dup_binding,
        plan=dup_plan,
        clock=dup_clock,
        ordinal=1,
        outcome=_selected(1),
    )
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="duplicate outcome"):
        publish_av1_v4r4_ordinal_registry_outcome(
            binding=dup_binding,
            plan=dup_plan,
            started=started2,
            outcome=_fatal(1),
            clock=dup_clock,
        )

    clock_binding, clock_plan, clock_clock = _publish_plan(tmp_path / "clock")
    grant = publish_av1_v4r4_ordinal_registry_grant(
        binding=clock_binding,
        plan=clock_plan,
        ordinal=1,
        clock=clock_clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    _write_private_execution_grant(clock_binding, clock_plan, grant)
    claim = publish_av1_v4r4_ordinal_registry_claim(
        binding=clock_binding,
        plan=clock_plan,
        grant=grant,
        clock=clock_clock,
    )
    clock_clock.current = datetime(2026, 8, 10, 4, 0, 1, tzinfo=UTC)
    with pytest.raises(
        AV1V4R4OrdinalRegistryError,
        match="execution/admission chain is invalid",
    ):
        _publish_admitted_started(
            binding=clock_binding,
            plan=clock_plan,
            grant=grant,
            claim=claim,
            clock=clock_clock,
        )


def test_registry_rejects_cross_revision_files_and_pathful_artifacts(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    r3_file = binding.registry / "ordinal_01.claim.json"
    r3_file.write_text("{}\n")
    r3_file.chmod(0o600)

    with pytest.raises(AV1V4R4OrdinalRegistryError, match="unsupported artifact"):
        assert_av1_v4r4_ordinal_registry(binding.registry)

    r3_file.unlink()
    plan = _plan(binding)
    pathful = deepcopy(plan)
    pathful["ordinal_targets"][0]["asset_id"] = "/tmp/media.mov"
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="target map"):
        publish_av1_v4r4_ordinal_registry_plan(binding=binding, plan=pathful)


def test_loaders_reject_payload_ordinal_mismatched_to_filename(
    tmp_path: Path,
) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=1,
        outcome=_selected(1),
    )
    grant2 = publish_av1_v4r4_ordinal_registry_grant(
        binding=binding,
        plan=plan,
        ordinal=2,
        clock=clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    _write_private_execution_grant(binding, plan, grant2)
    claim2 = publish_av1_v4r4_ordinal_registry_claim(
        binding=binding,
        plan=plan,
        grant=grant2,
        clock=clock,
    )
    started2 = _publish_admitted_started(
        binding=binding,
        plan=plan,
        grant=grant2,
        claim=claim2,
        clock=clock,
    )
    publish_av1_v4r4_ordinal_registry_outcome(
        binding=binding,
        plan=plan,
        started=started2,
        outcome=_selected(2),
        clock=clock,
    )

    slot3 = binding.registry / "v4r4-ordinal-03-sequencing-grant.json"
    slot3.write_bytes(serialize_av1_v4r4_ordinal_registry_grant(grant2))
    slot3.chmod(0o600)
    for suffix in (
        "sequencing-claim",
        "execution-grant",
        "execution-claim",
        "runner-admission",
        "started",
        "outcome-publication",
    ):
        copied = binding.registry / f"v4r4-ordinal-03-{suffix}.json"
        copied.write_bytes(
            (binding.registry / f"v4r4-ordinal-02-{suffix}.json").read_bytes()
        )
        copied.chmod(0o600)

    with pytest.raises(AV1V4R4OrdinalRegistryError, match="filename ordinal"):
        reconcile_av1_v4r4_ordinal_registry(
            binding=binding,
            plan=plan,
            clock=clock,
        )


def test_terminal_load_recomputes_against_validated_registry_prefix(
    tmp_path: Path,
) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    for ordinal in range(1, 3):
        _run_ordinal(
            binding=binding,
            plan=plan,
            clock=clock,
            ordinal=ordinal,
            outcome=_selected(ordinal),
        )
    terminal_publication = publish_av1_v4r4_ordinal_registry_terminal(
        binding=binding,
        plan=plan,
        clock=clock,
    )
    stale = build_av1_v4r4_ordinal_registry_terminal_publication(
        plan=plan,
        terminal=terminal_publication["terminal"],
        terminal_at="2026-08-10T00:59:59Z",
    )
    terminal_file = binding.registry / "v4r4-ordinal-registry-terminal.json"
    terminal_file.write_bytes(
        serialize_av1_v4r4_ordinal_registry_terminal_publication(stale)
    )
    terminal_file.chmod(0o600)

    with pytest.raises(AV1V4R4OrdinalRegistryError, match="terminal is stale"):
        publish_av1_v4r4_ordinal_registry_terminal(
            binding=binding,
            plan=plan,
            clock=clock,
        )


def test_terminal_rejects_rebound_execution_chain_after_publication(
    tmp_path: Path,
) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    for ordinal in range(1, 3):
        _run_ordinal(
            binding=binding,
            plan=plan,
            clock=clock,
            ordinal=ordinal,
            outcome=_selected(ordinal),
        )
    publish_av1_v4r4_ordinal_registry_terminal(
        binding=binding,
        plan=plan,
        clock=clock,
    )

    from tests.test_av1_validation_v4r4_execution_authority import (
        _private_canonical_bytes,
        _rebind_execution_claim,
    )

    execution_claim_file = binding.registry / "v4r4-ordinal-02-execution-claim.json"
    execution_claim = json.loads(execution_claim_file.read_bytes())
    execution_claim = _rebind_execution_claim(
        {**execution_claim, "owner_principal": "rebound.mediaforce"}
    )
    execution_claim_file.write_bytes(_private_canonical_bytes(execution_claim))
    execution_claim_file.chmod(0o600)

    with pytest.raises(AV1V4R4OrdinalRegistryError, match="terminal is stale"):
        publish_av1_v4r4_ordinal_registry_terminal(
            binding=binding,
            plan=plan,
            clock=clock,
        )


def test_loaded_grant_claim_and_start_times_must_fit_windows(
    tmp_path: Path,
) -> None:
    grant_binding, grant_plan, grant_clock = _publish_plan(tmp_path / "grant")
    grant = publish_av1_v4r4_ordinal_registry_grant(
        binding=grant_binding,
        plan=grant_plan,
        ordinal=1,
        clock=grant_clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    bad_grant = deepcopy(grant)
    bad_grant["valid_until"] = "2026-08-13T00:00:00Z"
    bad_grant["admission_closes_at"] = "2026-08-13T00:00:00Z"
    bad_grant = _rebind_for_test(
        bad_grant,
        id_field="grant_id",
        id_prefix="av1v4r4ordgrant_",
        domain=av1_v4r4_identity_domain("ordinal-registry-grant"),
    )
    grant_file = grant_binding.registry / "v4r4-ordinal-01-sequencing-grant.json"
    grant_file.write_bytes(serialize_av1_v4r4_ordinal_registry_grant(bad_grant))
    grant_file.chmod(0o600)
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="grant window"):
        publish_av1_v4r4_ordinal_registry_claim(
            binding=grant_binding,
            plan=grant_plan,
            grant=bad_grant,
            clock=grant_clock,
        )

    claim_binding, claim_plan, claim_clock = _publish_plan(tmp_path / "claim")
    claim_grant = publish_av1_v4r4_ordinal_registry_grant(
        binding=claim_binding,
        plan=claim_plan,
        ordinal=1,
        clock=claim_clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    _write_private_execution_grant(claim_binding, claim_plan, claim_grant)
    claim = publish_av1_v4r4_ordinal_registry_claim(
        binding=claim_binding,
        plan=claim_plan,
        grant=claim_grant,
        clock=claim_clock,
    )
    bad_claim = deepcopy(claim)
    bad_claim["claimed_at"] = "2026-08-10T01:00:00Z"
    bad_claim = _rebind_for_test(
        bad_claim,
        id_field="claim_id",
        id_prefix="av1v4r4ordclaim_",
        domain=av1_v4r4_identity_domain("ordinal-registry-claim"),
    )
    claim_file = claim_binding.registry / "v4r4-ordinal-01-sequencing-claim.json"
    claim_file.write_bytes(serialize_av1_v4r4_ordinal_registry_claim(bad_claim))
    claim_file.chmod(0o600)
    with pytest.raises(
        AV1V4R4OrdinalRegistryError,
        match="execution/admission chain is invalid",
    ):
        _publish_admitted_started(
            binding=claim_binding,
            plan=claim_plan,
            grant=claim_grant,
            claim=bad_claim,
            clock=claim_clock,
        )

    start_binding, start_plan, start_clock = _publish_plan(tmp_path / "start")
    start_grant = publish_av1_v4r4_ordinal_registry_grant(
        binding=start_binding,
        plan=start_plan,
        ordinal=1,
        clock=start_clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    _write_private_execution_grant(start_binding, start_plan, start_grant)
    start_claim = publish_av1_v4r4_ordinal_registry_claim(
        binding=start_binding,
        plan=start_plan,
        grant=start_grant,
        clock=start_clock,
    )
    started = _publish_admitted_started(
        binding=start_binding,
        plan=start_plan,
        grant=start_grant,
        claim=start_claim,
        clock=start_clock,
    )
    bad_started = deepcopy(started)
    bad_started["started_at"] = "2026-08-10T01:00:00Z"
    bad_started = _rebind_for_test(
        bad_started,
        id_field="started_id",
        id_prefix="av1v4r4ordstart_",
        domain=av1_v4r4_identity_domain("ordinal-registry-started"),
    )
    started_file = start_binding.registry / "v4r4-ordinal-01-started.json"
    started_file.write_bytes(
        serialize_av1_v4r4_ordinal_registry_started(bad_started)
    )
    started_file.chmod(0o600)
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="start is outside"):
        publish_av1_v4r4_ordinal_registry_outcome(
            binding=start_binding,
            plan=start_plan,
            started=bad_started,
            outcome=_selected(1),
            clock=start_clock,
        )


def test_temp_leftover_cleanup_requires_same_inode_final(
    tmp_path: Path,
) -> None:
    binding, plan, _clock = _publish_plan(tmp_path)
    final = binding.registry / "v4r4-ordinal-01-sequencing-grant.json"
    temp = binding.registry / ".v4r4-ordinal-01-sequencing-grant.json.1234.abcdef.tmp"

    final.write_text("{}\n")
    final.chmod(0o600)
    os.link(final, temp)
    assert temp.exists()

    names = os.listdir(binding.registry)
    names.remove(final.name)
    names.remove(temp.name)
    with patch(
        "mediaforce.tuning.av1_validation_v4r4_ordinal_registry.os.listdir",
        return_value=[final.name, *names, temp.name],
    ):
        assert_av1_v4r4_ordinal_registry(binding.registry)
    assert not temp.exists()

    bad = binding.registry / ".v4r4-ordinal-02-sequencing-grant.json.1234.abcdef.tmp"
    os.link(final, bad)
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="temporary custody"):
        assert_av1_v4r4_ordinal_registry(binding.registry)
    assert bad.exists()
    assert plan["registry_id"] == binding.registry_id


def test_supported_artifact_custody_uses_one_directory_snapshot(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    unsupported = binding.registry / "unsupported.json"
    unsupported.write_text("{}\n")
    unsupported.chmod(0o600)

    with patch(
        "mediaforce.tuning.av1_validation_v4r4_ordinal_registry.os.listdir",
        wraps=os.listdir,
    ) as listdir:
        with pytest.raises(AV1V4R4OrdinalRegistryError, match="unsupported"):
            assert_av1_v4r4_ordinal_registry(binding.registry)
    assert listdir.call_count == 1


def test_initialization_rejects_symlink_registry_leaf(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "registry-link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(AV1V4R4OrdinalRegistryError, match="custody"):
        initialize_av1_v4r4_ordinal_registry(link)


def test_initialization_does_not_chmod_leaf_swapped_to_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    registry = tmp_path / "registry"
    real_mkdir = os.mkdir

    def swap_leaf(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == registry.name and dir_fd is not None:
            registry.symlink_to(target, target_is_directory=True)
            raise FileExistsError
        if dir_fd is None:
            real_mkdir(path, mode)
        else:
            real_mkdir(path, mode, dir_fd=dir_fd)

    with patch(
        "mediaforce.tuning.av1_validation_v4r4_ordinal_registry.os.mkdir",
        side_effect=swap_leaf,
    ):
        with pytest.raises(AV1V4R4OrdinalRegistryError, match="custody"):
            initialize_av1_v4r4_ordinal_registry(registry)

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_reconcile_seals_cross_ordinal_clock_regression(tmp_path: Path) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=1,
        outcome=_selected(1),
    )
    grant, claim, started, publication = _run_ordinal(
        binding=binding,
        plan=plan,
        clock=clock,
        ordinal=2,
        outcome=_selected(2),
    )

    regressed_at = "2026-08-10T04:00:03Z"
    grant["authorized_at"] = regressed_at
    grant["admission_opens_at"] = regressed_at
    grant = _rebind_for_test(
        grant,
        id_field="grant_id",
        id_prefix="av1v4r4ordgrant_",
        domain=av1_v4r4_identity_domain("ordinal-registry-grant"),
    )
    claim["grant_id"] = grant["grant_id"]
    claim["grant_payload_sha256"] = grant["payload_sha256"]
    claim["claimed_at"] = regressed_at
    claim = _rebind_for_test(
        claim,
        id_field="claim_id",
        id_prefix="av1v4r4ordclaim_",
        domain=av1_v4r4_identity_domain("ordinal-registry-claim"),
    )
    exec_grant, exec_claim, admission = _execution_and_admission_for_test(
        binding,
        plan,
        grant,
        claim,
    )
    started["grant_id"] = grant["grant_id"]
    started["grant_payload_sha256"] = grant["payload_sha256"]
    started["claim_id"] = claim["claim_id"]
    started["claim_payload_sha256"] = claim["payload_sha256"]
    started["started_at"] = regressed_at
    started = _rebind_for_test(
        started,
        id_field="started_id",
        id_prefix="av1v4r4ordstart_",
        domain=av1_v4r4_identity_domain("ordinal-registry-started"),
    )
    outcome_publication = deepcopy(publication.outcome_publication)
    outcome_publication["started_id"] = started["started_id"]
    outcome_publication["started_payload_sha256"] = started["payload_sha256"]
    outcome_publication["outcome_at"] = regressed_at
    outcome_publication = _rebind_for_test(
        outcome_publication,
        id_field="outcome_publication_id",
        id_prefix="av1v4r4ordoutpub_",
        domain=av1_v4r4_identity_domain("ordinal-registry-outcome"),
    )

    from tests.test_av1_validation_v4r4_execution_authority import _private_canonical_bytes
    from mediaforce.tuning.av1_validation_v4r4_runner_admission import (
        serialize_av1_v4r4_runner_admission,
    )

    replacements = {
        "v4r4-ordinal-02-sequencing-grant.json": serialize_av1_v4r4_ordinal_registry_grant(
            grant
        ),
        "v4r4-ordinal-02-sequencing-claim.json": serialize_av1_v4r4_ordinal_registry_claim(
            claim
        ),
        "v4r4-ordinal-02-execution-grant.json": _private_canonical_bytes(exec_grant),
        "v4r4-ordinal-02-execution-claim.json": _private_canonical_bytes(exec_claim),
        "v4r4-ordinal-02-runner-admission.json": serialize_av1_v4r4_runner_admission(
            admission
        ),
        "v4r4-ordinal-02-started.json": serialize_av1_v4r4_ordinal_registry_started(
            started
        ),
        "v4r4-ordinal-02-outcome-publication.json": serialize_av1_v4r4_ordinal_registry_outcome_publication(
            outcome_publication
        ),
    }
    for filename, data in replacements.items():
        path = binding.registry / filename
        path.write_bytes(data)
        path.chmod(0o600)

    with pytest.raises(AV1V4R4OrdinalRegistryError, match="clock moved backward"):
        reconcile_av1_v4r4_ordinal_registry(
            binding=binding,
            plan=plan,
            clock=lambda: datetime(2026, 8, 10, 0, 1, tzinfo=UTC),
        )
    assert (binding.registry / "v4r4-ordinal-registry-terminal.json").exists()


def test_malformed_terminal_map_is_rejected(tmp_path: Path) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    for ordinal in range(1, 9):
        _run_ordinal(
            binding=binding,
            plan=plan,
            clock=clock,
            ordinal=ordinal,
            outcome=_conflict(3) if ordinal == 3 else _selected(ordinal),
        )
    terminal_publication = publish_av1_v4r4_ordinal_registry_terminal(
        binding=binding,
        plan=plan,
        clock=clock,
    )
    malformed = deepcopy(terminal_publication)
    malformed["terminal"]["conflict_reason_counts"][
        "target_band_violates_quality_floor"
    ] = 2

    with pytest.raises(AV1V4R4OrdinalRegistryError):
        assert_av1_v4r4_ordinal_registry_terminal_publication(malformed)


def test_complete_negative_policy_verdict_round_trips(tmp_path: Path) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    for ordinal in range(1, 9):
        _run_ordinal(
            binding=binding,
            plan=plan,
            clock=clock,
            ordinal=ordinal,
            outcome=_conflict(3) if ordinal == 3 else _selected(ordinal),
        )
    terminal_publication = publish_av1_v4r4_ordinal_registry_terminal(
        binding=binding,
        plan=plan,
        clock=clock,
    )
    terminal_file = binding.registry / "v4r4-ordinal-registry-terminal.json"
    loaded = deserialize_av1_v4r4_ordinal_registry_terminal_publication(
        terminal_file.read_bytes()
    )

    assert loaded == terminal_publication
    assert terminal_publication["terminal"]["traversal_complete"] is True
    assert terminal_publication["terminal"]["verdict"] == "policy_conflicted"


def test_grant_publication_reports_idempotent_owner_operation(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    plan = _plan(binding)
    clock = TickClock(datetime(2026, 8, 10, 0, 0, 1, tzinfo=UTC))
    publish_av1_v4r4_ordinal_registry_plan(binding=binding, plan=plan)

    first = publish_av1_v4r4_ordinal_registry_grant_with_status(
        binding=binding,
        plan=plan,
        ordinal=1,
        clock=clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    second = publish_av1_v4r4_ordinal_registry_grant_with_status(
        binding=binding,
        plan=plan,
        ordinal=1,
        clock=clock,
        valid_until=_GRANT_VALID_UNTIL,
    )

    assert first.created is True
    assert second.created is False
    assert second.grant == first.grant


def test_sequencing_claim_requires_execution_grant(tmp_path: Path) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    grant = publish_av1_v4r4_ordinal_registry_grant(
        binding=binding,
        plan=plan,
        ordinal=1,
        clock=clock,
        valid_until=_GRANT_VALID_UNTIL,
    )

    with pytest.raises(
        AV1V4R4OrdinalRegistryError,
        match="execution grant is required before claim",
    ):
        publish_av1_v4r4_ordinal_registry_claim(
            binding=binding,
            plan=plan,
            grant=grant,
            clock=clock,
        )

    assert not (binding.registry / "v4r4-ordinal-01-sequencing-claim.json").exists()


@pytest.mark.parametrize(
    ("authorized_at", "valid_until"),
    (
        ("2026-08-10T04:30:00Z", "2026-08-10T05:00:00Z"),
        ("2026-08-10T04:00:00Z", "2026-08-10T04:00:02Z"),
    ),
)
def test_sequencing_claim_rejects_inactive_execution_grant(
    tmp_path: Path,
    authorized_at: str,
    valid_until: str,
) -> None:
    from tests.test_av1_validation_v4r4_execution_authority import (
        _execution_grant,
        _private_canonical_bytes,
        _rebind_execution_grant,
    )

    binding, plan, clock = _publish_plan(tmp_path)
    grant = publish_av1_v4r4_ordinal_registry_grant(
        binding=binding,
        plan=plan,
        ordinal=1,
        clock=clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    request, preflight = load_av1_v4r4_ordinal_registry_preparation(
        binding=binding,
        plan=plan,
    )
    execution_grant = _execution_grant(
        plan,
        grant,
        qualification_request=request,
        execution_preflight=preflight,
    )
    execution_grant = _rebind_execution_grant(
        {
            **execution_grant,
            "authorized_at": authorized_at,
            "valid_until": valid_until,
        }
    )
    execution_grant_file = (
        binding.registry / "v4r4-ordinal-01-execution-grant.json"
    )
    execution_grant_file.write_bytes(_private_canonical_bytes(execution_grant))
    execution_grant_file.chmod(0o600)

    with pytest.raises(
        AV1V4R4OrdinalRegistryError,
        match="execution grant is inactive",
    ):
        publish_av1_v4r4_ordinal_registry_claim(
            binding=binding,
            plan=plan,
            grant=grant,
            clock=clock,
        )

    assert not (binding.registry / "v4r4-ordinal-01-sequencing-claim.json").exists()


def test_torn_sequencing_claim_burn_seals_without_deleting(tmp_path: Path) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    grant = publish_av1_v4r4_ordinal_registry_grant(
        binding=binding,
        plan=plan,
        ordinal=1,
        clock=clock,
        valid_until=_GRANT_VALID_UNTIL,
    )
    _write_private_execution_grant(binding, plan, grant)
    claim_file = binding.registry / "v4r4-ordinal-01-sequencing-claim.json"
    claim_file.write_bytes(b"")
    claim_file.chmod(0o600)

    with pytest.raises(
        AV1V4R4OrdinalRegistryError,
        match="sequencing claim burn is invalid",
    ):
        publish_av1_v4r4_ordinal_registry_claim(
            binding=binding,
            plan=plan,
            grant=grant,
            clock=clock,
        )

    assert claim_file.exists()
    assert claim_file.stat().st_size == 0
    assert (binding.registry / "v4r4-ordinal-registry-terminal.json").is_file()
    assert reconcile_av1_v4r4_ordinal_registry(
        binding=binding,
        plan=plan,
        clock=clock,
    ) == 0
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="sealed"):
        publish_av1_v4r4_ordinal_registry_claim(
            binding=binding,
            plan=plan,
            grant=grant,
            clock=clock,
        )


def test_low_level_publication_errors_are_path_free(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    plan = _plan(binding)

    with patch(
        "mediaforce.tuning.av1_validation_v4r4_ordinal_registry.os.link",
        side_effect=OSError(str(binding.registry / "leaked-name")),
    ):
        with pytest.raises(AV1V4R4OrdinalRegistryError) as error:
            publish_av1_v4r4_ordinal_registry_plan(binding=binding, plan=plan)

    message = str(error.value)
    assert str(binding.registry) not in message
    assert "leaked-name" not in message
