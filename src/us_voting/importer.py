"""Import a local archive of raw captures as deterministic bundles."""

import gzip
import hashlib
import json
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from us_voting.bundle import member_name, write_bundles
from us_voting.capture import route
from us_voting.config import Config, Source
from us_voting.kinds import KINDS
from us_voting.kinds.base import CapturedFile

LIMITS = (
    "response headers other than content-type and last-modified were not recorded",
    "polls that returned unchanged content were not recorded",
)
CHUNK = 1 << 20


@dataclass(frozen=True)
class ImportResult:
    bundles: list[Path]
    files: int
    unmatched: list[str]
    ambiguous: list[str]
    bad_hash: list[str]
    skipped_after: int
    rerouted: list[str]


def _saved_entries(archive: Path) -> list[dict]:
    entries = []
    with (archive / "_log.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("result") == "saved":
                entries.append(entry)
    return entries


def _gunzip(src: Path, dest: Path) -> tuple[str, int]:
    """Stream-decompress `src` to `dest`, returning the sha256 and size of the body."""
    digest = hashlib.sha256()
    size = 0
    with gzip.open(src, "rb") as fin, dest.open("wb") as fout:
        while chunk := fin.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            fout.write(chunk)
    return digest.hexdigest(), size


def _name(rel: str) -> str:
    base = Path(rel).name.removesuffix(".gz")
    return base.split("__", 1)[1] if "__" in base else base


def _nonempty(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def import_archive(archive: Path, cfg: Config, out: Path, *, through: date) -> ImportResult:
    for sub in ("public", "private"):
        if _nonempty(out / sub):
            raise FileExistsError(f"{out / sub} already exists and is not empty")
    work = out / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    unmatched: list[str] = []
    ambiguous: list[str] = []
    bad_hash: list[str] = []
    skipped_after = 0
    rerouted: list[str] = []
    groups: dict[tuple[str, date], list[tuple[CapturedFile, Source]]] = defaultdict(list)
    count = 0
    try:
        for n, entry in enumerate(_saved_entries(archive)):
            received = datetime.fromisoformat(entry["ts"]).astimezone(UTC)
            if received.date() > through:
                skipped_after += 1
                continue
            url, rel = entry["url"], entry["file"]
            owners = [s for s in cfg.sources.values() if KINDS[s.kind].owns(s, url)]
            if not owners:
                unmatched.append(url)
            elif len(owners) > 1:
                ambiguous.append(url)
            source_file = archive / rel
            if not source_file.is_file():
                bad_hash.append(f"{rel} (missing)")
                continue
            dest = work / f"{n}-{_name(rel)}"
            try:
                sha, size = _gunzip(source_file, dest)
            except (OSError, EOFError):
                bad_hash.append(f"{rel} (unreadable)")
                dest.unlink(missing_ok=True)
                continue
            if sha != entry["sha256"]:
                bad_hash.append(rel)
                dest.unlink(missing_ok=True)
                continue
            if len(owners) != 1:
                dest.unlink(missing_ok=True)
                continue
            headers = {
                k: v
                for k, v in (
                    ("content-type", entry.get("content_type")),
                    ("last-modified", entry.get("last_modified")),
                )
                if v
            }
            f = CapturedFile(
                source_id=owners[0].id,
                url=url,
                name=_name(rel),
                path=dest,
                sha256=sha,
                bytes=size,
                received_at=received,
                status=200,
                headers=headers,
            )
            privacy, reason = route(owners[0], f)
            if reason is not None:
                rerouted.append(member_name(f))
            groups[(privacy, received.date())].append((f, owners[0]))
            count += 1
        bundles: list[Path] = []
        if not (unmatched or ambiguous or bad_hash):
            for (privacy, day), items in sorted(groups.items()):
                bundles += write_bundles(
                    day.isoformat(),
                    "import",
                    privacy,
                    items,
                    out / privacy,
                    max_bytes=cfg.max_bundle_bytes,
                    limits=LIMITS,
                )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return ImportResult(
        bundles=bundles,
        files=count,
        unmatched=unmatched,
        ambiguous=ambiguous,
        bad_hash=bad_hash,
        skipped_after=skipped_after,
        rerouted=rerouted,
    )
