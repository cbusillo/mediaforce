import json
from typing import Any

from fastapi import FastAPI
from starlette.routing import Route

from mediaforce.web.routes.app_window import register_app_window_routes
from mediaforce.web.runtime.app_window import tv_shows_payload


def _card(prefix: str, *, items: int = 10, size: int = 1000, reclaim: int = 600, loading: bool = False,
          key: str = "needs_test", tone: str = "quiet", action: str | None = "make_sample") -> dict[str, Any]:
    return {
        "prefix": prefix,
        "item_count": items,
        "total_size_bytes": size,
        "projected_reclaim_bytes": reclaim,
        "details_loading": loading,
        "page_state": {"key": key, "tone": tone, "primary_action": action, "failure": None, "recovery_kind": None},
    }


def test_seasons_group_into_shows_with_their_totals_biggest_show_first() -> None:
    shows = tv_shows_payload([
        _card("tv/Futurama/Season 10", items=13, size=4000, reclaim=3000),
        _card("tv/ER/Season 1", items=25, size=9000, reclaim=7000),
        _card("tv/Futurama/Season 2", items=20, size=3000, reclaim=2000, key="ready_to_compare", tone="ready",
              action="review_sample"),
        _card("tv/Futurama/Specials", items=2, size=500, reclaim=100),
        {"prefix": ""},
    ])["shows"]

    assert [show["show"] for show in shows] == ["ER", "Futurama"]
    futurama = shows[1]
    assert (futurama["show_prefix"], futurama["season_count"], futurama["episode_count"], futurama["bytes"]) == (
        "tv/Futurama", 3, 35, 7500,
    )
    assert futurama["estimated_saved_bytes"] == 5100
    assert [season["season"] for season in futurama["seasons"]] == [2, 10, None]
    assert futurama["seasons"][0] == {
        "prefix": "tv/Futurama/Season 2", "season": 2, "state": "ready_to_compare", "action": "review_sample",
        "failure": None, "tone": "ready", "episode_count": 20, "bytes": 3000,
    }


def test_a_show_has_no_estimate_while_any_season_is_still_loading() -> None:
    shows = tv_shows_payload([_card("tv/ER/Season 1"), _card("tv/ER/Season 2", loading=True)])["shows"]
    assert shows[0]["estimated_saved_bytes"] is None


def test_the_route_serves_the_shows() -> None:
    app = FastAPI()
    register_app_window_routes(
        app, main_window_payload=dict, tv_shows_payload=lambda: tv_shows_payload([_card("tv/ER/Season 1")]),
        sample_payload=lambda _prefix: None, approve_sample_action=lambda *_args: {}, make_sample_action=lambda *_args: {},
        compress_season_action=lambda _prefix: {},
    )
    endpoint = next(route.endpoint for route in app.routes if isinstance(route, Route) and route.path == "/api/app/tv-shows")
    response = endpoint()
    assert [show["show_prefix"] for show in json.loads(response.body)["shows"]] == ["tv/ER"]
