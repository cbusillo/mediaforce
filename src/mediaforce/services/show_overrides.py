import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlmodel import Session

from mediaforce.db import ShowOverride, now_iso


_VALID_TIERS = {"pristine", "good", "mediocre", "poor"}


@dataclass(frozen=True)
class ShowConfigImportAction:
    show_name: str
    action: str  # create|update|skip
    tier: Optional[str] = None
    max_height: Optional[int] = None
    notes: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class ShowConfigImportResult:
    created: int
    updated: int
    skipped: int
    actions: list[ShowConfigImportAction] = field(default_factory=list)


def get_default_tier_for_show(session: Session, *, show_name: str) -> Optional[str]:
    existing = session.get(ShowOverride, show_name)
    return existing.default_tier if existing else None


def import_show_config_json(
    session: Session,
    *,
    config_path: pathlib.Path,
    dry_run: bool = True,
    overwrite_existing: bool = False,
) -> ShowConfigImportResult:
    """Import legacy show overrides from a JSON file into the DB-backed overrides.

    This is intended as a one-time migration path away from `show_config.json`.
    During normal operation, the application must not read that JSON.
    """

    payload = json.loads(config_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("show_config.json must be a JSON object mapping show_name -> overrides")

    created = 0
    updated = 0
    skipped = 0
    actions: list[ShowConfigImportAction] = []

    for show_name_raw, cfg_raw in payload.items():
        if not isinstance(show_name_raw, str) or not show_name_raw.strip():
            skipped += 1
            continue
        show_name = show_name_raw.strip()

        if not isinstance(cfg_raw, dict):
            actions.append(
                ShowConfigImportAction(
                    show_name=show_name,
                    action="skip",
                    reason="invalid_config_value",
                )
            )
            skipped += 1
            continue

        cfg: dict[str, Any] = cfg_raw
        tier_raw = cfg.get("tier")
        tier = tier_raw.strip().lower() if isinstance(tier_raw, str) and tier_raw.strip() else None
        if tier is not None and tier not in _VALID_TIERS:
            actions.append(
                ShowConfigImportAction(
                    show_name=show_name,
                    action="skip",
                    tier=tier,
                    reason="invalid_tier",
                )
            )
            skipped += 1
            continue

        max_height: Optional[int] = None
        mh_raw = cfg.get("max_height")
        if mh_raw is not None:
            try:
                mh_val = int(mh_raw)
                if mh_val > 0:
                    max_height = mh_val
            except (TypeError, ValueError):
                max_height = None

        notes: Optional[str] = None
        notes_raw = cfg.get("notes")
        if isinstance(notes_raw, str) and notes_raw.strip():
            notes = notes_raw.strip()

        if tier is None and max_height is None and notes is None:
            actions.append(
                ShowConfigImportAction(
                    show_name=show_name,
                    action="skip",
                    reason="no_supported_fields",
                )
            )
            skipped += 1
            continue

        existing = session.get(ShowOverride, show_name)
        if existing and not overwrite_existing:
            actions.append(
                ShowConfigImportAction(
                    show_name=show_name,
                    action="skip",
                    tier=tier,
                    max_height=max_height,
                    notes=notes,
                    reason="exists",
                )
            )
            skipped += 1
            continue

        action = "update" if existing else "create"
        actions.append(
            ShowConfigImportAction(
                show_name=show_name,
                action=action,
                tier=tier,
                max_height=max_height,
                notes=notes,
            )
        )

        if existing:
            updated += 1
        else:
            created += 1

        if dry_run:
            continue

        now_str = now_iso()
        row = existing or ShowOverride(show_name=show_name)
        if tier is not None:
            row.default_tier = tier
        if max_height is not None:
            row.max_height = max_height
        if notes is not None:
            row.notes = notes
        row.updated_at = now_str
        session.add(row)

    if not dry_run:
        session.commit()

    return ShowConfigImportResult(created=created, updated=updated, skipped=skipped, actions=actions)
