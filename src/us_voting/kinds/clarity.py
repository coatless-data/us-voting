"""Clarity/Civera ENR: download the report files when a new version is published."""

import json

from us_voting.config import Source
from us_voting.kinds.base import (
    GOOD,
    KindResult,
    SourceRun,
    fetch_file,
    text_failure,
    unparseable,
)


def _base(source: Source) -> str:
    return source.params["base"].rstrip("/")


class ClarityKind:
    def fetch(self, run: SourceRun) -> KindResult:
        params = run.source.params
        base = _base(run.source)
        eid = params.get("eid")
        if not eid:
            listing = f"{base}/elections.json"
            status, text, error = run.client.get_text(listing)
            if error is not None or status != 200:
                return KindResult([text_failure(listing, status, error)])
            try:
                elections = json.loads(text)
                if not isinstance(elections, list) or not all(
                    isinstance(e, dict) for e in elections
                ):
                    raise ValueError
            except ValueError:
                return KindResult([unparseable(listing)])
            want = params["discover_date"]
            hits = [
                e
                for e in elections
                if str(e.get("Date", "")).startswith(want) and not e.get("County")
            ]
            if not hits:
                return KindResult([], note="not_listed_yet")
            eid = str(hits[0]["EID"])
        ver_url = f"{base}/{eid}/current_ver.txt"
        status, text, error = run.client.get_text(ver_url)
        if error is not None or status != 200:
            return KindResult([text_failure(ver_url, status, error)])
        version = text.strip()
        if not version:
            return KindResult([unparseable(ver_url)])
        if version == run.status.version:
            return KindResult([], note="same_version")
        outcomes = [
            fetch_file(
                run,
                f"{base}/{eid}/{version}/{path}",
                f"e{eid}_v{version}__{path.replace('/', '_')}",
                conditional=False,
                version=version,
            )
            for path in params["files"]
        ]
        good = all(o.result in GOOD for o in outcomes)
        return KindResult(outcomes, version=version if good else None)

    def owns(self, source: Source, url: str) -> bool:
        if not url.startswith(_base(source) + "/"):
            return False
        eid = source.params.get("eid")
        return not eid or f"/{eid}/" in url
