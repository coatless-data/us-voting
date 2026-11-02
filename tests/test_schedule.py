from datetime import datetime

import pytest

from conftest import one_source
from us_voting.schedule import is_due

BODY = """id = "nc-x"
state = "NC"
kind = "url"
raw = "public"
schedule = "results"
provides = ["results"]
url = "https://a.gov/f.zip"
"""


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


@pytest.fixture
def src(tmp_path):
    return one_source(tmp_path, BODY)


@pytest.fixture
def src18(tmp_path):
    return one_source(tmp_path, BODY + "daily_at_utc = 18\n")


@pytest.fixture
def ovw(tmp_path):
    return one_source(
        tmp_path, BODY.replace('schedule = "results"', 'schedule = "early_vote_overwrite"')
    )


def test_outside_active_range(src):
    assert not is_due(src, None, dt("2026-10-03T23:59Z"))  # start is -720h = 2026-10-04T00:00Z
    assert not is_due(src, None, dt("2027-01-02T00:00Z"))  # end is +1440h


def test_never_run_is_due(src):
    assert is_due(src, None, dt("2026-10-20T10:00Z"))


def test_hourly_window_tolerates_drift(src):
    assert is_due(src, dt("2026-11-03T23:23Z"), dt("2026-11-04T00:21Z"))  # 58 minutes later
    assert not is_due(src, dt("2026-11-03T23:23Z"), dt("2026-11-03T23:50Z"))


def test_daily_anchor(src):
    assert is_due(src, dt("2026-11-05T15:23Z"), dt("2026-11-06T15:20Z"))
    assert not is_due(src, dt("2026-11-05T15:23Z"), dt("2026-11-06T14:59Z"))
    assert not is_due(src, dt("2026-11-06T15:05Z"), dt("2026-11-06T22:00Z"))


def test_source_daily_at_overrides_schedule(src18):  # daily_at_utc = 18 on the source
    assert not is_due(src18, dt("2026-11-05T19:00Z"), dt("2026-11-06T17:30Z"))
    assert is_due(src18, dt("2026-11-05T19:00Z"), dt("2026-11-06T18:10Z"))


def test_six_hour_profile(ovw):  # schedule = "early_vote_overwrite"
    assert is_due(ovw, dt("2026-10-30T06:23Z"), dt("2026-10-30T12:10Z"))
    assert not is_due(ovw, dt("2026-10-30T06:23Z"), dt("2026-10-30T11:50Z"))


# The results profile is hourly from 23:00 on election day to 15:00 the next day.
def test_hourly_gap_just_under_half_an_hour_is_not_due(src):
    assert not is_due(src, dt("2026-11-04T01:23:00Z"), dt("2026-11-04T01:52:59Z"))


def test_hourly_gap_of_half_an_hour_is_due(src):
    assert is_due(src, dt("2026-11-04T01:23:00Z"), dt("2026-11-04T01:53:00Z"))


def test_late_run_then_on_time_run_is_due(src):
    assert is_due(src, dt("2026-11-03T23:43Z"), dt("2026-11-04T00:23Z"))


def test_six_hour_profile_gap_of_five_and_a_half_hours_is_due(ovw):
    assert is_due(ovw, dt("2026-10-30T06:23Z"), dt("2026-10-30T11:53Z"))


@pytest.fixture
def daily(tmp_path):  # anchored at 14:00 UTC, no start or end
    return one_source(tmp_path, BODY.replace('schedule = "results"', 'schedule = "daily"'))


def test_failed_daily_source_is_due_on_the_next_hourly_run(daily):
    assert not is_due(daily, dt("2026-11-06T14:23Z"), dt("2026-11-06T15:23Z"), "ok")
    assert is_due(daily, dt("2026-11-06T14:23Z"), dt("2026-11-06T15:23Z"), "failed")


def test_blocked_daily_source_keeps_its_cadence(daily):
    assert not is_due(daily, dt("2026-11-06T14:23Z"), dt("2026-11-06T15:23Z"), "blocked")
    assert is_due(daily, dt("2026-11-06T14:23Z"), dt("2026-11-07T14:23Z"), "blocked")


def test_failed_source_still_respects_start_and_end(src):
    assert not is_due(src, dt("2027-01-01T23:23Z"), dt("2027-01-02T00:23Z"), "failed")
    assert not is_due(src, dt("2026-10-03T22:23Z"), dt("2026-10-03T23:23Z"), "failed")
