# Final Love Pass QA

This is the final visual and basic-user confidence pass for GitHub issue `#87`.
The screenshot artifacts are intentionally local-only under
`scratch/ui-checks/final-love-pass/` because live Folder Studio data can include
private media titles.

## Routes Checked

- `/`: Queue opens with the first decision in plain language: choose the next
  folder.
- `/folders`: Folders opens as a search/browse surface instead of a second
  dashboard.
- `/folders/[real-prefix]`: Folder Studio keeps the workflow visible: request a
  sample draft, review evidence, approve, then process.
- `/ops`: Ops answers whether Mediaforce can work now and what needs operator
  attention.
- `/completed`: Completed focuses on handled work and archive cleanup decisions.
- `/settings`: Settings groups libraries, storage, schedules, workers, and
  cleanup without making dangerous cleanup feel routine.

## Evidence

- Desktop and narrow screenshots were captured for every route above.
- Each checked route rendered a distinct `h1` and avoided horizontal overflow at
  desktop and narrow viewports.
- The frontend smoke gate covers every top-level route with a short timeout so a
  blank page or indefinitely spinning app shell fails before signoff.
- A first-viewport vocabulary sweep found remaining internal wording. The pass
  changed visible `Bench` language to review-assistant language and changed
  first-glance `host` language to worker language while leaving API/internal
  model names alone.

## Basic-User Walkthrough

- Queue: start here when the user wants Mediaforce to suggest the next folder.
  The next action is to open Folder Studio.
- Folders: use this when the user already knows what library or folder they want
  to inspect. Search and folder cards should lead back to Folder Studio.
- Folder Studio: follow the workflow strip from draft to sample, review,
  approval, and processing. The review assistant is there to produce or revise a
  sample proposal; nothing queues until the operator confirms.
- Ops: use this to answer "can work run now?" and "what is blocking work?" The
  worker rail should be readable without understanding host internals.
- Completed: use this to audit handled folders and decide whether cleanup is
  still waiting.
- Settings: make configuration changes here, then save. Cleanup remains guarded
  and separate from everyday settings.

## Current Judgment

The app is now cohesive enough to use as the primary operator surface. The
visual system is calm, route roles are clearer, first-glance copy is substantially
less internal, and the blank-page regression class is covered by smoke checks.
Future improvements can be incremental rather than another reset: automated
accessibility coverage, more realistic fixture states for screenshots, and
eventual internal model renames if the API vocabulary changes.
