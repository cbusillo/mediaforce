import json
import logging
import pathlib
import re
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

from mediaforce.config.logging import log_event
from mediaforce.domain.types import MediaInfo, QualityMetrics, TierSettings
from mediaforce.services.encoder import (
    DENOISE_FILTERS,
    apply_downscale_filter,
    choose_output_format,
    find_ffmpeg,
)
from mediaforce.services.media_probe import find_ffprobe, probe_media


def window_bitrate(path: pathlib.Path, start: float, duration: float = 5.0) -> Optional[float]:
    """Approximate bitrate (bps) in a short window using ffprobe packets."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=size",
        "-of",
        "csv=p=0",
        "-read_intervals",
        f"{start}%+{duration}",
        str(path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        total_bytes = 0
        for line in res.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                total_bytes += int(line.strip())
            except ValueError:
                continue
        if duration <= 0:
            return None
        return (total_bytes * 8) / duration
    except Exception:
        return None


def pick_sample_times(
    info: MediaInfo,
    count: int = 3,
    sample_len: float = 8.0,
    motion_aware: bool = True,
) -> list[float]:
    """Pick sample start times (seconds)."""
    duration = info.duration_seconds or 0
    if duration <= 0:
        return []

    def clamp_ts(ts: float) -> float:
        return max(0.0, min(ts, max(0.0, duration - sample_len)))

    if not motion_aware or duration < sample_len * 2:
        pct = [0.25, 0.5, 0.75][:count]
        return [clamp_ts(duration * p) for p in pct]

    # Probe 8 candidate windows across the file
    candidates = []
    steps = max(count * 3, 8)
    for i in range(1, steps + 1):
        p = i / (steps + 1)
        start = clamp_ts(duration * p)
        br = window_bitrate(info.path, start, duration=5.0)
        if br is not None:
            candidates.append((br, start))

    if not candidates:
        pct = [0.25, 0.5, 0.75][:count]
        return [clamp_ts(duration * p) for p in pct]

    candidates.sort(reverse=True, key=lambda x: x[0])
    chosen: List[float] = []
    for _, ts in candidates:
        if len(chosen) >= count:
            break
        # Keep simple spacing: avoid picks within sample_len of each other
        if all(abs(ts - c) > sample_len for c in chosen):
            chosen.append(ts)

    # If we didn't get enough distinct windows, pad with spaced positions
    if len(chosen) < count:
        pct = [0.25, 0.5, 0.75]
        for p in pct:
            if len(chosen) >= count:
                break
            ts = clamp_ts(duration * p)
            if all(abs(ts - c) > sample_len for c in chosen):
                chosen.append(ts)

    return chosen[:count]


def build_sample_plan(
    info: MediaInfo,
    count: int = 3,
    sample_len: float = 8.0,
    motion_aware: bool = True,
) -> list[tuple[float, float, str]]:
    """Return a weighted sampling plan."""
    duration = info.duration_seconds or 0.0
    if duration <= 0:
        return []

    def clamp_ts(ts: float) -> float:
        return max(0.0, min(ts, max(0.0, duration - sample_len)))

    plan: list[tuple[float, float, str]] = []
    if count >= 1:
        plan.append((clamp_ts(duration * 0.15), 1.0, "short"))
    if count >= 2:
        plan.append((clamp_ts(duration * 0.50), 1.0, "mid"))

    if count >= 3:
        motion_ts = clamp_ts(duration * 0.75)
        motion_weight = 1.0

        if motion_aware:
            candidates: list[tuple[float, float]] = []
            steps = max(count * 3, 8)
            for i in range(1, steps + 1):
                p = i / (steps + 1)
                start = clamp_ts(duration * p)
                br = window_bitrate(info.path, start, duration=5.0)
                if br is not None:
                    candidates.append((float(br), start))

            if candidates:
                candidates.sort(reverse=True, key=lambda x: x[0])
                best_br, best_ts = candidates[0]
                avg_br = sum(b for b, _ in candidates) / len(candidates)
                motion_ts = best_ts
                if avg_br > 0:
                    motion_weight = max(1.25, min(5.0, best_br / avg_br))
                else:
                    motion_weight = 1.5
            else:
                motion_weight = 1.5

        plan.append((motion_ts, motion_weight, "motion"))

    for idx in range(len(plan), count):
        frac = 0.2 + (idx * 0.15)
        plan.append((clamp_ts(duration * frac), 1.0, "auto"))

    return plan[:count]


def encode_sample_clip(
    path: pathlib.Path,
    settings: TierSettings,
    info: MediaInfo,
    start: float,
    duration: float,
    max_height: Optional[int],
) -> tuple[Optional[pathlib.Path], Optional[tuple[int, int]]]:
    """Encode a short sample with current settings; return path and (w,h)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None, None

    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="av1sample_"))
    out_path = tmp_dir / "sample.mkv"

    vf_parts: list[str] = []
    if info.is_interlaced:
        vf_parts.append("bwdif=mode=0:parity=-1:deint=0")
    if settings.denoise and settings.denoise in DENOISE_FILTERS:
        vf_parts.append(DENOISE_FILTERS[settings.denoise])
    vf_parts = apply_downscale_filter(vf_parts, info, max_height)
    pfmt = choose_output_format(info)
    vf_parts.append(f"format={pfmt}")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-v",
        "error",
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-vf",
        ",".join(vf_parts),
        "-c:v",
        "libsvtav1",
        "-crf",
        str(settings.crf),
        "-preset",
        str(settings.preset),
        "-svtav1-params",
        f"film-grain={settings.film_grain}",
        "-y",
        str(out_path),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
    except Exception:
        return None, None

    # Probe encoded dimensions
    ffprobe = find_ffprobe()
    if not ffprobe:
        return out_path, None
    try:
        res = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        st = data.get("streams", [{}])[0]
        w = st.get("width")
        h = st.get("height")
        if w and h:
            return out_path, (int(w), int(h))
    except Exception:
        pass
    return out_path, None


def compute_vmaf_score(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    start: float,
    duration: float,
    encoded_size: Optional[tuple[int, int]] = None,
) -> Optional[float]:
    """Compute VMAF for a short clip; returns mean VMAF."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log_event(40, "ffmpeg_missing", stage="vmaf")
        return None

    w, h = encoded_size if encoded_size else (None, None)
    scale_ref = f"scale={w}:{h}:flags=bicubic" if w and h else "format=yuv420p"

    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="vmaf_"))
    tmp_json = tmp_dir / "vmaf.json"

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(encoded_path),
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        str(source_path),
        "-lavfi",
        f"[1:v]{scale_ref}[ref];[0:v][ref]libvmaf=log_fmt=json:log_path={tmp_json}",
        "-f",
        "null",
        "-",
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=240)
        data = json.loads(tmp_json.read_text())
        frames = data.get("frames", [])
        if not frames:
            return None
        scores = [f.get("metrics", {}).get("vmaf") for f in frames if f.get("metrics", {}).get("vmaf") is not None]
        if not scores:
            return None
        return sum(scores) / len(scores)
    except Exception:
        return None
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass


def sample_vmaf(
    info: MediaInfo,
    settings: TierSettings,
    max_height: Optional[int],
    sample_count: int = 3,
    sample_length: float = 8.0,
    motion_aware: bool = True,
) -> dict:
    """Compute median/min VMAF across several samples."""
    times = pick_sample_times(info, count=sample_count, sample_len=sample_length, motion_aware=motion_aware)
    if not times:
        return {}

    scores = []
    for ts in times:
        enc_path, enc_size = encode_sample_clip(info.path, settings, info, ts, sample_length, max_height)
        if not enc_path:
            continue
        vmaf = compute_vmaf_score(info.path, enc_path, ts, sample_length, encoded_size=enc_size)
        try:
            enc_path.unlink(missing_ok=True)
            enc_path.parent.rmdir()
        except Exception:
            pass
        if vmaf is not None:
            scores.append(vmaf)

    if not scores:
        return {}

    scores.sort()
    median = scores[len(scores) // 2]
    return {
        "median": median,
        "min": min(scores),
        "samples": scores,
        "timestamps": times,
    }


def measure_ssim_psnr(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    start_sec: float = 0,
    duration_sec: float = 0,
) -> tuple[Optional[float], Optional[float]]:
    """Measure SSIM and PSNR between source and encoded video."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None, None

    cmd = [ffmpeg, "-hide_banner"]
    if start_sec > 0:
        cmd.extend(["-ss", str(start_sec)])
    cmd.extend(["-i", str(encoded_path)])
    if start_sec > 0:
        cmd.extend(["-ss", str(start_sec)])
    cmd.extend(["-i", str(source_path)])
    if duration_sec > 0:
        cmd.extend(["-t", str(duration_sec)])

    ssim_filter = "[0:v]format=yuv420p[enc];[1:v]format=yuv420p[ref];[enc][ref]ssim=stats_file=-"
    cmd.extend(["-lavfi", ssim_filter, "-f", "null", "-"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = result.stderr
        ssim = None
        ssim_match = re.search(r"SSIM All:([0-9.]+)", output)
        if ssim_match:
            ssim = float(ssim_match.group(1))
        else:
            ssim_match = re.search(r"All:([0-9.]+)\s*\([0-9.]+\)", output)
            if ssim_match:
                ssim = float(ssim_match.group(1))
    except Exception as e:
        log_event(30, "ssim_failed", source=str(source_path), error=str(e))
        return None, None

    psnr = None
    cmd_psnr = [ffmpeg, "-hide_banner"]
    if start_sec > 0:
        cmd_psnr.extend(["-ss", str(start_sec)])
    cmd_psnr.extend(["-i", str(encoded_path)])
    if start_sec > 0:
        cmd_psnr.extend(["-ss", str(start_sec)])
    cmd_psnr.extend(["-i", str(source_path)])
    if duration_sec > 0:
        cmd_psnr.extend(["-t", str(duration_sec)])
    psnr_filter = "[0:v]format=yuv420p[enc];[1:v]format=yuv420p[ref];[enc][ref]psnr=stats_file=-"
    cmd_psnr.extend(["-lavfi", psnr_filter, "-f", "null", "-"])

    try:
        result = subprocess.run(cmd_psnr, capture_output=True, text=True, check=False)
        output = result.stderr
        psnr_match = re.search(r"PSNR.*average:([0-9.]+)", output)
        if psnr_match:
            psnr = float(psnr_match.group(1))
    except Exception:
        pass

    return ssim, psnr


def measure_vmaf(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    start_sec: float = 0,
    duration_sec: float = 0,
    model: str = "vmaf_v0.6.1",
) -> Optional[float]:
    """Measure VMAF between source and encoded video."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None

    cmd = [ffmpeg, "-hide_banner"]
    if start_sec > 0:
        cmd.extend(["-ss", str(start_sec)])
    cmd.extend(["-i", str(encoded_path)])
    if start_sec > 0:
        cmd.extend(["-ss", str(start_sec)])
    cmd.extend(["-i", str(source_path)])
    if duration_sec > 0:
        cmd.extend(["-t", str(duration_sec)])

    vmaf_filter = (
        "[0:v]format=yuv420p[enc];[1:v]format=yuv420p[ref];"
        f"[enc][ref]libvmaf=model=version={model}:log_fmt=json:log_path=/dev/stdout"
    )
    cmd.extend(["-lavfi", vmaf_filter, "-f", "null", "-"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
        output = result.stdout
        json_match = re.search(r'\{[\s\S]*"VMAF score"[\s\S]*\}', output)
        if json_match:
            try:
                vmaf_data = json.loads(json_match.group(0))
                if "pooled_metrics" in vmaf_data:
                    return vmaf_data["pooled_metrics"]["vmaf"]["mean"]
            except (json.JSONDecodeError, KeyError):
                pass
        vmaf_match = re.search(r"VMAF score[:\s]+([0-9.]+)", output + result.stderr)
        if vmaf_match:
            return float(vmaf_match.group(1))
    except subprocess.TimeoutExpired:
        log_event(30, "vmaf_timeout", source=str(source_path))
    except Exception as e:
        log_event(30, "vmaf_failed", source=str(source_path), error=str(e))

    return None


def verify_encode_quality(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    sample_duration_sec: float = 60,
    sample_positions: list[float] | None = None,
    use_vmaf: bool = True,
) -> QualityMetrics:
    """Verify encoding quality by sampling and measuring metrics."""
    if sample_positions is None:
        sample_positions = [0.25, 0.5, 0.75]

    source_info = probe_media(source_path)
    if source_info is None or source_info.duration_seconds is None:
        log_event(30, "duration_unknown", source=str(source_path))
        return QualityMetrics()

    duration = source_info.duration_seconds
    ssim_values: list[float] = []
    psnr_values: list[float] = []
    vmaf_values: list[float] = []

    for pos_frac in sample_positions:
        start_sec = max(0, (duration * pos_frac) - (sample_duration_sec / 2))
        start_sec = min(start_sec, duration - sample_duration_sec)
        if start_sec < 0:
            start_sec = 0
        actual_duration = min(sample_duration_sec, duration - start_sec)

        log_event(20, "sample_segment", source=str(source_path), start_sec=start_sec, duration=actual_duration)

        ssim, psnr = measure_ssim_psnr(source_path, encoded_path, start_sec, actual_duration)
        if ssim is not None:
            ssim_values.append(ssim)
        if psnr is not None:
            psnr_values.append(psnr)

        if use_vmaf:
            vmaf = measure_vmaf(source_path, encoded_path, start_sec, actual_duration)
            if vmaf is not None:
                vmaf_values.append(vmaf)

    return QualityMetrics(
        ssim=sum(ssim_values) / len(ssim_values) if ssim_values else None,
        psnr=sum(psnr_values) / len(psnr_values) if psnr_values else None,
        vmaf=sum(vmaf_values) / len(vmaf_values) if vmaf_values else None,
        sample_duration_sec=sample_duration_sec * len(sample_positions),
        sample_start_sec=duration * sample_positions[0] if sample_positions else None,
    )


def generate_compare_html(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    output_file: pathlib.Path,
    source_info: Optional[MediaInfo] = None,
    encoded_info: Optional[MediaInfo] = None,
    encode_id: Optional[int] = None,
    vmaf_score: Optional[float] = None,
) -> None:
    """Generate HTML file for side-by-side video comparison."""
    # Get file sizes
    source_size_mb = source_path.stat().st_size / 1024 / 1024
    encoded_size_mb = encoded_path.stat().st_size / 1024 / 1024
    ratio_pct = encoded_size_mb / source_size_mb * 100

    # Format duration
    duration_str = ""
    if source_info and source_info.duration_seconds:
        mins = int(source_info.duration_seconds // 60)
        secs = int(source_info.duration_seconds % 60)
        duration_str = f"{mins}:{secs:02d}"

    # VMAF display
    vmaf_html = ""
    if vmaf_score is not None:
        vmaf_color = "#4a4" if vmaf_score >= 90 else "#aa4" if vmaf_score >= 80 else "#a44"
        vmaf_html = f'<span style="color: {vmaf_color}">VMAF: {vmaf_score:.1f}</span>'

    # Encode ID for promotion actions
    encode_id_attr = f'data-encode-id="{encode_id}"' if encode_id else ""

    html_content = f'''<!DOCTYPE html>
<html>
<head>
    <title>Compare: {source_path.stem}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: #1a1a1a;
            color: #fff;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            padding: 20px;
        }}
        h1 {{
            text-align: center;
            margin-bottom: 10px;
            font-size: 1.2em;
            color: #ccc;
        }}
        .info {{
            text-align: center;
            margin-bottom: 20px;
            font-size: 0.9em;
            color: #888;
        }}
        .container {{
            display: flex;
            gap: 20px;
            justify-content: center;
            flex-wrap: wrap;
        }}
        .video-box {{
            flex: 1;
            max-width: 960px;
            min-width: 400px;
        }}
        .label {{
            text-align: center;
            padding: 10px;
            font-weight: bold;
            font-size: 1.1em;
        }}
        .source .label {{ background: #2d4a2d; }}
        .encoded .label {{ background: #4a2d2d; }}
        video {{
            width: 100%;
            background: #000;
            display: block;
        }}
        .controls {{
            text-align: center;
            margin-top: 20px;
            padding: 15px;
            background: #2a2a2a;
            border-radius: 8px;
        }}
        button {{
            background: #444;
            color: #fff;
            border: none;
            padding: 10px 20px;
            margin: 5px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 1em;
        }}
        button:hover {{ background: #555; }}
        button.active {{ background: #0066cc; }}
        .time-display {{
            font-family: monospace;
            font-size: 1.2em;
            margin: 10px 0;
        }}
        .stats {{
            display: flex;
            justify-content: center;
            gap: 40px;
            margin-top: 15px;
            font-size: 0.9em;
            color: #aaa;
        }}
        .keyboard-hints {{
            margin-top: 15px;
            font-size: 0.8em;
            color: #666;
        }}
        .seek-bar {{
            width: 80%;
            margin: 15px auto;
            display: block;
        }}
    </style>
</head>
<body {encode_id_attr}>
    <h1>{source_path.stem}</h1>
    <div class="info">
        {duration_str} {vmaf_html}
    </div>

    <div class="container">
        <div class="video-box source">
            <div class="label">SOURCE ({source_size_mb:.0f} MB)</div>
            <video id="source" muted playsinline>
                <source src="file://{source_path}" type="video/mp4">
                Your browser cannot play this file directly.
            </video>
        </div>
        <div class="video-box encoded">
            <div class="label">ENCODED ({encoded_size_mb:.0f} MB)</div>
            <video id="encoded" muted playsinline>
                <source src="file://{encoded_path}" type="video/mp4">
                Your browser cannot play this file directly.
            </video>
        </div>
    </div>

    <div class="controls">
        <input type="range" class="seek-bar" id="seekBar" min="0" max="100" value="0" step="0.1">
        <div class="time-display">
            <span id="currentTime">0:00</span> / <span id="duration">{duration_str or '0:00'}</span>
        </div>
        <div>
            <button onclick="skipTime(-10)">-10s</button>
            <button onclick="skipTime(-5)">-5s</button>
            <button onclick="togglePlay()" id="playBtn">Play</button>
            <button onclick="skipTime(5)">+5s</button>
            <button onclick="skipTime(10)">+10s</button>
        </div>
        <div style="margin-top: 10px;">
            <button onclick="setSpeed(0.25)">0.25x</button>
            <button onclick="setSpeed(0.5)">0.5x</button>
            <button onclick="setSpeed(1)" class="active" id="speed1">1x</button>
            <button onclick="setSpeed(2)">2x</button>
        </div>
        <div class="stats">
            <span>Source: {source_size_mb:.1f} MB</span>
            <span>Encoded: {encoded_size_mb:.1f} MB</span>
            <span>Ratio: {ratio_pct:.0f}%</span>
        </div>
        <div class="keyboard-hints">
            Space: Play/Pause | Left/Right: ±5s | Shift+Left/Right: ±10s | 1-4: Speed
        </div>
    </div>

    <script>
        const source = document.getElementById('source');
        const encoded = document.getElementById('encoded');
        const playBtn = document.getElementById('playBtn');
        const currentTimeEl = document.getElementById('currentTime');
        const durationEl = document.getElementById('duration');
        const seekBar = document.getElementById('seekBar');
        let isSyncing = false;

        function formatTime(seconds) {{
            const mins = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return `${{mins}}:${{secs.toString().padStart(2, '0')}}`;
        }}

        function syncVideos(primary, secondary) {{
            if (isSyncing) return;
            isSyncing = true;
            secondary.currentTime = primary.currentTime;
            setTimeout(() => isSyncing = false, 50);
        }}

        source.addEventListener('seeked', () => syncVideos(source, encoded));
        source.addEventListener('timeupdate', () => {{
            syncVideos(source, encoded);
            currentTimeEl.textContent = formatTime(source.currentTime);
            if (source.duration) {{
                seekBar.value = (source.currentTime / source.duration) * 100;
            }}
        }});

        source.addEventListener('loadedmetadata', () => {{
            durationEl.textContent = formatTime(source.duration);
        }});

        source.addEventListener('play', () => {{
            encoded.play();
            playBtn.textContent = 'Pause';
        }});

        source.addEventListener('pause', () => {{
            encoded.pause();
            playBtn.textContent = 'Play';
        }});

        seekBar.addEventListener('input', () => {{
            if (source.duration) {{
                source.currentTime = (seekBar.value / 100) * source.duration;
            }}
        }});

        function togglePlay() {{
            if (source.paused) {{
                source.play();
            }} else {{
                source.pause();
            }}
        }}

        function skipTime(delta) {{
            source.currentTime = Math.max(0, Math.min(source.duration || 0, source.currentTime + delta));
        }}

        function setSpeed(speed) {{
            source.playbackRate = speed;
            encoded.playbackRate = speed;
            document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
            if (speed === 1) document.getElementById('speed1').classList.add('active');
            event.target.classList.add('active');
        }}

        // Keyboard controls
        document.addEventListener('keydown', (e) => {{
            if (e.code === 'Space') {{
                e.preventDefault();
                togglePlay();
            }} else if (e.code === 'ArrowLeft') {{
                e.preventDefault();
                skipTime(e.shiftKey ? -10 : -5);
            }} else if (e.code === 'ArrowRight') {{
                e.preventDefault();
                skipTime(e.shiftKey ? 10 : 5);
            }} else if (e.key >= '1' && e.key <= '4') {{
                const speeds = [0.25, 0.5, 1, 2];
                setSpeed(speeds[parseInt(e.key) - 1]);
            }}
        }});

        // Click on either video to play/pause
        source.addEventListener('click', togglePlay);
        encoded.addEventListener('click', togglePlay);
    </script>
</body>
</html>
'''
    output_file.write_text(html_content)
