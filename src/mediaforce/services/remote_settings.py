import hashlib
import json
import urllib.request
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select, col
from sqlalchemy import desc

from mediaforce.config.settings import AppSettings, LibrarySettings
from mediaforce.db import ProfileSettingsSource


def load_remote_settings(url: str) -> Optional[AppSettings]:
    try:
        with urllib.request.urlopen(url) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    settings_payload = payload.get("settings") if "settings" in payload else payload
    if not isinstance(settings_payload, dict):
        return None

    libs_raw = settings_payload.get("libraries", [])
    libraries: list[LibrarySettings] = []
    for raw in libs_raw:
        try:
            libraries.append(
                LibrarySettings(
                    id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    media_type=str(raw.get("media_type") or ""),
                    mac_path=str(raw.get("mac_path") or ""),
                    linux_path=str(raw.get("linux_path") or ""),
                    watch=bool(raw.get("watch", True)),
                    max_height=(int(raw.get("max_height")) if raw.get("max_height") else None),
                    weight=float(raw.get("weight", 1.0)),
                )
            )
        except Exception:
            continue

    global_max_height = settings_payload.get("global_max_height")
    try:
        global_max_height = int(global_max_height) if global_max_height is not None else None
    except Exception:
        global_max_height = None

    if not libraries:
        return None

    transcode_root = settings_payload.get("transcode_root")
    if isinstance(transcode_root, str) and not transcode_root.strip():
        transcode_root = None

    return AppSettings(
        libraries=libraries,
        global_max_height=global_max_height,
        transcode_root=str(transcode_root) if transcode_root else None,
    )


def _fetch_remote_profile_settings(url: str, existing_etag: str | None = None) -> tuple[Optional[str], Optional[str]]:
    headers = {"User-Agent": "mediaforce/0.2"}
    if existing_etag:
        headers["If-None-Match"] = existing_etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 304:
                return None, existing_etag
            payload = resp.read().decode("utf-8")
            etag = resp.headers.get("ETag")
            return payload, etag
    except Exception:
        return None, None


def ensure_active_profile_settings(
    session: Session,
    remote_url: Optional[str] = None
) -> Optional[ProfileSettingsSource]:
    src = session.exec(
        select(ProfileSettingsSource)
        .where(col(ProfileSettingsSource.is_active).is_(True))
        .order_by(desc(ProfileSettingsSource.id))
    ).first()

    if remote_url is None:
        return src

    needs_fetch = src is None
    if src and src.fetched_at:
        try:
            last = datetime.fromisoformat(src.fetched_at)
            needs_fetch = (datetime.now() - last).total_seconds() > 24 * 3600
        except Exception:
            needs_fetch = True

    if not needs_fetch:
        return src

    payload, etag = _fetch_remote_profile_settings(remote_url, existing_etag=src.etag if src else None)
    if payload is None and etag == (src.etag if src else None):
        return src
    if payload:
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        new_src = ProfileSettingsSource(
            name="remote-default",
            source_type="remote",
            url=remote_url,
            etag=etag,
            checksum=checksum,
            payload=payload,
            fetched_at=datetime.now().isoformat(),
            applied_at=datetime.now().isoformat(),
            is_active=True,
        )
        session.add(new_src)
        session.commit()
        session.refresh(new_src)
        return new_src
    return src
