import json
import tempfile
import unittest
from pathlib import Path

from mediaforce.core.audio_presets import SPEECH_STEREO
from mediaforce.web.runtime import folder_actions

REQUEST = {
    "schema_version": 2,
    "size_goal": {"mode": "normalized", "value_mb": 220.0, "reference_runtime_minutes": 45.0},
    "resolution": {"mode": "source", "max_height": None},
}


def _approval(*, sample_job_id: str, audio_preset: str | None = None) -> folder_actions.ActionPayload | None:
    audio = {"preset": audio_preset} if audio_preset else {}
    return folder_actions._production_approval_contract({
        "accepted_sample_job_id": sample_job_id,
        "accepted_policy_hash": f"policy-{sample_job_id}-{audio_preset}",
        "policy": {"audio": audio},
        "sample_item": {"resolved_operator_intent": {"request": REQUEST}},
    })


class FinalSizeRemakeAfterSoundChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        manifest_path = Path(temp_dir.name) / "manifest.json"
        manifest_path.write_text(json.dumps({
            "selection": {"production_approval_contract": _approval(sample_job_id="sample-surround")},
            "items": [{"duration_seconds": 2659.667}],
        }))
        self.missed_run: folder_actions.JobPayload = {
            "manifest_path": str(manifest_path),
            "progress": {"failure_analysis": {"kind": "final_size_target_miss", "manifest_index": 0}},
        }

    def test_a_fresh_sample_with_a_new_sound_choice_allows_the_remake(self) -> None:
        current = _approval(sample_job_id="sample-speech", audio_preset=SPEECH_STEREO)

        self.assertIsNone(folder_actions._final_size_requeue_contract_blocker(self.missed_run, current))

    def test_a_fresh_sample_with_the_same_request_stays_blocked(self) -> None:
        current = _approval(sample_job_id="sample-speech")

        self.assertIsNotNone(folder_actions._final_size_requeue_contract_blocker(self.missed_run, current))

    def test_the_same_sample_stays_blocked_after_a_new_sound_choice(self) -> None:
        current = _approval(sample_job_id="sample-surround", audio_preset=SPEECH_STEREO)

        self.assertIsNotNone(folder_actions._final_size_requeue_contract_blocker(self.missed_run, current))

    def test_a_show_without_a_sound_choice_keeps_its_approval_unchanged(self) -> None:
        approval = _approval(sample_job_id="sample-surround")

        assert approval is not None
        self.assertNotIn("audio_preset", approval["operator_intent"])


if __name__ == "__main__":
    unittest.main()
