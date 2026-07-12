import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from mediaforce.advising.privacy import redact_sensitive_text
from mediaforce.advising.routing import AdvisorModelPricing


_PRIVATE_TELEMETRY_KEYS = frozenset(
    {
        "images",
        "message",
        "model_output",
        "operator_note",
        "prompt",
        "raw",
        "response",
        "stderr",
        "stdout",
    }
)


def estimated_cost_usd(usage: dict[str, int], pricing: AdvisorModelPricing | None) -> float | None:
    if pricing is None:
        return None
    input_rate = pricing.input_usd_per_million
    cached_rate = pricing.cached_input_usd_per_million
    output_rate = pricing.output_usd_per_million
    if input_rate is None or output_rate is None:
        return None
    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    cached_tokens = min(input_tokens, max(0, int(usage.get("cached_input_tokens") or 0)))
    uncached_tokens = input_tokens - cached_tokens
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    cached_cost = cached_tokens * (input_rate if cached_rate is None else cached_rate)
    total = (uncached_tokens * input_rate + cached_cost + output_tokens * output_rate) / 1_000_000
    return round(total, 8)


def append_advisor_telemetry(path: Path | None, record: dict[str, Any], *, max_records: int) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_record = _safe_record(record)
    serialized = json.dumps(safe_record, separators=(",", ":"), sort_keys=True)
    with _locked_file(path):
        existing = path.read_text().splitlines() if path.exists() else []
        lines = [*existing[-max(0, max_records - 1):], serialized]
        with tempfile.NamedTemporaryFile(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
                mode="w",
        ) as handle:
            handle.write("\n".join(lines))
            handle.write("\n")
            temp_path = Path(handle.name)
        os.replace(temp_path, path)


def _safe_record(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_record(item)
            for key, item in value.items()
            if str(key).lower() not in _PRIVATE_TELEMETRY_KEYS
        }
    if isinstance(value, list):
        return [_safe_record(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value, limit=500)
    return value


@contextmanager
def _locked_file(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
