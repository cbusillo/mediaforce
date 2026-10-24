"""A failure reaches a screen as one plain sentence, with the raw text kept for Details."""

import re
import unittest

from mediaforce.encoding.plain_failure import FALLBACK_SUMMARY, plain_failure

# The Futurama S08E02 sample on 2026-10-21: encoder output, then the computer went away mid-encode.
FUTURAMA_SSH_LOG = """Svt[info]: -------------------------------------------
Svt[info]: SVT [version]:	SVT-AV1 Encoder Lib v2.3.0
Svt[info]: Level of Parallelism: 5
Svt[info]: SVT [config]: preset	: 6
Encoding: 41% |#####      | (3291/8028 frames)
client_loop: ssh_packet_write_poll: Connection to 10.0.0.12 port 22: Host is down"""
FINAL_SIZE_MISS = (
    "Final output size missed the approved target band: status=over_target, actual=237835925, "
    "target=190000000, lower=180500000, upper=199500000."
)
# Anything a person would have to decode: a raw log line, a key=value status, or a path.
RAW_TEXT = re.compile(r"Svt\[|client_loop|ssh_|\w+=\S|status=|(?:^|\s)/\w")


class PlainFailureTests(unittest.TestCase):
    def assert_plain(self, raw: str) -> str:
        failure = plain_failure(raw)
        assert failure is not None
        self.assertIsNone(RAW_TEXT.search(failure.summary), failure.summary)
        self.assertEqual(failure.detail, raw)
        return failure.summary

    def test_lost_computer_reads_as_one_sentence(self) -> None:
        summary = self.assert_plain(FUTURAMA_SSH_LOG)

        self.assertIn("couldn't reach the computer", summary)

    def test_final_size_miss_reads_as_one_sentence(self) -> None:
        summary = self.assert_plain(FINAL_SIZE_MISS)

        self.assertIn("bigger than the size you approved", summary)

    def test_refused_login_is_not_a_lost_computer(self) -> None:
        summary = self.assert_plain("cbusillo@m2-mbp.invalid: Permission denied (publickey,password).")

        self.assertIn("refused", summary)

    def test_unknown_raw_text_falls_back_to_the_failure_kind(self) -> None:
        failure = plain_failure("Traceback (most recent call last):\nKeyError: 'items'", failure_kind="storage_io")

        assert failure is not None
        self.assertEqual(failure.summary, "Mediaforce had trouble reading or writing media.")
        self.assertIn("KeyError", failure.detail or "")

    def test_unknown_raw_text_without_a_kind_uses_the_fallback(self) -> None:
        failure = plain_failure("bounded search failed (bounded_search_exhausted).")

        assert failure is not None
        self.assertEqual(failure.summary, FALLBACK_SUMMARY)

    def test_words_mediaforce_wrote_for_a_person_pass_through(self) -> None:
        failure = plain_failure(None, waiting_reason="Waiting for the next work window.")

        self.assertEqual(failure, plain_failure("Waiting for the next work window."))
        assert failure is not None
        self.assertEqual(failure.summary, "Waiting for the next work window.")
        self.assertIsNone(failure.detail)

    def test_nothing_to_say_is_none(self) -> None:
        self.assertIsNone(plain_failure("", waiting_reason=None))


if __name__ == "__main__":
    unittest.main()
