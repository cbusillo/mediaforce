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

Inside the app the same rule holds. The unit of work is one file, an
episode or a movie: it is encoded, checked, and published on its own, and
nothing else waits for it. A problem with one file stays with that file. Measured evidence is
handled automatically within stated limits, temporary failures retry, an
unknown failure retries before it asks, and a screen shows every reason
work is waiting, not only the first. Only a real judgment call reaches the
owner.

Everything else is ordinary engineering and needs no ceremony.

## Journey

The owner decides once, in plain words, how each kind of content should
look. Mediaforce applies that across the library in the background,
measures every file, publishes each one that passes, and asks only when a
show measurably does not fit. The owner's recurring act is approving
cleanup of rollback copies. The journey fails when one file stops or hides
the rest, when most shows still need their own approval, or when the owner
cannot tell what is happening without asking.
Whatever blocks that journey is the next piece of work.

## Retired

- holding a whole show or folder for one episode's problem
- publishing a season or show all at once
- approving every show separately as the normal path
- internal terms (CRF, VMAF, ledger, cadence, shard, manifest) as the
  owner's words on a screen
- the "semi-automated, review everything" stance in the README

## Milestones

- `Unattended show production` proves an approved show finishes and
  publishes every eligible episode one at a time, with each per-episode
  problem handled or listed in plain words while the rest continue; ends if
  a run still needs the owner to rescue episodes that measured evidence
  could have handled.
- `One approval covers many shows` proves one decision about a kind of
  content converts shows Mediaforce never sampled for the owner, asking
  only when a show measurably does not fit; ends if most shows still need
  their own sample approval.
- `Plain and beautiful Mediaforce` proves the owner can say what every
  screen means and what is theirs to do, without jargon, and likes using
  it; ends if a redesign pass leaves the owner still decoding terms.
