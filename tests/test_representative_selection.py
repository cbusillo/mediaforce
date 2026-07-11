import copy
import json
import unittest
from typing import Any

from mediaforce.core.evidence import build_evidence_envelope, evidence_staleness
from mediaforce.library.representatives import (
    REPRESENTATIVE_SELECTION_TOOL,
    REPRESENTATIVE_SELECTION_TOOL_VERSION,
    evidence_sources,
    public_representative_item,
    select_representatives,
)
from mediaforce.web.runtime.calibration_runtime import _stored_sample_item_payload
from mediaforce.web.runtime.folder_ai_tuning import _job_sample_item_payload


class RepresentativeSelectionTests(unittest.TestCase):
    def test_selection_is_order_independent_and_chooses_median_item(self) -> None:
        items = [
            self._item(index, duration_seconds=2580 + index * 60, source_size_bytes=(850 + index * 75) * 1_000_000)
            for index in range(1, 6)
        ]

        forward = select_representatives(items, prefix="tv/Example/Season 1", policy={"video": {"target_vmaf": 95}})
        reversed_selection = select_representatives(
            list(reversed(items)),
            prefix="tv/Example/Season 1",
            policy={"video": {"target_vmaf": 95}},
        )

        self.assertEqual(forward.payload, reversed_selection.payload)
        self.assertEqual(forward.primary_item()["rel_path"], "tv/Example/Season 1/Episode 03.mkv")
        self.assertEqual(forward.payload["coverage"]["selected_item_count"], 1)
        self.assertEqual(forward.payload["coverage"]["exact_profile_item_fraction"], 1.0)

    def test_mixed_technical_profiles_select_additional_coverage_samples(self) -> None:
        items = [
            self._item(1, duration_seconds=2700, source_size_bytes=1_000_000_000),
            self._item(2, duration_seconds=2700, source_size_bytes=1_050_000_000),
            self._item(
                3,
                duration_seconds=1350,
                source_size_bytes=550_000_000,
                video_codec="hevc",
                width=1280,
                height=720,
                cadence_class="interlaced_tff",
                channels=2,
            ),
            self._item(
                4,
                duration_seconds=1350,
                source_size_bytes=575_000_000,
                video_codec="hevc",
                width=1280,
                height=720,
                cadence_class="interlaced_tff",
                channels=2,
            ),
            self._item(5, duration_seconds=5400, source_size_bytes=1_900_000_000),
            self._item(6, duration_seconds=5400, source_size_bytes=1_950_000_000),
        ]

        selection = select_representatives(items, prefix="tv/Example/Season 1")

        selected_profiles = {
            item["technical_profile"]["runtime"]
            for item in selection.payload["selected_items"]
        }
        self.assertEqual(selected_profiles, {"short", "typical", "long"})
        self.assertEqual(selection.payload["coverage"]["selected_item_count"], 3)
        for dimension in ("video_codec", "resolution", "cadence", "audio_layout", "runtime"):
            self.assertEqual(selection.payload["coverage"]["dimensions"][dimension]["item_fraction"], 1.0)
        coverage_roles = [item["rationale"]["role"] for item in selection.payload["selected_items"]]
        self.assertEqual(coverage_roles.count("primary"), 1)
        self.assertEqual(coverage_roles.count("coverage"), 2)

    def test_largest_file_outlier_is_reported_without_becoming_a_sample(self) -> None:
        sizes = [900_000_000, 950_000_000, 1_000_000_000, 1_050_000_000, 12_000_000_000]
        items = [
            self._item(index, duration_seconds=2700, source_size_bytes=size)
            for index, size in enumerate(sizes, start=1)
        ]

        selection = select_representatives(items, prefix="tv/Example/Season 1")

        selected_paths = {item["rel_path"] for item in selection.payload["selected_items"]}
        self.assertNotIn("tv/Example/Season 1/Episode 05.mkv", selected_paths)
        self.assertEqual(selection.payload["coverage"]["selected_item_count"], 1)
        self.assertEqual(
            selection.payload["outliers"],
            [
                {
                    "source_id": selection.payload["outliers"][0]["source_id"],
                    "rel_path": "tv/Example/Season 1/Episode 05.mkv",
                    "reasons": ["source_size_outlier"],
                    "selected": False,
                }
            ],
        )

    def test_evidence_staleness_detects_source_policy_and_tool_changes(self) -> None:
        items = [self._item(index) for index in range(1, 4)]
        selection = select_representatives(items, prefix="tv/Example/Season 1", policy={"video": {"target_vmaf": 95}})
        evidence = selection.payload["evidence"]
        policy_hash = evidence["inputs"]["policy_hash"]

        self.assertEqual(
            evidence_staleness(
                evidence,
                sources=evidence_sources(items),
                policy_hash=policy_hash,
                tool_name=REPRESENTATIVE_SELECTION_TOOL,
                tool_version=REPRESENTATIVE_SELECTION_TOOL_VERSION,
            ),
            {"stale": False, "reasons": []},
        )

        changed_source = copy.deepcopy(items)
        changed_source[0]["source_fingerprint"] = "changed-fingerprint"
        self.assertEqual(
            evidence_staleness(
                evidence,
                sources=evidence_sources(changed_source),
                policy_hash=policy_hash,
                tool_name=REPRESENTATIVE_SELECTION_TOOL,
                tool_version=REPRESENTATIVE_SELECTION_TOOL_VERSION,
            )["reasons"],
            ["source_fingerprint_changed"],
        )
        self.assertEqual(
            evidence_staleness(
                evidence,
                sources=evidence_sources(items[:-1]),
                policy_hash=policy_hash,
                tool_name=REPRESENTATIVE_SELECTION_TOOL,
                tool_version=REPRESENTATIVE_SELECTION_TOOL_VERSION,
            )["reasons"],
            ["source_set_changed"],
        )
        self.assertEqual(
            evidence_staleness(
                evidence,
                sources=evidence_sources(items),
                policy_hash="sha256:changed-policy",
                tool_name=REPRESENTATIVE_SELECTION_TOOL,
                tool_version=REPRESENTATIVE_SELECTION_TOOL_VERSION,
            )["reasons"],
            ["policy_changed"],
        )
        self.assertEqual(
            evidence_staleness(
                evidence,
                sources=evidence_sources(items),
                policy_hash=policy_hash,
                tool_name=REPRESENTATIVE_SELECTION_TOOL,
                tool_version="2",
            )["reasons"],
            ["tool_changed"],
        )

    def test_evidence_ids_include_measurement_identity_without_local_paths(self) -> None:
        items = [self._item(1), self._item(2)]
        envelope = build_evidence_envelope(
            kind="quality_measurement",
            sources=evidence_sources(items),
            policy_hash="sha256:policy",
            tool_name="libvmaf",
            tool_version="3.0.0",
            subject={"metric": "vmaf"},
            measurement={
                "unit": "score",
                "sample_job_id": "sample-job-123",
                "media_ranges": [
                    {"source_id": "src", "start_seconds": 120.0, "end_seconds": 128.0}
                ],
            },
            result={"value": 95.4, "coverage": {"frame_count": 192}},
        )
        reversed_envelope = build_evidence_envelope(
            kind="quality_measurement",
            sources=evidence_sources(list(reversed(items))),
            policy_hash="sha256:policy",
            tool_name="libvmaf",
            tool_version="3.0.0",
            subject={"metric": "vmaf"},
            measurement={
                "unit": "score",
                "sample_job_id": "sample-job-123",
                "media_ranges": [
                    {"source_id": "src", "start_seconds": 120.0, "end_seconds": 128.0}
                ],
            },
            result={"value": 95.4, "coverage": {"frame_count": 192}},
        )

        self.assertEqual(envelope["evidence_id"], reversed_envelope["evidence_id"])
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(envelope["measurement"]["sample_job_id"], "sample-job-123")
        self.assertNotIn("/Volumes/private-media", json.dumps(envelope, sort_keys=True))

    def test_public_payload_scrubs_machine_local_paths(self) -> None:
        selection = select_representatives([self._item(1)], prefix="tv/Example/Season 1")

        public_item = public_representative_item(selection.primary_item())
        serialized_selection = json.dumps(selection.public_payload(), sort_keys=True)

        self.assertNotIn("source_path", public_item)
        self.assertNotIn("staging_path", public_item)
        self.assertNotIn("/Volumes/private-media", serialized_selection)
        self.assertIn("rationale", serialized_selection)
        self.assertIn("coverage", serialized_selection)

    def test_selection_evidence_persists_with_sample_jobs_and_results(self) -> None:
        selection = select_representatives([self._item(1), self._item(2)], prefix="tv/Example/Season 1")
        primary_item = selection.primary_item()

        queued_item = _job_sample_item_payload(primary_item)
        stored_item = _stored_sample_item_payload(primary_item)

        for item in (queued_item, stored_item):
            persisted_selection = item["representative_selection"]
            self.assertEqual(persisted_selection["selection_id"], selection.payload["selection_id"])
            self.assertEqual(persisted_selection["coverage"]["represented_item_count"], 2)
            self.assertEqual(item["representative_source_id"], selection.payload["primary_source_id"])

    @staticmethod
    def _item(
            index: int,
            *,
            duration_seconds: float = 2700,
            source_size_bytes: int = 1_000_000_000,
            video_codec: str = "h264",
            width: int = 1920,
            height: int = 1080,
            cadence_class: str = "progressive",
            channels: int = 6,
    ) -> dict[str, Any]:
        rel_path = f"tv/Example/Season 1/Episode {index:02d}.mkv"
        return {
            "library_item_id": index,
            "source_path": f"/Volumes/private-media/{rel_path}",
            "staging_path": f"/Volumes/private-transcode/{rel_path}",
            "rel_path": rel_path,
            "source_fingerprint": f"fingerprint-{index}",
            "source_size_bytes": source_size_bytes,
            "video_codec": video_codec,
            "video_bitrate": 4_000_000,
            "width": width,
            "height": height,
            "cadence_class": cadence_class,
            "duration_seconds": duration_seconds,
            "container": "mkv",
            "status": "planned",
            "audio_summary": [
                {
                    "codec_name": "eac3" if channels > 2 else "aac",
                    "channels": channels,
                    "language": "eng",
                    "default": 1,
                }
            ],
            "subtitle_summary": [],
            "resolved_policy": {"video": {"target_vmaf": 95}},
        }


if __name__ == "__main__":
    unittest.main()
