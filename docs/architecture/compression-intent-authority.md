# Compression Intent Authority

Compression intent is the machine-readable contract that tells Mediaforce how
to order acceptable AV1 outcomes. It is separate from the numeric size goal:
the size goal resolves the byte anchor, while compression intent controls
direction, evidence requirements, retries, and escalation.

## Versioned intent

`CompressionIntentV1` owns four stable strategy identifiers:

- `reference`: prefer the highest measured fidelity under the explicit size
  cap; use smaller size as the tie-breaker.
- `transparent`: prefer the smallest result that still satisfies the active
  indistinguishability contract.
- `balanced`: stay near the explicit byte goal, choose smaller on ties, and
  require measured benefit before spending upward.
- `perceptual_floor`: prefer the smallest result that satisfies the hard
  quality and review gates.

The identifiers are strategies, not an ordinal quality ladder. User-facing
wording may evolve independently. A missing, unknown, or unsupported persisted
value resolves to `legacy_unconfirmed`; it never silently becomes `balanced`.

Each intent has two identities:

- `semantic_id` hashes the schema and level, so compatible observations and
  decisions can be compared without depending on where the intent came from.
- `snapshot_id` additionally includes source and confirmation state, so queued
  work can prove which exact resolved value it retained.

## Resolution and persistence

Policy resolution follows the existing scope precedence:

1. a frozen job or manifest item snapshot
2. the deepest matching file or folder override
3. the workstation video default
4. `legacy_unconfirmed`

New sample and production work stores both the resolved user intent and a
top-level compression-intent snapshot. Jobs, retries, recovery, pending
proposals, and quality-search signatures consume that frozen identity rather
than re-reading a later default.

The shipped workstation default is a confirmed `balanced` starting point for
new work. That configured default is deliberate and gives new users a
usable first run. A persisted calibration, job, or item that predates the
contract does not inherit that later default: missing frozen metadata remains
`legacy_unconfirmed` until the user chooses a goal.

Pending proposals retain both their base and proposed semantic identities. A
proposal made before either intent changes is stale and must be refreshed before
it can queue work.

## Authority order

Authority is deterministic and short-circuiting:

1. Hard arithmetic, stream, cadence, quality, validation, and promotion
   constraints filter or veto work. A veto does not authorize more bytes.
2. Confirmed compression intent sets the preferred direction.
3. Compatible typed evidence may authorize a bounded contrary direction.
4. LLM output, heuristics, and priors remain advisory.

`authorize_compression_change()` compares an explicit decimal-byte anchor with
a candidate and returns a typed decision containing direction, outcome, reason
code, evidence IDs, and escalation scope. A larger candidate requires evidence
from the closed version-1 allow-list. Missing, stale, or mismatched evidence
returns a non-mutating decision.

Approved visual evidence never authorizes growth. A typed rejection, measured
quality-floor violation, measured item variance, arithmetic infeasibility, or
explicit user override may authorize an item-local exception when its
intent, source, policy, and job identities all match.

## Candidate and retry behavior

Hard quality and feasibility gates run before intent ordering. Among candidates
that pass those gates:

- `perceptual_floor` and `transparent` order by predicted bytes first.
- `reference` orders by measured quality first and bytes second.
- `balanced` orders by distance from the explicit target, then bytes.

This makes the exact 130 MB versus 150 MB case deterministic: under
`perceptual_floor`, 130 MB wins even when the 150 MB candidate has a marginally
higher metric score.

An under-target result is accepted without an upward retry for `transparent`
or `perceptual_floor`, including during sample selection. Their sample search
may cross the lower projection band while respecting the upper bound, source
video cap, quality floor, CRF bounds, and six-candidate limit. The trace records
`quality_safe_under_target_accepted_by_intent` when that exception selects a
candidate. Unconfirmed, `balanced`, and `reference` intents retain the lower
band requirement.

After a failed search, the workspace can show a safe smaller candidate's
historical measurements and offer **Allow smaller and create review sample**.
This explicitly selects `perceptual_floor` with the current size goal and
creates a new proposal and sample through the ordinary provenance checks. It
does not approve the failed job or trust old clips. The new search may select
a different candidate; newly generated comparison pairs must pass the ordinary
review and approval gates before production. Quality or source-cap failures
do not expose this recovery suggestion.

Remote sample review uses the worker-rendered source and preview clips for
browser comparison. It uses existing fingerprint moments or duration coverage
without controller source analysis, and omits the optional combined comparison
movie to avoid a controller-side video render. Local sample review retains the
combined movie.

For `balanced` and `reference`, a larger retry requires
item-, policy-, intent-, and job-bound `measured_item_variance` evidence plus an
authorization decision. A measured retry must improve the selected quality
score; merely filling unused bytes is not a benefit. Verification tolerances
still classify measured variance, but they do not create headroom or authorize
growth.

Under `balanced`, an under-target result whose measured score already reaches
the quality target is accepted as it is. Reaching the target leaves no measured
benefit to buy, so the item is not re-encoded to fill unused bytes and no growth
evidence is consulted. A result below the quality target still follows the
authorization path above, and `reference` still prefers fidelity under its cap.
In production on one 66-item show, 18 of 25 second encodes had been spent
enlarging results that already cleared the target (#578).

## Size limit

The size goal is a budget. The user sets one "size limit over goal"
percentage; Settings writes it to both stored tolerances. The sample search may
not accept a candidate predicted above the final limit, because that candidate
would be re-encoded even if its prediction were exact. On one production show
10 of 25 second encodes came from a ±10% sample band feeding a ±5% final band
(#578). Being under the goal is not a failure by itself: `transparent` and
`perceptual_floor` accept it, `balanced` accepts it once the quality target is
met, and `reference` treats it as headroom to spend on fidelity.

## Recovery boundaries

Automatic cap growth requires a confirmed frozen intent plus deterministic
current-run evidence. The resulting evidence and authorization decision are
stored on the affected manifest item, and the stream budget ledger is rebuilt
from that item-local policy.

When the target-size search proves that every size inside the goal breaks the
quality floor, that measurement is `measured_quality_floor_violation` evidence.
The item retries with an absolute item-local goal at the smallest quality-safe
size, up to 1.5x the original goal. A larger gap, or an unconfirmed intent,
waits for the user.

User-approved measured recovery also writes only exact file overrides. The
folder calibration policy and sibling items remain unchanged. Its saved
recovery record carries the source, policy, intent, job, evidence, and decision
identities. Aggregating repeated item exceptions into a folder default belongs
to the later cross-run learning layer and is not performed here.

## Advisor boundary

Advisor schemas and policy application treat compression intent, numeric size
targets, size-goal mode, quality floors, guard authorization, and protected
caps as user-owned. A model response that includes those keys is rejected
instead of clamped or silently treated as authority.

User-note parsing may recover an explicit requested experiment, but it
always emits `evidence_authority = none`. Approval and rejection authority come
only from the typed review workflow or deterministic measured evidence. Prose
such as “more headroom” cannot repair an unauthorized proposal.

## Quality memory

Quality-search signature version 2 includes the compression semantic identity.
Legacy observations are retained with the legacy identity, but they cannot
qualify a named-intent warm-start cohort. Changing intent therefore invalidates
compatibility without deleting historical evidence.

## User surface

The show page offers three compression goals next to the numeric size choice:
`reference`, `balanced`, and `perceptual_floor`. `transparent` remains a valid
stored identifier but is no longer offered; it displays as the
`perceptual_floor` choice. The selected value is sent in `operator_intent` schema
version 2, displayed as retained state, and frozen into the queued test.

For `transparent` and `perceptual_floor`, an under-target result is presented as
an acceptable smaller outcome rather than a warning to spend unused bytes.
Final explanation, provenance, confidence, and onboarding language remain
separate product work.
