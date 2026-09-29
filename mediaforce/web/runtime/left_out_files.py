"""Files a folder action leaves out while the rest of the folder goes ahead.

A problem with one file stays with that file: each queue gate names the files it
cannot take and why, and the files that pass are queued without them.
"""

from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

from mediaforce.core.type_defs import object_dict
from mediaforce.web.runtime.decision_evidence import CadenceQueuePartition

# Short plural-aware phrases for the summary line, keyed by left-out code.
_SUMMARY_PHRASES: dict[str, tuple[str, str]] = {
    "cadence_unresolved": (
        "has a motion pattern Mediaforce cannot convert safely on its own",
        "have a motion pattern Mediaforce cannot convert safely on its own",
    ),
    "cadence_analysis_required": ("is waiting for a motion-pattern check", "are waiting for a motion-pattern check"),
    "cadence_analysis_failed": ("had its motion-pattern check fail", "had their motion-pattern check fail"),
    "cadence_analysis_unavailable": (
        "cannot get a motion-pattern check right now",
        "cannot get a motion-pattern check right now",
    ),
    "target_size_infeasible": ("cannot fit the size goal", "cannot fit the size goal"),
    "target_size_provenance": ("has a size goal Mediaforce cannot trace", "have a size goal Mediaforce cannot trace"),
    "final_size_recovery_contract_unchanged": (
        "missed the approved final size and needs a fresh goal",
        "missed the approved final size and need a fresh goal",
    ),
    "movie_title_policy": ("is outside the movie title policy", "are outside the movie title policy"),
    "manifest_item_unknown": (
        "is not named in the folder's saved plan",
        "are not named in the folder's saved plan",
    ),
}


@dataclass(frozen=True, slots=True)
class LeftOutFile:
    library_item_id: int
    rel_path: str
    code: str
    reason: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "library_item_id": self.library_item_id,
            "rel_path": self.rel_path,
            "code": self.code,
            "reason": self.reason,
        }


def cadence_left_out_files(
        partition: CadenceQueuePartition,
        rel_paths: dict[int, str],
) -> list[LeftOutFile]:
    work = partition.evidence_work
    if bool(work.get("is_paused")):
        queued_reason = (
            "Needs a motion-pattern check first. The check is prepared in Activity; select Start analysis."
        )
    elif str(work.get("status") or "") == "running":
        queued_reason = "Needs a motion-pattern check first. The check is running in Activity."
    else:
        queued_reason = "Needs a motion-pattern check first. The check is queued in Activity."
    unavailable_detail = str(partition.analysis_unavailable_reason or "").strip().rstrip(".")
    groups: list[tuple[Collection[int], str, str]] = [
        (
            partition.blocked_item_ids,
            "cadence_unresolved",
            "Has a measured motion pattern Mediaforce cannot convert safely on its own. Review it in Activity.",
        ),
        (partition.analysis_queued_item_ids, "cadence_analysis_required", queued_reason),
        (
            partition.analysis_failed_item_ids,
            "cadence_analysis_failed",
            "Its motion-pattern check failed earlier. Open Activity and use Prepare item to retry it.",
        ),
        (
            partition.analysis_unavailable_item_ids,
            "cadence_analysis_unavailable",
            f"Needs a motion-pattern check, but {unavailable_detail[:1].lower()}{unavailable_detail[1:]}.",
        ),
    ]
    return [
        LeftOutFile(item_id, rel_paths.get(item_id, ""), code, reason)
        for item_ids, code, reason in groups
        for item_id in sorted(item_ids)
    ]


def manifest_rel_paths(manifest: dict[str, Any]) -> dict[int, str]:
    return {
        int(item.get("library_item_id") or 0): str(item.get("rel_path") or item.get("source_path") or "")
        for item in (object_dict(value) for value in manifest.get("items") or [])
        if int(item.get("library_item_id") or 0) > 0
    }


def drop_manifest_items(manifest: dict[str, Any], library_item_ids: Collection[int]) -> None:
    """Remove left-out files from a prepared, unwritten manifest and its selection record."""
    excluded = {int(item_id) for item_id in library_item_ids}
    if not excluded:
        return
    manifest["items"] = [
        item
        for item in manifest["items"]
        if int(object_dict(item).get("library_item_id") or 0) not in excluded
    ]
    selection = manifest.get("selection")
    if isinstance(selection, dict):
        if isinstance(selection.get("items"), list):
            selection["items"] = [
                item
                for item in selection["items"]
                if int(object_dict(item).get("library_item_id") or 0) not in excluded
            ]
        if "item_count" in selection:
            selection["item_count"] = len(manifest["items"])


def left_out_summary(left_out: Iterable[LeftOutFile]) -> str:
    """One plain sentence fragment covering every reason, not only the first."""
    counts = Counter(file.code for file in left_out)
    parts: list[str] = []
    for code, count in counts.items():
        singular, plural = _SUMMARY_PHRASES.get(code, ("needs attention", "need attention"))
        parts.append(f"{count} {singular if count == 1 else plural}")
    return "; ".join(parts)


def left_out_payload(left_out: Iterable[LeftOutFile]) -> list[dict[str, Any]]:
    return [file.to_payload() for file in left_out]


def nothing_queued_response(left_out: list[LeftOutFile], **extra: Any) -> dict[str, Any]:
    codes = {file.code for file in left_out}
    summary = left_out_summary(left_out)
    return {
        "ok": False,
        "code": next(iter(codes)) if len(codes) == 1 else "no_files_ready",
        "message": f"No files were queued. Of the selected files, {summary}.",
        "affected_item_count": len(left_out),
        "left_out": left_out_payload(left_out),
        "queued_count": 0,
        "next_route": "/ops",
        "next_action_label": "Open Activity",
        **extra,
    }
