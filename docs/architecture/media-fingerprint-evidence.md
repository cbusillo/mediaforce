# Media Fingerprint Evidence and Review Moments

## Ownership

- `mediaforce/encoding/fingerprint.py` owns bounded media fingerprint analysis,
  parser helpers, deterministic classification, source-scoped evidence shaping,
  and low-confidence advisory gates.
- `mediaforce/library/probe.py` records the persisted fingerprint summary during
  media probing alongside cadence evidence.
- `mediaforce/library/scanner.py` preserves fingerprint summaries while
  reconciling unchanged catalog rows, even when the summary is missing,
  malformed, old, or retryable. New or content-changed files still receive the
  existing full probe until the durable evidence worker owns refreshes.
- `mediaforce/library/planner.py` binds persisted fingerprint facts to the
  current source fingerprint and carries the versioned envelope on manifest
  items.
- `mediaforce/reviewing/helpers.py` converts measured fingerprint ranges into
  typical and hard review moments with rationale, confidence, coverage, and
  evidence IDs.

## Evidence contract

Media fingerprint evidence version 1 persists aggregated, bounded facts rather
than frame dumps:

- luma distribution, dark-frame prevalence, dark gradient/banding risk proxies
- motion and duplicate-like cadence proxies
- edge/texture density and temporal/chroma noise proxies
- clean animation cues derived from duplicate cadence plus edge structure
- audio layout, bitrate, channel, and measured loudness-range facts when audio
  can be sampled
- sampled ranges, frame limits, coverage, tool identity, and unknown/failure
  states

The manifest envelope includes the source fingerprint, analyzer/tool version,
policy hash, sampled ranges, aggregate measurements, audio probe facts, and the
derived decision. A source fingerprint, analyzer, or policy change therefore
creates a different evidence identity instead of silently reusing stale facts.
Catalog freshness does not make stale fingerprint evidence current; malformed
or missing summaries are projected as explicit unknown evidence at planning
boundaries instead of forcing a catalog-wide media reread.

## Classification and gates

The classifier emits measured traits such as `dark_luma`,
`dark_gradient_banding_risk`, `high_motion`, `high_texture`,
`duplicate_cadence`, `animation_cues`, `likely_film_grain`,
`likely_analog_noise`, `compression_noise_advisory`, `uncertain_noise_mix`, and
`audio_complexity`.

Noise and grain labels are advisory evidence, not cleanup orders. Low-confidence
findings are listed under `low_confidence_advisories`, and the decision carries
policy gates that explicitly prevent automatic destructive denoise/cleanup from
the fingerprint alone. Unknown or low-coverage summaries remain available to the
operator as explicit `unknown` evidence instead of disappearing from payloads.

The analyzer does not inspect or score path names, release years, genres, eras,
or folder categories. Representative selection may cover measured fingerprint
dimensions, but those dimensions come from the fingerprint decision only.
Missing fingerprint dimensions do not outvote available measured evidence when
choosing the primary representative.

## Review moments

Review packs use measured fingerprint ranges to select at least one typical
moment and, when present, hard moments for dark gradients, motion, texture,
duplicate cadence, texture/noise advisories, and audio complexity. Each moment
carries:

- timestamp and clip duration
- `typical` or `hard` role
- risk tags
- rationale
- confidence and coverage
- source fingerprint evidence ID when available

If fingerprint moments are unavailable, review generation falls back to the
existing packet-size, scene-change, and default timestamp path. The old
`recommend_review_timestamps` list API remains available for compatibility;
structured review packs include `review_moments` when the measured contract can
support them.
