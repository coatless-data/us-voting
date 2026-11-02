"""Open, refresh and close GitHub issues from a capture's status.

Each alert is identified by its issue title. Bodies carry source ids, results, error strings,
member names and column names only, never file contents.
"""

from datetime import datetime
from typing import Protocol

from us_voting.issues import run_url
from us_voting.status import TIME_FORMAT, SourceStatus, Status

FAILING_AFTER = 3
LABELS = ["alert"]
PUBLISH_TITLE = "Publish failing"
CAPTURE_TITLE = "Capture failing"


class IssueSink(Protocol):
    def ensure_open(self, title: str, body: str, labels: list[str] | None = None) -> None: ...

    def close(self, title: str, comment: str) -> None: ...


def _iso(value: datetime | None) -> str:
    return value.strftime(TIME_FORMAT) if value else "never"


def _run_line() -> str:
    url = run_url()
    return f"\nWorkflow run: {url}\n" if url else ""


def _source_body(source_id: str, st: SourceStatus, headline: str) -> str:
    return (
        f"{headline}\n\n"
        f"- Source: {source_id}\n"
        f"- Result: {st.result}\n"
        f"- Error: {st.error or 'none recorded'}\n"
        f"- Last success: {_iso(st.last_ok)}\n"
        f"- Consecutive failures: {st.consecutive_failures}\n"
        f"{_run_line()}"
        "\nThis issue closes itself when the source recovers.\n"
    )


def _reroute_body(source_id: str, entries: list[dict[str, str]]) -> str:
    lines = [
        f"Files from the public source `{source_id}` look voter-level. They were kept in the "
        "private store and left out of the public release.",
        "",
        "Files and reasons:",
    ]
    lines += [f"- `{e.get('member', '')}`: {e.get('reason', '')}" for e in entries]
    lines.append(_run_line())
    lines.append("Review the source and move it to the private class if this is expected.")
    return "\n".join(lines) + "\n"


def capture_alert(issues: IssueSink, capture_outcome: str) -> None:
    """Open an issue when the capture step failed or timed out; close it after a success."""
    if capture_outcome == "failure":
        body = (
            "The capture step failed or timed out. Nothing from this run was published and "
            "the status file was not committed, so the sources due on this run are due "
            "again on the next one.\n\n"
            f"Workflow run: {run_url() or 'unknown'}\n"
            "\nThis issue closes itself when a capture succeeds.\n"
        )
        issues.ensure_open(CAPTURE_TITLE, body, LABELS)
    elif capture_outcome == "success":
        issues.close(CAPTURE_TITLE, f"Recovered: the capture step succeeded.\n{_run_line()}")


def run_alerts(status: Status, issues: IssueSink, *, publish_outcome: str) -> None:
    when = _iso(status.generated_at)
    for source_id, st in status.sources.items():
        failing = f"Source failing: {source_id}"
        blocked = f"Source blocked: {source_id}"
        if st.result == "failed" and st.consecutive_failures >= FAILING_AFTER:
            headline = f"Source `{source_id}` failed {st.consecutive_failures} runs in a row."
            issues.ensure_open(failing, _source_body(source_id, st, headline), LABELS)
        elif st.result == "blocked":
            headline = f"Source `{source_id}` is blocking automated requests."
            issues.ensure_open(blocked, _source_body(source_id, st, headline), LABELS)
        elif st.result in ("ok", "waiting"):
            comment = f"Recovered: result is {st.result} as of {when}."
            issues.close(failing, comment)
            issues.close(blocked, comment)

    by_source: dict[str, list[dict[str, str]]] = {}
    for entry in status.rerouted:
        by_source.setdefault(entry["source"], []).append(entry)
    for source_id, entries in by_source.items():
        title = f"Voter-level content in public source: {source_id}"
        issues.ensure_open(title, _reroute_body(source_id, entries), LABELS)

    if publish_outcome == "failure":
        body = (
            f"Publishing the capture {status.capture_id or ''} failed. The fetch state was not "
            "advanced, so those files are fetched again the next time their sources are due."
            f"\n{_run_line()}"
        )
        issues.ensure_open(PUBLISH_TITLE, body, LABELS)
    elif publish_outcome == "success":
        issues.close(PUBLISH_TITLE, f"Recovered: publish succeeded as of {when}.")
