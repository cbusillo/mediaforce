import json
import tempfile
import unittest
from pathlib import Path

from mediaforce.core.evidence import stable_json_hash
from mediaforce.web.runtime import folder_actions


class LegacyFinalSizeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    @staticmethod
    def _contract(*, value_mb: float) -> folder_actions.ActionPayload:
        request = {
            "schema_version": 2,
            "size_goal": {
                "mode": "normalized",
                "value_mb": value_mb,
                "reference_runtime_minutes": 45.0,
            },
            "resolution": {"mode": "source"},
        }
        return {
            "schema_version": 1,
            "sample_job_id": "sample-recovery",
            "policy_hash": "policy-recovery",
            "operator_intent_hash": f"sha256:{stable_json_hash(request)}",
            "operator_intent": request,
        }

    def _legacy_job(self) -> folder_actions.JobPayload:
        manifest_path = Path(self.temp_dir.name) / "manifest.json"
        manifest_path.write_text(json.dumps({
            "selection": {},
            "items": [{"duration_seconds": 438.058}],
        }))
        return {
            "manifest_path": str(manifest_path),
            "progress": {
                "failure_analysis": {
                    "kind": "final_size_target_miss",
                    "target_size_verification": {"target_size_bytes": 48_673_111},
                }
            },
        }

    def test_changed_size_goal_is_recoverable(self) -> None:
        blocker = folder_actions._final_size_requeue_contract_blocker(
            self._legacy_job(),
            self._contract(value_mb=275.0),
        )

        self.assertIsNone(blocker)

    def test_unchanged_size_goal_remains_blocked(self) -> None:
        blocker = folder_actions._final_size_requeue_contract_blocker(
            self._legacy_job(),
            self._contract(value_mb=300.0),
        )

        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertEqual(blocker["code"], "final_size_recovery_contract_unchanged")

    def test_show_run_uses_the_missed_file_duration(self) -> None:
        job = self._legacy_job()
        path = Path(str(job["manifest_path"]))
        path.write_text(json.dumps({"items": [{"duration_seconds": 2700}, {"duration_seconds": 438.058}]}))
        job["progress"]["failure_analysis"]["manifest_index"] = 1
        self.assertIsNone(folder_actions._final_size_requeue_contract_blocker(job, self._contract(value_mb=275)))
        self.assertIsNotNone(folder_actions._final_size_requeue_contract_blocker(job, self._contract(value_mb=300)))

    def test_show_run_without_a_named_file_stays_blocked(self) -> None:
        job = self._legacy_job()
        Path(str(job["manifest_path"])).write_text(json.dumps({"items": [
            {"duration_seconds": 2700}, {"duration_seconds": 438.058}
        ]}))
        self.assertIsNotNone(folder_actions._final_size_requeue_contract_blocker(job, self._contract(value_mb=275)))

    def test_show_run_checks_each_missed_file(self) -> None:
        job = self._legacy_job()
        Path(str(job["manifest_path"])).write_text(json.dumps({"items": [
            {"duration_seconds": 2700}, {"duration_seconds": 438.058}
        ]}))
        job["progress"]["failure_analysis"]["item_analyses"] = [
            {"kind": "final_size_target_miss", "manifest_index": 0,
             "target_size_verification": {"target_size_bytes": 275_000_000}},
            {"kind": "final_size_target_miss", "manifest_index": 1,
             "target_size_verification": {"target_size_bytes": 48_673_111}}
        ]
        self.assertIsNotNone(folder_actions._final_size_requeue_contract_blocker(job, self._contract(value_mb=275)))
        self.assertIsNone(folder_actions._final_size_requeue_contract_blocker(job, self._contract(value_mb=220)))

    def test_a_named_size_miss_is_independent_of_another_files_quality_failure(self) -> None:
        job = self._legacy_job()
        Path(str(job["manifest_path"])).write_text(json.dumps({"items": [
            {"duration_seconds": 2700}, {"duration_seconds": 438.058}
        ]}))
        job["progress"]["failure_analysis"]["item_analyses"] = [
            {"kind": "quality_policy_failure", "manifest_index": 0},
            {"kind": "final_size_target_miss", "manifest_index": 1,
             "target_size_verification": {"target_size_bytes": 48_673_111}}
        ]
        self.assertIsNone(folder_actions._final_size_requeue_contract_blocker(job, self._contract(value_mb=220)))
        self.assertIsNotNone(folder_actions._final_size_requeue_contract_blocker(job, self._contract(value_mb=300)))
