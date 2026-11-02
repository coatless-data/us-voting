"""Deterministic raw bundles: a `.tar.gz` of captured files plus a JSON sidecar."""

import gzip
import io
import json
import tarfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from us_voting.config import Source
from us_voting.kinds.base import CapturedFile

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
INFO_NAME = "_bundle.json"


class BundleTooLarge(Exception):
    pass


@dataclass(frozen=True)
class FileEntry:
    source_id: str
    state: str
    election: str
    url: str
    member: str
    sha256: str
    bytes: int
    received_at: str
    status: int
    headers: dict[str, str]
    version: str | None


@dataclass(frozen=True)
class BundleInfo:
    name: str
    capture_id: str
    origin: str
    privacy: str
    files: list[FileEntry]
    limits: list[str]


def member_name(f: CapturedFile) -> str:
    return f"{f.source_id}/{f.received_at:%Y%m%dT%H%M%SZ}__{f.name}"


def info_to_dict(info: BundleInfo) -> dict:
    return asdict(info)


def read_info(sidecar: Path) -> BundleInfo:
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["files"] = [FileEntry(**f) for f in data["files"]]
    return BundleInfo(**data)


def release_tag(info: BundleInfo) -> str:
    return f"raw-{info.capture_id[:7]}"


def _render(info: BundleInfo) -> bytes:
    return (json.dumps(info_to_dict(info), indent=1, sort_keys=True) + "\n").encode("utf-8")


def _unique(items: list[tuple[CapturedFile, Source]]) -> list[tuple[CapturedFile, Source, str]]:
    seen: set[str] = set()
    out: list[tuple[CapturedFile, Source, str]] = []
    for f, source in items:
        candidate = f
        base = Path(f.name)
        n = 1
        while member_name(candidate) in seen:
            n += 1
            candidate = replace(f, name=f"{base.stem}-{n}{base.suffix}")
        member = member_name(candidate)
        seen.add(member)
        out.append((f, source, member))
    return out


def _split(
    items: list[tuple[CapturedFile, Source, str]], max_bytes: int
) -> list[list[tuple[CapturedFile, Source, str]]]:
    groups: list[list[tuple[CapturedFile, Source, str]]] = []
    total = 0
    for item in items:
        size = item[0].bytes
        if size > max_bytes:
            raise BundleTooLarge(f"{item[0].url} is {size} bytes, over the {max_bytes} limit")
        if not groups or total + size > max_bytes:
            groups.append([])
            total = 0
        groups[-1].append(item)
        total += size
    return groups


def _entry(f: CapturedFile, source: Source, member: str) -> FileEntry:
    return FileEntry(
        source_id=f.source_id,
        state=source.state,
        election=source.election.id,
        url=f.url,
        member=member,
        sha256=f.sha256,
        bytes=f.bytes,
        received_at=f.received_at.strftime(TIME_FORMAT),
        status=f.status,
        headers=dict(f.headers),
        version=f.version,
    )


def _tarinfo(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size = size
    ti.mtime = mtime
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mode = 0o644
    return ti


def _write_one(
    stem: str,
    group: list[tuple[CapturedFile, Source, str]],
    ident: str,
    origin: str,
    privacy: str,
    limits: Sequence[str],
    out_dir: Path,
) -> Path:
    group = sorted(group, key=lambda g: g[2])
    info = BundleInfo(
        name=f"{stem}.tar.gz",
        capture_id=ident,
        origin=origin,
        privacy=privacy,
        files=[_entry(f, s, m) for f, s, m in group],
        limits=list(limits),
    )
    payload = _render(info)
    newest = max(int(f.received_at.timestamp()) for f, _, _ in group)
    tar_path = out_dir / info.name
    with (
        tar_path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        tar.addfile(_tarinfo(INFO_NAME, len(payload), newest), io.BytesIO(payload))
        for f, _, member in group:
            with f.path.open("rb") as fh:
                tar.addfile(_tarinfo(member, f.bytes, int(f.received_at.timestamp())), fh)
    (out_dir / f"{stem}.json").write_bytes(payload)
    return tar_path


def write_bundles(
    ident: str,
    origin: str,
    privacy: str,
    items: list[tuple[CapturedFile, Source]],
    out_dir: Path,
    *,
    max_bytes: int,
    limits: Sequence[str] = (),
) -> list[Path]:
    if not items:
        return []
    groups = _split(_unique(items), max_bytes)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{origin}-{ident}"
    return [
        _write_one(
            base if i == 0 else f"{base}-{i + 1}", g, ident, origin, privacy, limits, out_dir
        )
        for i, g in enumerate(groups)
    ]
