# mypy: ignore-errors

from mediaforce.services.encoder import parse_ffmpeg_progress


def test_parse_ffmpeg_out_time_ms_is_microseconds() -> None:
    parsed = parse_ffmpeg_progress("out_time_ms=1000000")
    assert parsed["time_encoded_sec"] == 1.0


def test_parse_ffmpeg_out_time_us_is_microseconds() -> None:
    parsed = parse_ffmpeg_progress("out_time_us=2500000")
    assert parsed["time_encoded_sec"] == 2.5
