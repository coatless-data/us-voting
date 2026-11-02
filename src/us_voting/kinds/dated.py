"""URL templates expanded over a date range ({d:...}) or a counter ({n})."""

from datetime import date, timedelta

from us_voting.config import Source
from us_voting.kinds.base import KindResult, SourceRun, fetch_file, fetch_in_turn, url_name


def _templates(source: Source) -> list[str]:
    p = source.params
    return list(p["templates"]) if "templates" in p else [p["template"]]


def _expand(source: Source, last: date | None) -> list[str]:
    p = source.params
    templates = _templates(source)
    if "n_range" in p:
        lo, hi = p["n_range"]
        return [t.format(n=n) for t in templates for n in range(lo, hi + 1)]
    start = date.fromisoformat(p["start"])
    end = date.fromisoformat(p["end"])
    if last is not None:
        end = min(end, last)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return [t.format(d=d) for d in days for t in templates]


def _urls(source: Source, today: date) -> list[str]:
    return _expand(source, today)


class DatedKind:
    def fetch(self, run: SourceRun) -> KindResult:
        urls = _urls(run.source, run.now.date())
        outcomes, failure = fetch_in_turn(
            run, urls, lambda url: fetch_file(run, url, url_name(url))
        )
        return KindResult(outcomes, failure=failure)

    def owns(self, source: Source, url: str) -> bool:
        return url in _expand(source, None)
