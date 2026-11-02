"""Per-source status and fetch state, saved as one JSON file per capture."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FAILURE_RESULTS = ("failed", "blocked")


@dataclass
class UrlState:
    etag: str | None = None
    last_modified: str | None = None
    sha256: str | None = None
    checked: str | None = None


@dataclass
class SourceStatus:
    last_run: datetime | None = None
    last_ok: datetime | None = None
    result: str | None = None
    consecutive_failures: int = 0
    error: str | None = None
    version: str | None = None
    urls: dict[str, UrlState] = field(default_factory=dict)


@dataclass
class Status:
    capture_id: str | None = None
    generated_at: datetime | None = None
    sources: dict[str, SourceStatus] = field(default_factory=dict)
    rerouted: list[dict[str, str]] = field(default_factory=list)

    def source(self, source_id: str) -> SourceStatus:
        if source_id not in self.sources:
            self.sources[source_id] = SourceStatus()
        return self.sources[source_id]


def record_run(st: SourceStatus, result: str, error: str | None, now: datetime) -> None:
    """Record one run's outcome. Only failed or blocked runs count toward alerts."""
    st.last_run = now
    st.result = result
    st.error = error
    if result in FAILURE_RESULTS:
        st.consecutive_failures += 1
    else:
        st.consecutive_failures = 0
    if result == "ok":
        st.last_ok = now


def load_status(path: Path) -> Status:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Status()
    return Status(
        capture_id=data.get("capture_id"),
        generated_at=_parse(data.get("generated_at")),
        rerouted=data.get("rerouted", []),
        sources={sid: _source_from_json(raw) for sid, raw in data.get("sources", {}).items()},
    )


def save_status(status: Status, path: Path) -> None:
    """Write the status atomically: a temp file in the same directory, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_status_to_json(status), indent=1, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _format(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).strftime(TIME_FORMAT)


def _parse(text: str | None) -> datetime | None:
    if text is None:
        return None
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)


def _status_to_json(status: Status) -> dict:
    return {
        "capture_id": status.capture_id,
        "generated_at": _format(status.generated_at),
        "rerouted": status.rerouted,
        "sources": {sid: _source_to_json(st) for sid, st in status.sources.items()},
    }


def _source_to_json(st: SourceStatus) -> dict:
    return {
        "last_run": _format(st.last_run),
        "last_ok": _format(st.last_ok),
        "result": st.result,
        "consecutive_failures": st.consecutive_failures,
        "error": st.error,
        "version": st.version,
        "urls": {url: asdict(state) for url, state in st.urls.items()},
    }


def _source_from_json(data: dict) -> SourceStatus:
    return SourceStatus(
        last_run=_parse(data.get("last_run")),
        last_ok=_parse(data.get("last_ok")),
        result=data.get("result"),
        consecutive_failures=data.get("consecutive_failures", 0),
        error=data.get("error"),
        version=data.get("version"),
        urls={url: UrlState(**fields) for url, fields in data.get("urls", {}).items()},
    )
