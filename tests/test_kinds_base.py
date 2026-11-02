import hashlib
from datetime import UTC, datetime

import httpx
import pytest

from conftest import NOW, Clock, make_run, one_source
from us_voting.kinds.base import (
    KindResult,
    UrlOutcome,
    fetch_file,
    safe_name,
    source_result,
    text_failure,
)
from us_voting.status import UrlState

URL = "https://a.gov/f.csv"
BEFORE_EXPECTED = datetime(2026, 11, 3, 22, 0, tzinfo=UTC)
AFTER_EXPECTED = datetime(2026, 11, 5, tzinfo=UTC)


def test_saved_then_unchanged(tmp_path):
    run = make_run(tmp_path, handler=lambda r: httpx.Response(200, content=b"x,y\n"))
    first = fetch_file(run, URL, "f.csv")
    assert first.result == "saved" and first.file.path.exists()
    assert first.file.path == run.work_dir / "f.csv"
    assert first.file.status == 200 and first.file.bytes == 4
    assert first.file.received_at.tzinfo is not None
    second = fetch_file(run, URL, "f.csv")
    assert second.result == "unchanged" and second.file is None
    assert run.status.urls[URL].sha256 == hashlib.sha256(b"x,y\n").hexdigest()
    assert run.status.urls[URL].checked is not None
    assert [p.name for p in run.work_dir.iterdir()] == ["f.csv"]


def test_name_collision_gets_suffix(tmp_path):
    run = make_run(tmp_path, handler=lambda r: httpx.Response(200, content=r.url.path.encode()))
    a = fetch_file(run, "https://a.gov/1/f.csv", "f.csv")
    b = fetch_file(run, "https://a.gov/2/f.csv", "f.csv")
    c = fetch_file(run, "https://a.gov/3/f.csv", "f.csv")
    assert [o.file.path.name for o in (a, b, c)] == ["f.csv", "f-2.csv", "f-3.csv"]


def test_conditional_uses_state_etag(tmp_path):
    seen = {}

    def h(r):
        seen.update(r.headers)
        return httpx.Response(304)

    run = make_run(tmp_path, h)
    run.status.urls[URL] = UrlState(etag='"e"')
    out = fetch_file(run, URL, "f.csv")
    assert seen["if-none-match"] == '"e"'
    assert out.result == "not_modified"
    assert run.status.urls[URL].checked is not None


def test_conditional_false_skips_headers(tmp_path):
    seen = {}

    def h(r):
        seen.update(r.headers)
        return httpx.Response(200, content=b"z")

    run = make_run(tmp_path, h)
    run.status.urls[URL] = UrlState(etag='"e"', last_modified="Mon, 05 Oct 2026 14:57:45 GMT")
    fetch_file(run, URL, "f.csv", conditional=False)
    assert "if-none-match" not in seen and "if-modified-since" not in seen


def test_saved_records_validators(tmp_path):
    run = make_run(
        tmp_path,
        lambda r: httpx.Response(
            200,
            content=b"q",
            headers={"etag": '"n"', "last-modified": "Mon, 05 Oct 2026 14:57:45 GMT"},
        ),
    )
    out = fetch_file(run, URL, "f.csv", version="v7")
    st = run.status.urls[URL]
    assert st.etag == '"n"' and st.last_modified.startswith("Mon, 05 Oct 2026")
    assert out.file.version == "v7"


@pytest.mark.parametrize(
    ("status", "body", "ctype", "result"),
    [
        (404, b"", "text/plain", "missing"),
        (403, b"", "text/plain", "missing"),
        (401, b"", "text/plain", "missing"),
        (500, b"", "text/plain", "http_error"),
        (200, b"<html>", "text/html", "soft_404"),
    ],
)
def test_outcome_classes(tmp_path, status, body, ctype, result):
    run = make_run(
        tmp_path,
        lambda r: httpx.Response(status, content=body, headers={"content-type": ctype}),
    )
    out = fetch_file(run, URL, "f.csv")
    assert out.result == result and out.file is None
    assert list(run.work_dir.iterdir()) == []


def test_safe_name():
    assert safe_name("a b/c?d=e&f") == "a_b_c_d=e_f"


@pytest.mark.parametrize(
    ("kind", "results", "note", "now", "expected"),
    [
        ("url", ["saved", "unchanged"], None, NOW, ("ok", None)),
        ("url", ["saved", "missing"], None, NOW, ("failed", "1 of 2 URLs failed")),
        ("dated", ["missing", "missing", "saved"], None, NOW, ("ok", None)),
        ("dated", ["missing", "missing"], None, NOW, ("waiting", None)),
        ("dated", ["http_error", "saved"], None, NOW, ("failed", "1 of 2 URLs failed")),
        ("page_links", ["saved"] * 14 + ["soft_404"] * 4, None, NOW, ("ok", None)),
        ("page_links", ["saved", "unchanged", "missing", "soft_404"], None, NOW, ("ok", None)),
        ("page_links", ["soft_404"] * 3, None, NOW, ("failed", "3 of 3 URLs failed")),
        ("page_links", ["missing", "soft_404"], None, NOW, ("failed", "2 of 2 URLs failed")),
        ("page_links", ["saved", "http_error"], None, NOW, ("failed", "1 of 2 URLs failed")),
        ("url", ["http_error"], None, BEFORE_EXPECTED, ("waiting", None)),
        ("url", ["http_error"], None, AFTER_EXPECTED, ("failed", "1 of 1 URLs failed")),
        ("url", ["saved", "blocked"], None, NOW, ("blocked", "https://a.gov/u2")),
        ("clarity", [], "not_listed_yet", NOW, ("waiting", None)),
        ("clarity", [], "same_version", NOW, ("ok", None)),
        ("page_links", [], "no_links", NOW, ("waiting", None)),
    ],
)
def test_source_result(tmp_path, kind, results, note, now, expected):
    if kind in ("clarity", "page_links"):
        body_kind = 'kind = "url"\nurl = "https://a.gov/f"\n'
    else:
        body_kind = f'kind = "{kind}"\n' + (
            'template = "https://a.gov/{n}"\nn_range = [1, 2]\n'
            if kind == "dated"
            else 'url = "https://a.gov/f"\n'
        )
    body = (
        'id = "s"\nstate = "NC"\nraw = "public"\nschedule = "results"\nprovides = ["results"]\n'
        + ("expect_missing_until = 2026-11-04T00:00:00Z\n" if now != NOW else "")
        + body_kind
    )
    source = one_source(tmp_path, body)
    object.__setattr__(source, "kind", kind)
    outcomes = [
        UrlOutcome(f"https://a.gov/u{i + 1}", r, status=500 if r == "http_error" else 404)
        for i, r in enumerate(results)
    ]
    result, error = source_result(source, KindResult(outcomes, note=note), now)
    assert result == expected[0]
    if expected[1] is None:
        assert error is None
    else:
        assert error.startswith(expected[1]) and len(error) <= 300


@pytest.mark.parametrize("error", ["host_unresponsive", "deadline"])
def test_unresponsive_and_deadline_are_errors_not_blocks(error):
    assert text_failure(URL, None, error).result == "error"
    assert text_failure(URL, None, "host_abandoned").result == "blocked"


def test_fetch_after_the_deadline_is_an_error_and_touches_no_fetch_state(tmp_path):
    clock = Clock()
    run = make_run(tmp_path, lambda r: httpx.Response(200, content=b"x"), monotonic=clock)
    run.status.urls[URL] = UrlState(etag='"old"', sha256="ab")
    run.client.set_deadline(0)
    out = fetch_file(run, URL, "f.csv")
    assert (out.result, out.error, out.file) == ("error", "deadline", None)
    assert run.status.urls == {URL: UrlState(etag='"old"', sha256="ab")}
    other = fetch_file(run, "https://a.gov/new.csv", "new.csv")
    assert other.result == "error" and "https://a.gov/new.csv" not in run.status.urls


def test_unresponsive_host_is_an_error(tmp_path):
    def h(r):
        raise httpx.ReadTimeout("stalled", request=r)

    run = make_run(tmp_path, h)
    assert fetch_file(run, URL, "f.csv").error == "timeout"
    out = fetch_file(run, "https://a.gov/g.csv", "g.csv")
    assert (out.result, out.error) == ("error", "host_unresponsive")
    assert run.status.urls == {}


def _url_source(tmp_path, extra=""):
    body = (
        'id = "s"\nstate = "NC"\nraw = "public"\nschedule = "results"\nprovides = ["results"]\n'
        'kind = "url"\nurl = "https://a.gov/f"\n' + extra
    )
    return one_source(tmp_path, body)


def test_source_level_failure_makes_the_source_failed(tmp_path):
    source = _url_source(tmp_path, "expect_missing_until = 2026-11-04T00:00:00Z\n")
    kr = KindResult(
        [UrlOutcome(URL, "saved")], failure="capture deadline reached after 1 of 3 URLs"
    )
    assert source_result(source, kr, BEFORE_EXPECTED) == (
        "failed",
        "capture deadline reached after 1 of 3 URLs",
    )


def test_blocked_wins_over_a_source_level_failure(tmp_path):
    source = _url_source(tmp_path)
    kr = KindResult([UrlOutcome(URL, "blocked")], failure="capture deadline reached after 1 of 3")
    assert source_result(source, kr, NOW) == ("blocked", URL)
