import hashlib

import httpx
import pytest

from conftest import CONFIG_DIR, mock_client
from us_voting.config import load_config
from us_voting.http import is_block

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
    assert c.get_text("https://a.gov/v") == (200, "382865\n")

    def h(r):
        raise httpx.ConnectError("nope")

    c2, _ = mock_client(h)
    assert c2.get_text("https://a.gov/v") == (None, "")
