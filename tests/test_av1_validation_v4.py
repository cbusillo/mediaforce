from __future__ import annotations

import copy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1ValidationV4Error,
    assert_av1_validation_manifest_v4,
    assert_av1_validation_manifest_v4_current,
    av1_validation_v4_manifest_id,
    load_av1_validation_manifest_v4,
    load_av1_validation_v4_discovery_public,
    serialize_av1_validation_manifest_v4,
)


MANIFEST_PATH = Path("docs/validation/av1-cold-start-preregistration-v4.json")
DISCOVERY_PATH = Path("docs/validation/av1-v4-discovery-public-v1.json")
V3_PATH = Path("docs/validation/av1-cold-start-preregistration-v3.json")


class AV1ValidationV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_av1_validation_manifest_v4(
            MANIFEST_PATH,
            discovery_public_path=DISCOVERY_PATH,
        )
        self.discovery = load_av1_validation_v4_discovery_public(DISCOVERY_PATH)

    def test_checked_in_payloads_are_canonical_and_bound(self) -> None:
        self.assertEqual(
            MANIFEST_PATH.read_bytes(),
            serialize_av1_validation_manifest_v4(self.manifest),
        )
        self.assertEqual(self.manifest["manifest_id"], AV1_VALIDATION_V4_MANIFEST_ID)
        self.assertEqual(self.manifest["payload_sha256"], AV1_VALIDATION_V4_PAYLOAD_SHA256)
        self.assertEqual(
            self.manifest["discovery_public_sha256"],
            AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        )
        self.assertEqual(
            f"sha256:{hashlib.sha256(DISCOVERY_PATH.read_bytes()).hexdigest()}",
            AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        )
        self.assertEqual(self.manifest["sources"], self.discovery["sources"])

    def test_manifest_is_explicitly_unapproved_and_nonexecuting(self) -> None:
        self.assertEqual(self.manifest["state"], "draft_unapproved")
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            with self.subTest(field=field):
                self.assertIs(self.manifest[field], False)
                self.assertIs(self.discovery[field], False)
        self.assertTrue(
            self.manifest["preparation_requirements"]["required_before_owner_freeze"]
        )

    def test_source_order_and_class_role_coverage_are_frozen(self) -> None:
        self.assertEqual(tuple(self.manifest["source_order"]), AV1_VALIDATION_V4_SOURCE_IDS)
        self.assertEqual(
            [source["asset_id"] for source in self.manifest["sources"]],
            list(AV1_VALIDATION_V4_SOURCE_IDS),
        )
        self.assertEqual(
            {(source["class"], source["role"]) for source in self.manifest["sources"]},
            {
                ("animation_content", "primary"),
                ("animation_content", "confirmation"),
                ("live_action_content", "primary"),
                ("live_action_content", "confirmation"),
            },
        )

    def test_primary_traversals_precede_confirmation_traversals(self) -> None:
        traversals = self.manifest["qualification_matrix"]["traversals"]
        self.assertEqual(
            [item["asset_id"] for item in traversals],
            [
                AV1_VALIDATION_V4_SOURCE_IDS[0],
                AV1_VALIDATION_V4_SOURCE_IDS[0],
                AV1_VALIDATION_V4_SOURCE_IDS[2],
                AV1_VALIDATION_V4_SOURCE_IDS[2],
                AV1_VALIDATION_V4_SOURCE_IDS[1],
                AV1_VALIDATION_V4_SOURCE_IDS[1],
                AV1_VALIDATION_V4_SOURCE_IDS[3],
                AV1_VALIDATION_V4_SOURCE_IDS[3],
            ],
        )

    def test_nasa_audio_is_structurally_excluded(self) -> None:
        nasa = self.manifest["sources"][3]
        self.assertEqual(nasa["qualification_video_stream_index"], 0)
        self.assertEqual(nasa["stream_constraint"]["allowed_stream_indexes"], [0])
        self.assertEqual(nasa["stream_constraint"]["excluded_stream_indexes"], [1])
        self.assertEqual(nasa["observed_streams"][1]["codec_type"], "audio")
        self.assertTrue(self.manifest["rights_constraints"]["nasa_video_stream_only"])

    def test_archive_and_license_resolution_are_frozen(self) -> None:
        tears = self.manifest["sources"][2]
        self.assertEqual(tears["outer_archive"]["entry_name"], "ToS-4k-1920.mov")
        self.assertEqual(tears["outer_archive"]["entry_count"], 1)
        self.assertTrue(tears["outer_archive"]["safe_path"])
        self.assertEqual(
            self.manifest["rights_constraints"]["cosmos_license_precedence"],
            "official_blender_gooseberry_title_grant",
        )

    def test_balanced_policy_and_preparation_split_are_frozen(self) -> None:
        invocation = self.manifest["qualification_invocation"]
        self.assertEqual(
            invocation["video_policy"],
            {
                "compression_intent_schema_version": 1,
                "compression_intent": "balanced",
                "compression_intent_source": "operator",
                "compression_intent_confirmed": True,
                "min_crf": 10,
                "max_crf": 45,
            },
        )
        self.assertEqual(
            invocation["baseline"],
            {"warm_start": None, "expected_search_signature_id": None},
        )
        self.assertTrue(invocation["concrete_invocation_digests_preparation_required"])
        self.assertTrue(invocation["invocation_digests_must_differ"])

    def test_manifest_id_and_payload_sha_recompute(self) -> None:
        semantic = {
            key: value
            for key, value in self.manifest.items()
            if key not in {"manifest_id", "payload_sha256"}
        }
        self.assertEqual(av1_validation_v4_manifest_id(semantic), AV1_VALIDATION_V4_MANIFEST_ID)
        payload_without_sha = {
            key: value
            for key, value in self.manifest.items()
            if key != "payload_sha256"
        }
        expected = hashlib.sha256(
            json.dumps(
                payload_without_sha,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        self.assertEqual(self.manifest["payload_sha256"], f"sha256:{expected}")

    def test_activity_window_boundaries(self) -> None:
        assert_av1_validation_manifest_v4_current(
            self.manifest,
            as_of=datetime(2026, 8, 6, 18, 53, 35, tzinfo=UTC),
        )
        with self.assertRaises(AV1ValidationV4Error):
            assert_av1_validation_manifest_v4_current(
                self.manifest,
                as_of=datetime(2026, 8, 6, 18, 53, 34, tzinfo=UTC),
            )
        with self.assertRaises(AV1ValidationV4Error):
            assert_av1_validation_manifest_v4_current(
                self.manifest,
                as_of=datetime(2027, 2, 2, 18, 53, 35, tzinfo=UTC),
            )
        with self.assertRaisesRegex(AV1ValidationV4Error, "timezone-aware"):
            assert_av1_validation_manifest_v4_current(
                self.manifest,
                as_of=datetime(2026, 8, 6, 18, 53, 35),
            )

    def test_mutations_fail_closed(self) -> None:
        mutations = []
        authority = copy.deepcopy(self.manifest)
        authority["qualification_execution_authorized"] = True
        mutations.append(authority)
        reordered = copy.deepcopy(self.manifest)
        reordered["sources"][0], reordered["sources"][1] = (
            reordered["sources"][1],
            reordered["sources"][0],
        )
        mutations.append(reordered)
        nasa_audio = copy.deepcopy(self.manifest)
        nasa_audio["sources"][3]["stream_constraint"]["allowed_stream_indexes"] = [0, 1]
        mutations.append(nasa_audio)
        archive = copy.deepcopy(self.manifest)
        archive["sources"][2]["outer_archive"]["safe_path"] = False
        mutations.append(archive)
        policy = copy.deepcopy(self.manifest)
        policy["qualification_invocation"]["video_policy"]["max_crf"] = 46
        mutations.append(policy)
        traversal = copy.deepcopy(self.manifest)
        traversal["qualification_matrix"]["traversals"][0]["asset_id"] = (
            AV1_VALIDATION_V4_SOURCE_IDS[1]
        )
        mutations.append(traversal)
        private_path = copy.deepcopy(self.manifest)
        private_path["preparation_requirements"]["workspace"] = "/Volumes/private"
        mutations.append(private_path)
        for index, payload in enumerate(mutations):
            with self.subTest(index=index):
                with self.assertRaises(AV1ValidationV4Error):
                    assert_av1_validation_manifest_v4(payload)

    def test_noncanonical_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(self.manifest, indent=2) + "\n")
            with self.assertRaisesRegex(AV1ValidationV4Error, "not canonical"):
                load_av1_validation_manifest_v4(path)

    def test_discovery_projection_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.json"
            mutated = copy.deepcopy(self.discovery)
            mutated["source_count"] = 3
            path.write_text(
                json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n"
            )
            with self.assertRaises(AV1ValidationV4Error):
                load_av1_validation_manifest_v4(
                    MANIFEST_PATH,
                    discovery_public_path=path,
                )

    def test_standalone_discovery_loader_binds_exact_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.json"
            mutated = copy.deepcopy(self.discovery)
            mutated["rights_notes"]["redistribution"] += " mutated"
            path.write_text(
                json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n"
            )
            with self.assertRaisesRegex(AV1ValidationV4Error, "SHA-256"):
                load_av1_validation_v4_discovery_public(path)

    def test_missing_source_identity_uses_contract_error(self) -> None:
        payload = copy.deepcopy(self.manifest)
        payload["sources"][0].pop("asset_id")
        with self.assertRaises(AV1ValidationV4Error):
            assert_av1_validation_manifest_v4(payload)

    def test_isolated_validator_negative_paths_return_json_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            cases: list[tuple[str, bytes]] = []
            cases.append(("noncanonical", json.dumps(self.manifest, indent=2).encode()))
            missing_state = copy.deepcopy(self.manifest)
            missing_state.pop("state")
            cases.append((
                "missing-state",
                json.dumps(missing_state, sort_keys=True, separators=(",", ":")).encode()
                + b"\n",
            ))
            for name, content in cases:
                with self.subTest(name=name):
                    manifest_path = directory_path / f"{name}.json"
                    manifest_path.write_bytes(content)
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-S",
                            "scripts/verify_av1_v4_manifest.py",
                            str(manifest_path),
                            str(DISCOVERY_PATH),
                            "--json",
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(completed.stderr, "")
                    result = json.loads(completed.stdout)
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["error"])

    def test_public_files_contain_no_machine_local_paths(self) -> None:
        for path in (MANIFEST_PATH, DISCOVERY_PATH):
            content = path.read_text()
            for forbidden in (
                "/Users/",
                "/Volumes/",
                "/opt/homebrew/",
                '"workspace"',
                '"ffprobe_path"',
                '"source_path"',
                '"media_root"',
            ):
                with self.subTest(path=path, forbidden=forbidden):
                    self.assertNotIn(forbidden, content)

    def test_v3_protocol_is_unchanged_and_distinct(self) -> None:
        v3 = json.loads(V3_PATH.read_text())
        self.assertEqual(v3["protocol_id"], "av1vprotocol3_ba85a44eef70b857d678b236bb1b4afc")
        self.assertEqual(
            v3["payload_sha256"],
            "sha256:d17606e4920846de810ab467d63a194f6a9b9138f6d8416a3ff3e0416c37a590",
        )
        self.assertNotEqual(v3["payload_sha256"], self.manifest["payload_sha256"])

    def test_isolated_validator_reports_only_false_authorities(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "scripts/verify_av1_v4_manifest.py",
                str(MANIFEST_PATH),
                str(DISCOVERY_PATH),
                "--as-of",
                "2026-08-06T18:53:35Z",
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        summary = json.loads(completed.stdout)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["state"], "draft_unapproved")
        self.assertEqual(summary["source_count"], 4)
        self.assertEqual(summary["traversal_count"], 8)
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            with self.subTest(field=field):
                self.assertIs(summary[field], False)


if __name__ == "__main__":
    unittest.main()
