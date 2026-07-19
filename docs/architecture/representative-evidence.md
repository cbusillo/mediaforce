# Representative Selection and Evidence

## Ownership

- `mediaforce/library/representatives.py` owns folder candidate loading,
  deterministic representative selection, coverage, rationale, and safe public
  shaping.
- `mediaforce/core/evidence.py` owns the reusable versioned evidence envelope,
  stable hashing, and source/policy/tool staleness checks.
- `mediaforce/web/app.py::_sample_item` remains a thin compatibility wrapper for
  calibration and test patch points.

## Selection policy v1

The selector considers preferred runnable catalog states first and falls back
to all matching catalog items only when none are runnable. Input order is not a
selection signal.

The primary representative is the non-outlier candidate nearest the dominant
video codec, resolution tier, measured cadence class, audio layout, runtime
class, and measured fingerprint profile, with median runtime and source size
used as deterministic proximity signals. Source size is never a positive
ranking signal, so a largest-file outlier is reported rather than automatically
selected.

A technical value requires coverage when it occurs in at least 20% of the
folder. The selector greedily adds the smallest deterministic set of samples
needed to cover those meaningful codec, resolution, cadence, audio-layout,
runtime, and fingerprint clusters. Numeric runtime outliers are reported but do
not create a coverage requirement by themselves.

Cadence is consumed only when a measured `cadence_class` (or compatible
structured fact) is present. Missing cadence remains `unknown`; representative
selection must not infer cadence from names, era, or content category. Cadence
probing and transform decisions remain outside this module.

Media fingerprint dimensions are consumed only from measured
`media_fingerprint_decision` facts. Missing fingerprint evidence remains
`unknown`; selection must not infer dark scenes, grain, animation, era, genre,
or cleanup policy from paths, names, or categories.

Coverage reports:

- selected, represented, and exact-profile item counts and runtime
- per-dimension covered and uncovered values
- meaningful-cluster coverage
- numeric outliers
- known-fact and exact-profile confidence inputs
- per-sample rationale and represented item/runtime totals

Changing the representative-selection policy is versioned. Policy v2 adds
measured fingerprint coverage dimensions to the earlier technical profile.

Changing the threshold, clustering, tie breakers, or coverage semantics
requires a representative-selection tool or policy version bump.

## Bounded acquisition

Fingerprint completeness is not a catalog invariant. Acquisition starts from
the representative set produced by codec, resolution, measured cadence, audio
layout, and runtime alone. Missing or stale fingerprint decisions are removed
from the selection input, so hidden historical facts cannot influence the next
candidate.

After those technical representatives are current, Mediaforce may select a
small follow-up frontier near representatives that expose a non-typical measured
trait but have not yet established a meaningful cluster. The frontier is capped
at three items per prepared batch and the total automatic scope budget is the
smaller of the candidate count or `max(3, 2 × technical representatives)`.
Acquisition stops when meaningful representative coverage is satisfied or the
scope budget is exhausted; unselected items intentionally remain unmeasured.

`mediaforce evidence replay <prefix>` reports technical-only, all-dimension, and
leave-one-fingerprint-dimension-out selections. Each replay includes added and
removed representatives, hard-case recall, sample-set growth, full acquisition
cost, and remaining cost. This is a policy/cost report only and launches no
media subprocess.

`audio_complexity` remains available because local replay across 20 measured TV
season scopes changed the representative set in 8 scopes and improved hard-case
recall in 4. Its separate audio sampling cost is reported explicitly; scopes
where it changes neither selection nor recall are marked `defer`. This keeps the
dimension reviewable without granting it safety or cleanup authority.

## Evidence envelope v1

Every envelope has a deterministic `evidence_id`, schema version, kind,
subject, inputs, measurement, and result. Stable identity includes:

- safe source IDs and source fingerprints
- a canonical policy hash
- tool name and version
- item/range/sample-job measurement identity when supplied
- the derived result, including selection rationale and coverage

Wall-clock creation time and machine-local paths are not identity inputs.
Repeating the same derivation from the same facts therefore produces the same
ID regardless of input ordering. `evidence_staleness` reports source-set,
source-fingerprint, policy, tool, and schema changes independently.

The folder API publishes only relative item identity and allow-listed media
facts. Internal `source_path`, `staging_path`, runtime-state locations, and
review-media locations remain available to workers but are not copied into the
representative-selection API payload.
