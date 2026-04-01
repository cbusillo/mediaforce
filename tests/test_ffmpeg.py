import unittest
from unittest.mock import patch

from mediaforce import ffmpeg


class FfmpegCodecArgsTests(unittest.TestCase):
    def test_ffmpeg_hwaccel_only_for_h264_and_hevc_sources(self) -> None:
        with patch("mediaforce.ffmpeg.local_videotoolbox_support", return_value=True):
            self.assertEqual(ffmpeg.ffmpeg_hwaccel_input_args("vp9"), [])

    def test_ffmpeg_hwaccel_omitted_when_videotoolbox_unavailable(self) -> None:
        with patch("mediaforce.ffmpeg.local_videotoolbox_support", return_value=False):
            self.assertEqual(ffmpeg.ffmpeg_hwaccel_input_args("h264"), [])

    def test_ffmpeg_hwaccel_included_when_videotoolbox_available(self) -> None:
        with patch("mediaforce.ffmpeg.local_videotoolbox_support", return_value=True):
            self.assertEqual(ffmpeg.ffmpeg_hwaccel_input_args("h264"), ["-hwaccel", "videotoolbox"])

    def test_ab_av1_hwaccel_respects_support_flag(self) -> None:
        with patch("mediaforce.ffmpeg.local_videotoolbox_support", return_value=False):
            self.assertEqual(ffmpeg.ab_av1_hwaccel_input_args("h265"), [])

    def test_ffmpeg_hwaccel_omitted_for_linux_hosts(self) -> None:
        with patch("mediaforce.ffmpeg.local_videotoolbox_support", return_value=True):
            self.assertEqual(ffmpeg.ffmpeg_hwaccel_input_args("h264", platform_name="linux"), [])

    def test_ab_av1_hwaccel_omitted_for_linux_hosts(self) -> None:
        with patch("mediaforce.ffmpeg.local_videotoolbox_support", return_value=True):
            self.assertEqual(ffmpeg.ab_av1_hwaccel_input_args("h265", platform_name="linux"), [])


if __name__ == "__main__":
    unittest.main()
