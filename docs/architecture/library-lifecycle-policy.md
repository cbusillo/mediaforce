# Library Lifecycle Policy

This document defines the durable eligibility and ranking contract for TV
library work. It deliberately separates source lifecycle, policy eligibility,
ranking, and queue execution so each layer remains explainable and recoverable.

## Invariants

- Policy never rewrites `library_items.status` to represent a hold.
- Eligibility is derived before bucket filtering, ranking, and limits.
- Existing queued work remains FIFO; lifecycle policy does not reorder jobs.
- A run manifest freezes selected membership and per-item decision provenance.
- Retry and recovery reuse manifest membership instead of selecting again.
- Retry recovery sends missing, unreadable, or invalid manifests to operator
  attention instead of leaving the job in `retry_backoff`; only transient
  artifact cleanup failures wait for another backoff interval.
- Specials and Season 0 never identify a series' current season.
- Provider failures preserve the last successful metadata.

## Current-season modes

The effective `planning.series_lifecycle_mode` is resolved through the normal
folder policy stack and has three values:

- `auto`: protect the highest positive numbered season while TMDB reports an
  active series. Unknown or stale metadata is conservative until inactivity
  releases the season. Ended series do not receive a current-season hold.
- `on`: protect the highest positive numbered season regardless of TMDB status.
- `off`: do not apply current-season protection.

The highest numbered season releases as soon as a higher numbered season is
present. Otherwise the hold expires after
`planning.current_season_inactive_days` without an addition or replacement;
the checked-in default is 365 days.

## Acquisition guard

Every numbered season and Specials has an independent acquisition guard. The
whole season uses the newest activity timestamp among its items, so adding or
replacing one episode refreshes the guard for the season. The checked-in default
is `planning.season_acquisition_hold_days = 30`.

Activity evidence is the newest of:

1. Plex `addedAt`
2. `library_items.content_version_changed_at`
3. `library_items.discovered_at`

The scanner records `content_version_changed_at` on first discovery, when a
missing source returns, and when the source fingerprint changes. Promotion also
records the source-version change. Replacement detection uses a bounded content
signature sampled from the beginning, middle, and end of the file, so an mtime
touch does not create a false 30-day hold and a same-size replacement is still
detected.

## Metadata contract

A full catalog scan optionally refreshes external metadata:

1. Plex item metadata is fetched and matched to a library row by an exact,
   normalized part path after configured root translation.
2. Episode rows provide Plex `addedAt`, show rating key, and season index.
3. Plex show GUIDs provide an unambiguous TMDB series ID.
4. TMDB provides cached status and `in_production` state.

`plex_item_metadata` and `series_metadata` are caches, not source lifecycle
tables. Authentication, rate limiting, transport failures, and malformed
provider responses must not erase their last successful values.

Tokens come only from environment variables. Runtime settings store provider
URLs, enablement, token environment-variable names, and Plex path mappings; the
web settings payload exposes only whether each token is configured. Provider
pagination must report a complete inventory before successful reconciliation;
ambiguous claims preserve the last successful cache rather than choosing or
deleting evidence.

## Ranking evidence

Eligible items are ordered oldest first by season age. A season uses the newest
item age within that season so recently added episodes do not inherit an older
season-mate's rank. Item age evidence is selected in this order:

1. Plex `addedAt`
2. Mediaforce `discovered_at`
3. filesystem modification time

The source and timestamp are included in selection provenance. Recommendation
score, source size, normalized season path, item path, and item ID remain stable
tie breakers; they do not make an ineligible item runnable.

## Manual override

An exact season prefix may bypass lifecycle holds from the season surface after
the operator confirms the bypass. An exact TV episode may also borrow its
parent-season override; candidate selection still uses the season hold context,
but the exact-item scope keeps the manifest bounded to that one episode. A
separate show-level action may process older numbered seasons with one approved
setup. That action always excludes the highest numbered season, Specials,
ambiguous season identities, and unsafe items; it does not change the show
policy. All override paths bypass only current-season and acquisition timing
holds. Missing sources, active processing, staged-output consistency,
validation, promotion, and other workflow safety rules still apply.

## Manifest provenance

Every selected manifest item carries a versioned `selection_provenance` payload
with:

- effective policy mode and provider state
- series and season identity
- current-season determination
- acquisition and current-season hold reasons
- item and season ranking age evidence
- manual-override state
- final rank position

The manifest also carries a top-level selection snapshot. These facts explain
why work was selected at queue time and keep later retries independent from live
metadata or policy changes.

## Operator setup

Use Settings to configure the Plex server URL and, when Plex reports different
filesystem paths, one path root for each Mediaforce library. Set
`MEDIAFORCE_PLEX_TOKEN` and `MEDIAFORCE_TMDB_TOKEN` in the environment that
starts `mediaforce-web` or the CLI. Connect Plex and scan once before relying on
Plex age or automatic active-series status.
