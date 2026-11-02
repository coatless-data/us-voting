"""URL templates expanded over a date range ({d:...}) or a counter ({n})."""

import re
from datetime import date, timedelta
from string import Formatter

from us_voting.config import Source
from us_voting.kinds.base import KindResult, SourceRun, UrlOutcome, fetch_file, url_name


def _templates(source: Source) -> list[str]:
    p = source.params
    return list(p["templates"]) if "templates" in p else [p["template"]]


def _urls(source: Source, today: date) -> list[str]:
    p = source.params
    templates = _templates(source)
    if "n_range" in p:
        lo, hi = p["n_range"]
        return [t.format(n=n) for t in templates for n in range(lo, hi + 1)]
    start = date.fromisoformat(p["start"])
    end = min(date.fromisoformat(p["end"]), today)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return [t.format(d=d) for d in days for t in templates]


def _pattern(template: str) -> re.Pattern[str]:
    out = []
    for literal, field_name, _spec, _conv in Formatter().parse(template):
        out.append(re.escape(literal))
        if field_name is not None:
            out.append(".+?")
    return re.compile("".join(out))


class DatedKind:
    def fetch(self, run: SourceRun) -> KindResult:
        outcomes: list[UrlOutcome] = []
        for url in _urls(run.source, run.now.date()):
            outcomes.append(fetch_file(run, url, url_name(url)))
        return KindResult(outcomes)

    def owns(self, source: Source, url: str) -> bool:
        return any(_pattern(t).fullmatch(url) for t in _templates(source))
