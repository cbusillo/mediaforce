# Direction

This file is the current direction for Mediaforce. The owner's overall
direction in `cbusillo/direction` comes first; Mediaforce is an own project
spent from its share. When an issue, milestone, or other document here
disagrees with this file, this file wins and the other source is corrected
or closed. Issues are a work list, not instructions.

## Purpose

Mediaforce reclaims space on the owner's media library by re-encoding it to
AV1 video and Opus audio with minimal, acceptable quality loss, while asking
the owner for as little as possible. Expect small files: measured H.264
sources have averaged about 9% of their original size at acceptable quality,
and some shows need more. A size goal is a budget, and measured quality wins
over it within stated limits.

The app is something the owner enjoys using: beautiful, obvious at a glance,
and written in plain words. A term the owner has to look up is a defect.

Judge every change by one question: does this reclaim more of the library
safely, with less of the owner's attention and less to decode?

## Stop Boundaries

An agent asks the owner before:

- deleting an original or its last rollback copy
- lowering a quality floor or skipping validation to get work through
- changing a computer's system settings beyond Mediaforce's own files
- anything the overall direction already reserves

Inside the app the same rule holds: a problem with one episode stays with
that episode, and the rest of the work continues. Measured evidence is
handled automatically within stated limits, temporary failures retry, an
unknown failure retries before it asks, and a screen shows every reason
work is waiting, not only the first. Only a real judgment call reaches the
owner.

Everything else is ordinary engineering and needs no ceremony.

## Journey

The owner approves a show once from a sample, in plain words, on a screen
they understand. Mediaforce encodes every eligible episode, handles each
episode's problems itself, and brings back only the calls that are the
owner's, in plain words. The journey fails when one episode stops or hides
the rest, or when the owner cannot tell what is happening without asking.
Whatever blocks that journey is the next piece of work.

## Retired

- holding a whole show or folder for one episode's problem
- internal terms (CRF, VMAF, ledger, cadence, shard, manifest) as the
  owner's words on a screen
- the "semi-automated, review everything" stance in the README

## Milestones

- `Sample Approval Decision Flow` proves approving a sample starts the
  exact authorized encode with no further setup; ends if approval still
  needs a second manual step.
- `Unattended show production` proves an approved show finishes every
  eligible episode, with each per-episode problem handled or listed in
  plain words while the rest continue; ends if a run still needs the owner
  to rescue episodes that measured evidence could have handled.
- `Plain and beautiful Mediaforce` proves the owner can say what every
  screen means and what is theirs to do, without jargon, and likes using
  it; ends if a redesign pass leaves the owner still decoding terms.
