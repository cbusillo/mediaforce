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
inside the show, so an older saved override never shadows the new choice.

The sound choice is an owner setting, like the series lifecycle mode, not part
of what a sample approves. A sample's calibration keeps a snapshot of the
whole policy, but approving it or making the season applies only what the
sample owns (`policy_with_owner_settings` in `production_approval.py`): the
owner's current sound choice replaces the snapshot's, and the folder's saved
planning is kept. A changed choice therefore leaves the approval, its policy
hash and its contract unchanged, so production uses the new sound and its
files still publish. The sound check (`sample-sound-check.md`) only advises
review of a sample; it never gates publishing. The folder payload's
`audio_preset.sound_changed_since_sample` says when the approved sample was
made with another choice, for an owner who wants to hear the new sound first.

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

## Genre suggestions

TMDB genres are kept in `series_metadata.tmdb_genres_json` during the metadata
sync. Rows saved before genres were kept refresh once at the next sync instead
of waiting out the refresh interval.

When a show has no sound choice of its own and its library has no default,
and TMDB lists it as Reality or Talk, the show's Sound control offers
Speech-heavy with the reason ("TMDB lists this show as Reality."):

- **Use Speech-heavy** makes the same explicit choice as the Sound control.
- **Keep as is** records the dismissal under
  `audio_preset_suggestions_dismissed` in runtime settings. It writes no
  policy, so it never unbinds an approval.

A suggestion is never applied without that one confirmation, and building it
never changes a policy.
