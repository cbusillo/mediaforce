from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from mediaforce.tuning import av1_validation_v4r4_execution_issuer as issuer
from mediaforce.tuning.av1_validation_v4r4_execution_authority import (
    assert_av1_v4r4_execution_chain,
    assert_av1_v4r4_execution_preparation_chain_active,
)
from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    publish_av1_v4r4_ordinal_registry_claim,
)
from tests.test_av1_validation_v4r4_execution_authority import _prepared_chain


def test_grant_issuance_is_canonical_idempotent_and_does_not_consume_claim(
    tmp_path: Path,
) -> None:
    prepared = _prepared_chain(tmp_path)

    with _repository_patch(prepared):
        first = _issue_grant(tmp_path, prepared)
        second = _issue_grant(tmp_path, prepared)

    assert first.created is True
    assert second.created is False
    assert second.grant == first.grant
    assert first.grant["prepared_invocation_sha256"] == next(
        item["invocation_sha256"]
        for item in prepared.request["invocation_digests"]
        if item["ordinal"] == 1
    )
    assert_av1_v4r4_execution_preparation_chain_active(
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
        plan=prepared.plan,
        sequencing_grant=prepared.sequencing_grant,
        execution_grant=first.grant,
        now=datetime(2026, 8, 10, 4, 0, 10, tzinfo=UTC),
    )
    registry = prepared.binding.registry
    grant_file = registry / "v4r4-ordinal-01-execution-grant.json"
    assert grant_file.stat().st_mode & 0o777 == 0o600
    assert grant_file.stat().st_nlink == 1
    assert not (registry / "v4r4-ordinal-01-sequencing-claim.json").exists()
    assert not (registry / "v4r4-ordinal-01-execution-claim.json").exists()


@pytest.mark.parametrize(
    ("owner", "confirmed_owner", "ordinal", "confirmed_ordinal"),
    [
        ("owner.mediaforce", "owner.other", 1, 1),
        ("owner.mediaforce", "owner.mediaforce", 1, 2),
    ],
)
def test_grant_issuance_requires_owner_and_ordinal_confirmation(
    tmp_path: Path,
    owner: str,
    confirmed_owner: str,
    ordinal: int,
    confirmed_ordinal: int,
) -> None:
    prepared = _prepared_chain(tmp_path)

    with pytest.raises(issuer.AV1V4R4ExecutionIssuerError, match="confirmation"):
        issuer.issue_av1_v4r4_execution_grant(
            repository_root=tmp_path / "repo",
            binding=prepared.binding,
            owner_principal=owner,
            confirmed_owner_principal=confirmed_owner,
            ordinal=ordinal,
            confirmed_ordinal=confirmed_ordinal,
            valid_until=str(prepared.sequencing_grant["valid_until"]),
            clock=prepared.clock,
        )

    assert not (
        prepared.binding.registry / "v4r4-ordinal-01-execution-grant.json"
    ).exists()


def test_grant_issuance_rejects_repository_and_window_drift(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    reviewed = prepared.request["reviewed_repository"]

    with patch.object(
        issuer,
        "_repository_identity",
        return_value=("f" * 40, reviewed["tree"]),
    ):
        with pytest.raises(issuer.AV1V4R4ExecutionIssuerError, match="identity"):
            _issue_grant(tmp_path, prepared)

    with _repository_patch(prepared):
        with pytest.raises(issuer.AV1V4R4ExecutionIssuerError, match="operation"):
            _issue_grant(tmp_path, prepared, valid_until="2026-08-11T00:00:01Z")


def test_claim_issuance_requires_separate_sequencing_claim_and_is_idempotent(
    tmp_path: Path,
) -> None:
    prepared = _prepared_chain(tmp_path)
    with _repository_patch(prepared):
        grant = _issue_grant(tmp_path, prepared).grant
        with pytest.raises(issuer.AV1V4R4ExecutionIssuerError, match="claim operation"):
            _issue_claim(tmp_path, prepared)

        sequencing_claim = publish_av1_v4r4_ordinal_registry_claim(
            binding=prepared.binding,
            plan=prepared.plan,
            grant=prepared.sequencing_grant,
            clock=prepared.clock,
        )
        first = _issue_claim(tmp_path, prepared)
        second = _issue_claim(tmp_path, prepared)

    assert first.created is True
    assert second.created is False
    assert second.claim == first.claim
    assert_av1_v4r4_execution_chain(
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
        plan=prepared.plan,
        sequencing_grant=prepared.sequencing_grant,
        sequencing_claim=sequencing_claim,
        execution_grant=grant,
        execution_claim=first.claim,
        now=datetime(2026, 8, 10, 4, 0, 20, tzinfo=UTC),
    )
    assert not (
        prepared.binding.registry / "v4r4-ordinal-01-runner-admission.json"
    ).exists()

    with _repository_patch(prepared):
        retained_grant = _issue_grant(tmp_path, prepared)
    assert retained_grant.created is False
    assert retained_grant.grant == grant


def test_existing_grant_conflict_fails_closed(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    with _repository_patch(prepared):
        issued = _issue_grant(tmp_path, prepared)
        with pytest.raises(issuer.AV1V4R4ExecutionIssuerError, match="conflicts"):
            issuer.issue_av1_v4r4_execution_grant(
                repository_root=tmp_path / "repo",
                binding=prepared.binding,
                owner_principal="owner.mediaforce",
                confirmed_owner_principal="owner.mediaforce",
                ordinal=1,
                confirmed_ordinal=1,
                valid_until="2026-08-10T23:59:59Z",
                clock=prepared.clock,
            )

    assert issued.grant["execution_grant_id"]
    assert not (
        prepared.binding.registry / "v4r4-ordinal-01-sequencing-claim.json"
    ).exists()


def test_torn_execution_claim_is_preserved_and_terminalized(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    with _repository_patch(prepared):
        _issue_grant(tmp_path, prepared)
        publish_av1_v4r4_ordinal_registry_claim(
            binding=prepared.binding,
            plan=prepared.plan,
            grant=prepared.sequencing_grant,
            clock=prepared.clock,
        )
        claim_file = (
            prepared.binding.registry / "v4r4-ordinal-01-execution-claim.json"
        )
        claim_file.write_bytes(b"")
        claim_file.chmod(0o600)

        with pytest.raises(
            issuer.AV1V4R4ExecutionIssuerError,
            match="claim burn is invalid",
        ):
            _issue_claim(tmp_path, prepared)

    assert claim_file.exists()
    assert claim_file.stat().st_size == 0
    assert (
        prepared.binding.registry / "v4r4-ordinal-registry-terminal.json"
    ).exists()


def test_downstream_artifact_prevents_execution_claim_recreation(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    with _repository_patch(prepared):
        _issue_grant(tmp_path, prepared)
        publish_av1_v4r4_ordinal_registry_claim(
            binding=prepared.binding,
            plan=prepared.plan,
            grant=prepared.sequencing_grant,
            clock=prepared.clock,
        )
        first = _issue_claim(tmp_path, prepared)
        claim_file = (
            prepared.binding.registry / "v4r4-ordinal-01-execution-claim.json"
        )
        claim_file.unlink()
        admission = prepared.binding.registry / "v4r4-ordinal-01-runner-admission.json"
        admission.write_bytes(b"{}\n")
        admission.chmod(0o600)

        with pytest.raises(
            issuer.AV1V4R4ExecutionIssuerError,
            match="downstream artifacts",
        ):
            _issue_claim(tmp_path, prepared)

    assert first.claim["execution_claim_id"]
    assert not claim_file.exists()
    assert (
        prepared.binding.registry / "v4r4-ordinal-registry-terminal.json"
    ).exists()


def test_torn_execution_grant_is_preserved_and_not_reissued(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    grant_file = prepared.binding.registry / "v4r4-ordinal-01-execution-grant.json"
    grant_file.write_bytes(b"")
    grant_file.chmod(0o600)

    with _repository_patch(prepared):
        with pytest.raises(issuer.AV1V4R4ExecutionIssuerError, match="operation"):
            _issue_grant(tmp_path, prepared)

    assert grant_file.exists()
    assert grant_file.stat().st_size == 0
    assert not (
        prepared.binding.registry / "v4r4-ordinal-01-sequencing-claim.json"
    ).exists()


def test_cli_emits_one_path_free_json_line(tmp_path: Path, capsys: Any) -> None:
    cli = _load_cli_module()
    key_file = tmp_path / "registry.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    grant = {
        "ordinal": 1,
        "execution_grant_id": "av1v4r4execgrant_" + "1" * 32,
        "payload_sha256": "sha256:" + "2" * 64,
        "media_read_authorized": True,
        "qualification_execution_authorized": True,
        "runtime_execution_authorized": True,
        "dogfood_authorized": False,
    }
    with (
        patch.object(cli, "av1_v4r4_ordinal_registry_binding", return_value=object()),
        patch.object(
            cli,
            "issue_av1_v4r4_execution_grant",
            return_value=SimpleNamespace(grant=grant, created=True),
        ),
    ):
        code = cli.main(
            [
                "grant",
                "--repository-root",
                str(tmp_path),
                "--registry",
                str(tmp_path / "private-registry"),
                "--registry-key-file",
                str(key_file),
                "--owner-principal",
                "owner.mediaforce",
                "--confirm-owner-principal",
                "owner.mediaforce",
                "--ordinal",
                "1",
                "--confirm-ordinal",
                "1",
                "--valid-until",
                "2026-08-10T05:00:00Z",
            ]
        )

    completed = capsys.readouterr()
    assert code == 0
    assert completed.err == ""
    assert completed.out.count("\n") == 1
    payload = json.loads(completed.out)
    assert payload["ok"] is True
    assert payload["sequencing_claim_created"] is False
    assert payload["media_execution_started"] is False
    assert str(tmp_path) not in completed.out
    assert "kkkk" not in completed.out


@pytest.mark.parametrize("custody", ["world_readable", "symlink", "hardlink"])
def test_cli_rejects_key_custody_without_leaking(
    tmp_path: Path,
    capsys: Any,
    custody: str,
) -> None:
    cli = _load_cli_module()
    key_file = tmp_path / "private-registry.key"
    key_file.write_bytes(b"secret-private-key-material-123456")
    key_file.chmod(0o600)
    key_argument = key_file
    if custody == "world_readable":
        key_file.chmod(0o644)
    elif custody == "symlink":
        key_argument = tmp_path / "private-registry.key.link"
        key_argument.symlink_to(key_file)
    else:
        key_argument = tmp_path / "private-registry.key.hardlink"
        key_argument.hardlink_to(key_file)

    code = cli.main(
        [
            "grant",
            "--repository-root",
            str(tmp_path),
            "--registry",
            str(tmp_path / "private-registry"),
            "--registry-key-file",
            str(key_argument),
            "--owner-principal",
            "owner.mediaforce",
            "--confirm-owner-principal",
            "owner.mediaforce",
            "--ordinal",
            "1",
            "--confirm-ordinal",
            "1",
            "--valid-until",
            "2026-08-10T05:00:00Z",
        ]
    )

    completed = capsys.readouterr()
    assert code == 1
    assert completed.err == ""
    assert completed.out.count("\n") == 1
    assert json.loads(completed.out) == cli._ERROR_RESULT
    assert str(tmp_path) not in completed.out
    assert "secret-private" not in completed.out


def test_issuer_keeps_verifier_separate_and_imports_no_media_execution() -> None:
    source = inspect.getsource(issuer)
    assert "search_quality_for_source" not in source
    assert "run_v4_qualification_search" not in source
    assert "publish_av1_v4r4_ordinal_registry_claim" not in source
    assert "def _build_execution_grant" in source
    assert "def _build_execution_claim" in source


def _issue_grant(
    tmp_path: Path,
    prepared: Any,
    *,
    valid_until: str | None = None,
) -> issuer.AV1V4R4ExecutionGrantIssuance:
    return issuer.issue_av1_v4r4_execution_grant(
        repository_root=tmp_path / "repo",
        binding=prepared.binding,
        owner_principal="owner.mediaforce",
        confirmed_owner_principal="owner.mediaforce",
        ordinal=1,
        confirmed_ordinal=1,
        valid_until=valid_until or str(prepared.sequencing_grant["valid_until"]),
        clock=prepared.clock,
    )


def _issue_claim(
    tmp_path: Path,
    prepared: Any,
) -> issuer.AV1V4R4ExecutionClaimIssuance:
    return issuer.issue_av1_v4r4_execution_claim(
        repository_root=tmp_path / "repo",
        binding=prepared.binding,
        owner_principal="owner.mediaforce",
        confirmed_owner_principal="owner.mediaforce",
        ordinal=1,
        confirmed_ordinal=1,
        clock=prepared.clock,
    )


def _repository_patch(prepared: Any) -> Any:
    reviewed = prepared.request["reviewed_repository"]
    return patch.object(
        issuer,
        "_repository_identity",
        return_value=(reviewed["commit"], reviewed["tree"]),
    )


def _load_cli_module() -> Any:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "issue_av1_v4r4_execution_authority.py"
    )
    spec = importlib.util.spec_from_file_location(
        "issue_av1_v4r4_execution_authority_test",
        script,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load CLI module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
