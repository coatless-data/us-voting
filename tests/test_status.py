from datetime import UTC, datetime

from conftest import NOW
from us_voting.status import (
    SourceStatus,
    Status,
    UrlState,
    load_status,
    record_run,
    save_status,
)


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
