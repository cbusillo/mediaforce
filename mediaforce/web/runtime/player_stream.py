from __future__ import annotations

import io
import re
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import av
from av.container import InputContainer
from av.packet import Packet
from av.video.stream import VideoStream

from mediaforce.core.binaries import ffmpeg_binary

SEGMENT_SECONDS = 6.0
# Mediaforce's own encodes store sound up to about ten seconds ahead of the picture it plays with.
SOUND_LEAD_SECONDS = 12.0
# How far past a cut the picture is read looking for its keyframe before giving up.
KEYFRAME_SEARCH_SECONDS = 30.0
CUT_TOLERANCE_SECONDS = 0.0005
SOUND_TIMEOUT_SECONDS = 60
PLAYER_COPIED_SOUND = frozenset({"aac", "ac3", "eac3", "opus"})
FRAGMENTED_MP4 = {
    "movflags": "frag_keyframe+empty_moov+default_base_moof+frag_discont+delay_moov",
    "frag_duration": "100000000",
    "avoid_negative_ts": "disabled",
}
_NAME = re.compile(r"(?P<track>picture|sound)(?:\.(?P<playlist>m3u8)|-(?P<init>init)\.mp4|-(?P<index>\d+)\.m4s)")


class SegmentNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Episode:
    cuts: tuple[float, ...]
    duration: float
    picture: str
    sound: str | None
    bytes_per_second: float

    def span(self, index: int) -> tuple[float, float]:
        if not 0 <= index < len(self.cuts):
            raise SegmentNotFound(index)
        return self.cuts[index], self.cuts[index + 1] if index + 1 < len(self.cuts) else self.duration


@dataclass(frozen=True)
class Served:
    body: bytes
    media_type: str


def serve(source: Path, name: str) -> Served:
    facts = episode(source)
    if name == "index.m3u8":
        return Served(main_playlist(facts).encode(), "application/vnd.apple.mpegurl")
    match = _NAME.fullmatch(name)
    if match is None or (match["track"] == "sound" and facts.sound is None):
        raise SegmentNotFound(name)
    track = match["track"]
    if match["playlist"]:
        return Served(track_playlist(facts, track).encode(), "application/vnd.apple.mpegurl")
    if match["init"]:
        return Served(_init_segment(source, track, *_identity(source)), "video/mp4")
    return Served(_split(_segment(source, facts, track, int(match["index"])))[1], "video/iso.segment")


def episode(source: Path) -> Episode:
    return _episode(source, *_identity(source))


def main_playlist(facts: Episode) -> str:
    bandwidth = max(int(facts.bytes_per_second * 8), 1)
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    if facts.sound is None:
        lines += [f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}", "picture.m3u8"]
    else:
        lines += [
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="Sound",DEFAULT=YES,AUTOSELECT=YES,URI="sound.m3u8"',
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},AUDIO="sound"', "picture.m3u8",
        ]
    return "\n".join([*lines, ""])


def track_playlist(facts: Episode, track: str) -> str:
    spans = [facts.span(index) for index in range(len(facts.cuts))]
    longest = max(end - start for start, end in spans)
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{int(longest) + 1}", "#EXT-X-PLAYLIST-TYPE:VOD",
             "#EXT-X-INDEPENDENT-SEGMENTS", f'#EXT-X-MAP:URI="{track}-init.mp4"']
    for index, (start, end) in enumerate(spans):
        lines += [f"#EXTINF:{end - start:.6f},", f"{track}-{index}.m4s"]
    return "\n".join([*lines, "#EXT-X-ENDLIST", ""])


def cuts(keyframes: Iterable[float]) -> tuple[float, ...]:
    chosen: list[float] = []
    for keyframe in sorted(keyframes):
        if not chosen or keyframe - chosen[-1] >= SEGMENT_SECONDS:
            chosen.append(keyframe)
    return tuple(chosen)


def _identity(source: Path) -> tuple[int, int]:
    stat = source.stat()
    return stat.st_size, stat.st_mtime_ns


@lru_cache(maxsize=32)
def _episode(source: Path, size: int, _changed: int) -> Episode:
    with av.open(str(source)) as container:
        video = container.streams.video[0]
        sound = container.streams.audio[0].codec_context.codec.canonical_name if container.streams.audio else None
        keyframes = _keyframe_times(container, video)
        duration = container.duration / av.time_base if container.duration else _seconds(video.duration or 0, video)
    return Episode(cuts=cuts(keyframes), duration=duration, picture=video.codec_context.codec.canonical_name,
                   sound=sound, bytes_per_second=size / max(duration, 1))


def _seconds(stamp: int, stream: VideoStream) -> float:
    return stamp * float(stream.time_base or 0)


def _keyframe_times(container: InputContainer, video: VideoStream) -> list[float]:
    # A Matroska file reads its index on the first seek.
    container.seek(0, stream=video)
    indexed = [_seconds(entry.timestamp, video) for entry in video.index_entries if entry.is_keyframe]
    if len(indexed) > 1:
        return indexed
    container.seek(0, stream=video)
    scanned: list[float] = []
    for packet in container.demux(video):
        if packet.is_keyframe and packet.pts is not None:
            scanned.append(_seconds(packet.pts, video))
    return scanned


@lru_cache(maxsize=16)
def _init_segment(source: Path, track: str, _size: int, _changed: int) -> bytes:
    return _split(_segment(source, episode(source), track, 0))[0]


def _segment(source: Path, facts: Episode, track: str, index: int) -> bytes:
    start, end = facts.span(index)
    if track == "picture":
        return _picture_segment(source, facts, start, end)
    return _sound_segment(source, facts, start, end)


def _picture_segment(source: Path, facts: Episode, start: float, end: float) -> bytes:
    out = io.BytesIO()
    with av.open(str(source)) as container:
        video = container.streams.video[0]
        container.seek(int(start / _seconds(1, video)), stream=video, backward=True)
        with av.open(out, "w", format="mp4", options=FRAGMENTED_MP4) as sink:
            target = sink.add_stream_from_template(video, opaque=True)
            # The app's player takes HEVC only under its hvc1 tag.
            if facts.picture == "hevc":
                target.codec_tag = "hvc1"
            for packet in _with_decode_times(_keyframe_to_keyframe(container.demux(video), video, start, end)):
                packet.stream = target
                sink.mux(packet)
    return out.getvalue()


def _keyframe_to_keyframe(packets: Iterable[Packet], video: VideoStream, start: float, end: float) -> Iterator[Packet]:
    started = False
    for packet in packets:
        if packet.size == 0:
            continue
        times = [_seconds(stamp, video) for stamp in (packet.pts, packet.dts) if stamp is not None]
        if not started:
            if packet.is_keyframe and _at(times, start):
                started = True
            elif times and min(times) > start + KEYFRAME_SEARCH_SECONDS:
                return
            else:
                continue
        elif packet.is_keyframe and (_at(times, end) or (times and min(times) > end)):
            return
        yield packet


def _at(times: list[float], cut: float) -> bool:
    return any(abs(time - cut) < CUT_TOLERANCE_SECONDS for time in times)


def _with_decode_times(packets: Iterable[Packet]) -> Iterator[Packet]:
    waiting: list[Packet] = []
    for packet in packets:
        if packet.dts is None:
            waiting.append(packet)
            continue
        for back, held in enumerate(reversed(waiting), start=1):
            held.dts = packet.dts - back * (held.duration or packet.duration)
        yield from waiting
        waiting = []
        yield packet
    yield from waiting


def _sound_segment(source: Path, facts: Episode, start: float, end: float) -> bytes:
    result = subprocess.run(_sound_command(source, facts.sound or "", start, end), capture_output=True,
                            timeout=SOUND_TIMEOUT_SECONDS, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Could not read the sound of {source.name}: {result.stderr.decode(errors='replace')[:300]}")
    return result.stdout


def _sound_command(source: Path, sound: str, start: float, end: float) -> list[str]:
    core_only = ["-core_only", "1"] if sound == "dts" else []
    encoding = ["-c:a", "copy"] if sound in PLAYER_COPIED_SOUND else ["-c:a", "pcm_s16le", "-ac", "2"]
    options = [value for pair in FRAGMENTED_MP4.items() for value in (f"-{pair[0]}", pair[1])]
    return [
        ffmpeg_binary(), "-nostdin", "-loglevel", "error", *core_only,
        "-copyts", "-ss", f"{max(start - SOUND_LEAD_SECONDS, 0):.6f}", "-i", str(source),
        "-ss", f"{start:.6f}", "-to", f"{end:.6f}", "-output_ts_offset", f"{start:.6f}",
        "-map", "0:a:0", "-map_chapters", "-1", *encoding, *options, "-f", "mp4", "pipe:1",
    ]


def _split(fragmented: bytes) -> tuple[bytes, bytes]:
    header, fragments, offset = bytearray(), bytearray(), 0
    while offset + 8 <= len(fragmented):
        size = int.from_bytes(fragmented[offset:offset + 4], "big")
        box = fragmented[offset:offset + size]
        (header if fragmented[offset + 4:offset + 8] in (b"ftyp", b"moov") else fragments).extend(box)
        offset += max(size, 8)
    return bytes(header), bytes(fragments)
