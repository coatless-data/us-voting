from datetime import UTC, datetime

from conftest import NOW
from us_voting.cli import main
from us_voting.status import (
    SourceStatus,
    Status,
    UrlState,
    hold_fetch_state,
    load_status,
    record_run,
    save_status,
)

EARLIER = datetime(2026, 11, 3, 15, 23, tzinfo=UTC)
LAST_OK = datetime(2026, 11, 3, 20, 23, tzinfo=UTC)


def committed() -> Status:
    s = Status(capture_id="old", generated_at=EARLIER)
    st = s.source("a")
    st.urls["https://x.gov/f"] = UrlState(etag='"old"', sha256="aa")
    st.version = "v1"
    record_run(st, "ok", None, EARLIER)
    return s


def captured() -> Status:
    s = Status(capture_id="new", generated_at=NOW, rerouted=[{"source": "a"}])
    st = s.source("a")
    st.urls = {"https://x.gov/f": UrlState(sha256="bb"), "https://x.gov/g": UrlState(sha256="cc")}
    st.version = "v2"
    st.last_ok = LAST_OK
    record_run(st, "failed", "1 of 2 URLs failed", NOW)
    new = s.source("b")
    new.urls["https://y.gov/h"] = UrlState(sha256="dd")
    new.version = "w1"
    record_run(new, "ok", None, NOW)
    return s


def test_roundtrip(tmp_path):
    s = Status(capture_id="2026-11-03T2323Z", generated_at=NOW, sources={}, rerouted=[])
    s.source("a").urls["https://x.gov/f"] = UrlState(
        etag='"e"', sha256="ab", checked="2026-11-03T23:23:00Z"
    )
    record_run(s.source("a"), "ok", None, NOW)
    save_status(s, tmp_path / "s.json")
    back = load_status(tmp_path / "s.json")
    assert back == s
    assert '"last_run": "2026-11-03T23:23:00Z"' in (tmp_path / "s.json").read_text()


def test_missing_file_is_empty(tmp_path):
    assert load_status(tmp_path / "none.json").sources == {}


def test_empty_status_has_defaults():
    s = Status()
    assert s.capture_id is None and s.generated_at is None
    assert s.sources == {} and s.rerouted == []


def test_save_creates_parent_and_loads_utc(tmp_path):
    path = tmp_path / "nested" / "dir" / "s.json"
    s = Status(generated_at=NOW)
    save_status(s, path)
    assert path.read_text(encoding="utf-8").endswith("}\n")
    assert load_status(path).generated_at == datetime(2026, 11, 3, 23, 23, tzinfo=UTC)
    assert not list(path.parent.glob(".*.tmp"))


def test_record_run_counts_failures():
    st = SourceStatus()
    record_run(st, "failed", "500", NOW)
    record_run(st, "blocked", "cf", NOW)
    assert st.consecutive_failures == 2 and st.error == "cf" and st.last_ok is None
    record_run(st, "waiting", None, NOW)
    assert st.consecutive_failures == 0 and st.error is None and st.last_ok is None
    record_run(st, "ok", None, NOW)
    assert st.last_ok == NOW


def test_hold_fetch_state_keeps_schedule_state_and_restores_fetch_state():
    previous, current = committed(), captured()
    hold_fetch_state(previous, current)
    a, b = current.sources["a"], current.sources["b"]
    assert a.urls == {"https://x.gov/f": UrlState(etag='"old"', sha256="aa")}
    assert a.version == "v1"
    assert (a.last_run, a.last_ok, a.result) == (NOW, LAST_OK, "failed")
    assert (a.consecutive_failures, a.error) == (1, "1 of 2 URLs failed")
    assert (b.urls, b.version, b.last_run, b.last_ok) == ({}, None, NOW, NOW)
    assert (current.capture_id, current.generated_at) == ("new", NOW)
    assert current.rerouted == [{"source": "a"}]


def test_hold_fetch_state_copies_rather_than_shares():
    previous, current = committed(), captured()
    hold_fetch_state(previous, current)
    current.sources["a"].urls["https://x.gov/f"].sha256 = "zz"
    assert previous.sources["a"].urls["https://x.gov/f"].sha256 == "aa"


def test_hold_fetch_state_cli_rewrites_current(tmp_path):
    prev_path, cur_path = tmp_path / "prev.json", tmp_path / "out" / "cur.json"
    save_status(committed(), prev_path)
    save_status(captured(), cur_path)
    assert main(["hold-fetch-state", str(prev_path), str(cur_path)]) == 0
    expected = captured()
    hold_fetch_state(committed(), expected)
    assert load_status(cur_path) == expected
    assert load_status(prev_path) == committed()
    assert sorted(p.name for p in cur_path.parent.iterdir()) == ["cur.json"]


def test_hold_fetch_state_cli_missing_previous_empties_fetch_state(tmp_path):
    cur_path = tmp_path / "cur.json"
    save_status(captured(), cur_path)
    assert main(["hold-fetch-state", str(tmp_path / "none.json"), str(cur_path)]) == 0
    back = load_status(cur_path)
    assert all(st.urls == {} and st.version is None for st in back.sources.values())
    assert back.sources["a"].result == "failed" and back.sources["b"].last_run == NOW


def test_hold_fetch_state_cli_missing_current_fails(tmp_path, capsys):
    save_status(committed(), tmp_path / "prev.json")
    argv = ["hold-fetch-state", str(tmp_path / "prev.json"), str(tmp_path / "none.json")]
    assert main(argv) == 1
    assert "none.json" in capsys.readouterr().err
    assert not (tmp_path / "none.json").exists()


def test_hold_fetch_state_cli_bad_previous_fails_and_leaves_current(tmp_path, capsys):
    (tmp_path / "prev.json").write_text("not json", encoding="utf-8")
    cur_path = tmp_path / "cur.json"
    save_status(captured(), cur_path)
    before = cur_path.read_bytes()
    assert main(["hold-fetch-state", str(tmp_path / "prev.json"), str(cur_path)]) == 1
    assert capsys.readouterr().err
    assert cur_path.read_bytes() == before
