import dataclasses
import hashlib

import httpx
import pytest

from conftest import CONFIG_DIR, Clock, mock_client
from us_voting.config import load_config
from us_voting.http import Client, is_block

HTML = b"<!DOCTYPE html><html><body>Not found</body></html>"


def test_fetch_streams_to_disk_and_hashes(tmp_path):
    body = b"a,b\n1,2\n"
    c, _ = mock_client(
        lambda r: httpx.Response(
            200, content=body, headers={"content-type": "text/csv", "set-cookie": "s=1"}
        )
    )
    f = c.fetch("https://dl.ncsbe.gov/x.csv", tmp_path / "x")
    assert f.path.read_bytes() == body
    assert f.sha256 == hashlib.sha256(body).hexdigest()
    assert f.bytes == 8
    assert f.error is None
    assert f.headers["content-type"] == "text/csv"
    assert "set-cookie" not in f.headers
    assert f.received_at.microsecond == 0


def test_conditional_request_and_304(tmp_path):
    seen = {}

    def h(r):
        seen.update(r.headers)
        return httpx.Response(304)

    c, _ = mock_client(h)
    f = c.fetch(
        "https://a.gov/x",
        tmp_path / "x",
        etag='"abc"',
        last_modified="Mon, 05 Oct 2026 14:57:45 GMT",
    )
    assert seen["if-none-match"] == '"abc"'
    assert "if-modified-since" in seen
    assert f.status == 304
    assert f.path is None
    assert f.error is None


def test_soft_404_deletes_file(tmp_path):
    c, _ = mock_client(
        lambda r: httpx.Response(200, content=HTML, headers={"content-type": "text/html"})
    )
    f = c.fetch("https://a.gov/x.csv", tmp_path / "x")
    assert f.error == "soft_404"
    assert f.path is None
    assert not (tmp_path / "x").exists()


def test_html_allowed_when_not_data(tmp_path):
    c, _ = mock_client(
        lambda r: httpx.Response(200, content=HTML, headers={"content-type": "text/html"})
    )
    f = c.fetch("https://a.gov/x", tmp_path / "x", data_expected=False)
    assert f.error is None
    assert f.path == tmp_path / "x"
    assert f.path.read_bytes() == HTML


def test_empty_body(tmp_path):
    c, _ = mock_client(lambda r: httpx.Response(200, content=b""))
    f = c.fetch("https://a.gov/x", tmp_path / "x")
    assert f.error == "empty"
    assert f.path is None
    assert not (tmp_path / "x").exists()


def test_identity_headers(tmp_path):
    ua, xproject = {}, {}

    def h(r):
        ua[r.url.host] = r.headers["user-agent"]
        xproject[r.url.host] = r.headers["x-project"]
        return httpx.Response(200, content=b"x")

    http = load_config(CONFIG_DIR).http
    c, _ = mock_client(h)
    c.fetch("https://sos-prod.tnsosgovfiles.com/a", tmp_path / "a")
    c.fetch("https://dl.ncsbe.gov/b", tmp_path / "b")
    assert ua["sos-prod.tnsosgovfiles.com"] == http.browser_user_agent
    assert ua["dl.ncsbe.gov"] == http.project_user_agent
    assert all(v == http.x_project for v in xproject.values())


def test_retry_then_success(tmp_path):
    responses = [httpx.Response(503), httpx.Response(200, content=b"ok")]
    c, sleeps = mock_client(lambda r: responses.pop(0))
    f = c.fetch("https://a.gov/x", tmp_path / "x")
    assert f.status == 200
    assert f.path.read_bytes() == b"ok"
    assert 2.0 in sleeps


def test_retries_exhausted_keeps_status(tmp_path):
    calls = []

    def h(r):
        calls.append(1)
        return httpx.Response(500, content=b"boom")

    c, _ = mock_client(h)
    f = c.fetch("https://a.gov/x", tmp_path / "x")
    assert len(calls) == 3
    assert f.status == 500
    assert f.path is None
    assert f.sha256 is None
    assert f.bytes == 0


def test_pacing_between_requests_to_one_host(tmp_path):
    c, sleeps = mock_client(lambda r: httpx.Response(200, content=b"x"))
    c.fetch("https://a.gov/1", tmp_path / "1")
    assert sleeps == []
    c.fetch("https://a.gov/2", tmp_path / "2")
    assert sleeps == [c.cfg.min_interval_seconds]


def test_blocks_abandon_host(tmp_path):
    calls = []

    def h(r):
        calls.append(1)
        return httpx.Response(403, headers={"cf-mitigated": "challenge"})

    c, _ = mock_client(h)
    first = c.fetch("https://a.gov/1", tmp_path / "1")
    second = c.fetch("https://a.gov/2", tmp_path / "2")
    assert first.error == "blocked"
    assert second.error == "blocked"
    assert not c.abandoned("https://other.gov/") and c.abandoned("https://a.gov/3")
    third = c.fetch("https://a.gov/3", tmp_path / "3")
    assert len(calls) == 2
    assert third.error == "host_abandoned"
    assert third.path is None


def test_block_in_200_body_removes_file(tmp_path):
    c, _ = mock_client(lambda r: httpx.Response(200, content=b"<title>Just a moment...</title>"))
    f = c.fetch("https://a.gov/x", tmp_path / "x")
    assert f.error == "blocked"
    assert f.path is None
    assert not (tmp_path / "x").exists()


def test_transport_error(tmp_path):
    def h(r):
        raise httpx.ConnectError("nope")

    c, _ = mock_client(h)
    f = c.fetch("https://a.gov/x", tmp_path / "x")
    assert f.error == "transport"
    assert f.status is None
    assert f.path is None


def test_timeout_error(tmp_path):
    def h(r):
        raise httpx.ReadTimeout("slow")

    c, _ = mock_client(h)
    assert c.fetch("https://a.gov/x", tmp_path / "x").error == "timeout"


def test_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr("us_voting.http.MAX_FILE_BYTES", 10)
    c, _ = mock_client(lambda r: httpx.Response(200, content=b"x" * 50))
    f = c.fetch("https://a.gov/x", tmp_path / "x")
    assert f.error == "too_large"
    assert f.path is None
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize(
    ("status", "headers", "head", "expected"),
    [
        (429, {}, b"", True),
        (403, {"cf-mitigated": "challenge"}, b"", True),
        (403, {}, b"<title>Just a moment...</title>", True),
        (200, {}, b"Request unsuccessful. Incapsula incident ID", True),
        (403, {}, b"Access Denied ... Reference #18.2", True),
        (403, {}, b"<html>Forbidden</html>", False),
        (404, {}, b"", False),
    ],
)
def test_is_block(status, headers, head, expected):
    assert is_block(status, headers, head) is expected


def test_get_text():
    c, _ = mock_client(lambda r: httpx.Response(200, content=b"382865\n"))
    assert c.get_text("https://a.gov/v") == (200, "382865\n", None)


def test_get_text_transport_error():
    def h(r):
        raise httpx.ConnectError("nope")

    c, _ = mock_client(h)
    assert c.get_text("https://a.gov/v") == (None, "", "transport")


def test_get_text_404():
    c, _ = mock_client(lambda r: httpx.Response(404, content=b"gone"))
    assert c.get_text("https://a.gov/v") == (404, "", None)


def test_get_text_block_page_is_not_content():
    c, _ = mock_client(
        lambda r: httpx.Response(200, content=b"Request unsuccessful. Incapsula incident ID: 1")
    )
    assert c.get_text("https://a.gov/v") == (200, "", "blocked")


def test_get_text_abandoned_host():
    c, _ = mock_client(lambda r: httpx.Response(429))
    c.get_text("https://a.gov/1")
    c.get_text("https://a.gov/2")
    assert c.get_text("https://a.gov/3") == (None, "", "host_abandoned")


def test_get_text_too_large(monkeypatch):
    monkeypatch.setattr("us_voting.http.SMALL_LIMIT", 10)
    c, _ = mock_client(lambda r: httpx.Response(200, content=b"x" * 50))
    assert c.get_text("https://a.gov/v") == (200, "", "too_large")


def stalled(calls):
    def h(r):
        calls.append(str(r.url))
        raise httpx.ReadTimeout("stalled", request=r)

    return h


def test_timeouts_before_abandon_is_configured():
    assert load_config(CONFIG_DIR).http.timeouts_before_abandon == 3


def test_unresponsive_host_gets_three_attempts_in_total(tmp_path):
    calls = []
    http = dataclasses.replace(load_config(CONFIG_DIR).http, max_attempts=5)
    c, _ = mock_client(stalled(calls), http=http)
    first = c.fetch("https://a.gov/1", tmp_path / "1")
    rest = [c.fetch(f"https://a.gov/{n}", tmp_path / str(n)) for n in (2, 3, 4)]
    assert len(calls) == 3
    assert first.error == "timeout"
    assert [f.error for f in rest] == ["host_unresponsive"] * 3
    assert all(f.status is None and f.path is None for f in rest)
    assert c.get_text("https://a.gov/v") == (None, "", "host_unresponsive")
    assert c.get_text("https://other.gov/v") == (None, "", "timeout")


def test_transport_errors_count_as_no_response(tmp_path):
    calls = []

    def h(r):
        calls.append(1)
        raise httpx.ConnectError("refused", request=r)

    c, _ = mock_client(h)
    c.fetch("https://a.gov/1", tmp_path / "1")
    assert c.fetch("https://a.gov/2", tmp_path / "2").error == "host_unresponsive"
    assert len(calls) == 3


def test_a_response_resets_the_no_response_count(tmp_path):
    answers = ["timeout", "timeout", 200, "timeout", "timeout", 503, "timeout", "timeout", 200]
    calls = []

    def h(r):
        calls.append(1)
        answer = answers.pop(0)
        if answer == "timeout":
            raise httpx.ConnectTimeout("slow", request=r)
        return httpx.Response(answer, content=b"ok")

    c, _ = mock_client(h)
    assert c.fetch("https://a.gov/1", tmp_path / "1").error is None
    assert c.get_text("https://a.gov/2") == (503, "", None)
    assert c.fetch("https://a.gov/3", tmp_path / "3").error is None
    assert len(calls) == 9
    assert not c.abandoned("https://a.gov/4")


def test_unresponsive_is_not_a_block(tmp_path):
    c, _ = mock_client(stalled([]))
    c.fetch("https://a.gov/1", tmp_path / "1")
    assert c.fetch("https://a.gov/2", tmp_path / "2").error == "host_unresponsive"
    assert c.abandoned("https://a.gov/3")


def test_no_time_left_sends_nothing(tmp_path):
    calls = []
    clock = Clock()
    c, _ = mock_client(
        lambda r: calls.append(1) or httpx.Response(200, content=b"x"), monotonic=clock
    )
    c.set_deadline(60)
    assert c.time_left() == 60 and not c.past_deadline()
    clock.now = 60
    assert c.past_deadline()
    f = c.fetch("https://a.gov/x", tmp_path / "x")
    assert (f.error, f.status, f.path) == ("deadline", None, None)
    assert not (tmp_path / "x").exists()
    assert c.get_text("https://a.gov/v") == (None, "", "deadline")
    assert calls == []


def test_no_deadline_means_no_limit():
    c, _ = mock_client(lambda r: httpx.Response(200))
    assert c.time_left() is None and not c.past_deadline()


def test_deadline_clamps_the_attempt_timeouts(tmp_path):
    seen = []

    def h(r):
        seen.append(r.extensions["timeout"])
        return httpx.Response(200, content=b"x")

    clock = Clock()
    c, _ = mock_client(h, monotonic=clock)
    c.fetch("https://a.gov/1", tmp_path / "1")
    c.set_deadline(100)
    clock.now = 95
    c.fetch("https://a.gov/2", tmp_path / "2")
    cfg = c.cfg
    assert seen[0] == {
        "connect": cfg.connect,
        "read": cfg.read,
        "write": cfg.read,
        "pool": cfg.connect,
    }
    assert seen[1] == {"connect": 5, "read": 5, "write": 5, "pool": 5}


def test_deadline_clamps_the_overall_download_time(tmp_path, monkeypatch):
    monkeypatch.setattr("us_voting.http.CHUNK", 4)
    clock = Clock()

    def body():
        for _ in range(5):
            yield b"abcd"
            clock.now += 10

    c, _ = mock_client(lambda r: httpx.Response(200, content=body()), monotonic=clock)
    assert c.fetch("https://a.gov/1", tmp_path / "1").bytes == 20  # total is 1800 s
    clock.now = 0
    c.set_deadline(25)
    f = c.fetch("https://a.gov/2", tmp_path / "2")
    assert f.error == "timeout" and f.path is None


def test_retries_stop_at_the_deadline(tmp_path):
    clock = Clock()
    calls = []

    def h(r):
        calls.append(clock.now)
        clock.now += 1000
        return httpx.Response(503)

    c, _ = mock_client(h, monotonic=clock)
    c.set_deadline(1500)
    f = c.fetch("https://a.gov/x", tmp_path / "x")
    assert calls == [0, 1000]
    assert f.status == 503 and f.error is None


def test_time_running_out_during_pacing_sends_nothing(tmp_path):
    clock = Clock()
    calls = []

    def sleep(seconds):
        clock.now += seconds

    c = Client(
        load_config(CONFIG_DIR).http,
        transport=httpx.MockTransport(
            lambda r: calls.append(1) or httpx.Response(200, content=b"x")
        ),
        sleep=sleep,
        monotonic=clock,
    )
    c.set_deadline(c.cfg.min_interval_seconds / 2)
    assert c.fetch("https://a.gov/1", tmp_path / "1").error is None
    assert c.fetch("https://a.gov/2", tmp_path / "2").error == "deadline"
    assert calls == [1]
