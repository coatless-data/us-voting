"""Enhanced Voting ENR: download metadata and export when a new version is published."""

import json

from us_voting.config import Source
from us_voting.kinds.base import (
    GOOD,
    KindResult,
    SourceRun,
    fetch_file,
    safe_name,
    text_failure,
    unparseable,
)


def _host(source: Source) -> str:
    return source.params["host"].rstrip("/")


def _get_object(run: SourceRun, url: str) -> tuple[dict | None, KindResult | None]:
    status, text, error = run.client.get_text(url)
    if error is not None or status != 200:
        return None, KindResult([text_failure(url, status, error)])
    try:
        data = json.loads(text)
    except ValueError:
        return None, KindResult([unparseable(url)])
    if not isinstance(data, dict):
        return None, KindResult([unparseable(url)])
    return data, None


class EnhancedKind:
    def fetch(self, run: SourceRun) -> KindResult:
        params = run.source.params
        host = _host(run.source)
        jur = params["jurisdiction"]
        jur_url = f"{host}/results/public/api/jurisdictions/{jur}"
        jd, failed = _get_object(run, jur_url)
        if jd is None:
            return failed
        short = jd.get("shortName") or jur
        election = params.get("election")
        if not election:
            listed = jd.get("elections", [])
            if not isinstance(listed, list):
                return KindResult([unparseable(jur_url)])
            hits = [
                e
                for e in listed
                if isinstance(e, dict)
                and e.get("electionDate") == params["discover_date"]
                and "special" not in json.dumps(e.get("name", "")).lower()
            ]
            if not hits:
                return KindResult([], note="not_listed_yet")
            election = hits[0]["publicElectionId"]
        meta_url = f"{host}/results/public/api/elections/{jur}/{election}"
        md, failed = _get_object(run, meta_url)
        if md is None:
            return failed
        version = "|".join(str(md.get(k)) for k in ("publishVersionId", "lastUpdated", "asOf"))
        if version == run.status.version:
            return KindResult([], note="same_version")
        stamp = run.now.strftime("%Y%m%dT%H%M%SZ")
        tag = safe_name(str(md.get("publishVersionId") or stamp))[:12]
        outcomes = [
            fetch_file(
                run, url, f"{election}_{tag}__{label}.json", conditional=False, version=version
            )
            for url, label in (
                (meta_url, "meta"),
                (f"{host}/cdn/results/{short}/export-{election}.json", "export"),
            )
        ]
        good = all(o.result in GOOD for o in outcomes)
        return KindResult(outcomes, version=version if good else None)

    def owns(self, source: Source, url: str) -> bool:
        if not url.startswith(_host(source) + "/"):
            return False
        return "/results/public/api/elections/" in url or (
            "/cdn/results/" in url and "/export-" in url
        )
