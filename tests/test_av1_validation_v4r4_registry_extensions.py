from __future__ import annotations

from pathlib import Path

import pytest

from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    AV1V4R4OrdinalRegistryError,
    assert_av1_v4r4_ordinal_registry,
    publish_av1_v4r4_ordinal_registry_runner_admission_started,
    reconcile_av1_v4r4_ordinal_registry,
)
from mediaforce.tuning.av1_validation_v4r4_runner_admission import (
    serialize_av1_v4r4_runner_admission,
)
from tests.test_av1_validation_v4r4_execution_authority import (
    _execution_claim,
    _execution_grant,
    _private_canonical_bytes,
    _sequencing_claim,
    _sequencing_grant,
)
from tests.test_av1_validation_v4r4_one_ordinal_runner import _write_authority_files
from tests.test_av1_validation_v4r4_ordinal_registry import _publish_plan
from tests.test_av1_validation_v4r4_runner_admission import _admission


def test_registry_extension_accepts_execution_and_admission_filenames(tmp_path: Path) -> None:
    ctx = _extension_context(tmp_path)
    admission = _admission(
        ctx["plan"],
        ctx["seq_grant"],
        ctx["seq_claim"],
        ctx["exec_grant"],
        ctx["exec_claim"],
    )

    started = publish_av1_v4r4_ordinal_registry_runner_admission_started(
        binding=ctx["binding"],
        plan=ctx["plan"],
        sequencing_grant=ctx["seq_grant"],
        sequencing_claim=ctx["seq_claim"],
        execution_grant=ctx["exec_grant"],
        execution_claim=ctx["exec_claim"],
        admission=admission,
        clock=ctx["clock"],
    )

    assert started.created is True
    assert started.started["ordinal"] == 1
    assert_av1_v4r4_ordinal_registry(ctx["binding"].registry)
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="already used"):
        publish_av1_v4r4_ordinal_registry_runner_admission_started(
            binding=ctx["binding"],
            plan=ctx["plan"],
            sequencing_grant=ctx["seq_grant"],
            sequencing_claim=ctx["seq_claim"],
            execution_grant=ctx["exec_grant"],
            execution_claim=ctx["exec_claim"],
            admission=admission,
            clock=ctx["clock"],
        )


def test_registry_extension_rejects_partial_execution_slot(tmp_path: Path) -> None:
    ctx = _extension_context(tmp_path)
    (ctx["binding"].registry / "v4r4-ordinal-01-execution-claim.json").unlink()

    with pytest.raises(AV1V4R4OrdinalRegistryError, match="incomplete publication"):
        reconcile_av1_v4r4_ordinal_registry(
            binding=ctx["binding"],
            plan=ctx["plan"],
            clock=ctx["clock"],
        )


def test_registry_extension_rejects_partial_admission_crash(tmp_path: Path) -> None:
    ctx = _extension_context(tmp_path)
    admission = _admission(
        ctx["plan"],
        ctx["seq_grant"],
        ctx["seq_claim"],
        ctx["exec_grant"],
        ctx["exec_claim"],
    )
    admission_file = ctx["binding"].registry / "v4r4-ordinal-01-runner-admission.json"
    admission_file.write_bytes(serialize_av1_v4r4_runner_admission(admission))
    admission_file.chmod(0o600)

    with pytest.raises(AV1V4R4OrdinalRegistryError, match="incomplete publication"):
        publish_av1_v4r4_ordinal_registry_runner_admission_started(
            binding=ctx["binding"],
            plan=ctx["plan"],
            sequencing_grant=ctx["seq_grant"],
            sequencing_claim=ctx["seq_claim"],
            execution_grant=ctx["exec_grant"],
            execution_claim=ctx["exec_claim"],
            admission=admission,
            clock=ctx["clock"],
        )
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="incomplete publication"):
        reconcile_av1_v4r4_ordinal_registry(
            binding=ctx["binding"],
            plan=ctx["plan"],
            clock=ctx["clock"],
        )
    assert (ctx["binding"].registry / "v4r4-ordinal-registry-terminal.json").exists()


def test_registry_extension_loads_canonical_execution_authority(tmp_path: Path) -> None:
    ctx = _extension_context(tmp_path)
    forged = dict(ctx["exec_claim"])
    forged["execution_claim_id"] = "av1v4r4execclaim_" + "0" * 32
    with pytest.raises(AV1V4R4OrdinalRegistryError, match="execution claim"):
        publish_av1_v4r4_ordinal_registry_runner_admission_started(
            binding=ctx["binding"],
            plan=ctx["plan"],
            sequencing_grant=ctx["seq_grant"],
            sequencing_claim=ctx["seq_claim"],
            execution_grant=ctx["exec_grant"],
            execution_claim=forged,
            admission=_admission(
                ctx["plan"],
                ctx["seq_grant"],
                ctx["seq_claim"],
                ctx["exec_grant"],
                ctx["exec_claim"],
            ),
            clock=ctx["clock"],
        )


def _extension_context(tmp_path: Path) -> dict[str, object]:
    binding, plan, clock = _publish_plan(tmp_path)
    seq_grant = _sequencing_grant(binding, plan, clock)
    seq_claim = _sequencing_claim(binding, plan, seq_grant, clock)
    exec_grant = _execution_grant(plan, seq_grant)
    exec_claim = _execution_claim(plan, seq_claim, exec_grant)
    _write_authority_files(binding, plan, seq_grant, seq_claim, exec_grant, exec_claim)
    return {
        "binding": binding,
        "plan": plan,
        "clock": clock,
        "seq_grant": seq_grant,
        "seq_claim": seq_claim,
        "exec_grant": exec_grant,
        "exec_claim": exec_claim,
    }
