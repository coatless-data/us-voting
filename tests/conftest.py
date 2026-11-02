import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from us_voting.config import HttpConfig, Source, load_config
from us_voting.http import Client
from us_voting.kinds.base import SourceRun
from us_voting.status import SourceStatus

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
NOW = datetime(2026, 11, 3, 23, 23, tzinfo=UTC)

TOP_LEVEL = ("http.toml", "capture.toml", "schedules.toml", "stores.toml", "elections.toml")


def write_config(tmp_path: Path, sources_toml: str) -> Path:
    root = tmp_path / "config"
    (root / "sources").mkdir(parents=True)
    for name in TOP_LEVEL:
        shutil.copy(CONFIG_DIR / name, root / name)
    (root / "sources" / "test.toml").write_text(
        'election = "2026-11-03-general"\n' + sources_toml, encoding="utf-8"
    )
    return root


def one_source(tmp_path: Path, body: str) -> Source:
    root = write_config(tmp_path, "[[source]]\n" + body)
    (source,) = load_config(root).sources.values()
    return source


def mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    http: HttpConfig | None = None,
    monotonic: Callable[[], float] | None = None,
) -> tuple[Client, list[float]]:
    sleeps: list[float] = []
    cfg = http if http is not None else load_config(CONFIG_DIR).http
    client = Client(
        cfg,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        monotonic=monotonic if monotonic is not None else lambda: 1000.0,
    )
    return client, sleeps


class Clock:
    """A monotonic clock that moves only when a test moves it."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def ticking(
    handler: Callable[[httpx.Request], httpx.Response], clock: Clock, step: float
) -> Callable[[httpx.Request], httpx.Response]:
    """Wrap a handler so that each request it answers takes `step` seconds."""

    def h(request: httpx.Request) -> httpx.Response:
        clock.now += step
        return handler(request)

    return h


def make_run(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    body: str | None = None,
    now: datetime = NOW,
    monotonic: Callable[[], float] | None = None,
) -> SourceRun:
    if body is None:
        body = (
            'id = "src"\nstate = "NC"\nkind = "url"\nraw = "public"\n'
            'schedule = "results"\nprovides = ["results"]\nurl = "https://a.gov/f.csv"\n'
        )
    source = one_source(tmp_path, body)
    client, _ = mock_client(handler, monotonic=monotonic)
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    return SourceRun(
        source=source, client=client, status=SourceStatus(), work_dir=work_dir, now=now
    )
