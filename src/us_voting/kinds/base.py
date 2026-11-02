"""Shared fetch helper, result types and source-result rules for capture kinds."""

import itertools
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from us_voting.config import Source
from us_voting.http import Client
from us_voting.status import TIME_FORMAT, SourceStatus, UrlState

GOOD = frozenset({"saved", "unchanged", "not_modified"})
ERROR_LIMIT = 300


@dataclass(frozen=True)
class CapturedFile:
    source_id: str
    url: str
    name: str
    path: Path
    sha256: str
    bytes: int
    received_at: datetime
    status: int
    headers: dict[str, str]
    version: str | None = None


@dataclass(frozen=True)
class UrlOutcome:
    url: str
    result: str
    status: int | None = None
    error: str | None = None
    file: CapturedFile | None = None


@dataclass
class SourceRun:
    source: Source
    client: Client
    status: SourceStatus
    work_dir: Path
    now: datetime
    counter: Iterator[int] = field(default_factory=itertools.count, repr=False)


@dataclass(frozen=True)
class KindResult:
    outcomes: list[UrlOutcome]
    version: str | None = None
    note: str | None = None

    @property
    def files(self) -> list[CapturedFile]:
        return [o.file for o in self.outcomes if o.file is not None]


class Kind(Protocol):
    def fetch(self, run: SourceRun) -> KindResult: ...

    def owns(self, source: Source, url: str) -> bool: ...


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._=-]", "_", text)


def url_name(url: str) -> str:
    """Last path segment, plus `_` and the query when there is one."""
    parts = urlsplit(url)
    name = parts.path.rsplit("/", 1)[-1]
    if parts.query:
        name = f"{name}_{parts.query}"
    return safe_name(name) or "index"


def _free_path(work_dir: Path, name: str) -> Path:
    target = work_dir / name
    if not target.exists():
        return target
    base = Path(name)
    for i in itertools.count(2):
        target = work_dir / f"{base.stem}-{i}{base.suffix}"
        if not target.exists():
            return target
    raise AssertionError("unreachable")


def fetch_file(
    run: SourceRun,
    url: str,
    name: str,
    *,
    conditional: bool = True,
    version: str | None = None,
) -> UrlOutcome:
    name = safe_name(name)
    state = run.status.urls.setdefault(url, UrlState())
    dest = run.work_dir / f".part-{next(run.counter)}-{name}"
    fetched = run.client.fetch(
        url,
        dest,
        etag=state.etag if conditional else None,
        last_modified=state.last_modified if conditional else None,
        data_expected=True,
    )
    status = fetched.status
    if fetched.error is not None:
        if fetched.error in ("blocked", "host_abandoned"):
            result = "blocked"
        elif fetched.error == "soft_404":
            result = "soft_404"
        else:
            result = "error"
        return UrlOutcome(url, result, status, fetched.error)
    checked = fetched.received_at.strftime(TIME_FORMAT)
    if status == 304:
        state.checked = checked
        return UrlOutcome(url, "not_modified", status)
    if status != 200 or fetched.path is None or fetched.sha256 is None:
        if fetched.path is not None:
            fetched.path.unlink(missing_ok=True)
        result = "missing" if status in (401, 403, 404, 410) else "http_error"
        return UrlOutcome(url, result, status)
    unchanged = fetched.sha256 == state.sha256
    state.checked = checked
    state.etag = fetched.headers.get("etag")
    state.last_modified = fetched.headers.get("last-modified")
    state.sha256 = fetched.sha256
    if unchanged:
        fetched.path.unlink(missing_ok=True)
        return UrlOutcome(url, "unchanged", status)
    final = _free_path(run.work_dir, name)
    fetched.path.rename(final)
    captured = CapturedFile(
        source_id=run.source.id,
        url=url,
        name=name,
        path=final,
        sha256=fetched.sha256,
        bytes=fetched.bytes,
        received_at=fetched.received_at,
        status=200,
        headers=fetched.headers,
        version=version,
    )
    return UrlOutcome(url, "saved", status, file=captured)


def source_result(source: Source, kr: KindResult, now: datetime) -> tuple[str, str | None]:
    for o in kr.outcomes:
        if o.result == "blocked":
            return "blocked", o.url
    ignored = {"missing"} if source.kind == "dated" else set()
    if source.kind == "page_links" and any(o.result in GOOD for o in kr.outcomes):
        ignored = {"missing", "soft_404"}
    failures = [o for o in kr.outcomes if o.result not in GOOD and o.result not in ignored]
    if failures:
        if source.expect_missing_until is not None and now < source.expect_missing_until:
            return "waiting", None
        first = failures[0]
        error = (
            f"{len(failures)} of {len(kr.outcomes)} URLs failed: "
            f"{first.url} {first.status or first.error}"
        )
        return "failed", error[:ERROR_LIMIT]
    if kr.note in ("not_listed_yet", "no_links"):
        return "waiting", None
    if source.kind == "dated" and all(o.result == "missing" for o in kr.outcomes):
        return "waiting", None
    return "ok", None


def text_failure(url: str, status: int | None, error: str | None) -> UrlOutcome:
    """Outcome for a discovery or version request that did not return a usable 200."""
    if error in ("blocked", "host_abandoned"):
        return UrlOutcome(url, "blocked", status, error)
    if error is not None:
        return UrlOutcome(url, "error", status, error)
    return UrlOutcome(url, "missing" if status in (401, 403, 404, 410) else "http_error", status)


def unparseable(url: str) -> UrlOutcome:
    return UrlOutcome(url, "error", 200, "unparseable")
