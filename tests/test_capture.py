import dataclasses
import re
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import us_voting.capture as capture_mod
from conftest import NOW, Clock, mock_client, ticking, write_config
from us_voting import cli
from us_voting.capture import (
    CaptureResult,
    capture_id_for,
    due_sources,
    route,
    run_capture,
)
from us_voting.config import load_config
from us_voting.kinds import KINDS
from us_voting.kinds.base import CapturedFile
from us_voting.status import Status, UrlState, hold_fetch_state, load_status

SOURCES = """
[[source]]
id = "pub"
state = "NC"
kind = "url"
raw = "public"
schedule = "results"
provides = ["results"]
url = "https://a.gov/p.csv"

[[source]]
id = "priv"
state = "NC"
kind = "url"
raw = "private"
schedule = "results"
provides = ["registration"]
url = "https://a.gov/v.csv"
"""

PUB_BODY = b"county,votes\nWAKE,1\n"
VOTER_BODY = b"county_desc,voter_reg_num\nWAKE,1\n"


def handler_for(pub_body: bytes = PUB_BODY):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/p.csv":
            return httpx.Response(200, content=pub_body)
        return httpx.Response(200, content=b"id,x\n1,2\n")

    return handler


def setup(tmp_path, handler=None):
    cfg = load_config(write_config(tmp_path, SOURCES))
    client, _ = mock_client(handler or handler_for(), http=cfg.http)
    return cfg, Status(), client


def members(path: Path) -> list[str]:
    with tarfile.open(path) as tar:
        return sorted(tar.getnames())


def stamped(names: list[str]) -> list[str]:
    """Replace the capture-time stamp, which comes from the client's wall clock."""
    return [re.sub(r"/\d{8}T\d{6}Z__", "/STAMP__", n) for n in names]


def tars(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.tar.gz")) if folder.exists() else []


def test_capture_id_for():
    assert capture_id_for(datetime(2026, 11, 3, 23, 23, 45, tzinfo=UTC)) == "2026-11-03T2323Z"


def test_due_and_only(tmp_path):
    cfg, status, _ = setup(tmp_path)
    status.source("pub").last_run = NOW
    assert [s.id for s in due_sources(cfg, status, NOW, None)] == ["priv"]
    assert [s.id for s in due_sources(cfg, status, NOW, ["pub"])] == ["pub"]
    assert [s.id for s in due_sources(cfg, status, NOW, ["priv", "pub"])] == ["pub", "priv"]
    with pytest.raises(ValueError, match="nope"):
        due_sources(cfg, status, NOW, ["pub", "nope"])


def test_failed_source_is_due_again_but_blocked_is_not(tmp_path):
    cfg, status, _ = setup(tmp_path)
    for source_id, result in (("pub", "failed"), ("priv", "blocked")):
        status.source(source_id).last_run = NOW
        status.source(source_id).result = result
    later = NOW.replace(minute=50)
    assert [s.id for s in due_sources(cfg, status, later, None)] == ["pub"]


def test_route(tmp_path):
    cfg, _, _ = setup(tmp_path)
    f = tmp_path / "x.csv"
    f.write_bytes(VOTER_BODY)
    cf = CapturedFile("pub", "u", "x.csv", f, "h", 1, NOW, 200, {})
    assert route(cfg.sources["priv"], cf) == ("private", None)
    assert route(cfg.sources["pub"], cf) == ("private", "column 'voter_reg_num'")
    f.write_bytes(PUB_BODY)
    assert route(cfg.sources["pub"], cf) == ("public", None)


def test_end_to_end_split_by_privacy(tmp_path):
    cfg, status, client = setup(tmp_path)
    r = run_capture(cfg, status, tmp_path / "out", NOW, client)
    pub = tmp_path / "out/capture/public/capture-2026-11-03T2323Z.tar.gz"
    prv = tmp_path / "out/capture/private/capture-2026-11-03T2323Z.tar.gz"
    assert stamped(members(pub)) == ["_bundle.json", "pub/STAMP__p.csv"]
    assert stamped(members(prv)) == ["_bundle.json", "priv/STAMP__v.csv"]
    saved = load_status(tmp_path / "out/capture/status.json")
    assert saved.sources["pub"].result == "ok" and saved.capture_id == "2026-11-03T2323Z"
    assert saved.generated_at == NOW
    assert r.files == 2 and not (tmp_path / "out/work").exists()
    assert r.ran == ["pub", "priv"] and not r.all_failed
    assert isinstance(r, CaptureResult) and len(r.bundles) == 2


def test_public_file_with_voter_columns_is_rerouted(tmp_path):
    cfg, status, client = setup(tmp_path, handler_for(VOTER_BODY))
    run_capture(cfg, status, tmp_path / "out", NOW, client)
    assert tars(tmp_path / "out/capture/public") == []
    saved = load_status(tmp_path / "out/capture/status.json")
    (entry,) = saved.rerouted
    assert stamped([entry["member"]]) == ["pub/STAMP__p.csv"]
    assert entry["source"] == "pub" and entry["reason"] == "column 'voter_reg_num'"
    assert entry["member"] in members(
        tmp_path / "out/capture/private/capture-2026-11-03T2323Z.tar.gz"
    )


def test_unchanged_files_make_no_bundle(tmp_path):
    cfg, status, client = setup(tmp_path)
    run_capture(cfg, status, tmp_path / "out", NOW, client)
    saved = load_status(tmp_path / "out/capture/status.json")
    r = run_capture(cfg, saved, tmp_path / "out2", NOW, client, only=["pub", "priv"])
    assert r.files == 0 and r.bundles == []
    assert tars(tmp_path / "out2/capture/public") == []
    assert tars(tmp_path / "out2/capture/private") == []
    assert saved.rerouted == []


def test_kind_exception_is_recorded(tmp_path, monkeypatch):
    class Boom:
        def fetch(self, run):
            raise RuntimeError("boom")

        def owns(self, source, url):
            return False

    monkeypatch.setitem(KINDS, "url", Boom())
    cfg, status, client = setup(tmp_path)
    r = run_capture(cfg, status, tmp_path / "out", NOW, client)
    assert status.sources["pub"].result == "failed"
    assert status.sources["pub"].error == "RuntimeError: boom"
    assert r.all_failed and r.files == 0


TWO_URLS = """
[[source]]
id = "pub"
state = "NC"
kind = "url"
raw = "public"
schedule = "results"
provides = ["results"]
urls = ["https://a.gov/one.csv", "https://a.gov/two.csv"]
"""


def test_fetch_state_is_restored_when_a_kind_raises_partway(tmp_path):
    cfg = load_config(write_config(tmp_path, TWO_URLS))

    def handler(request):
        if request.url.path == "/two.csv":
            raise OSError("disk full simulated")
        return httpx.Response(200, content=PUB_BODY, headers={"etag": '"e1"'})

    client, _ = mock_client(handler, http=cfg.http)

    def before():
        return {"https://a.gov/one.csv": UrlState(etag='"old"', sha256="00")}

    status = Status()
    status.source("pub").urls = before()
    r = run_capture(cfg, status, tmp_path / "out", NOW, client)
    st = status.sources["pub"]
    assert (st.result, st.error) == ("failed", "OSError: disk full simulated")
    assert st.urls == before()
    assert r.files == 0 and r.bundles == []
    assert load_status(tmp_path / "out/capture/status.json").sources["pub"].urls == before()


def test_max_seconds_leaves_rest_due(tmp_path):
    cfg, status, client = setup(tmp_path)
    cfg = dataclasses.replace(cfg, workers=1)
    clock = iter([0.0, 0.0] + [cfg.max_seconds + 1] * 10)
    r = run_capture(cfg, status, tmp_path / "out", NOW, client, monotonic=lambda: next(clock))
    assert r.ran == ["pub"]
    assert status.sources["pub"].last_run == NOW
    assert status.sources["priv"].last_run is None
    assert [s.id for s in due_sources(cfg, status, NOW, None)] == ["priv"]


def test_existing_output_is_refused(tmp_path):
    cfg, status, client = setup(tmp_path)
    keep = tmp_path / "out/capture/keep.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("x")
    with pytest.raises(FileExistsError, match="capture"):
        run_capture(cfg, status, tmp_path / "out", NOW, client)
    assert keep.exists()


def cli_args(tmp_path, root, *extra):
    return [
        "capture",
        "--config",
        str(root),
        "--status",
        str(tmp_path / "st.json"),
        *extra,
    ]


def test_cli_writes_github_outputs(tmp_path, monkeypatch, capsys):
    root = write_config(tmp_path, SOURCES)
    client, _ = mock_client(handler_for())
    monkeypatch.setattr(capture_mod, "Client", lambda cfg: client)
    out_file = tmp_path / "gh_out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    assert cli.main(cli_args(tmp_path, root, "--out", str(tmp_path / "out"))) == 0
    lines = out_file.read_text().splitlines()
    assert lines[0].startswith("capture_id=")
    assert lines[1:] == ["files=2", "all_failed=false"]
    shown = capsys.readouterr().out
    assert "pub" in shown and "ok" in shown and "WAKE" not in shown


def test_cli_unknown_only_and_existing_out(tmp_path, monkeypatch, capsys):
    root = write_config(tmp_path, SOURCES)
    client, _ = mock_client(handler_for())
    monkeypatch.setattr(capture_mod, "Client", lambda cfg: client)
    assert cli.main(cli_args(tmp_path, root, "--out", str(tmp_path / "o1"), "--only", "nope")) == 1
    assert "nope" in capsys.readouterr().err
    (tmp_path / "o2/capture").mkdir(parents=True)
    (tmp_path / "o2/capture/f").write_text("x")
    assert cli.main(cli_args(tmp_path, root, "--out", str(tmp_path / "o2"))) == 1
    assert "capture" in capsys.readouterr().err


DATED = """
[[source]]
id = "dated"
state = "NC"
kind = "dated"
raw = "public"
schedule = "results"
provides = ["results"]
template = "https://a.gov/d{n}.csv"
n_range = [1, 5]
"""


def test_dated_source_cut_off_by_the_deadline(tmp_path):
    cfg = load_config(write_config(tmp_path, DATED))
    clock = Clock()
    step = cfg.max_seconds / 3
    client, _ = mock_client(
        ticking(lambda r: httpx.Response(200, content=r.url.path.encode() + b",1\n"), clock, step),
        http=cfg.http,
        monotonic=clock,
    )
    status = Status()
    untouched = UrlState(etag='"e5"', sha256="55", checked="2026-11-03T22:23:00Z")
    status.source("dated").urls["https://a.gov/d5.csv"] = untouched
    r = run_capture(cfg, status, tmp_path / "out", NOW, client, monotonic=lambda: 0.0)
    st = status.sources["dated"]
    assert st.result == "failed"
    assert st.error.startswith("capture deadline reached after 3 of 5 URLs")
    assert r.files == 3
    tar = tmp_path / "out/capture/public/capture-2026-11-03T2323Z.tar.gz"
    assert stamped(members(tar)) == [
        "_bundle.json",
        "dated/STAMP__d1.csv",
        "dated/STAMP__d2.csv",
        "dated/STAMP__d3.csv",
    ]
    assert sorted(st.urls) == [f"https://a.gov/d{n}.csv" for n in (1, 2, 3, 5)]
    assert st.urls["https://a.gov/d5.csv"] == untouched
    assert all(st.urls[f"https://a.gov/d{n}.csv"].sha256 for n in (1, 2, 3))


def test_stalled_host_costs_three_requests_and_is_not_blocked(tmp_path):
    cfg = load_config(write_config(tmp_path, DATED))
    calls = []

    def stalled(request):
        calls.append(str(request.url))
        raise httpx.ReadTimeout("stalled", request=request)

    client, _ = mock_client(stalled, http=cfg.http)
    status = Status()
    r = run_capture(cfg, status, tmp_path / "out", NOW, client)
    assert len(calls) == 3
    assert r.results == {"dated": "failed"}
    assert "timeout" in status.sources["dated"].error
    assert status.sources["dated"].urls == {}


DAILY = """
[[source]]
id = "daily"
state = "NC"
kind = "url"
raw = "public"
schedule = "daily"
provides = ["results"]
url = "https://a.gov/p.csv"
"""


def test_source_captured_in_a_failed_publish_run_waits_for_its_next_slot(tmp_path):
    cfg = load_config(write_config(tmp_path, DAILY))
    client, _ = mock_client(handler_for(), http=cfg.http)
    now = datetime(2026, 11, 6, 14, 23, tzinfo=UTC)
    committed = Status()
    run_capture(cfg, load_status(tmp_path / "none.json"), tmp_path / "out", now, client)
    current = load_status(tmp_path / "out/capture/status.json")
    hold_fetch_state(committed, current)
    st = current.sources["daily"]
    assert st.urls == {} and st.result == "ok" and st.last_run == now
    for hours in (1, 12, 23):
        assert due_sources(cfg, current, now + timedelta(hours=hours), None) == [], hours
    assert [s.id for s in due_sources(cfg, current, now + timedelta(days=1), None)] == ["daily"]
