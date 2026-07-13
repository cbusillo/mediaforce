# Typed Library Settings

Mediaforce stores operator-facing library configuration as ordered typed roots.
The Settings workstation is the authority for root labels, order, type,
availability, processing profile, Plex path translation, and type-specific
policy.

## Configuration contract

`media.libraries` is the ordered canonical record. Each row contains:

- `key`: stable root identity used by catalog rows, hosts, manifests, and saved
  work
- `label`: editable operator-facing name
- `path`: the controller's local path
- `color`: operator color used by Library views
- `plex_path`: optional path reported by Plex when it differs from `path`
- `type`: `tv`, `movie`, `spatial`, or `other`
- `availability`: `production`, `browse_only`, or `disabled`
- `default_profile`: a profile compatible with the selected type
- `policy`: only the controls relevant to the selected type

`media.source_roots`, `media.library_colors`, and
`metadata.plex.library_roots` remain compatibility projections for existing
runtime consumers. When `media.libraries` exists, its rows define effective
root membership and order; omitted compatibility-map entries do not resurrect
roots from checked-in defaults.

Legacy configurations with only `media.source_roots` remain readable. Settings
projects `tv` as TV, `movies` as Movies, and unknown root IDs as Other. TV
defaults to Production; other inferred types default to Browse only on the
first typed save.

## Availability

- `production`: scan, display, select, calibrate, queue, validate, and promote
- `browse_only`: scan and display, but never select or process
- `disabled`: neither scan nor process; a full scan marks former catalog rows
  missing

TV and Movies are production-enabled types. Movies keeps extras and uncertain
nested files out of title-wide production by default while exact-file actions
remain deliberate. Other remains Browse only until its bounded generic workflow
is complete. Spatial media remains safety-blocked
until the 3D/VR qualification plan proves geometry, stream, metadata, audio,
and target-device playback invariants.

## Type policies

TV exposes current-season mode, inactivity release days, acquisition hold days,
and series-status freshness. These values layer above global planning defaults
and below explicit folder overrides.

Movies records title grouping, separate editions, extras inclusion, and ranking
intent. Other records a bounded folder or exact-file work unit. Spatial records
the playback target, stereo layout, projection, source-preserving geometry, and
an unqualified container state. Controls that do not apply to the selected type
are structurally absent from Settings.

## Type changes

Changing an existing root type requires a server-produced compatibility
preview. The acknowledgement is bound to the root ID and exact old/new type.
Confirmation returns the root to Browse only, schedules a catalog refresh, and
preserves historical folder overrides with their former library type. Those
overrides remain recoverable but are ignored while their type is incompatible.
Media files are never moved or deleted by a type change.

## Credentials

Plex and TMDB tokens remain environment-only. Settings exposes the environment
variable name and a ready/needed signal, but never accepts, persists, logs, or
returns a token value. Per-root Plex path translation is configuration, not a
credential.

## Refresh behavior

Root add/remove, path changes, scan enablement changes, and type changes require
a catalog scan. Label, color, ordering, profile, and policy changes do not alter
filesystem membership. Metadata configuration changes continue through the
metadata refresh performed by the scan runtime. Unrelated host, schedule,
working-folder, and advanced runtime settings survive typed-library saves.
