from datetime import UTC, datetime

import pytest

from us_voting.alerts import CAPTURE_TITLE, FAILING_AFTER, capture_alert, run_alerts
from us_voting.cli import main
from us_voting.status import SourceStatus, Status

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class FakeIssues:
    def __init__(self):
        self.opened = []
        self.closed = []

    def ensure_open(self, title, body, labels=None):
        self.opened.append((title, body, labels))

    def close(self, title, comment):
        self.closed.append((title, comment))


def make_status(result, failures=0, error=None, last_ok=None):
    st = Status(capture_id="c1", generated_at=NOW)
    st.sources["pub"] = SourceStatus(
        result=result, consecutive_failures=failures, error=error, last_ok=last_ok
    )
    return st


def test_failing_needs_three_runs():
    fake = FakeIssues()
    st = make_status("failed", FAILING_AFTER - 1, "HTTP 500")
    run_alerts(st, fake, publish_outcome="")
    assert fake.opened == [] and fake.closed == []
    st.sources["pub"].consecutive_failures = 3
    st.sources["pub"].last_ok = datetime(2026, 10, 30, 1, 2, 3, tzinfo=UTC)
    run_alerts(st, fake, publish_outcome="")
    title, body, labels = fake.opened[0]
    assert title == "Source failing: pub" and labels == ["alert"]
    assert "HTTP 500" in body and "2026-10-30T01:02:03Z" in body and "3" in body
    st.sources["pub"].result = "ok"
    run_alerts(st, fake, publish_outcome="")
    assert "Source failing: pub" in [t for t, _ in fake.closed]
    assert "Recovered" in fake.closed[-1][1] and "2026-11-01T12:00:00Z" in fake.closed[-1][1]


def test_never_ok_is_reported():
    fake = FakeIssues()
    run_alerts(make_status("failed", 3, "x"), fake, publish_outcome="")
    assert "never" in fake.opened[0][1]


def test_blocked_opens_and_closes():
    fake = FakeIssues()
    st = make_status("blocked", 1, "HTTP 403")
    run_alerts(st, fake, publish_outcome="")
    assert fake.opened[0][0] == "Source blocked: pub"
    st.sources["pub"].result = "waiting"
    run_alerts(st, fake, publish_outcome="")
    assert {t for t, _ in fake.closed} == {"Source blocked: pub", "Source failing: pub"}


def test_rerouted_opens_one_issue_per_source():
    fake = FakeIssues()
    st = make_status("ok")
    st.rerouted = [
        {"source": "pub", "member": "pub/x__p.csv", "reason": "column 'voter_reg_num'"},
        {"source": "pub", "member": "pub/y__p.csv", "reason": "column 'first_name'"},
        {"source": "other", "member": "other/z.csv", "reason": "header"},
    ]
    run_alerts(st, fake, publish_outcome="")
    assert [o[0] for o in fake.opened] == [
        "Voter-level content in public source: pub",
        "Voter-level content in public source: other",
    ]
    body = fake.opened[0][1]
    assert "pub/x__p.csv" in body and "voter_reg_num" in body and "pub/y__p.csv" in body
    assert "private" in body
    assert fake.opened[0][2] == ["alert"]


def test_publish_failing():
    for outcome, opened, closed in [
        ("failure", 1, 0),
        ("success", 0, 1),
        ("skipped", 0, 0),
        ("cancelled", 0, 0),
        ("", 0, 0),
    ]:
        fake = FakeIssues()
        run_alerts(Status(generated_at=NOW), fake, publish_outcome=outcome)
        assert len(fake.opened) == opened and len(fake.closed) == closed
        if opened:
            assert fake.opened[0][0] == "Publish failing" and fake.opened[0][2] == ["alert"]
        if closed:
            assert fake.closed[0][0] == "Publish failing"


def test_cli_dry_mode_always_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    path = tmp_path / "s.json"
    path.write_text(
        '{"capture_id": "c", "generated_at": "2026-11-01T12:00:00Z", "sources": '
        '{"pub": {"result": "blocked", "consecutive_failures": 1, "error": "HTTP 403"}}}',
        encoding="utf-8",
    )
    assert main(["alerts", str(path), "--publish-outcome", "weird"]) == 0
    assert "Source blocked: pub" in capsys.readouterr().out


def test_cli_swallows_unexpected_errors(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert main(["alerts", str(path)]) == 0
    assert capsys.readouterr().out.startswith("::warning::")


def test_capture_failing_opens_with_the_run_and_success_closes(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "coatless-data/us-voting")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    fake = FakeIssues()
    capture_alert(fake, "failure")
    ((title, body, labels),) = fake.opened
    assert CAPTURE_TITLE == "Capture failing" == title and labels == ["alert"]
    assert "https://github.com/coatless-data/us-voting/actions/runs/42" in body
    capture_alert(fake, "success")
    assert [t for t, _ in fake.closed] == ["Capture failing"]
    assert "Recovered" in fake.closed[0][1]


@pytest.mark.parametrize("outcome", ["skipped", "cancelled", ""])
def test_capture_alert_ignores_other_outcomes(outcome):
    fake = FakeIssues()
    capture_alert(fake, outcome)
    assert fake.opened == [] and fake.closed == []


def test_cli_capture_failure_needs_no_status_file(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert main(["alerts", "--capture-outcome", "failure"]) == 0
    out = capsys.readouterr().out
    assert "ensure_open: Capture failing" in out and "::warning::" not in out


def test_cli_capture_success_closes_the_issue(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    path = tmp_path / "s.json"
    path.write_text('{"capture_id": "c", "sources": {}}', encoding="utf-8")
    argv = ["alerts", str(path), "--publish-outcome", "success", "--capture-outcome", "success"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "close: Capture failing" in out and "close: Publish failing" in out
