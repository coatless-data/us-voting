import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from us_voting.config import HttpConfig, Source, load_config
from us_voting.http import Client

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
    handler: Callable[[httpx.Request], httpx.Response], *, http: HttpConfig | None = None
) -> tuple[Client, list[float]]:
    sleeps: list[float] = []
    cfg = http if http is not None else load_config(CONFIG_DIR).http
    client = Client(
        cfg,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        monotonic=lambda: 1000.0,
    )
    return client, sleeps
