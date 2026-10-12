# Sample Sound Check

A sample's picture gets a measured score. When Mediaforce re-encodes the
audio, the sound gets one too. Copied audio is left alone and shows
"Kept as is".

## What runs

`mediaforce/encoding/audio_quality.py` owns the check. Sampled calibration
(`mediaforce/web/runtime/calibration_runtime.py`) runs it on the controller
after the comparison clips are built, in the `checking_sound` stage, and
stores the result as `sample_result.audio_quality`.

1. Copy the chosen audio stream out of the source once, so a source on a
   network share is read only once.
2. Encode the whole episode's audio with the production Opus arguments
   (`opus_encode_arguments` in `mediaforce/encoding/streams.py`). The sample
   and the production encode share those arguments.
3. Measure the original and the new audio over the whole episode with ffmpeg:
   EBU R128 loudness, loudness range and true peak; loudness of the
   300–3400 Hz speech band; and the `astats` flat factor.
4. Cut three 30-second clips at 20%, 50% and 80% of the episode, downmixed to
   48 kHz stereo on both sides, and score each with ViSQOL audio mode. The
   sound score is the mean of the three clips. ViSQOL's ceiling is 4.73, the
   score a file gets against itself.

Clips are cut by seeking after the input. Seeking before the input is faster,
but on Mediaforce's AV1/Opus Matroska output it can land several seconds off.
That produces a misaligned clip that scores far too low.

## Checks

| Check | Fails when |
| --- | --- |
| Sound score | the mean ViSQOL score is below `audio.min_sound_score` (default 4.3) |
| Dialogue | speech-band loudness relative to the whole mix drops by more than 1.5 dB |
| Volume | integrated loudness moves by more than 2 dB |
| Loud and quiet parts | loudness range moves by more than 2 dB |
| Peaks | the new true peak is above 0 dBTP and more than 0.5 dB above the original's |
| Clipping | the flat factor rises by more than 0.5 |

The 4.3 floor comes from the Big Brother remake measurements on #868: stereo
Opus at 96 kbps averaged 4.44 or higher on every episode, and a stereo Opus
32 kbps anchor scored 3.77–4.21.

## Results

`status` is one of:

- `passed`: every check passed.
- `failed`: at least one check failed.
- `unmeasured`: a tool could not run, for example Docker is stopped or the
  scorer image is missing. The `reason` is shown as written. An unmeasured
  check never counts as a pass.
- `unchanged`: the audio is copied, or the sample has no checkable audio.

A failed or unmeasured check sets the quality-risk contract's comparison
reason and adds an `audio_quality_layout` risk, so the sample asks for review
instead of silent reuse. The season review shows the result in its facts, and
shows a warning above the comparison when the check fails or cannot run.

## True-peak limiter

Every Opus re-encode runs `TRUE_PEAK_LIMITER_FILTER` after any layout
remap. It upsamples to 192 kHz so `alimiter` sees most inter-sample peaks,
limits to −1 dBTP without auto-levelling, compensates the limiter's delay,
and returns to 48 kHz. Opus can still overshoot by about a decibel. That
headroom keeps most output below 0 dBTP, and the Peaks check catches the
rest.

## ViSQOL image

ViSQOL does not build with the macOS toolchain. Mediaforce runs it from a
local Docker image built from `docker/visqol/Dockerfile`, pinned to
google/visqol `38d0b01` and Bazel 5.3.2:

```bash
docker build -t mediaforce-visqol:38d0b01 docker/visqol
```

The build takes about five minutes on Apple silicon. Each run uses
`--pull=never` and `--network=none` and mounts only the clip pair read-only.
A cancelled or failed run removes its named container.
`MEDIAFORCE_VISQOL_IMAGE` overrides the image and `MEDIAFORCE_DOCKER` the
Docker CLI.

A check takes about three minutes on an idle controller for a 45-minute
episode. Most of that is the whole-episode audio encode and the three ViSQOL
runs.
