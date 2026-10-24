# Mac app main window facts

`GET /api/app/main-window` gives the Mac app everything its main window
shows, as facts and stable codes. It sends no screen words: the app turns every
code into words. Folder names (`show`, `season_folder`) are data, not wording.
The web pages keep their own endpoints until the web app is retired.

The read is pure: database rows, the computers' cached check results and the
controller storage record. It touches no media storage, so it still answers
when shared storage is gone, and it starts no work.

## Sections

- `work`: `state` is `running`, `idle`, `paused` (the owner paused the queue)
  or `stopped` (`stop_reasons` say why: `storage_unreachable`,
  `storage_unknown`). `running_count` and `waiting_count` count encode jobs.
  `storage` lists each required mount with its recovery `status` and last
  `failure` code.
- `library`: per kind (`tv`, `movies`, `other`), counts of present files and
  `total_bytes`, plus `original_bytes`, the size still in original form.
- `space_saved`: bytes saved by replaced files, the replaced file and episode
  counts, and `season_count`, the number of finished seasons.
- `recently_finished_seasons`: finished seasons, newest first. A season is
  finished when every present file is replaced or kept original; its
  `finished_at` is its last replacement.
- `biggest_not_started`: the largest shows with no encode job, no replaced
  file and no approved sample. `waiting_sample_prefix` names a season whose
  sample waits on the owner.
- `samples`: `waiting_on_you` (page state `ready_to_compare`, or `needs_help`
  for a sample) and `approved_not_started` (`ready_to_make`). `state`,
  `action` and `failure` are the folder page's own codes, from
  `mediaforce/web/runtime/page_state.py`.
- `computers`: each computer's `state`: `ready`, `busy`, `checking`,
  `storage_unreachable`, `not_ready`, or the kind of failed check
  (`password_needed`, `unreachable`, `remote_login_off`, `identity_changed`,
  `new_name`, `not_found`, `no_address`, `python_missing`,
  `unreadable_reply`, `check_failed`).
- `events` and `last_change`: recent encode, check and replace events, newest
  first, using their event types as codes.
- `original_backups`: originals moved aside on replacement and not yet
  removed, by count and original size.
- `kept_original`: `kept` lists seasons with files the owner kept original,
  by motion reason (`motion_mixed`, `motion_partly_interlaced`,
  `motion_unclear`). `held` lists seasons waiting for a reason that lifts on
  its own (`current_season`, `recent_acquisition`, and so on) or for a motion
  decision (`motion_pattern`).
