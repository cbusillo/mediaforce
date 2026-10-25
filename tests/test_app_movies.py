import json
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.routing import Route

from mediaforce.web.routes.app_window import register_app_window_routes
from mediaforce.web.runtime.app_window import movie_state, movies_payload


def _title(prefix: str, *, lane: str | None = "encode", state: str | None = "encode_candidates", size: int = 1000,
           reclaim: int | None = 600, loading: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "prefix": prefix,
        "title": prefix.split("/")[-1],
        "library_label": "Movies",
        "availability": "production",
        "item_count": 2,
        "total_size_bytes": size,
        "projected_reclaim_bytes": reclaim,
        "details_loading": loading,
        "workflow_state": {"state": state, "primary_lane": lane},
        "review_badge": {"label": None, "tone": None, "detail": None},
        "promotion_conflicts": [],
    } | extra


def test_movies_list_biggest_first_with_their_estimates() -> None:
    movies = movies_payload([
        _title("movies/Alien (1979)", size=3000, reclaim=2000),
        _title("movies/Heat (1995)", size=9000, reclaim=None),
        _title("movies/Up (2009)", size=500, loading=True),
        {"prefix": ""},
    ])["movies"]

    assert [movie["prefix"] for movie in movies] == ["movies/Heat (1995)", "movies/Alien (1979)", "movies/Up (2009)"]
    assert movies[1] == {
        "prefix": "movies/Alien (1979)", "title": "Alien (1979)", "library": "Movies", "file_count": 2,
        "bytes": 3000, "estimated_saved_bytes": 2000, "state": "ready",
    }
    assert [movie["estimated_saved_bytes"] for movie in movies] == [None, 2000, None]


_PENDING_BADGE = {"review_badge": {"label": "Needs a look", "tone": "attention"}}


@pytest.mark.parametrize(("title", "code"), [
    (_title("m/a", availability="browse_only"), "view_only"),
    (_title("m/b", state="browse_only", lane="complete"), "view_only"),
    (_title("m/c", state="complete", lane="complete", promotion_conflicts=[{"kind": "destination_exists"}]), "finished"),
    (_title("m/d", state="mixed", lane="complete"), "finished"),
    (_title("m/e", promotion_conflicts=[{"kind": "destination_exists"}], state="explicit_selection_required"), "cannot_start"),
    (_title("m/f", state="blocked", lane="blocked") | _PENDING_BADGE, "cannot_start"),
    (_title("m/g", state="explicit_selection_required", lane="none") | _PENDING_BADGE, "needs_file_choice"),
    (_title("m/h", state="motion_held", lane="none") | _PENDING_BADGE, "motion_held"),
    (_title("m/i", lane="encode") | _PENDING_BADGE, "needs_attention"),
    (_title("m/j", state="attention", lane="attention"), "needs_attention"),
    (_title("m/k", state="processing", lane="processing") | _PENDING_BADGE, "compressing"),
    (_title("m/l", state="ready_to_validate", lane="validate") | _PENDING_BADGE, "ready"),
    (_title("m/m", state="ready_to_promote", lane="promote"), "ready"),
    (_title("m/n", state="mixed", lane="mixed"), "ready"),
    (_title("m/o", lane="encode") | {"review_badge": {"label": "Approved draft", "tone": "ok"}}, "ready"),
    (_title("m/p", lane="none") | {"review_badge": {"label": "  ", "tone": "attention"}}, "no_work"),
    (_title("m/q", state=None, lane=None) | {"workflow_state": None}, "no_work"),
])
def test_each_movie_gets_the_web_librarys_state(title: dict[str, Any], code: str) -> None:
    assert movie_state(title) == code


def test_the_route_serves_the_movies() -> None:
    app = FastAPI()
    register_app_window_routes(
        app, main_window_payload=dict, tv_shows_payload=dict,
        movies_payload=lambda: movies_payload([_title("movies/Heat (1995)")]), other_media_payload=dict,
        sample_payload=lambda _prefix: None, approve_sample_action=lambda *_args: {}, make_sample_action=lambda *_args: {},
        compress_season_action=lambda _prefix: {},
    )
    endpoint = next(route.endpoint for route in app.routes if isinstance(route, Route) and route.path == "/api/app/movies")
    response = endpoint()
    assert [movie["prefix"] for movie in json.loads(response.body)["movies"]] == ["movies/Heat (1995)"]
