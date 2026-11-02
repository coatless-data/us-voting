"""Fixed URLs, fetched as they are."""

from us_voting.config import Source
from us_voting.kinds.base import KindResult, SourceRun, UrlOutcome, fetch_file, url_name


def _urls(source: Source) -> list[str]:
    p = source.params
    return list(p["urls"]) if "urls" in p else [p["url"]]


class UrlKind:
    def fetch(self, run: SourceRun) -> KindResult:
        outcomes: list[UrlOutcome] = []
        name = run.source.params.get("name")
        for url in _urls(run.source):
            outcomes.append(fetch_file(run, url, name or url_name(url)))
        return KindResult(outcomes)

    def owns(self, source: Source, url: str) -> bool:
        return url in _urls(source)
