# Mac app main window facts

`GET /api/app/main-window` gives the Mac app everything its main window
shows, as facts and stable codes. It sends no screen words: the app turns every
code into words. Folder names (`show`, `season_folder`) are data, not wording.
The older endpoints the removed web pages read are still served; some are still used by the app, the CLI or the deploy.

The read is pure: database rows, the computers' cached check results, the
controller storage record and the saved sample state the library cards already
read from the local review cache. It reads no shared media storage, so it
still answers when that storage is gone, and it starts no work.

## Sections

- `work`: `state` is `running`, `idle`, `paused` (the owner paused the queue)
  or `stopped`. `stop_reasons` say why work cannot start, even while paused:
  `storage_unavailable` (the same rule that admits encode work) and
  `no_computer_ready` (work is waiting and no computer can take it).
  `running_count` and `waiting_count` count encode jobs. `storage` lists each
  required mount with its recovery `status` and last `failure` code.
- `library`: per kind (`tv`, `movies`, `other`), counts of present files and
  `total_bytes`, plus `original_bytes`, the size still in original form.
- `space_saved`: bytes saved by replaced files, the replaced file and episode
  counts, and `season_count`, the number of finished seasons with at least one
  replaced file.
- `recently_finished_seasons`: finished seasons, newest first. A season is
  finished when every present file is replaced or kept original, including a
  season kept entirely original, which saved nothing; its `finished_at` is its
  last replacement or keep decision.
- `biggest_not_started`: the largest shows with no encode job, no replaced
  file and no approved sample. `waiting_sample_prefix` names a season whose
  sample waits on the owner.
- `samples`: `waiting_on_you` (page state `ready_to_compare`, or `needs_help`
  for a sample) and `approved_not_started` (`ready_to_make`). `state`,
  `action` and `failure` are the folder page's own codes, from
  `mediaforce/web/runtime/page_state.py`.
- `computers`: each computer's `state`, from the same rows as the computers
  page: `ready`, `busy`, `outside_schedule` (with `schedule_opens_at`),
  `cooling_down` (with `cooldown_until`), `not_encoding`, `checking`,
  `storage_unreachable`, `not_ready`, or the kind of failed check
  (`password_needed`, `unreachable`, `remote_login_off`, `identity_changed`,
  `new_name`, `not_found`, `no_address`, `python_missing`,
  `unreadable_reply`, `check_failed`).
- `events` and `last_change`: recent encode, check and replace events, newest
  first, using their event types as codes.
- `original_backups`: originals moved aside on replacement and not yet
  removed, by count and original size.
- `kept_original`: `kept` lists seasons with present files the owner kept
  original for their current source, by motion reason (`motion_mixed`, `motion_partly_interlaced`,
  `motion_unclear`). `held` lists seasons waiting for a reason that lifts on
  its own (`current_season`, `recent_acquisition`, and so on) or for a motion
  decision (`motion_pattern`).

## Show posters

`GET /api/app/posters/{show_prefix}`, for example `/api/app/posters/tv/Futurama`,
returns the show's poster image, or `404` with `{"poster": "none"}` when
Mediaforce has no poster for it. The app then shows the show's name instead.

Posters come from Mediaforce's own TMDB metadata. The metadata sync keeps each
series' TMDB poster path, using the series id and token it already uses for
status, and downloads the image once from TMDB's public image host, without
the token, into `state.poster_dir`
(`~/Library/Caches/mediaforce/posters` by default). The GET only reads that
cache. A failed download is retried on the next sync.

## One season's sample

Every sample button in the main window opens the season's sample page in the
app. `GET /api/app/samples/{prefix}` gives its facts, read from the same
payloads as the season web page: the page state codes, the playable
`moments` (each with the `original` and `sample` clip URL and size, only once
the clips play in a browser), `episode_bytes` (the sampled episode now),
`estimated_episode_bytes`, `season_bytes` and `season_episode_count` (what
compressing would take), `goal_bytes`, `size_status`, `draft_hash`, and the
`size_choices` keys with their per-episode `target_bytes`.

`POST .../approve` with `confirm` (`high_impact`, `size_tradeoff`) and
`reviewed_draft_hash` approves the sample and starts compressing the season,
like the web page's approval. It answers `{"ok": true, "compressing": bool}`,
or `409` with `code` `confirm_high_impact` or `confirm_size_tradeoff` when the
approval check wants the Director to confirm again, `sample_changed` when the
sample is no longer the draft the Director watched (nothing is approved), or
`refused`.

`POST .../compress` starts compressing an approved season that is not
compressing (action `queue_encode`), the same way approval does, and answers
`{"ok": true, "compressing": bool}`. Approve and compress both add `reason`,
the queue's code when compressing did not start, such as
`confirm_compression_goal` or `confirm_legacy_size` (a choice the app says is
needed but has no step for yet) or `free_space_reserve`.

`POST .../make` with `size` (a choice key, or null) makes the sample again: it
retries the saved request when the sample failed for a reason a retry fixes,
remakes a reviewed sample at its measured goal, or asks for a new sample at
the chosen or recommended size. It answers `409` with `choose_size` when a
size must be chosen, `choose_balance` when the season has no size and detail
balance chosen yet (the app says so; it has no step for it yet), or `refused`.

## TV shows

The TV shows page lists every show in the library. `GET /api/app/tv-shows`
gives its facts, read from the same season cards as the main window's
samples and only while the page is open: `shows`, biggest first, each with
`show_prefix`, `show`, `season_count`, `episode_count`, `bytes`,
`estimated_saved_bytes` (the seasons' projected savings, or null while any
season's details are still loading) and `seasons` in season order. Each season
has its `prefix`, `season` number (null for a folder without one), the page
state codes `state`, `action`, `failure` and `tone`, its `episode_count`
and `bytes`, `files_need_you` (compressing while some files already need the
Director) and `checks_only` (approved, but starting it only runs motion-pattern
checks first). The app groups a show by its seasons the way the web library
does (needs you, in progress, ready for you, no active work), opens the sample
page for a season whose sample waits on the Director, and opens the season's
item page for anything else.

## Movies

The Movies page lists every movie title in the library. `GET /api/app/movies`
gives its facts, built from the same titles as the web movie library's details
and read only while the page is open: `movies`, biggest first, each with
`prefix`, `title`, `library` (the movie library's name), `file_count`,
`bytes`, `estimated_saved_bytes` (the title's projected savings, or null while
its details load or no sample measured it) and one `state` code. `state` is
the web movie library's state group, checked in its order: `view_only`,
`finished`, `cannot_start`, `needs_file_choice`, `motion_held`,
`needs_attention`, `compressing`, `ready` or `no_work`. The app groups those
codes the same way as TV shows (needs you, in progress, ready for you, no
active work) and opens a movie's item page for anything it does.

## Other media

The Other media page lists the Other library's folders and single files, the
same work units as the web Other library. `GET /api/app/other` gives its facts,
built from the web Other library's details and read only while the page is
open: `items`, biggest first, each with `prefix`, `title`, `library`,
`file_count`, `bytes`, `estimated_saved_bytes` (null while details load or
nothing measured it) and one `state` code, plus `truncated`, true when the web
library's listing cap hid part of the library. `state` is the web Other
library's state group, checked in its order: `cannot_start`, `view_only`,
`needs_attention`, `compressing`, `ready` or `no_work`. The sidebar's Other
media number counts files; the page counts folders and files and names the
files they hold, which match that number unless `truncated`.

## One show season, movie or folder

Every list row, the sample page and a finished season's notification open the
item page; the app never opens a browser. `GET /api/app/items/{prefix}`
gives its facts, or `404` when the prefix names no present file: `prefix`,
`domain`, `kind` and `title` (the media scope's), for a TV season `show`,
`show_prefix` and `season`, and `files` in episode order. Each file has its
`library_item_id`, `rel_path`, `name`, `episode` number (null outside
`SxxEyy` names), `stage` (the episode list's stage codes), `validation`
(`failed` or `size_held` when its copy waits on the Director), `kept_reason`
(the keep decision's reason, such as `mixed` or `put_back`), `held_reason`
(`motion_pattern`), `percent_complete`, `size_question`, `bytes` (the file in
the library now), `original_bytes` (its size before replacement, or null) and
`original_backup` (whether its backup is still there to compare). A TV item
also has `sound` (`show_prefix`, `current`, `options`) and `still_airing`
(`series_prefix`, `mode` `auto`, `on` or `off`, and `airing`, TMDB's answer or
null). The page saves those through the folder routes `audio-preset` and
`series-lifecycle`, and acts on a file through `size-held-decision` (keep this
copy, or make it again) and `put-original-back`.

`GET /api/app/player/{library_item_id}/{side}` serves one file for the page's
player, `side` `compressed` or `original` (the backup of a replaced file, or the
file itself when it was never replaced), or `404` when that side is gone. The
Mac player cannot open MKV, so the first request remuxes the file to MP4,
copying the picture and making the sound stereo AAC, into the `player`
folder of the web state folder (`paths.web_state_dir`); the newest six stay cached. It answers byte
ranges, so the player can seek.

## Settings

Mediaforce > Settings… (⌘,) opens the Settings window, one tab per section of
the web settings page. The Libraries tab is the web page's library and working
space: each library's name, folder and kind, the working folder, and how often
Mediaforce looks for new files.

`GET /api/app/settings/library` gives `libraries` (each `key`, `name`,
`folder`, `type`), `working_folder`, `catalog_refresh_hours`, the `types`
a library can be and `revision`, a hash of the stored `media` section.
The fields and the revision come from one stored state: the read reloads the
settings and retries while the file changes underneath it, answering `409`
`settings_changed` if it keeps changing.
`POST /api/app/settings/library` takes the same fields, including that
`revision`, and is refused with `settings_changed`, writing nothing, when the
stored section no longer matches it under the runtime settings lock; the app
then says so and reloads. It
saves only the runtime settings `media` keys, with the same validation, type
change binding, rescan and reload as the web save; computers, schedules, size
defaults and metadata stay as saved. A library with an empty `key` is new and
gets one from its name that no saved library has and that the stored
settings do not still mention (Plex paths, folder overrides, computers), so it
never inherits a removed library's retained settings. Settings the tab does not show carry over from the
saved library; a confirmed kind change starts from the new kind's defaults,
as on the web. A saved library changes kind only with the
`type_change_confirmation` from `POST /api/settings/library-type-preview`,
and removing a library asks first. A refusal answers `400` with
`{"ok": false, "code": ...}` and writes nothing.

The Computers tab is the web page's computers and work windows: each
computer's name, address, what it can do, its work window, which libraries it
works on, how many files at once, its priority and wake address, with the
technical fields under Connection details; and the custom work windows' days,
all-day days and hours. `GET /api/app/settings/computers` gives `computers`
(each with an `id`, its place in the stored list), `schedules`, the
`built_in_schedules`, `abilities`, `media_access_options`, `days`, the
`libraries` a computer can work on, and `revision`, a hash of the stored
`remote_hosts`, `encode_queue.scheduler`, `encode_queue.schedule_profiles` and
legacy `heavy_queue`; each tab's revision covers only its own keys, so saving
one tab never makes the other stale. The read and the save work as the
Libraries tab's do, and `POST /api/app/settings/computers` saves only the
runtime settings `remote_hosts` and `encode_queue` keys, through the same
validation as the web save; the overall work schedule, which no tab edits, is
written back normalized as the web save does. A computer is known by its
address, as everywhere in Mediaforce; its `id` only carries over stored fields
no form shows, so removing or reordering computers never moves them to
another computer, and a computer added without an `id` carries none. A work
window whose key is not saved is new: it gets a key from its name that no
saved work window has and no stored computer names, and computers that chose
it by its placeholder key follow. Hours must be whole hours from 0 to 23.

The Plex and TMDB tab is the web page's metadata panel: Plex on or off and its
server address, TMDB on or off, and for each whether its access key is set.
`GET /api/app/settings/metadata` gives `plex` (`enabled`, `address`,
`key_name`, `key_found`), `tmdb` (`enabled`, `key_name`, `key_found`) and
`revision`, a hash of the stored `metadata` section. Settings never hold a
key: `key_name` is the environment variable the server reads it from and
`key_found` only says whether it is set, so no key can reach the app, and
neither field is accepted back. `POST /api/app/settings/metadata` takes `plex`
(`enabled`, `address`), `tmdb` (`enabled`) and `revision`, works as the other
tabs do, and saves only the runtime settings `metadata` key, normalized as the
web save does. Key variable names, refresh intervals and the TMDB address carry
over, and every stored Plex path carries over unchanged, including one kept for
a removed library; each library's Plex path is a library setting, edited with
the library on the web.

The Compression tab is the web page's default episode size: what Mediaforce
aims for, the size goal for an episode of a given length, how far over the
goal a file may be, the largest allowed size as a share of the original, the
tallest picture and film grain, with how picture quality is measured (the
picture appearance and picture detail scores) and how samples are made under
Advanced. `GET /api/app/settings/compression` gives `aim`, `size_goal_mb`,
`size_goal_minutes`, `size_limit_percent`, `largest_percent`,
`tallest_picture`, `grain`, `measurement`, `appearance_target`,
`appearance_minimum`, `detail_target`, `detail_minimum`, `sample_method`, the
read-only `size_goal_mode`, the choices `aims`, `measurements` and
`sample_methods`, and `revision`, a hash of the stored `video` section.
`POST /api/app/settings/compression` takes every one of those editable fields
and `revision`, works as the other tabs do, and saves only the runtime
settings `video` key through the web save's validation; a choice the web does
not offer is refused rather than replaced. As on the web, changing the size
limit holds samples to the same limit; video defaults the tab does not show
carry over.

## Notifications and the Dock badge

The app reads these facts every 30 seconds while it runs, even with its window
closed, and asks the Director only by Mac notification and Dock badge:

- a season finishing after the newest finish the app has already seen, so a
  season that leaves the library and comes back stays quiet; more than three
  at once arrive as one notification with the count;
- something waiting on the Director: a sample ready to watch or needing to be
  made again, a sample that needs a size decision, or a computer that needs its
  password, Remote Login, or a trust check;
- original backups becoming ready to delete (when their number rises).

Clicking a notification opens the page it is about: a finished season's item
page, a sample's sample page, a computer the Computers page, and original
backups Space saved.

The Dock badge counts what waits on the Director: samples waiting, computers
that need the Director, and one for original backups ready to delete. It clears
as each item clears. Problems the Director cannot act on, such as shared
storage being gone or a computer being off schedule, never notify. The first
check after the app starts with no memory only records what it sees, so a new
install does not announce the whole library. Opened from /Applications, the
app registers itself to open at login.
