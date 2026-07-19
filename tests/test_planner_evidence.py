import unittest

from mediaforce.library.planner import _evidence_summary


class PlannerEvidenceTests(unittest.TestCase):
    def test_malformed_evidence_is_projected_as_missing(self) -> None:
        self.assertIsNone(_evidence_summary(None))
        self.assertIsNone(_evidence_summary("not-json"))
        self.assertIsNone(_evidence_summary("[]"))
        self.assertEqual(_evidence_summary('{"retry_required":true}'), {"retry_required": True})


if __name__ == "__main__":
    unittest.main()
