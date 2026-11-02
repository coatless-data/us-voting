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
    assert not is_due(ovw, dt("2026-10-30T06:23Z"), dt("2026-10-30T12:05Z"))
