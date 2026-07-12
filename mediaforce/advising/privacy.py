import re
from typing import Any


_PRIVATE_KEYS = frozenset(
    {
        "artifact_path",
        "file_name",
        "images",
        "library_item_id",
        "manifest_path",
        "media_fingerprint",
        "output_path",
        "parent_dir",
        "raw",
        "rel_path",
        "review_dir",
        "seed_raw_response",
        "source_path",
        "staging_path",
    }
)
_PRIVATE_SUFFIXES = ("_path", "_paths")
_FILE_URI_RE = re.compile(r"file://[^\s\"']+", re.IGNORECASE)
_UNIX_PATH_RE = re.compile(
    r"(?<![\w.])/(?:Users|Volumes|home|private|tmp|var/folders)/[^\s\"']+",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s\"']+")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PRIVATE_REFERENCE_KEYS = frozenset(
    {
        "diagnosis",
        "folder",
        "message",
        "note",
        "operator_note",
        "reasoning_note",
        "request_response",
        "request_text",
        "summary",
    }
)


def sanitize_advisor_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if lowered == "images":
                sanitized["image_count"] = len(item) if isinstance(item, list) else 0
                continue
            if lowered in _PRIVATE_KEYS or lowered.endswith(_PRIVATE_SUFFIXES):
                continue
            sanitized[key] = sanitize_advisor_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_advisor_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_advisor_payload(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def redact_sensitive_text(value: str, *, limit: int | None = None) -> str:
    text = _FILE_URI_RE.sub("[redacted-path]", value)
    text = _UNIX_PATH_RE.sub("[redacted-path]", text)
    text = _WINDOWS_PATH_RE.sub("[redacted-path]", text)
    text = _EMAIL_RE.sub("[redacted-email]", text)
    if limit is not None and len(text) > limit:
        return f"{text[:limit].rstrip()}..."
    return text


def advisor_evidence_references(payload: dict[str, Any]) -> list[str]:
    references: list[str] = []
    _collect_evidence_references(sanitize_advisor_payload(payload), prefix="", references=references)
    return references[:64]


def _collect_evidence_references(value: Any, *, prefix: str, references: list[str]) -> None:
    if len(references) >= 64:
        return
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            if key.lower() in _PRIVATE_REFERENCE_KEYS:
                continue
            path = f"{prefix}.{key}" if prefix else key
            _collect_evidence_references(item, prefix=path, references=references)
        return
    if isinstance(value, list):
        if value and prefix and prefix not in references:
            references.append(prefix)
        return
    if value is not None and value != "" and prefix and prefix not in references:
        references.append(prefix)
