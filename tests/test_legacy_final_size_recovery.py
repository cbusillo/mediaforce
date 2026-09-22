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
