import json
import posixpath
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

JSONPayload = dict[str, Any]
FetchJson = Callable[[str, Mapping[str, str], float], tuple[JSONPayload, Mapping[str, str]]]
MAX_JSON_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_PLEX_METADATA_ITEMS = 1_000_000


class ProviderHttpError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class PlexPathMapping:
    plex_root: str
    mediaforce_root: str


@dataclass(frozen=True, slots=True)
class PlexItem:
    rating_key: str
    show_rating_key: str | None
    season_index: int | None
    added_at: str | None
    part_id: str | None
    part_path: str


@dataclass(frozen=True, slots=True)
class PlexShow:
    rating_key: str
    guids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TmdbSeries:
    series_id: int
    status: str | None
    in_production: bool | None


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
            self,
            request: Request,
            fp: Any,
            code: int,
            message: str,
            headers: Mapping[str, str],
            new_url: str,
    ) -> Request | None:
        original = urlsplit(request.full_url)
        redirected = urlsplit(new_url)
        if (original.scheme, original.netloc) != (redirected.scheme, redirected.netloc):
            raise ProviderHttpError("Provider redirected to a different origin", status_code=code)
        return super().redirect_request(request, fp, code, message, headers, new_url)


def fetch_json(url: str, headers: Mapping[str, str], timeout_seconds: float) -> tuple[JSONPayload, Mapping[str, str]]:
    request = Request(url, headers=dict(headers), method="GET")
    opener = build_opener(_SameOriginRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_JSON_RESPONSE_BYTES + 1)
            if len(body) > MAX_JSON_RESPONSE_BYTES:
                raise ProviderHttpError("Provider response exceeded the size limit")
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ProviderHttpError("Provider response must be a JSON object")
            return payload, dict(response.headers.items())
    except ProviderHttpError:
        raise
    except HTTPError as exc:
        raise ProviderHttpError(
            f"Provider request failed with HTTP {exc.code}",
            status_code=exc.code,
            retry_after=exc.headers.get("Retry-After") if exc.headers is not None else None,
        ) from exc
    except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderHttpError(f"Provider request failed: {type(exc).__name__}") from exc


class PlexClient:
    def __init__(
            self,
            *,
            base_url: str,
            token: str,
            client_identifier: str,
            timeout_seconds: float = 20.0,
            fetch_json_fn: FetchJson = fetch_json,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._fetch_json = fetch_json_fn
        self._headers = {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": client_identifier,
            "X-Plex-Product": "mediaforce",
            "X-Plex-Token": token,
        }

    def server_id(self) -> str:
        payload, _ = self._get("identity")
        container = _media_container(payload)
        value = str(container.get("machineIdentifier") or "").strip()
        if not value:
            raise ProviderHttpError("Plex identity response did not include machineIdentifier")
        return value

    def items(self, metadata_type: int) -> Iterator[PlexItem]:
        for metadata in self._metadata(metadata_type):
            rating_key = str(metadata.get("ratingKey") or "").strip()
            if not rating_key:
                raise ProviderHttpError("Plex item metadata did not include ratingKey")
            show_rating_key = str(metadata.get("grandparentRatingKey") or "").strip() or None
            season_index = _non_negative_int(metadata.get("parentIndex"))
            if metadata_type == 4 and show_rating_key is None:
                raise ProviderHttpError("Plex episode metadata did not include grandparentRatingKey")
            if metadata_type == 4 and season_index is None:
                raise ProviderHttpError("Plex episode metadata did not include parentIndex")
            added_at = plex_timestamp(metadata.get("addedAt"))
            for media in _required_object_list(metadata.get("Media"), "Plex item Media"):
                for part in _required_object_list(media.get("Part"), "Plex item Part"):
                    part_path = str(part.get("file") or "").strip()
                    if not part_path:
                        raise ProviderHttpError("Plex item Part did not include a file path")
                    yield PlexItem(
                        rating_key=rating_key,
                        show_rating_key=show_rating_key,
                        season_index=season_index,
                        added_at=added_at,
                        part_id=str(part.get("id") or "").strip() or None,
                        part_path=part_path,
                    )

    def shows(self) -> Iterator[PlexShow]:
        for metadata in self._metadata(2, include_guids=True):
            rating_key = str(metadata.get("ratingKey") or "").strip()
            if not rating_key:
                raise ProviderHttpError("Plex show metadata did not include ratingKey")
            raw_guids = metadata.get("Guid")
            if raw_guids is None:
                guid_rows: list[JSONPayload] = []
            else:
                guid_rows = _optional_object_list(raw_guids, "Plex show Guid")
            guids = tuple(sorted({
                str(value.get("id") or "").strip()
                for value in guid_rows
                if str(value.get("id") or "").strip()
            }))
            yield PlexShow(rating_key=rating_key, guids=guids)

    def _metadata(self, metadata_type: int, *, include_guids: bool = False) -> Iterator[JSONPayload]:
        page_size = 500
        offset = 0
        while True:
            query: dict[str, object] = {"type": metadata_type, "includeElements": "Guid,Media"}
            if include_guids:
                query["includeGuids"] = 1
            payload, response_headers = self._get(
                "library/all",
                query=query,
                headers={
                    "X-Plex-Container-Start": str(offset),
                    "X-Plex-Container-Size": str(page_size),
                },
            )
            container = _media_container(payload)
            page = _object_list(container.get("Metadata"))
            if offset + len(page) > MAX_PLEX_METADATA_ITEMS:
                raise ProviderHttpError("Plex metadata exceeded the item limit")
            yield from page
            total_value = container.get("totalSize")
            if total_value is None:
                total_value = response_headers.get("X-Plex-Container-Total-Size")
            total = _non_negative_int(total_value)
            if total is None:
                raise ProviderHttpError("Plex metadata response did not include totalSize")
            if not page:
                if offset < total:
                    raise ProviderHttpError("Plex metadata pagination ended before the reported total")
                break
            if offset + len(page) >= total:
                break
            offset += len(page)

    def _get(
            self,
            path: str,
            *,
            query: Mapping[str, object] | None = None,
            headers: Mapping[str, str] | None = None,
    ) -> tuple[JSONPayload, Mapping[str, str]]:
        url = urljoin(self._base_url, path.lstrip("/"))
        if query:
            url = f"{url}?{urlencode(query)}"
        request_headers = dict(self._headers)
        if headers:
            request_headers.update(headers)
        return self._fetch_json(url, request_headers, self._timeout_seconds)


class TmdbClient:
    def __init__(
            self,
            *,
            base_url: str,
            token: str,
            timeout_seconds: float = 20.0,
            fetch_json_fn: FetchJson = fetch_json,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._fetch_json = fetch_json_fn
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def series(self, series_id: int) -> TmdbSeries:
        url = urljoin(self._base_url, f"tv/{series_id}")
        payload, _ = self._fetch_json(url, self._headers, self._timeout_seconds)
        returned_id = _positive_int(payload.get("id"))
        if returned_id != series_id:
            raise ProviderHttpError("TMDB response did not match the requested series id")
        in_production = payload.get("in_production")
        return TmdbSeries(
            series_id=series_id,
            status=str(payload.get("status") or "").strip() or None,
            in_production=in_production if isinstance(in_production, bool) else None,
        )


def map_plex_part_path(path: str, mappings: tuple[PlexPathMapping, ...]) -> str | None:
    normalized_path = _normalized_media_path(path)
    for mapping in sorted(mappings, key=lambda value: len(_normalized_media_path(value.plex_root)), reverse=True):
        plex_root = _normalized_media_path(mapping.plex_root).rstrip("/")
        if normalized_path != plex_root and not normalized_path.startswith(f"{plex_root}/"):
            continue
        suffix = normalized_path[len(plex_root):].lstrip("/")
        mediaforce_root = mapping.mediaforce_root.rstrip("/")
        return f"{mediaforce_root}/{suffix}" if suffix else mediaforce_root
    return normalized_path if normalized_path.startswith("/") else None


def tmdb_id_from_guids(guids: tuple[str, ...]) -> int | None:
    ids = {
        parsed
        for guid in guids
        if guid.lower().startswith("tmdb://")
        for parsed in [_positive_int(guid.split("://", 1)[1])]
        if parsed is not None
    }
    if len(ids) != 1:
        return None
    return next(iter(ids))


def plex_timestamp(value: object) -> str | None:
    seconds = _non_negative_int(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def _normalized_media_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if not normalized:
        return ""
    prefix = "/" if normalized.startswith("/") else ""
    return prefix + posixpath.normpath(normalized).lstrip("/")


def _media_container(payload: JSONPayload) -> JSONPayload:
    container = payload.get("MediaContainer")
    if not isinstance(container, dict):
        raise ProviderHttpError("Provider response did not include MediaContainer")
    return container


def _object_list(value: object) -> list[JSONPayload]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _required_object_list(value: object, label: str) -> list[JSONPayload]:
    if value is None or value == []:
        raise ProviderHttpError(f"{label} was missing or empty")
    rows = _optional_object_list(value, label)
    return rows


def _optional_object_list(value: object, label: str) -> list[JSONPayload]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ProviderHttpError(f"{label} was malformed")
    return list(value)


def _positive_int(value: object) -> int | None:
    parsed = _non_negative_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _non_negative_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
