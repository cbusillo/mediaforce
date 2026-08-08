from __future__ import annotations

import ast
from pathlib import Path
import unittest

from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning import av1_validation_v4r3_runner_admission as module
from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
from mediaforce.tuning.av1_validation_v4r3_runner_admission import (
    AV1V4R3RunnerAdmissionError,
    assert_av1_v4r3_execution_claim,
    assert_av1_v4r3_execution_claim_chain,
    assert_av1_v4r3_runner_admission,
    assert_av1_v4r3_runner_admission_chain,
    build_av1_v4r3_execution_claim,
    build_av1_v4r3_runner_admission,
    deserialize_av1_v4r3_execution_claim,
    deserialize_av1_v4r3_runner_admission,
    serialize_av1_v4r3_execution_claim,
    serialize_av1_v4r3_runner_admission,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_FULL_VIDEO_POLICY,
    av1_v4_r3_resolved_size_goal_payload,
    av1_v4_r3_stream_ledger_closure_payload,
)
from mediaforce.tuning.size_goals import size_goal_from_policy
from tests.test_av1_validation_v4r3_execution_grant import _execution_chain


class AV1V4R3RunnerAdmissionTests(unittest.TestCase):
    def test_claim_and_admission_round_trip_without_authority(self) -> None:
        plan, preflight, sequencing, grant = _execution_chain()
        claim = build_av1_v4r3_execution_claim(
            execution_grant=grant,
            sequencing_grant=sequencing,
            claimed_at="2026-08-08T04:10:00Z",
        )
        stream_plan, ledger = _runtime_payloads(preflight)
        admission = _admission(
            plan,
            claim,
            grant,
            sequencing,
            preflight,
            production_stream_plan=stream_plan,
            stream_budget_ledger=ledger,
        )
        self.assertEqual(
            deserialize_av1_v4r3_execution_claim(
                serialize_av1_v4r3_execution_claim(claim)
            ),
            claim,
        )
        self.assertEqual(
            deserialize_av1_v4r3_runner_admission(
                serialize_av1_v4r3_runner_admission(admission)
            ),
            admission,
        )
        self.assertEqual(
            admission["stream_budget_ledger_id"],
            admission["returned_stream_budget_ledger_id"],
        )
        for artifact in (claim, admission):
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                self.assertIs(artifact[field], False)
        assert_av1_v4r3_execution_claim_chain(
            execution_claim=claim,
            execution_grant=grant,
            sequencing_grant=sequencing,
        )
        assert_av1_v4r3_runner_admission_chain(
            plan=plan,
            preflight=preflight,
            sequencing_grant=sequencing,
            execution_grant=grant,
            execution_claim=claim,
            runner_admission=admission,
            production_stream_plan=stream_plan,
            stream_budget_ledger=ledger,
            returned_stream_budget_ledger=ledger,
        )

    def test_rejects_claim_admission_and_runtime_binding_substitution(self) -> None:
        plan, preflight, sequencing, grant = _execution_chain()
        claim = build_av1_v4r3_execution_claim(
            execution_grant=grant,
            sequencing_grant=sequencing,
            claimed_at="2026-08-08T04:10:00Z",
        )
        mutated_claim = dict(claim)
        mutated_claim["ordinal"] = 2
        rebound_claim = _rebind(mutated_claim, "claim_id", "av1v4r3execclaim")
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            _admission(plan, rebound_claim, grant, sequencing, preflight)

        readiness = preflight["ordinal_readiness"][0]
        stream_plan, ledger = _runtime_payloads(preflight)
        returned_ledger = dict(ledger)
        returned_ledger["uncertainty"] = {
            **ledger["uncertainty"],
            "confidence": "high",
        }
        returned_ledger = _rebind_content(returned_ledger, "ledger_id", "sb1")
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            _admission(
                plan,
                claim,
                grant,
                sequencing,
                preflight,
                production_stream_plan=stream_plan,
                stream_budget_ledger=ledger,
                returned_stream_budget_ledger=returned_ledger,
            )
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            _admission(
                plan,
                claim,
                grant,
                sequencing,
                preflight,
                transform_plan_id="tp1_" + "f" * 32,
            )
        admission = _admission(plan, claim, grant, sequencing, preflight)
        mutated = dict(admission)
        mutated["source_path_hmac_id"] = "/Volumes/private/source.mkv"
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            assert_av1_v4r3_runner_admission(
                _rebind(mutated, "admission_id", "av1v4r3admit")
            )
        for artifact, assertion, id_field, prefix, field, value in (
            (
                claim,
                assert_av1_v4r3_execution_claim,
                "claim_id",
                "av1v4r3execclaim",
                "schema_version",
                True,
            ),
            (
                claim,
                assert_av1_v4r3_execution_claim,
                "claim_id",
                "av1v4r3execclaim",
                "asset_id",
                "",
            ),
            (
                admission,
                assert_av1_v4r3_runner_admission,
                "admission_id",
                "av1v4r3admit",
                "protocol_version",
                4.0,
            ),
            (
                admission,
                assert_av1_v4r3_runner_admission,
                "admission_id",
                "av1v4r3admit",
                "asset_id",
                "/Volumes/private/source.mkv",
            ),
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(AV1V4R3RunnerAdmissionError),
            ):
                rebound = dict(artifact)
                rebound[field] = value
                assertion(_rebind(rebound, id_field, prefix))
        self.assertEqual(admission["transform_plan_id"], readiness["transform_plan_id"])

    def test_rejects_unrelated_runtime_plan_and_ledger_payloads(self) -> None:
        plan, preflight, sequencing, grant = _execution_chain()
        claim = build_av1_v4r3_execution_claim(
            execution_grant=grant,
            sequencing_grant=sequencing,
            claimed_at="2026-08-08T04:10:00Z",
        )
        stream_plan, ledger = _runtime_payloads(preflight)

        unrelated_plan = dict(stream_plan)
        unrelated_plan["source_id"] = "src1_" + "f" * 24
        unrelated_plan = _rebind_content(unrelated_plan, "plan_id", "sp1")
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            _admission(
                plan,
                claim,
                grant,
                sequencing,
                preflight,
                production_stream_plan=unrelated_plan,
                stream_budget_ledger=ledger,
            )

        unrelated_ledger = dict(ledger)
        unrelated_ledger["source"] = {
            **ledger["source"],
            "source_size_bytes": int(ledger["source"]["source_size_bytes"]) + 1,
        }
        unrelated_ledger = _rebind_content(unrelated_ledger, "ledger_id", "sb1")
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            _admission(
                plan,
                claim,
                grant,
                sequencing,
                preflight,
                production_stream_plan=stream_plan,
                stream_budget_ledger=unrelated_ledger,
                returned_stream_budget_ledger=unrelated_ledger,
            )

        nested_mutations = (
            (
                "source_size_float",
                {
                    **ledger,
                    "source": {
                        **ledger["source"],
                        "source_size_bytes": float(
                            ledger["source"]["source_size_bytes"]
                        ),
                    },
                },
            ),
            (
                "boolean_as_integer",
                {
                    **ledger,
                    "feasibility": {
                        **ledger["feasibility"],
                        "requires_measurement": 0,
                    },
                },
            ),
            (
                "size_goal_schema_boolean",
                {
                    **ledger,
                    "size_goal": {**ledger["size_goal"], "schema_version": True},
                },
            ),
            (
                "size_goal_unknown_field",
                {
                    **ledger,
                    "size_goal": {**ledger["size_goal"], "unexpected": "value"},
                },
            ),
            (
                "size_goal_target_mb",
                {
                    **ledger,
                    "size_goal": {**ledger["size_goal"], "target_size_mb": 0.001},
                },
            ),
        )
        for label, mutated in nested_mutations:
            with (
                self.subTest(label=label),
                self.assertRaises(AV1V4R3RunnerAdmissionError),
            ):
                rebound = _rebind_content(mutated, "ledger_id", "sb1")
                _admission(
                    plan,
                    claim,
                    grant,
                    sequencing,
                    preflight,
                    production_stream_plan=stream_plan,
                    stream_budget_ledger=rebound,
                    returned_stream_budget_ledger=rebound,
                )

        stream_plan_with_audio = _stream_plan_with_audio(stream_plan)
        module._assert_stream_plan_payload(stream_plan_with_audio)
        for field, value in (("source_index", 1.0), ("default", 1)):
            with (
                self.subTest(plan_field=field),
                self.assertRaises(AV1V4R3RunnerAdmissionError),
            ):
                mutated_plan = dict(stream_plan_with_audio)
                stream = dict(mutated_plan["streams"][0])
                stream[field] = value
                mutated_plan["streams"] = [stream]
                mutated_plan = _rebind_content(mutated_plan, "plan_id", "sp1")
                module._assert_stream_plan_payload(mutated_plan)

    def test_chain_rejects_rebound_claim_transform_and_timeline_substitution(
        self,
    ) -> None:
        plan, preflight, sequencing, grant = _execution_chain()
        claim = build_av1_v4r3_execution_claim(
            execution_grant=grant,
            sequencing_grant=sequencing,
            claimed_at="2026-08-08T04:10:00Z",
        )
        mutated_claim = dict(claim)
        mutated_claim["preflight_payload_sha256"] = "sha256:" + "f" * 64
        rebound_claim = _rebind(mutated_claim, "claim_id", "av1v4r3execclaim")
        assert_av1_v4r3_execution_claim(rebound_claim)
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            assert_av1_v4r3_execution_claim_chain(
                execution_claim=rebound_claim,
                execution_grant=grant,
                sequencing_grant=sequencing,
            )

        stream_plan, ledger = _runtime_payloads(preflight)
        admission = _admission(
            plan,
            claim,
            grant,
            sequencing,
            preflight,
            production_stream_plan=stream_plan,
            stream_budget_ledger=ledger,
        )
        mutated_admission = dict(admission)
        mutated_admission["transform_plan_id"] = "tp1_" + "f" * 32
        rebound_admission = _rebind(mutated_admission, "admission_id", "av1v4r3admit")
        assert_av1_v4r3_runner_admission(rebound_admission)
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            assert_av1_v4r3_runner_admission_chain(
                plan=plan,
                preflight=preflight,
                sequencing_grant=sequencing,
                execution_grant=grant,
                execution_claim=claim,
                runner_admission=rebound_admission,
                production_stream_plan=stream_plan,
                stream_budget_ledger=ledger,
                returned_stream_budget_ledger=ledger,
            )

        future_claim = build_av1_v4r3_execution_claim(
            execution_grant=grant,
            sequencing_grant=sequencing,
            claimed_at="2026-08-08T04:12:00Z",
        )
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            _admission(
                plan,
                future_claim,
                grant,
                sequencing,
                preflight,
                admitted_at="2026-08-08T04:11:00Z",
            )
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            build_av1_v4r3_execution_claim(
                execution_grant=grant,
                sequencing_grant=sequencing,
                claimed_at="2026-08-08T04:09:59Z",
            )

    def test_noncanonical_bytes_and_modules_are_pure(self) -> None:
        plan, preflight, sequencing, grant = _execution_chain()
        claim = build_av1_v4r3_execution_claim(
            execution_grant=grant,
            sequencing_grant=sequencing,
            claimed_at="2026-08-08T04:10:00Z",
        )
        admission = _admission(plan, claim, grant, sequencing, preflight)
        with self.assertRaises(AV1V4R3RunnerAdmissionError):
            deserialize_av1_v4r3_runner_admission(
                serialize_av1_v4r3_runner_admission(admission).rstrip(b"\n")
            )
        for deserializer in (
            deserialize_av1_v4r3_execution_claim,
            deserialize_av1_v4r3_runner_admission,
        ):
            with (
                self.subTest(deserializer=deserializer.__name__),
                self.assertRaises(AV1V4R3RunnerAdmissionError),
            ):
                deserializer(b'{"value":NaN}\n')
        tree = ast.parse(Path(module.__file__).read_text())
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertTrue(
            {
                "os",
                "pathlib",
                "subprocess",
                "requests",
                "mediaforce.core.db",
                "mediaforce.execution",
            }.isdisjoint(imports)
        )


def _admission(
    plan: dict[str, object],
    claim: dict[str, object],
    grant: dict[str, object],
    sequencing: dict[str, object],
    preflight: dict[str, object],
    *,
    admitted_at: str = "2026-08-08T04:11:00Z",
    transform_plan_id: str | None = None,
    production_stream_plan: dict[str, object] | None = None,
    stream_budget_ledger: dict[str, object] | None = None,
    returned_stream_budget_ledger: dict[str, object] | None = None,
) -> dict[str, object]:
    readiness = preflight["ordinal_readiness"][0]
    default_stream_plan, default_ledger = _runtime_payloads(preflight)
    selected_stream_plan = production_stream_plan or default_stream_plan
    selected_ledger = stream_budget_ledger or default_ledger
    return build_av1_v4r3_runner_admission(
        plan=plan,
        execution_claim=claim,
        execution_grant=grant,
        sequencing_grant=sequencing,
        preflight=preflight,
        admitted_at=admitted_at,
        source_path_hmac_id="av1vsource4r3_" + "a" * 32,
        instance_path_hmac_ids={
            "runtime_lock": "av1vpath4r3_" + "1" * 32,
            "source_root": "av1vpath4r3_" + "2" * 32,
            "state_root": "av1vpath4r3_" + "3" * 32,
            "temp_root": "av1vpath4r3_" + "4" * 32,
        },
        quality_temp_hmac_id="av1vqtemp4r3_" + "b" * 32,
        quality_temp_key_id="av1vqtkey4r3_" + "c" * 32,
        production_stream_plan=selected_stream_plan,
        stream_budget_ledger=selected_ledger,
        transform_plan_id=transform_plan_id or str(readiness["transform_plan_id"]),
        returned_stream_budget_ledger=(
            returned_stream_budget_ledger or selected_ledger
        ),
    )


def _runtime_payloads(
    preflight: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    asset_id = str(preflight["ordinal_readiness"][0]["asset_id"])
    closure = av1_v4_r3_stream_ledger_closure_payload(asset_id)
    size_goal = av1_v4_r3_resolved_size_goal_payload(asset_id)
    stream_plan_identity: dict[str, object] = {
        "schema_version": 1,
        "source_id": "src1_" + "a" * 24,
        "source_fingerprint": "sha256:" + "b" * 64,
        "policy_hash": "sha256:" + "c" * 64,
        "output_container": closure["output_container"],
        "attachments_known": True,
        "copy_unknown_attachments": False,
        "streams": [],
    }
    stream_plan = {
        **stream_plan_identity,
        "plan_id": "sp1_" + stable_json_hash(stream_plan_identity)[:32],
    }
    resolved_size_goal = (
        size_goal_from_policy(dict(AV1_V4_R3_FULL_VIDEO_POLICY))
        .resolve(float(size_goal["source_duration_seconds"]))
        .to_payload()
    )
    target_bytes = int(size_goal["target_size_bytes"])
    ledger_identity: dict[str, object] = {
        "schema_version": 1,
        "source": {
            "source_id": stream_plan["source_id"],
            "source_fingerprint": stream_plan["source_fingerprint"],
            "source_size_bytes": closure["source_media_bytes"],
            "source_video_bitrate_bps": closure["source_video_bitrate_bps"],
            "duration_seconds": closure["source_duration_seconds"],
        },
        "policy_hash": stream_plan["policy_hash"],
        "size_goal": resolved_size_goal,
        "stream_plan": stream_plan,
        "entries": [],
        "totals": {
            "total_target_bytes": target_bytes,
            "audio_bytes": 0,
            "subtitle_bytes": 0,
            "attachment_bytes": 0,
            "container_bytes": 0,
            "non_video_bytes": 0,
            "minimum_non_video_bytes": 0,
            "maximum_non_video_bytes": 0,
            "remaining_video_bytes": target_bytes,
            "remaining_video_bitrate_bps": None,
        },
        "source_relative_cap": {
            "configured_total_percent": None,
            "total_cap_bytes": None,
            "video_cap_bytes": None,
            "video_cap_bitrate_bps": None,
            "video_cap_percent": None,
            "status": "unavailable",
        },
        "feasibility": {
            "status": "feasible",
            "reasons": [],
            "arithmetic_infeasible": False,
            "aggressive": False,
            "requires_measurement": False,
        },
        "uncertainty": {
            "confidence": "exact",
            "requires_measurement": False,
            "minimum_non_video_bytes": 0,
            "maximum_non_video_bytes": 0,
        },
    }
    ledger = {
        **ledger_identity,
        "ledger_id": "sb1_" + stable_json_hash(ledger_identity)[:32],
    }
    return stream_plan, ledger


def _rebind_content(
    payload: dict[str, object], id_field: str, prefix: str
) -> dict[str, object]:
    rebound = dict(payload)
    identity = {key: value for key, value in rebound.items() if key != id_field}
    rebound[id_field] = f"{prefix}_{stable_json_hash(identity)[:32]}"
    return rebound


def _stream_plan_with_audio(
    stream_plan: dict[str, object],
) -> dict[str, object]:
    payload = {
        **stream_plan,
        "streams": [
            {
                "kind": "audio",
                "source_index": 1,
                "source_codec": "aac",
                "action": "copy",
                "output_codec": "aac",
                "codec_argument": None,
                "output_bitrate_bps": 128_000,
                "output_bitrate_text": "128k",
                "channels": 2,
                "language": "eng",
                "default": True,
                "forced": False,
                "source_bitrate_bps": 128_000,
                "source_duration_seconds": 60.0,
                "source_size_bytes": 960_000,
                "file_name": None,
                "mime_type": None,
            }
        ],
    }
    return _rebind_content(payload, "plan_id", "sp1")


def _rebind(
    payload: dict[str, object], id_field: str, prefix: str
) -> dict[str, object]:
    rebound = dict(payload)
    semantic = {
        key: value
        for key, value in rebound.items()
        if key not in {id_field, "payload_sha256"}
    }
    rebound[id_field] = f"{prefix}_{stable_json_hash(semantic)[:32]}"
    without_sha = {
        key: value for key, value in rebound.items() if key != "payload_sha256"
    }
    rebound["payload_sha256"] = "sha256:" + stable_json_hash(without_sha)
    return rebound


if __name__ == "__main__":
    unittest.main()
