from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mediaforce.web.runtime.app_computers import _open_windows
from mediaforce.web.runtime.encode_scheduler import (
    EncodeScheduleTransition, evaluate_encode_schedule_transition, max_encode_schedule_window_seconds,
    schedule_policy_deps,
)
from mediaforce.web.settings_runtime import normalize_encode_queue_scheduler

EASTERN = ZoneInfo("America/New_York")
HOST = {"key": "mini", "timezone": "America/New_York"}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
WEEKENDS = ["sat", "sun"]
SUMMER: dict[str, Any] = {"label": "Summer", "from": "05-01", "to": "09-30", "days_of_week": WEEKDAYS,
                          "all_day_days_of_week": WEEKENDS, "start_hour": 18, "end_hour": 15}
OFF_PEAK: dict[str, Any] = {
    "mode": "night", "timezone": "host_local", "days_of_week": WEEKDAYS, "all_day_days_of_week": WEEKENDS,
    "start_hour": 20, "end_hour": 6, "second_hours": [9, 17], "seasons": [SUMMER],
}


def _at(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=EASTERN)


def _transition(raw: dict[str, Any], now: datetime) -> EncodeScheduleTransition:
    policy = normalize_encode_queue_scheduler(raw)
    return evaluate_encode_schedule_transition(policy, schedule_policy_deps(), now=now, host_payload=HOST)


def _is_open(raw: dict[str, Any], now: datetime) -> bool:
    return _transition(raw, now).is_open


@pytest.mark.parametrize(("hour", "is_open"), [(10, True), (21, True), (7, False), (18, False), (2, True)])
def test_winter_weekday_avoids_both_peaks(hour: int, is_open: bool) -> None:
    assert _is_open(OFF_PEAK, _at(2026, 11, 17, hour)) is is_open


@pytest.mark.parametrize(("hour", "is_open"), [(10, True), (19, True), (16, False), (14, True)])
def test_summer_weekday_avoids_the_afternoon_peak(hour: int, is_open: bool) -> None:
    assert _is_open(OFF_PEAK, _at(2026, 7, 14, hour)) is is_open


@pytest.mark.parametrize("day", [_at(2026, 11, 21, 0), _at(2026, 7, 18, 0)])
def test_weekends_are_open_all_day_in_both_seasons(day: datetime) -> None:
    assert all(_is_open(OFF_PEAK, day + timedelta(hours=hour)) for hour in range(24))


def test_winter_weekday_opens_and_closes_at_the_right_block() -> None:
    assert _transition(OFF_PEAK, _at(2026, 11, 17, 7)) == EncodeScheduleTransition(
        is_open=False, next_opens_at=_at(2026, 11, 17, 9))
    assert _transition(OFF_PEAK, _at(2026, 11, 17, 10)) == EncodeScheduleTransition(
        is_open=True, current_closes_at=_at(2026, 11, 17, 17), next_opens_at=_at(2026, 11, 17, 20))


def test_summer_night_crossing_october_first_closes_at_the_winter_morning_peak() -> None:
    assert _transition(OFF_PEAK, _at(2026, 9, 30, 19)) == EncodeScheduleTransition(
        is_open=True, current_closes_at=_at(2026, 10, 1, 6), next_opens_at=_at(2026, 10, 1, 9))
    assert not _is_open(OFF_PEAK, _at(2026, 10, 1, 7))
    assert _is_open(OFF_PEAK, _at(2026, 10, 1, 16))


def test_winter_night_crossing_may_first_runs_until_the_summer_peak() -> None:
    assert _transition(OFF_PEAK, _at(2026, 4, 30, 18)) == EncodeScheduleTransition(
        is_open=False, next_opens_at=_at(2026, 4, 30, 20))
    assert _transition(OFF_PEAK, _at(2026, 4, 30, 21)).current_closes_at == _at(2026, 5, 1, 15)
    assert _is_open(OFF_PEAK, _at(2026, 5, 1, 7))


def test_season_wrapping_the_year_end_applies_on_both_sides_of_it() -> None:
    winter_only = {**OFF_PEAK, "start_hour": 0, "end_hour": 0, "second_hours": None,
                   "seasons": [{**OFF_PEAK, "from": "10-01", "to": "04-30", "seasons": None}]}
    assert not _is_open(winter_only, _at(2026, 12, 31, 7))
    assert not _is_open(winter_only, _at(2027, 1, 5, 7))
    assert _is_open(winter_only, _at(2026, 7, 14, 7))


def test_week_lists_every_open_span_of_two_block_days() -> None:
    monday = _at(2026, 11, 16, 0)
    spans = _open_windows(HOST, normalize_encode_queue_scheduler(OFF_PEAK), monday, monday + timedelta(days=7))

    expected = [(_at(2026, 11, 16, 0), _at(2026, 11, 16, 6))]
    for day in range(16, 20):
        expected += [(_at(2026, 11, day, 9), _at(2026, 11, day, 17)), (_at(2026, 11, day, 20), _at(2026, 11, day + 1, 6))]
    expected += [(_at(2026, 11, 20, 9), _at(2026, 11, 20, 17)), (_at(2026, 11, 20, 20), _at(2026, 11, 23, 0))]
    assert [(datetime.fromisoformat(span["starts_at"]), datetime.fromisoformat(span["ends_at"])) for span in spans] == expected


def test_longest_window_follows_the_season() -> None:
    weekdays_only = {**OFF_PEAK, "all_day_days_of_week": [],
                     "seasons": [{**SUMMER, "all_day_days_of_week": []}]}
    policy = normalize_encode_queue_scheduler(weekdays_only)
    deps = schedule_policy_deps()

    winter = max_encode_schedule_window_seconds(policy, deps, now=_at(2026, 11, 17, 10), host_payload=HOST)
    summer = max_encode_schedule_window_seconds(policy, deps, now=_at(2026, 7, 14, 10), host_payload=HOST)

    assert (winter, summer) == (timedelta(hours=10).total_seconds(), timedelta(hours=21).total_seconds())


def test_one_block_window_without_seasons_is_the_same_all_year() -> None:
    stopgap = {key: value for key, value in OFF_PEAK.items() if key not in {"second_hours", "seasons"}}
    for month in (1, 7):
        tuesday = next(_at(2026, month, day, 0) for day in range(1, 8) if _at(2026, month, day, 0).weekday() == 1)
        assert [_is_open(stopgap, tuesday + timedelta(hours=hour)) for hour in range(24)] == [
            hour < 6 or hour >= 20 for hour in range(24)]
