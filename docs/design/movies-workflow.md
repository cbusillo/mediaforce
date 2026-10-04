# Movies Workflow Design Brief

The Movies workflow extends Mediaforce's existing manifest-driven pipeline with
movie-title projections and movie-specific user language. It does not add a
second encode path.

## User model

- Library remains one workstation surface with TV and Movies as explicit views.
- A root-level movie file is one exact-file title.
- A conventional movie directory is one title containing one or more feature or
  edition files plus separately visible extras.
- Editions remain distinct rows and distinct exact-file actions.
- Extras stay visible but are excluded from title-wide production unless the
  configured movie policy includes them or the user opens the exact extra.
- Unknown nested files remain visible and blocked from title-wide production
  until the user deliberately opens the exact file.

## Layout

- Movies uses the shared Library list, not a poster wall: one inline detail
  opens directly after the selected title, with an explicit Collapse.
- The list exposes state, title, file membership, size, reclaim estimate, age
  provenance, and the next action.
- Reclaim is evidence-backed: an unmeasured movie reports no estimate rather
  than borrowing TV or generic codec history. Known staged savings remain
  visible independently.
- The detail exposes every feature, edition, extra, and uncertain member as
  a reachable row.
- Narrow layouts open the detail with an explicit Inspect control, without
  horizontal page overflow.

## Movie Page

- Movie scopes reuse the existing folder APIs, calibration, manifests, queue,
  validation, promotion, and recovery machinery.
- The route selects movie or TV presentation from `media_scope.domain`.
- Movie copy uses title, feature, edition, extra, and file. It never uses show,
  season, episode, or lifecycle language.
- Movie membership is visible before sample or queue actions.
- Structure renders before details; background detail refreshes never overlap.
- A failed or non-queueable sample proposal stays visible as a blocked plan and
  never exposes a start action.
- Review keeps Original and Sample visible in an evidence-dominant stage with
  an explicit viewing-only `Full screen` control.
- The ordinary review outcomes are `Keep this version`, `Use less space`, and
  `Improve picture or sound`. Keeping a version records the user's judgment
  about the sample and does not queue production. Once production is admitted,
  matching files are checked and published without separate check or replace decisions.
- The separate production action is scope-derived. For one exact movie it
  uses `Compress this movie` and states that it adds one file to the compression
  queue while the original remains unchanged until checked replacement.
- Each finished production file is checked and published automatically under
  current approval, keeping its original backup first. A file's failed check or
  temporary hold does not stop another file. `Replace original now` remains an
  explicit manual override. The user approves deletion of original backups.

## Safety

- Typed movie roots may enter Production only after this workflow is available.
- Broad title scopes exclude extras and uncertain nested files by default.
- Exact-file scopes remain the deliberate escape hatch for an extra or unusual
  file.
- Promotion remains per source file, so adjacent editions, extras, subtitles,
  and sidecars are never removed as collateral work.
