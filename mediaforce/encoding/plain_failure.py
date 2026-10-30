"""One plain sentence for a stored error, waiting reason or failure kind, with the raw text kept as detail.

Stored errors stay technical because recovery reads them; this module only words them for a screen.
It never decides what happens to the work.
"""

import re
from dataclasses import dataclass
from typing import Any

from mediaforce.core.db import is_database_busy_failure
from mediaforce.encoding.quality import REMOTE_QUALITY_TIMEOUT_FAILURE_KIND
from mediaforce.hosts.transport import ssh_connection_dropped, ssh_host_unreachable
from mediaforce.tuning.target_size_search import QUALITY_SAFE_BRACKET_MISS_REASON

FINAL_SIZE_MISS_RE = re.compile(
    r"Final output size missed the approved target band: "
    r"status=(?P<status>[a-z_]+), "
    r"actual=(?P<actual>\d+|None), target=(?P<target>\d+|None), "
    r"lower=(?P<lower>\d+|None), upper=(?P<upper>\d+|None)\."
)
QUALITY_FLOOR_CONFLICT_RE = re.compile(
    r"The approved target size conflicts with the configured quality floor "
    r"\((?P<reason>target_band_violates_quality_floor|target_requires_crossing_quality_floor)\); "
    r"target=(?P<target>\d+) bytes, best_reachable=(?P<best>\d+) bytes\."
)
QUALITY_SAFE_BRACKET_MISS_RE = re.compile(
    r"No setting lands inside the approved target size between two adjacent quality-safe settings "
    rf"\((?P<reason>{re.escape(QUALITY_SAFE_BRACKET_MISS_REASON)})\); "
    r"target=(?P<target>\d+) bytes, larger_quality_safe=(?P<larger>\d+) bytes, "
    r"smaller_quality_safe=(?P<smaller>\d+) bytes\."
)
_FINAL_SIZE_MISS_PREFIX = "final output size missed the approved target band"
_MISSING_PATH_RE = re.compile(r"no such file or directory:\s*['\"]", re.IGNORECASE)
_UNREADABLE_PATH_RE = re.compile(r"permission denied:\s*['\"]", re.IGNORECASE)
# SSH names the login methods it tried; a file's "Permission denied" names a path instead.
_SSH_LOGIN_REFUSED_RE = re.compile(r"permission denied \([a-z,-]+\)|host key verification failed", re.IGNORECASE)
_RETRY_WAITING_RE = re.compile(r"^retrying after .+ at \S+$", re.IGNORECASE)

FALLBACK_SUMMARY = "Mediaforce couldn't finish this."

# Messages Mediaforce writes itself, matched by their fixed opening words, first match wins.
_KNOWN_MESSAGES: tuple[tuple[str, str], ...] = (
    ("no setting lands inside the approved target size", "No setting fits the size goal while keeping the picture quality."),
    ("the approved target size conflicts with the configured quality floor",
     "Keeping the picture quality needs a bigger file than the size goal allows."),
    ("the approved target size was not reached", "No setting Mediaforce tried reached the size goal."),
    ("the approved target size leaves no positive", "The size goal is too small to leave room for the picture."),
    ("target-size search needs review", "Mediaforce couldn't find a setting that fits the size goal."),
    ("failed to find a suitable crf", "No setting kept the picture quality within the size limit."),
    ("containment cleanup is unproven", "Mediaforce couldn't confirm the last attempt fully stopped on its computer."),
)

_FAILURE_KIND_SUMMARIES = {
    "automatic_measurement_unavailable": "These files are outside the current automatic measuring pipeline.",
    "original_unavailable": "The original is not available for measuring.",
    "original_not_assessed": "The original has not been identified safely.",
    "conflicting_measurements": "The original measurements disagree.",
    "historical_output_conflict": "The original's identity conflicts with a compressed copy.",
    "known_output_match": "The supposed original matches a compressed copy.",
    "possible_output_match": "The supposed original may be a compressed copy.",
    "known_output_unresolved": "Mediaforce could not rule out a compressed copy.",
    "raw_probe_unavailable": "Mediaforce could not read the original's picture and sound details.",
    "mediaforce_stamp": "This file is already marked as a compressed copy.",
    "historical_output_codec_or_unknown": "The file's format does not identify it safely as an original.",
    "historical_output_encoder": "The file may have been compressed before.",
    "digest_unavailable": "Mediaforce could not read enough of the original to identify it.",
    "measurement_failed": "Mediaforce could not measure an original.",
    "worker_restart": "Mediaforce restarted while this was working.",
    "stale_lease": "The computer doing this stopped reporting in.",
    "host_unavailable": "The computer doing this wasn't available.",
    "ssh_transport": "Mediaforce lost its connection to the computer doing this.",
    "host_configuration": "The computer doing this has a setup problem.",
    "host_scratch": "The computer doing this had trouble with its work folder.",
    "controller_storage_unavailable": "Mediaforce couldn't reach its storage.",
    "storage_io": "Mediaforce had trouble reading or writing media.",
    "controller_database_busy": "Mediaforce was busy for a moment.",
    "unreadable_output": "The compressed file couldn't be read.",
    REMOTE_QUALITY_TIMEOUT_FAILURE_KIND:"Checking the picture quality took too long on a computer.",
    "target_size_needs_review": "The finished file didn't pass the size check.",
    "cancelled": "This was stopped.",
}


@dataclass(frozen=True)
class PlainFailure:
    summary: str
    detail: str | None


def plain_failure(error: Any = None, *, waiting_reason: Any = None, failure_kind: Any = None) -> PlainFailure | None:
    """The sentence a screen shows for a failure and the raw text that stays behind Details.

    The error wins over the waiting reason; a failure kind words a failure whose text no rule knows.
    Returns None when there is nothing to say.
    """
    raw = str(error or waiting_reason or "").strip()
    kind_summary = _FAILURE_KIND_SUMMARIES.get(str(failure_kind or "").strip())
    if not raw:
        return PlainFailure(kind_summary, None) if kind_summary else None
    summary = _known_summary(raw)
    if summary is None and _is_plain(raw):
        return PlainFailure(" ".join(raw.split()), None)
    return PlainFailure(summary or kind_summary or FALLBACK_SUMMARY, raw)


def plain_failure_summary(error: Any = None, *, waiting_reason: Any = None, failure_kind: Any = None) -> str | None:
    failure = plain_failure(error, waiting_reason=waiting_reason, failure_kind=failure_kind)
    return failure.summary if failure is not None else None


def final_size_miss_sentence(status: str) -> str:
    """How a finished file missed the approved size, from the final size check's status."""
    if status == "over_target":
        return "The finished file came out bigger than the size you approved."
    if status == "under_target":
        return "The finished file came out smaller than the size you approved."
    return "Mediaforce couldn't confirm the finished file is the size you approved."


def _known_summary(raw: str) -> str | None:
    lowered = raw.lower()
    final_size = FINAL_SIZE_MISS_RE.search(raw)
    if final_size is not None or _FINAL_SIZE_MISS_PREFIX in lowered:
        return final_size_miss_sentence(final_size.group("status") if final_size is not None else "")
    if _MISSING_PATH_RE.search(raw):
        return "Mediaforce can't find the media on this computer. Mount the storage, then try again."
    if _UNREADABLE_PATH_RE.search(raw):
        return "Mediaforce can't open the media on this computer. Mount the storage, then try again."
    for opening, summary in _KNOWN_MESSAGES:
        if opening in lowered:
            return summary
    # A line the SSH client prints itself only reaches a stored error when the client ended the run,
    # which it reports with status 255.
    if _SSH_LOGIN_REFUSED_RE.search(raw):
        return "The computer doing this refused Mediaforce's login."
    if ssh_host_unreachable(255, raw):
        if ssh_connection_dropped(255, raw):
            return "Mediaforce lost its connection to the computer doing this."
        return "Mediaforce couldn't reach the computer doing this. It may be asleep or off the network."
    if is_database_busy_failure(raw):
        return "Mediaforce was busy for a moment."
    if _RETRY_WAITING_RE.match(" ".join(raw.split())):
        return "Something went wrong, so Mediaforce will try again soon."
    return None


def _is_plain(text: str) -> bool:
    """Words Mediaforce wrote for a person, as opposed to a log line, code name, status value or path."""
    compact = " ".join(text.split())
    if not compact or len(compact) > 280 or "\n" in text.strip():
        return False
    if not compact[0].isupper() and not compact[0].isdigit():
        return False
    return not (
        re.search(r"\b(?:[A-Za-z.]+)?(?:Error|Exception)\b|\b(?:Traceback|errno)\b", compact)
        or re.search(r"\b[a-z]+_[a-z_]+\b", compact)
        or re.search(r"\b\w+=\S", compact)
        or re.search(r"[{}\[\]]", compact)
        or re.search(r"(?:^|\s)/[A-Za-z0-9._/-]+", compact)
        or re.search(r"\bstatus \d{3}\b", compact, re.IGNORECASE)
    )
