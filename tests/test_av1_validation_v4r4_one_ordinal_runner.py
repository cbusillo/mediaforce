from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import importlib.util
from typing import Any, Never
from unittest.mock import patch

import pytest

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.encoding.quality import QualitySearchResult
from mediaforce.tuning.av1_validation_v4r4_one_ordinal_runner import (
    AV1V4R4OneOrdinalRuntimeInputs,
    AV1V4R4OneOrdinalRunResult,
    run_av1_v4r4_one_ordinal,
)
from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    AV1V4R4OrdinalRegistryError,
    _RegistryContext,
    publish_av1_v4r4_ordinal_registry_grant,
)
from mediaforce.tuning.target_size_search import TargetSizeSearchError
from tests.test_av1_validation_v4r4_execution_authority import (
    _execution_claim,
    _execution_grant,
    _prepared_chain,
    _private_canonical_bytes,
    _sequencing_claim,
    _sequencing_grant,
)


class FrozenClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 10, 0, 0, 5, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def test_runner_selected_success_publishes_admission_started_and_outcome(tmp_path: Path) -> None:
    ctx = _authority_context(tmp_path, ordinal=1)

    result = _run(ctx, lambda *_args, **kwargs: _quality_result(kwargs["stream_budget_ledger"]))

    assert result.public_result == {
        "schema": "mediaforce.av1_cold_start_v4r4_one_ordinal_cli_result",
        "schema_version": 1,
        "completed": True,
        "disposition": "selected_success",
        "ordinal": 1,
        "outcome_id": result.outcome["outcome_id"],
        "outcome_publication_id": result.outcome_publication["outcome_publication_id"],
        "terminal_publication_id": None,
        "failure_search_reason": None,
        "conflict_quality_gap_band": None,
        "conflict_size_gap_band": None,
    }
    assert (ctx["binding"].registry / "v4r4-ordinal-01-runner-admission.json").exists()
    assert (ctx["binding"].registry / "v4r4-ordinal-01-started.json").exists()


@pytest.mark.parametrize(
    ("reason", "expected_quality", "expected_size"),
    [
        ("all_candidates_violate_quality_floor", "within_relax_step", "no_quality_safe_candidate"),
        ("target_band_violates_quality_floor", "within_target_floor_span", "below_sample_lower_bound"),
        ("target_requires_crossing_quality_floor", "within_two_target_floor_spans", "over_source_cap"),
    ],
)
def test_runner_bounded_conflict_recomputes_from_candidates(
    reason: str,
    expected_quality: str,
    expected_size: str,
    tmp_path: Path,
) -> None:
    ctx = _authority_context(tmp_path, ordinal=4)
    trace = _conflict_trace(reason, ctx["target"], ctx["cap"])
    trace["best_reachable_candidate"] = {"metric_score": 999.0, "predicted_whole_episode_bytes": 1}

    result = _run(
        ctx,
        lambda *_args, **kwargs: _raise(
            _conflict_error(trace, kwargs["stream_budget_ledger"])
        ),
    )

    diagnostic = result.outcome["conflict_diagnostic"]
    assert result.public_result["disposition"] == "bounded_quality_conflict"
    assert diagnostic["failure_search_reason"] == reason
    assert diagnostic["conflict_quality_gap_band"] == expected_quality
    assert diagnostic["conflict_size_gap_band"] == expected_size


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), "81.0", None])
def test_runner_malformed_or_nonfinite_candidates_publish_fatal(
    tmp_path: Path,
    bad_value: Any,
) -> None:
    ctx = _authority_context(tmp_path, ordinal=4)
    trace = _conflict_trace("all_candidates_violate_quality_floor", ctx["target"], ctx["cap"])
    trace["candidates"][0]["metric_score"] = bad_value

    result = _run(
        ctx,
        lambda *_args, **kwargs: _raise(
            _conflict_error(trace, kwargs["stream_budget_ledger"])
        ),
    )

    assert result.public_result["completed"] is False
    assert result.public_result["disposition"] == "fatal_failure"


def test_runner_missing_success_trace_binding_publishes_fatal(tmp_path: Path) -> None:
    ctx = _authority_context(tmp_path, ordinal=1)

    result = _run(ctx, lambda *_args, **_kwargs: _quality_result(None))

    assert result.public_result["completed"] is False
    assert result.public_result["disposition"] == "fatal_failure"


@pytest.mark.parametrize("binding", ["missing", "mismatched"])
def test_runner_invalid_conflict_trace_binding_publishes_fatal(
    tmp_path: Path,
    binding: str,
) -> None:
    ctx = _authority_context(tmp_path, ordinal=4)
    trace = _conflict_trace(
        "all_candidates_violate_quality_floor",
        ctx["target"],
        ctx["cap"],
    )
    if binding == "mismatched":
        trace["ledger"] = {
            "ledger_id": "stream-budget-ledger:drifted",
            "source_id": "source:drifted",
            "stream_plan_id": "stream-plan:drifted",
        }

    result = _run(
        ctx,
        lambda *_args, **_kwargs: _raise(
            TargetSizeSearchError(
                "private",
                status="quality_conflict",
                trace=trace,
            )
        ),
    )

    assert result.public_result["completed"] is False
    assert result.public_result["disposition"] == "fatal_failure"


def test_runner_target_size_error_conflict_and_other_errors_are_absorbed(tmp_path: Path) -> None:
    conflict_ctx = _authority_context(tmp_path / "conflict", ordinal=4)
    trace = _conflict_trace("all_candidates_violate_quality_floor", conflict_ctx["target"], conflict_ctx["cap"])

    conflict = _run(
        conflict_ctx,
        lambda *_args, **kwargs: _raise(
            _conflict_error(trace, kwargs["stream_budget_ledger"], message="private bytes")
        ),
    )
    assert conflict.public_result["disposition"] == "bounded_quality_conflict"

    fatal_ctx = _authority_context(tmp_path / "fatal", ordinal=4)
    fatal = _run(fatal_ctx, lambda *_args, **_kwargs: (_raise(RuntimeError("/Users/private stderr 123"))))
    assert fatal.public_result["disposition"] == "fatal_failure"
    assert "/Users" not in json.dumps(fatal.public_result)
    assert "stderr" not in json.dumps(fatal.public_result)


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_runner_interrupts_publish_fatal_without_reraising(
    tmp_path: Path,
    interrupt_type: type[BaseException],
) -> None:
    ctx = _authority_context(tmp_path, ordinal=1)

    result = _run(
        ctx,
        lambda *_args, **_kwargs: _raise(interrupt_type()),
    )

    assert result.public_result["completed"] is False
    assert result.public_result["disposition"] == "fatal_failure"
    assert result.terminal_publication is not None


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_runner_retries_interrupt_during_outcome_publication(
    tmp_path: Path,
    interrupt_type: type[BaseException],
) -> None:
    ctx = _authority_context(tmp_path, ordinal=1)
    from mediaforce.tuning import av1_validation_v4r4_one_ordinal_runner as runner

    real_publish = runner.publish_av1_v4r4_ordinal_registry_outcome
    interrupted = False

    def interrupt_once(**kwargs: Any) -> Any:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise interrupt_type()
        return real_publish(**kwargs)

    with patch.object(
        runner,
        "publish_av1_v4r4_ordinal_registry_outcome",
        side_effect=interrupt_once,
    ):
        result = _run(
            ctx,
            lambda *_args, **_kwargs: _raise(interrupt_type()),
        )

    assert result.public_result["disposition"] == "fatal_failure"
    assert result.outcome_publication["outcome"]["disposition"] == "fatal_failure"
    assert result.terminal_publication is not None


def test_runner_recovers_interrupt_between_outcome_and_terminal(tmp_path: Path) -> None:
    ctx = _authority_context(tmp_path, ordinal=1)
    real_publish_terminal = _RegistryContext.publish_terminal
    interrupted = False

    def interrupt_once(
        registry: _RegistryContext,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return real_publish_terminal(registry, *args, **kwargs)

    with patch.object(_RegistryContext, "publish_terminal", new=interrupt_once):
        result = _run(
            ctx,
            lambda *_args, **_kwargs: _raise(RuntimeError("private")),
        )

    assert result.public_result["disposition"] == "fatal_failure"
    assert result.terminal_publication is not None


def test_runner_positive_control_divergence_becomes_fatal(tmp_path: Path) -> None:
    ctx = _authority_context(tmp_path, ordinal=1)
    trace = _trace_for_ledger(None, warm_status="rejected_fallback", fallback_reason="size_band_miss")

    result = _run(ctx, lambda *_args, **_kwargs: QualitySearchResult(crf=28, metric="vmaf", target=85, score=90, stdout="private", target_size_trace=trace))

    assert result.public_result["disposition"] == "fatal_failure"
    assert result.terminal_publication is not None


def test_runner_admission_is_single_use_and_guided_after_conflict_needs_fresh_authority(
    tmp_path: Path,
) -> None:
    ctx = _authority_context(tmp_path, ordinal=4)
    first = _run(ctx, lambda *_args, **kwargs: _quality_result(kwargs["stream_budget_ledger"], warm_start=kwargs.get("warm_start")))
    assert first.public_result["disposition"] == "selected_success"

    with pytest.raises(AV1V4R4OrdinalRegistryError, match="already used|sealed|authority"):
        _run(ctx, lambda *_args, **kwargs: _quality_result(kwargs["stream_budget_ledger"], warm_start=kwargs.get("warm_start")))

    fresh = _authority_context(tmp_path / "fresh", ordinal=4)
    conflict_trace = _conflict_trace("all_candidates_violate_quality_floor", fresh["target"], fresh["cap"])
    conflict = _run(
        fresh,
        lambda *_args, **kwargs: _raise(
            _conflict_error(conflict_trace, kwargs["stream_budget_ledger"])
        ),
    )
    assert conflict.public_result["disposition"] == "bounded_quality_conflict"
    grant5 = publish_av1_v4r4_ordinal_registry_grant(
        binding=fresh["binding"],
        plan=fresh["plan"],
        ordinal=5,
        clock=fresh["clock"],
        valid_until=str(fresh["plan"]["plan_closes_at"]),
    )
    assert grant5["ordinal"] == 5


def test_cli_exact_json_keys_exit_codes_and_empty_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli = _load_cli_module()
    for disposition, expected_code in (
        ("selected", 0),
        ("bounded_quality_conflict", 2),
        ("fatal", 1),
        ("keyboard_interrupt", 1),
        ("system_exit", 1),
    ):
        ctx = _authority_context(tmp_path / disposition, ordinal=4)
        request_file = _runtime_request_file(tmp_path / disposition, ctx)
        key_file = ctx["registry_key_file"]
        if disposition == "selected":
            cli.search_quality_for_source = lambda *_args, **kwargs: _quality_result(
                kwargs["stream_budget_ledger"],
                warm_start=kwargs.get("warm_start"),
            )
        elif disposition == "bounded_quality_conflict":
            trace = _conflict_trace("all_candidates_violate_quality_floor", ctx["target"], ctx["cap"])
            cli.search_quality_for_source = lambda *_args, **kwargs: _raise(
                _conflict_error(trace, kwargs["stream_budget_ledger"])
            )
        elif disposition == "fatal":
            cli.search_quality_for_source = lambda *_args, **_kwargs: _raise(RuntimeError("/Users/private"))
        elif disposition == "keyboard_interrupt":
            cli.search_quality_for_source = lambda *_args, **_kwargs: _raise(KeyboardInterrupt())
        else:
            cli.search_quality_for_source = lambda *_args, **_kwargs: _raise(SystemExit(7))
        with patch(
            "mediaforce.tuning.av1_validation_v4r4_one_ordinal_runner._utc_now",
            lambda: datetime(2026, 8, 10, 4, 0, 30, tzinfo=UTC),
        ):
            code = cli.main(
                [
                    "--registry",
                    str(ctx["binding"].registry),
                    "--registry-key-file",
                    str(key_file),
                    "--plan",
                    str(ctx["plan_file"]),
                    "--qualification-request",
                    str(ctx["request_file"]),
                    "--execution-preflight",
                    str(ctx["preflight_file"]),
                    "--sequencing-grant",
                    str(ctx["seq_grant_file"]),
                    "--sequencing-claim",
                    str(ctx["seq_claim_file"]),
                    "--execution-grant",
                    str(ctx["exec_grant_file"]),
                    "--execution-claim",
                    str(ctx["exec_claim_file"]),
                    "--runtime-request",
                    str(request_file),
                ]
            )
        completed = capsys.readouterr()
        assert code == expected_code
        assert completed.err == ""
        lines = completed.out.splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert set(payload) == {
            "schema",
            "schema_version",
            "completed",
            "disposition",
            "ordinal",
            "outcome_id",
            "outcome_publication_id",
            "terminal_publication_id",
            "failure_search_reason",
            "conflict_quality_gap_band",
            "conflict_size_gap_band",
        }
        assert "ok" not in payload and "success" not in payload
        public = json.dumps(payload)
        for forbidden in ("/Users", "stderr", "argv", "crf", "score", "bytes", "asset_id", "content_class", "configuration", "public_summary"):
            assert forbidden not in public


@pytest.mark.parametrize("custody", ["world_readable", "symlink"])
def test_cli_rejects_registry_key_file_custody_without_leaking(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    custody: str,
) -> None:
    cli = _load_cli_module()
    key_file = tmp_path / "registry.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    if custody == "world_readable":
        argv_key = key_file
        key_file.chmod(0o644)
    else:
        argv_key = tmp_path / "registry.key.link"
        argv_key.symlink_to(key_file)

    code = cli.main(
        [
            "--registry",
            str(tmp_path / "registry"),
            "--registry-key-file",
            str(argv_key),
            "--plan",
            str(tmp_path / "plan.json"),
            "--qualification-request",
            str(tmp_path / "qualification-request.json"),
            "--execution-preflight",
            str(tmp_path / "execution-preflight.json"),
            "--sequencing-grant",
            str(tmp_path / "sequencing-grant.json"),
            "--sequencing-claim",
            str(tmp_path / "sequencing-claim.json"),
            "--execution-grant",
            str(tmp_path / "execution-grant.json"),
            "--execution-claim",
            str(tmp_path / "execution-claim.json"),
            "--runtime-request",
            str(tmp_path / "runtime-request.json"),
        ]
    )

    completed = capsys.readouterr()
    assert code == 1
    assert completed.err == ""
    payload = json.loads(completed.out)
    assert payload == {
        "schema": "mediaforce.av1_cold_start_v4r4_one_ordinal_cli_result",
        "schema_version": 1,
        "completed": False,
        "disposition": "fatal_failure",
        "ordinal": None,
        "outcome_id": None,
        "outcome_publication_id": None,
        "terminal_publication_id": None,
        "failure_search_reason": None,
        "conflict_quality_gap_band": None,
        "conflict_size_gap_band": None,
    }
    public = json.dumps(payload)
    assert str(argv_key) not in public
    assert "kkkk" not in public


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_cli_pre_run_interrupt_emits_one_bounded_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    interrupt_type: type[BaseException],
) -> None:
    cli = _load_cli_module()

    with patch.object(cli, "_read_custody_key", side_effect=interrupt_type()):
        code = cli.main(_cli_argv_for_paths(tmp_path))

    completed = capsys.readouterr()
    assert code == 1
    assert completed.err == ""
    assert completed.out.count("\n") == 1
    assert json.loads(completed.out) == cli._ERROR_RESULT


@pytest.mark.parametrize(
    "custody",
    ["world_readable", "symlink", "hardlink", "oversized"],
)
def test_cli_rejects_runtime_request_custody_without_leaking(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    custody: str,
) -> None:
    cli = _load_cli_module()
    ctx = _authority_context(tmp_path, ordinal=1)
    request_file = _runtime_request_file(tmp_path, ctx)
    argv_request = request_file
    if custody == "world_readable":
        request_file.chmod(0o644)
    elif custody == "symlink":
        argv_request = tmp_path / "runtime-request.link"
        argv_request.symlink_to(request_file)
    elif custody == "hardlink":
        argv_request = tmp_path / "runtime-request.hardlink"
        os.link(request_file, argv_request)
    else:
        request_file.write_bytes(b"{" + b" " * cli._MAX_PRIVATE_REQUEST_BYTES + b"}")
        request_file.chmod(0o600)

    code = cli.main(_cli_argv(ctx, argv_request))

    completed = capsys.readouterr()
    assert code == 1
    assert completed.err == ""
    assert completed.out.count("\n") == 1
    assert json.loads(completed.out) == cli._ERROR_RESULT
    public = completed.out
    assert str(argv_request) not in public
    assert "runtime-request" not in public


def test_private_runtime_request_replacement_fails_closed(tmp_path: Path) -> None:
    cli = _load_cli_module()
    ctx = _authority_context(tmp_path, ordinal=1)
    request_file = _runtime_request_file(tmp_path, ctx)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(request_file.read_bytes())
    replacement.chmod(0o600)
    real_open = os.open
    swapped = False

    def swapping_open(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if path == request_file and not swapped:
            replacement.replace(request_file)
            swapped = True
        return real_open(path, *args, **kwargs)

    with patch.object(cli.os, "open", side_effect=swapping_open):
        with pytest.raises(ValueError, match="invalid private runtime request"):
            cli._load_private_request(request_file)


def test_private_runtime_request_mutation_fails_closed(tmp_path: Path) -> None:
    cli = _load_cli_module()
    ctx = _authority_context(tmp_path, ordinal=1)
    request_file = _runtime_request_file(tmp_path, ctx)
    real_open = os.open
    real_read = os.read
    request_fd: int | None = None
    mutated = False

    def tracking_open(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal request_fd
        fd = real_open(path, *args, **kwargs)
        if path == request_file:
            request_fd = fd
        return fd

    def mutating_read(fd: int, size: int) -> bytes:
        nonlocal mutated
        data = real_read(fd, size)
        if fd == request_fd and not mutated:
            with request_file.open("ab") as stream:
                stream.write(b" ")
            mutated = True
        return data

    with (
        patch.object(cli.os, "open", side_effect=tracking_open),
        patch.object(cli.os, "read", side_effect=mutating_read),
    ):
        with pytest.raises(ValueError, match="invalid private runtime request"):
            cli._load_private_request(request_file)


def test_private_runtime_request_accepts_short_descriptor_reads(tmp_path: Path) -> None:
    cli = _load_cli_module()
    ctx = _authority_context(tmp_path, ordinal=1)
    request_file = _runtime_request_file(tmp_path, ctx)
    real_read = os.read

    with patch.object(
        cli.os,
        "read",
        side_effect=lambda fd, size: real_read(fd, min(size, 7)),
    ):
        payload = cli._load_private_request(request_file)

    assert payload["source_path"] == str(_runtime_inputs(ctx).source_path)


def test_private_runtime_request_rejects_premature_eof(tmp_path: Path) -> None:
    cli = _load_cli_module()
    ctx = _authority_context(tmp_path, ordinal=1)
    request_file = _runtime_request_file(tmp_path, ctx)
    real_read = os.read
    reads = 0

    def truncated_read(fd: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        if reads > 1:
            return b""
        return real_read(fd, min(size, 7))

    with patch.object(cli.os, "read", side_effect=truncated_read):
        with pytest.raises(ValueError, match="invalid private runtime request"):
            cli._load_private_request(request_file)


def test_private_runtime_request_requires_no_follow(tmp_path: Path) -> None:
    cli = _load_cli_module()
    ctx = _authority_context(tmp_path, ordinal=1)
    request_file = _runtime_request_file(tmp_path, ctx)

    with patch.object(cli.os, "O_NOFOLLOW", 0):
        with pytest.raises(ValueError, match="invalid private runtime request"):
            cli._load_private_request(request_file)


def _authority_context(tmp_path: Path, *, ordinal: int) -> dict[str, Any]:
    prepared = _prepared_chain(tmp_path)
    binding, plan, clock = prepared.binding, prepared.plan, prepared.clock
    for prior in range(1, ordinal):
        seq_grant = (
            prepared.sequencing_grant
            if prior == 1
            else _sequencing_grant(binding, plan, clock, ordinal=prior)
        )
        seq_claim = _sequencing_claim(binding, plan, seq_grant, clock)
        exec_grant = _execution_grant(
            plan,
            seq_grant,
            qualification_request=prepared.request,
            execution_preflight=prepared.preflight,
        )
        exec_claim = _execution_claim(plan, seq_claim, exec_grant)
        _write_authority_files(binding, plan, seq_grant, seq_claim, exec_grant, exec_claim)
        _run(
            {
                "binding": binding,
                "request": prepared.request,
                "preflight": prepared.preflight,
                "private_inputs": prepared.private_inputs,
                "plan": plan,
                "clock": clock,
                "seq_grant": seq_grant,
                "seq_claim": seq_claim,
                "exec_grant": exec_grant,
                "exec_claim": exec_claim,
            },
            lambda *_args, prior=prior, target=int(seq_grant["target_size_bytes"]), cap=int(seq_grant["source_cap_total_bytes"]), **kwargs: _prior_runtime_result(
                prior,
                target,
                cap,
                kwargs["stream_budget_ledger"],
                kwargs.get("warm_start"),
            ),
        )
    seq_grant = (
        prepared.sequencing_grant
        if ordinal == 1
        else _sequencing_grant(binding, plan, clock, ordinal=ordinal)
    )
    seq_claim = _sequencing_claim(binding, plan, seq_grant, clock)
    exec_grant = _execution_grant(
        plan,
        seq_grant,
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
    )
    exec_claim = _execution_claim(plan, seq_claim, exec_grant)
    files = _write_authority_files(binding, plan, seq_grant, seq_claim, exec_grant, exec_claim)
    return {
        "binding": binding,
        "request": prepared.request,
        "preflight": prepared.preflight,
        "private_inputs": prepared.private_inputs,
        "plan": plan,
        "clock": clock,
        "seq_grant": seq_grant,
        "seq_claim": seq_claim,
        "exec_grant": exec_grant,
        "exec_claim": exec_claim,
        "target": int(seq_grant["target_size_bytes"]),
        "cap": int(seq_grant["source_cap_total_bytes"]),
        "request_file": Path(prepared.private_inputs["preparation_registry"])
        / "v4r4-qualification-request.json",
        "preflight_file": Path(prepared.private_inputs["preparation_registry"])
        / "v4r4-execution-preflight.json",
        "registry_key_file": Path(prepared.private_inputs["preparation_registry"])
        / "v4r4-path-privacy.key",
        **files,
    }


def _run(ctx: Mapping[str, Any], callback: Callable[..., QualitySearchResult]) -> AV1V4R4OneOrdinalRunResult:
    with patch(
        "mediaforce.tuning.av1_validation_v4r4_one_ordinal_runner._utc_now",
        ctx["clock"],
    ):
        return run_av1_v4r4_one_ordinal(
            binding=ctx["binding"],
            qualification_request=ctx["request"],
            execution_preflight=ctx["preflight"],
            plan=ctx["plan"],
            sequencing_grant=ctx["seq_grant"],
            sequencing_claim=ctx["seq_claim"],
            execution_grant=ctx["exec_grant"],
            execution_claim=ctx["exec_claim"],
            runtime_inputs=_runtime_inputs(ctx),
            search_quality_for_source=callback,
        )


def _write_authority_files(
    binding: Any,
    plan: Mapping[str, Any],
    seq_grant: Mapping[str, Any],
    seq_claim: Mapping[str, Any],
    exec_grant: Mapping[str, Any],
    exec_claim: Mapping[str, Any],
) -> dict[str, Path]:
    plan_file = binding.registry / "v4r4-ordinal-registry-plan.json"
    seq_grant_file = binding.registry / f"v4r4-ordinal-{seq_grant['ordinal']:02d}-sequencing-grant.json"
    seq_claim_file = binding.registry / f"v4r4-ordinal-{seq_claim['ordinal']:02d}-sequencing-claim.json"
    exec_grant_file = binding.registry / f"v4r4-ordinal-{exec_grant['ordinal']:02d}-execution-grant.json"
    exec_claim_file = binding.registry / f"v4r4-ordinal-{exec_claim['ordinal']:02d}-execution-claim.json"
    exec_grant_file.write_bytes(_private_canonical_bytes(exec_grant))
    exec_claim_file.write_bytes(_private_canonical_bytes(exec_claim))
    exec_grant_file.chmod(0o600)
    exec_claim_file.chmod(0o600)
    return {
        "plan_file": plan_file,
        "seq_grant_file": seq_grant_file,
        "seq_claim_file": seq_claim_file,
        "exec_grant_file": exec_grant_file,
        "exec_claim_file": exec_claim_file,
    }


def _conflict_trace(reason: str, target: int, cap: int) -> dict[str, object]:
    if reason == "all_candidates_violate_quality_floor":
        candidates = [_candidate(1, 79.5, target)]
    elif reason == "target_band_violates_quality_floor":
        candidates = [_candidate(1, 79.0, target), _candidate(2, 80.1, target * 80 // 100)]
    else:
        candidates = [_candidate(1, 70.0, target), _candidate(2, 80.1, cap + 1)]
    return {"status": "quality_conflict", "selection_reason": reason, "candidates": candidates}


def _candidate(attempt: int, score: float, projected: int) -> dict[str, object]:
    return {
        "attempt": attempt,
        "metric": "VMAF",
        "metric_target": 85.0,
        "metric_score": score,
        "min_metric_score": 80.0,
        "predicted_whole_episode_bytes": projected,
    }


def _prior_runtime_result(
    ordinal: int,
    target: int,
    cap: int,
    ledger: Any,
    warm_start: Any,
) -> QualitySearchResult:
    if ordinal == 3:
        raise TargetSizeSearchError(
            "private",
            status="quality_conflict",
            trace=_bind_trace_to_ledger(
                _conflict_trace("target_band_violates_quality_floor", target, cap),
                ledger,
            ),
        )
    if ordinal == 2:
        return _quality_result(ledger, warm_start=warm_start, warm_status="rejected_fallback", fallback_reason="size_band_miss")
    return _quality_result(ledger)


def _quality_result(
    ledger: Any,
    *,
    warm_start: Any = None,
    warm_status: str | None = None,
    fallback_reason: str | None = None,
) -> QualitySearchResult:
    return QualitySearchResult(
        crf=28.0,
        metric="vmaf",
        target=85.0,
        score=86.0,
        stdout="private",
        target_size_trace=_trace_for_ledger(
            ledger,
            warm_start=warm_start,
            warm_status=warm_status or ("accepted" if warm_start is not None else None),
            fallback_reason=fallback_reason,
        ),
    )


def _trace_for_ledger(
    ledger: Any,
    *,
    warm_start: Any = None,
    warm_status: str | None = None,
    fallback_reason: str | None = None,
) -> dict[str, object]:
    trace: dict[str, object] = {
        "schema_version": 1,
        "status": "selected",
        "selection_reason": "candidate_inside_sample_projection_band",
        "curve": {"candidate_count": 1},
    }
    if ledger is not None:
        trace["ledger"] = {
            "ledger_id": ledger.ledger_id,
            "source_id": ledger.source_id,
            "stream_plan_id": ledger.stream_plan.plan_id,
        }
    if warm_status is not None:
        trace["warm_start"] = {
            "attempted": True,
            "status": warm_status,
            "fallback_used": warm_status == "rejected_fallback",
            "fallback_reason": fallback_reason,
            "candidate_count": 1,
            "duration_seconds": 1.0,
            "baseline_candidate_count": 1,
            "requested_crf": getattr(warm_start, "requested_crf", 28.0),
            "candidate_crf": getattr(warm_start, "candidate_crf", 28),
            "search_signature_id": getattr(warm_start, "search_signature_id", "av1v4r4sig_testtrace"),
            "cohort_id": getattr(warm_start, "cohort_id", "av1v4r4cohort_testtrace"),
            "source": getattr(warm_start, "source", "av1_cold_start_v4r4_qualification"),
            "confidence": getattr(warm_start, "confidence", None),
            "provenance_id": getattr(warm_start, "provenance_id", None),
            "review_risks": list(getattr(warm_start, "review_risks", ())),
        }
    return trace


def _bind_trace_to_ledger(trace: Mapping[str, Any], ledger: Any) -> dict[str, Any]:
    bound = dict(trace)
    bound["ledger"] = {
        "ledger_id": ledger.ledger_id,
        "source_id": ledger.source_id,
        "stream_plan_id": ledger.stream_plan.plan_id,
    }
    return bound


def _conflict_error(
    trace: Mapping[str, Any],
    ledger: Any,
    *,
    message: str = "private",
) -> TargetSizeSearchError:
    return TargetSizeSearchError(
        message,
        status="quality_conflict",
        trace=_bind_trace_to_ledger(trace, ledger),
    )


def _runtime_inputs(ctx: Mapping[str, Any]) -> AV1V4R4OneOrdinalRuntimeInputs:
    ordinal = int(ctx["seq_grant"]["ordinal"])
    asset_id = str(ctx["seq_grant"]["asset_id"])
    invocation = next(
        item
        for item in ctx["request"]["invocation_digests"]
        if item["ordinal"] == ordinal
    )
    return AV1V4R4OneOrdinalRuntimeInputs(
        source_path=Path(ctx["private_inputs"]["source_paths"][asset_id]),
        quality_temp_path=Path(ctx["private_inputs"]["quality_temp_paths"][asset_id]),
        width=int(invocation["width"]),
        height=int(invocation["height"]),
        source_codec=str(invocation["source_codec"]),
    )


def _runtime_request_file(tmp_path: Path, ctx: Mapping[str, Any]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    request = tmp_path / "runtime-request.json"
    runtime_inputs = _runtime_inputs(ctx)
    request.write_bytes(
        canonical_json_bytes(
            {
                "source_path": str(runtime_inputs.source_path),
                "quality_temp_path": str(runtime_inputs.quality_temp_path),
                "width": runtime_inputs.width,
                "height": runtime_inputs.height,
                "source_codec": runtime_inputs.source_codec,
            }
        )
        + b"\n"
    )
    request.chmod(0o600)
    return request


def _cli_argv(ctx: Mapping[str, Any], request_file: Path) -> list[str]:
    return [
        "--registry",
        str(ctx["binding"].registry),
        "--registry-key-file",
        str(ctx["registry_key_file"]),
        "--plan",
        str(ctx["plan_file"]),
        "--qualification-request",
        str(ctx["request_file"]),
        "--execution-preflight",
        str(ctx["preflight_file"]),
        "--sequencing-grant",
        str(ctx["seq_grant_file"]),
        "--sequencing-claim",
        str(ctx["seq_claim_file"]),
        "--execution-grant",
        str(ctx["exec_grant_file"]),
        "--execution-claim",
        str(ctx["exec_claim_file"]),
        "--runtime-request",
        str(request_file),
    ]


def _cli_argv_for_paths(tmp_path: Path) -> list[str]:
    return [
        "--registry",
        str(tmp_path / "registry"),
        "--registry-key-file",
        str(tmp_path / "registry.key"),
        "--plan",
        str(tmp_path / "plan.json"),
        "--qualification-request",
        str(tmp_path / "qualification-request.json"),
        "--execution-preflight",
        str(tmp_path / "execution-preflight.json"),
        "--sequencing-grant",
        str(tmp_path / "sequencing-grant.json"),
        "--sequencing-claim",
        str(tmp_path / "sequencing-claim.json"),
        "--execution-grant",
        str(tmp_path / "execution-grant.json"),
        "--execution-claim",
        str(tmp_path / "execution-claim.json"),
        "--runtime-request",
        str(tmp_path / "runtime-request.json"),
    ]


def _load_cli_module() -> Any:
    script = Path.cwd() / "scripts" / "run_av1_v4r4_one_ordinal.py"
    spec = importlib.util.spec_from_file_location("run_av1_v4r4_one_ordinal_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load CLI module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raise(exc: BaseException) -> Never:
    raise exc
