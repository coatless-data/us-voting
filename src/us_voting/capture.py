"""Run due sources and write public and private bundles plus a status file."""

import copy
import shutil
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from us_voting.bundle import member_name, write_bundles
from us_voting.config import Config, Source
from us_voting.guard import voter_level_reason
from us_voting.http import Client
from us_voting.kinds import KINDS
from us_voting.kinds.base import ERROR_LIMIT, CapturedFile, KindResult, SourceRun, source_result
from us_voting.schedule import is_due
from us_voting.status import FAILURE_RESULTS, SourceStatus, Status, record_run, save_status

ORIGIN = "capture"


@dataclass(frozen=True)
class CaptureResult:
    capture_id: str
    ran: list[str]
    results: dict[str, str]
    files: int
    bundles: list[Path]

    @property
    def all_failed(self) -> bool:
        return bool(self.ran) and all(self.results[i] in FAILURE_RESULTS for i in self.ran)


def capture_id_for(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H%MZ")


def due_sources(cfg: Config, status: Status, now: datetime, only: list[str] | None) -> list[Source]:
    if only is not None:
        unknown = [i for i in only if i not in cfg.sources]
        if unknown:
            raise ValueError(f"unknown source ids: {', '.join(unknown)}")
        wanted = set(only)
        return [s for s in cfg.sources.values() if s.id in wanted]
    due = []
    for source in cfg.sources.values():
        known = status.sources.get(source.id)
        if is_due(source, known.last_run if known else None, now):
            due.append(source)
    return due


def route(source: Source, f: CapturedFile) -> tuple[str, str | None]:
    """Return the privacy class for a file and, when the guard changed it, the reason."""
    if source.raw == "private":
        return "private", None
    reason = voter_level_reason(f.path, source.guard_allow)
    if reason is not None:
        return "private", reason
    return source.raw, None


def _fetch_one(
    source: Source, run: SourceRun, now: datetime
) -> tuple[KindResult | None, str, str | None]:
    saved_urls = copy.deepcopy(run.status.urls)
    try:
        kr = KINDS[source.kind].fetch(run)
    except Exception as e:
        # Fetch state must not advance for files that were never bundled.
        run.status.urls = saved_urls
        return None, "failed", f"{type(e).__name__}: {e}"[:ERROR_LIMIT]
    result, error = source_result(source, kr, now)
    return kr, result, error


def run_capture(
    cfg: Config,
    status: Status,
    out: Path,
    now: datetime,
    client: Client,
    *,
    only: list[str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> CaptureResult:
    now = now.replace(microsecond=0)
    capture_dir = out / "capture"
    if capture_dir.exists() and any(capture_dir.iterdir()):
        raise FileExistsError(f"{capture_dir} already exists and is not empty")
    due = due_sources(cfg, status, now, only)
    ident = capture_id_for(now)
    work = out / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    start = monotonic()
    statuses: dict[str, SourceStatus] = {s.id: status.source(s.id) for s in due}

    def worker(source: Source):
        if monotonic() - start >= cfg.max_seconds:
            return None
        work_dir = work / source.id
        work_dir.mkdir(parents=True, exist_ok=True)
        run = SourceRun(
            source=source, client=client, status=statuses[source.id], work_dir=work_dir, now=now
        )
        return _fetch_one(source, run, now)

    try:
        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            outcomes = list(pool.map(worker, due))

        ran: list[str] = []
        results: dict[str, str] = {}
        groups: dict[str, list[tuple[CapturedFile, Source]]] = {"public": [], "private": []}
        status.rerouted = []
        for source, outcome in zip(due, outcomes, strict=True):
            if outcome is None:
                continue
            kr, result, error = outcome
            ran.append(source.id)
            results[source.id] = result
            st = statuses[source.id]
            record_run(st, result, error, now)
            if kr is None:
                continue
            if kr.version is not None:
                st.version = kr.version
            for f in kr.files:
                privacy, reason = route(source, f)
                groups[privacy].append((f, source))
                if reason is not None:
                    status.rerouted.append(
                        {"source": source.id, "member": member_name(f), "reason": reason}
                    )

        bundles: list[Path] = []
        for privacy, items in groups.items():
            bundles += write_bundles(
                ident,
                ORIGIN,
                privacy,
                items,
                capture_dir / privacy,
                max_bytes=cfg.max_bundle_bytes,
            )
        status.capture_id = ident
        status.generated_at = now
        save_status(status, capture_dir / "status.json")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    files = sum(len(items) for items in groups.values())
    return CaptureResult(ident, ran, results, files, bundles)
