# Sound Choices

The owner picks how a TV show sounds from named choices, rather than editing
bitrates. `mediaforce/core/audio_presets.py` owns the choices.

| Choice | Key | What it does |
| --- | --- | --- |
| Film | `keep_surround` | The configured audio policy: surround stays surround, AAC and Opus are copied |
| Speech-heavy | `speech_stereo` | Downmixes surround to stereo, Opus at 96 kbps, and re-encodes AAC too |

The Big Brother remake measurements on #868 back Speech-heavy. Stereo 96 kbps
saved 10–78 MB per episode against the audio Mediaforce made before (5.1 Opus
or copied AAC). Its ViSQOL score stayed within 0.1 of good stereo Opus, and
the owner heard no difference in a blind A/B.

## Where a choice is set

- **Per show:** the Sound control in the TV library's show inspector, or on the
  season page before the first sample. Both call
  `POST /api/folders/{prefix}/audio-preset`, which applies the choice to the
  show (the first two path parts). `GET` on the same route reads it.
- **Library default:** Settings › TV library › Sound for shows (`audio_preset`
  in the TV library policy). Empty means Standard, the configured audio
  policy, and adds nothing to any policy.

A show choice outranks the library default. An override that names a preset
owns every preset key, so choosing Film for a show also removes a downmix the
library default would add.

## How it is stored

`set_runtime_folder_audio_preset` writes the preset's audio values into the
show's folder override, and into the audio section of every saved override
inside the show. Approving a sample saves a season's whole policy, audio
included, so without that rewrite an approved season would keep its old sound.
A changed choice changes those seasons' policy hash, so they need a new sample
before production uses it. The sound check (`sample-sound-check.md`) gates
that sample.

The audio policy gains two keys, only where a choice is made:

- `preset`: the chosen key, for display and provenance
- `downmix_to_stereo`: makes the stream plan's `output_channels` 2 for
  surround sources

Neither key is in `defaults.toml`. A new default key would change every
folder's policy hash and unbind existing approvals.

## Downmix

`resolve_stream_plan` records `output_channels` on the audio stream only when
it differs from the source, so plans without a downmix keep their identity.
`planned_opus_bitrate` uses the output channels, so a downmix uses
`stereo_opus_bitrate`. `opus_encode_arguments` emits
`aformat=channel_layouts=stereo` (swresample's standard downmix, the same
matrix as `-ac 2`) before the true-peak limiter, with no surround mapping
family. Production encodes, review clips and the sound check all use it.

## Authority

Only the owner sets a sound choice. `preset` and `downmix_to_stereo` are
owner-only keys: the quality-risk compiler rejects any proposal that changes
them (`OWNER_ONLY_AUDIO_KEYS`). Names, era or genre never apply a choice; see
`quality-risk-contract.md`.
