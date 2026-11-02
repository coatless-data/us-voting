"""The shared HTTP client: identity, pacing, retries, streaming and block detection."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from us_voting.config import HttpConfig

MAX_FILE_BYTES = 1_800_000_000
SMALL_LIMIT = 20_000_000
HEAD_BYTES = 65_536
CHUNK = 1 << 20


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int | None
    headers: dict[str, str]
    received_at: datetime
    elapsed_ms: int
    path: Path | None
    sha256: str | None
    bytes: int
    error: str | None


def is_block(status: int | None, headers: Mapping[str, str], head: bytes) -> bool:
    if status == 429:
        return True
    if any(k.lower() == "cf-mitigated" for k in headers):
        return True
    text = head.lower()
    if any(m in text for m in (b"just a moment...", b"cf-chl", b"_incapsula_resource")):
        return True
    if b"incapsula incident" in text:
        return True
    if b"access denied" in text and b"reference #" in text:
        return True
    return b"radware" in text and b"captcha" in text


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class _Outcome:
    """What one HTTP attempt produced."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.head = b""
        self.body = b""
        self.size = 0
        self.sha256: str | None = None
        self.error: str | None = None


class Client:
    def __init__(
        self,
        cfg: HttpConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._sleep = sleep
        self._monotonic = monotonic
        self._http = httpx.Client(transport=transport, follow_redirects=True)
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}
        self._signals: dict[str, int] = {}

    def close(self) -> None:
        self._http.close()

    def abandoned(self, url: str) -> bool:
        with self._lock:
            return self._signals.get(_host(url), 0) >= self.cfg.signals_before_abandon

    def _pace(self, host: str) -> None:
        with self._lock:
            now = self._monotonic()
            allowed = self._next_allowed.get(host, now)
            wait = max(0.0, allowed - now)
            self._next_allowed[host] = max(now, allowed) + self.cfg.min_interval_seconds
        if wait > 0:
            self._sleep(wait)

    def _signal(self, host: str) -> None:
        with self._lock:
            self._signals[host] = self._signals.get(host, 0) + 1

    def _headers(self, host: str, extra: dict[str, str]) -> dict[str, str]:
        return {
            "User-Agent": self.cfg.user_agent_for(host),
            "X-Project": self.cfg.x_project,
            **extra,
        }

    def _attempt(
        self, url: str, headers: dict[str, str], dest: Path | None, limit: int
    ) -> _Outcome:
        out = _Outcome()
        timeout = httpx.Timeout(
            connect=self.cfg.connect,
            read=self.cfg.read,
            write=self.cfg.read,
            pool=self.cfg.connect,
        )
        deadline = self._monotonic() + self.cfg.total
        try:
            with self._http.stream("GET", url, headers=headers, timeout=timeout) as resp:
                out.status = resp.status_code
                out.headers = {
                    k.lower(): v for k, v in resp.headers.items() if k.lower() != "set-cookie"
                }
                keep = resp.status_code == 200
                digest = hashlib.sha256()
                head = bytearray()
                chunks: list[bytes] = []
                fh = open(dest, "wb") if (keep and dest is not None) else None  # noqa: SIM115
                try:
                    for chunk in resp.iter_bytes(CHUNK):
                        if len(head) < HEAD_BYTES:
                            head += chunk[: HEAD_BYTES - len(head)]
                        if keep:
                            out.size += len(chunk)
                            digest.update(chunk)
                            if out.size > limit:
                                out.error = "too_large"
                                break
                            if fh is not None:
                                fh.write(chunk)
                            else:
                                chunks.append(chunk)
                        elif len(head) >= HEAD_BYTES:
                            break
                        if self._monotonic() > deadline:
                            out.error = "timeout"
                            break
                finally:
                    if fh is not None:
                        fh.close()
                out.head = bytes(head)
                out.body = b"".join(chunks)
                if keep:
                    out.sha256 = digest.hexdigest()
        except httpx.TimeoutException:
            out.error = "timeout"
            out.status = None
        except httpx.HTTPError:
            out.error = "transport"
            out.status = None
        return out

    def _run(self, url: str, extra: dict[str, str], dest: Path | None, limit: int) -> _Outcome:
        host = _host(url)
        headers = self._headers(host, extra)
        attempt = 0
        while True:
            attempt += 1
            self._pace(host)
            out = self._attempt(url, headers, dest, limit)
            status = out.status
            retryable = (
                out.error == "transport"
                or (out.error == "timeout" and status is None)
                or status == 429
                or (status is not None and status >= 500)
            )
            if not retryable or attempt >= self.cfg.max_attempts:
                break
            backoff = self.cfg.backoff_seconds
            if backoff:
                self._sleep(backoff[min(attempt - 1, len(backoff) - 1)])
        if out.error is None and is_block(out.status, out.headers, out.head):
            out.error = "blocked"
            self._signal(host)
        return out

    def fetch(
        self,
        url: str,
        dest: Path,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        data_expected: bool = True,
    ) -> Fetched:
        started = self._monotonic()
        received_at = datetime.now(UTC).replace(microsecond=0)

        def done(out: _Outcome | None, error: str | None, *, keep: bool = False) -> Fetched:
            elapsed = int((self._monotonic() - started) * 1000)
            ok = keep and out is not None
            if not ok:
                dest.unlink(missing_ok=True)
            return Fetched(
                url=url,
                status=out.status if out else None,
                headers=out.headers if out else {},
                received_at=received_at,
                elapsed_ms=elapsed,
                path=dest if ok else None,
                sha256=out.sha256 if ok and out else None,
                bytes=out.size if ok and out else 0,
                error=error,
            )

        if self.abandoned(url):
            return done(None, "host_abandoned")
        extra: dict[str, str] = {}
        if etag:
            extra["If-None-Match"] = etag
        if last_modified:
            extra["If-Modified-Since"] = last_modified
        dest.parent.mkdir(parents=True, exist_ok=True)
        out = self._run(url, extra, dest, MAX_FILE_BYTES)
        if out.error is not None:
            return done(out, out.error)
        if out.status != 200:
            return done(out, None)
        if out.size == 0:
            return done(out, "empty")
        ctype = out.headers.get("content-type", "").lower()
        if (
            data_expected
            and ctype.startswith("text/html")
            and out.head.lstrip(b"\xef\xbb\xbf").strip().startswith(b"<")
        ):
            return done(out, "soft_404")
        return done(out, None, keep=True)

    def get_text(self, url: str) -> tuple[int | None, str]:
        if self.abandoned(url):
            return None, ""
        out = self._run(url, {}, None, SMALL_LIMIT)
        if out.status is None or out.error in ("transport", "timeout"):
            return None, ""
        return out.status, out.body.decode("utf-8", errors="replace")
