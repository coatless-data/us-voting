"""Typed loader and validator for the files in config/."""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any


class ConfigError(Exception):
    """A config file is missing, malformed or breaks a validation rule."""


@dataclass(frozen=True)
class HttpConfig:
    project_user_agent: str
    browser_user_agent: str
    x_project: str
    browser_hosts: tuple[str, ...]
    connect: float
    read: float
    total: float
    max_attempts: int
    backoff_seconds: tuple[float, ...]
    min_interval_seconds: float
    signals_before_abandon: int

    def user_agent_for(self, host: str) -> str:
        host = host.lower()
        for suffix in self.browser_hosts:
            if host == suffix or host.endswith("." + suffix):
                return self.browser_user_agent
        return self.project_user_agent


@dataclass(frozen=True)
class Window:
    start_hour: int
    end_hour: int
    every_hours: int


@dataclass(frozen=True)
class Schedule:
    name: str
    every_hours: int
    start_hour: int | None
    end_hour: int | None
    daily_at_utc: int | None
    windows: tuple[Window, ...]


@dataclass(frozen=True)
class Election:
    id: str
    date: date
    type: str


@dataclass(frozen=True)
class StoreConfig:
    spec: str
    token_env: str
    private: bool


@dataclass(frozen=True)
class Source:
    id: str
    state: str
    election: Election
    kind: str
    raw: str
    schedule: Schedule
    provides: tuple[str, ...]
    params: Mapping[str, Any]
    expect_missing_until: datetime | None
    daily_at_utc: int | None
    guard_allow: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    http: HttpConfig
    schedules: dict[str, Schedule]
    elections: dict[str, Election]
    stores: dict[str, StoreConfig]
    sources: dict[str, Source]
    workers: int
    max_seconds: float
    max_bundle_bytes: int


_STATE_CODES = """
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM
NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC
"""
STATES = frozenset(_STATE_CODES.split())
PROVIDES = frozenset(
    ["results", "results_by_method", "turnout_daily", "registration", "demographics", "sites"]
)
COMMON_KEYS = frozenset(
    {"id", "state", "kind", "raw", "schedule", "provides"}
    | {"expect_missing_until", "daily_at_utc", "guard_allow"}
)
KIND_KEYS: dict[str, frozenset[str]] = {
    "url": frozenset(["url", "urls", "name"]),
    "dated": frozenset(["template", "templates", "start", "end", "n_range"]),
    "page_links": frozenset(["page", "pattern", "max_links"]),
    "clarity": frozenset(["base", "files", "eid", "discover_date"]),
    "enhanced": frozenset(["host", "jurisdiction", "election", "discover_date"]),
}
ID_RE = re.compile(r"^[a-z0-9-]+$")


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"{path}: file not found") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None


def _section(data: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: missing table [{key}]")
    return value


def _get(table: dict[str, Any], key: str, types: type | tuple[type, ...], where: str) -> Any:
    value = table.get(key)
    if key not in table or isinstance(value, bool) or not isinstance(value, types):
        raise ConfigError(f"{where}: {key} is missing or has the wrong type")
    return value


def _load_http(root: Path) -> HttpConfig:
    path = root / "http.toml"
    data = _read(path)
    ident = _section(data, "identity", path)
    timeouts = _section(data, "timeouts", path)
    retry = _section(data, "retry", path)
    pacing = _section(data, "pacing", path)
    blocks = _section(data, "blocks", path)
    w = str(path)
    num = (int, float)
    return HttpConfig(
        project_user_agent=_get(ident, "project_user_agent", str, w),
        browser_user_agent=_get(ident, "browser_user_agent", str, w),
        x_project=_get(ident, "x_project", str, w),
        browser_hosts=tuple(_get(ident, "browser_hosts", list, w)),
        connect=float(_get(timeouts, "connect", num, w)),
        read=float(_get(timeouts, "read", num, w)),
        total=float(_get(timeouts, "total", num, w)),
        max_attempts=_get(retry, "max_attempts", int, w),
        backoff_seconds=tuple(float(x) for x in _get(retry, "backoff_seconds", list, w)),
        min_interval_seconds=float(_get(pacing, "min_interval_seconds", num, w)),
        signals_before_abandon=_get(blocks, "signals_before_abandon", int, w),
    )


def _load_schedules(root: Path) -> dict[str, Schedule]:
    path = root / "schedules.toml"
    out: dict[str, Schedule] = {}
    for name, t in _read(path).items():
        where = f"{path} [{name}]"
        if not isinstance(t, dict):
            raise ConfigError(f"{where}: must be a table")
        windows = tuple(
            Window(
                _get(win, "start_hour", int, where),
                _get(win, "end_hour", int, where),
                _get(win, "every_hours", int, where),
            )
            for win in t.get("windows", [])
        )
        out[name] = Schedule(
            name=name,
            every_hours=_get(t, "every_hours", int, where),
            start_hour=t.get("start_hour"),
            end_hour=t.get("end_hour"),
            daily_at_utc=t.get("daily_at_utc"),
            windows=windows,
        )
    return out


def _load_elections(root: Path) -> dict[str, Election]:
    path = root / "elections.toml"
    out: dict[str, Election] = {}
    for item in _read(path).get("election", []):
        where = f"{path} election"
        election = Election(
            _get(item, "id", str, where),
            _get(item, "date", date, where),
            _get(item, "type", str, where),
        )
        if election.id in out:
            raise ConfigError(f"{where}: duplicate election id {election.id}")
        out[election.id] = election
    return out


def _load_stores(root: Path) -> dict[str, StoreConfig]:
    path = root / "stores.toml"
    out: dict[str, StoreConfig] = {}
    for name, t in _read(path).items():
        where = f"{path} [{name}]"
        out[name] = StoreConfig(
            _get(t, "store", str, where),
            _get(t, "token_env", str, where),
            bool(t.get("private", False)),
        )
    return out


def _check_params(params: dict[str, Any], kind: str, where: str) -> None:
    def one_of(*keys: str) -> None:
        present = [k for k in keys if k in params]
        if len(present) != 1:
            raise ConfigError(f"{where}: exactly one of {' or '.join(keys)} is required")

    def string(key: str) -> None:
        if not isinstance(params.get(key), str) or not params[key]:
            raise ConfigError(f"{where}: {key} is required and must be a string")

    def str_list(key: str) -> None:
        value = params.get(key)
        if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
            raise ConfigError(f"{where}: {key} must be a non-empty list of strings")

    if kind == "url":
        one_of("url", "urls")
        if "url" in params:
            string("url")
        else:
            str_list("urls")
            if "name" in params:
                raise ConfigError(f"{where}: name is allowed only with url")
    elif kind == "dated":
        one_of("template", "templates")
        if "template" in params:
            string("template")
        else:
            str_list("templates")
        if "n_range" in params:
            if "start" in params or "end" in params:
                raise ConfigError(f"{where}: n_range cannot be combined with start and end")
            rng = params["n_range"]
            if (
                not isinstance(rng, list)
                or len(rng) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in rng)
                or rng[0] > rng[1]
            ):
                raise ConfigError(f"{where}: n_range must be [lo, hi] integers with lo <= hi")
        else:
            if "start" not in params or "end" not in params:
                raise ConfigError(f"{where}: start and end, or n_range, are required")
            try:
                start = date.fromisoformat(params["start"])
                end = date.fromisoformat(params["end"])
            except (TypeError, ValueError):
                raise ConfigError(f"{where}: start and end must be ISO dates") from None
            if start > end:
                raise ConfigError(f"{where}: start must not be after end")
    elif kind == "page_links":
        string("page")
        string("pattern")
        try:
            re.compile(params["pattern"], re.I)
        except re.error as exc:
            raise ConfigError(f"{where}: pattern does not compile: {exc}") from None
        if "max_links" in params and (
            not isinstance(params["max_links"], int) or isinstance(params["max_links"], bool)
        ):
            raise ConfigError(f"{where}: max_links must be an integer")
    elif kind == "clarity":
        string("base")
        str_list("files")
        if ("eid" in params) == ("discover_date" in params):
            raise ConfigError(f"{where}: exactly one of eid or discover_date is required")
    elif kind == "enhanced":
        string("host")
        string("jurisdiction")
        one_of("election", "discover_date")


def _load_source(
    item: dict[str, Any],
    path: Path,
    election: Election,
    schedules: dict[str, Schedule],
    seen: set[str],
) -> Source:
    sid = item.get("id")
    where = f"{path} source {sid!r}"
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise ConfigError(f"{where}: id must match [a-z0-9-]+")
    if sid in seen:
        raise ConfigError(f"{where}: duplicate source id")
    for key in ("state", "kind", "raw", "schedule", "provides"):
        if key not in item:
            raise ConfigError(f"{where}: {key} is required")
    if item["state"] not in STATES:
        raise ConfigError(f"{where}: state {item['state']!r} is not a US state code")
    kind = item["kind"]
    if kind not in KIND_KEYS:
        raise ConfigError(f"{where}: kind {kind!r} is not one of {sorted(KIND_KEYS)}")
    if item["raw"] not in ("public", "private"):
        raise ConfigError(f"{where}: raw must be 'public' or 'private'")
    if item["schedule"] not in schedules:
        raise ConfigError(f"{where}: schedule {item['schedule']!r} is not defined")
    provides = item["provides"]
    if not isinstance(provides, list) or not provides or any(p not in PROVIDES for p in provides):
        raise ConfigError(f"{where}: provides must be a non-empty list from {sorted(PROVIDES)}")
    unknown = set(item) - COMMON_KEYS - KIND_KEYS[kind]
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) for kind {kind}: {sorted(unknown)}")
    daily = item.get("daily_at_utc")
    if daily is not None and (
        not isinstance(daily, int) or isinstance(daily, bool) or not 0 <= daily <= 23
    ):
        raise ConfigError(f"{where}: daily_at_utc must be an integer from 0 to 23")
    missing = item.get("expect_missing_until")
    if missing is not None and (not isinstance(missing, datetime) or missing.tzinfo is None):
        raise ConfigError(f"{where}: expect_missing_until must be a datetime with an offset")
    allow = item.get("guard_allow", [])
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        raise ConfigError(f"{where}: guard_allow must be a list of strings")
    params = {k: v for k, v in item.items() if k in KIND_KEYS[kind]}
    _check_params(params, kind, where)
    return Source(
        id=sid,
        state=item["state"],
        election=election,
        kind=kind,
        raw=item["raw"],
        schedule=schedules[item["schedule"]],
        provides=tuple(provides),
        params=MappingProxyType(params),
        expect_missing_until=missing,
        daily_at_utc=daily,
        guard_allow=tuple(allow),
    )


def _load_sources(
    root: Path, elections: dict[str, Election], schedules: dict[str, Schedule]
) -> dict[str, Source]:
    out: dict[str, Source] = {}
    folder = root / "sources"
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.toml")):
        data = _read(path)
        extra = set(data) - {"election", "source"}
        if extra:
            raise ConfigError(f"{path}: unknown top-level key(s): {sorted(extra)}")
        eid = data.get("election")
        if eid not in elections:
            raise ConfigError(f"{path}: election {eid!r} is not in elections.toml")
        for item in data.get("source", []):
            source = _load_source(item, path, elections[eid], schedules, set(out))
            out[source.id] = source
    return out


def load_config(root: Path = Path("config")) -> Config:
    root = Path(root)
    capture = _read(root / "capture.toml")
    where = str(root / "capture.toml")
    elections = _load_elections(root)
    schedules = _load_schedules(root)
    return Config(
        http=_load_http(root),
        schedules=schedules,
        elections=elections,
        stores=_load_stores(root),
        sources=_load_sources(root, elections, schedules),
        workers=_get(capture, "workers", int, where),
        max_seconds=float(_get(capture, "max_seconds", (int, float), where)),
        max_bundle_bytes=_get(capture, "max_bundle_bytes", int, where),
    )
