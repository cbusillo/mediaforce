import json
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.routing import Route

from mediaforce.web.routes.app_window import register_app_window_routes
from mediaforce.web.runtime.app_window import other_media_payload, other_media_state


def _unit(prefix: str, *, readiness: str = "ready", lane: str | None = "encode", size: int = 1000,
          reclaim: int | None = 600, loading: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "prefix": prefix,
        "title": prefix.split("/")[-1],
        "library_label": "Home videos",
        "item_count": 3,
        "total_size_bytes": size,
        "projected_reclaim_bytes": reclaim,
        "details_loading": loading,
        "profile_readiness": {"state": readiness},
        "workflow_state": {"primary_lane": lane},
    } | extra


def _library(*units: dict[str, Any], truncated: bool = False) -> dict[str, Any]:
    return {"work_units": list(units), "catalog_truncated": truncated}


def test_other_media_list_biggest_first_with_their_estimates() -> None:
    payload = other_media_payload(_library(
        _unit("other/Trips", size=3000, reclaim=2000),
        _unit("other/Wedding.mkv", size=9000, reclaim=None),
        _unit("other/Recitals", size=500, loading=True),
        {"prefix": ""},
    ))

    items = payload["items"]
    assert [item["prefix"] for item in items] == ["other/Wedding.mkv", "other/Trips", "other/Recitals"]
    assert items[1] == {
        "prefix": "other/Trips", "title": "Trips", "library": "Home videos", "file_count": 3,
        "bytes": 3000, "estimated_saved_bytes": 2000, "state": "ready",
    }
    assert [item["estimated_saved_bytes"] for item in items] == [None, 2000, None]
    assert payload["truncated"] is False


def test_a_capped_library_says_so() -> None:
    assert other_media_payload(_library(_unit("other/a"), truncated=True))["truncated"] is True


@pytest.mark.parametrize(("unit", "code"), [
    (_unit("o/a", readiness="blocked", lane="processing"), "cannot_start"),
    (_unit("o/b", readiness="browse_only", lane="attention"), "view_only"),
    (_unit("o/c", lane="attention"), "needs_attention"),
    (_unit("o/d", lane="blocked"), "needs_attention"),
    (_unit("o/e", lane="processing"), "compressing"),
    (_unit("o/f", lane="encode"), "ready"),
    (_unit("o/g", lane="validate"), "ready"),
    (_unit("o/h", lane="promote"), "ready"),
    (_unit("o/i", lane="mixed"), "ready"),
    (_unit("o/j", lane="none"), "no_work"),
    (_unit("o/k", lane="complete"), "no_work"),
    (_unit("o/l") | {"workflow_state": None, "profile_readiness": None}, "no_work"),
])
def test_each_item_gets_the_web_librarys_state(unit: dict[str, Any], code: str) -> None:
    assert other_media_state(unit) == code


def test_the_route_serves_the_other_media() -> None:
    app = FastAPI()
    register_app_window_routes(
        app, main_window_payload=dict, tv_shows_payload=dict, movies_payload=dict,
        other_media_payload=lambda: other_media_payload(_library(_unit("other/Trips"), truncated=True)),
        sample_payload=lambda _prefix: None, approve_sample_action=lambda *_args: {}, make_sample_action=lambda *_args: {},
        compress_season_action=lambda _prefix: {},
    )
    endpoint = next(route.endpoint for route in app.routes if isinstance(route, Route) and route.path == "/api/app/other")
    body = json.loads(endpoint().body)
    assert [item["prefix"] for item in body["items"]] == ["other/Trips"]
    assert body["truncated"] is True
