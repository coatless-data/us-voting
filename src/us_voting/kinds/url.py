"""Fixed URLs, fetched as they are."""

from us_voting.config import Source
from us_voting.kinds.base import KindResult, SourceRun, fetch_file, fetch_in_turn, url_name


def _urls(source: Source) -> list[str]:
    p = source.params
    return list(p["urls"]) if "urls" in p else [p["url"]]


class UrlKind:
    def fetch(self, run: SourceRun) -> KindResult:
        name = run.source.params.get("name")
        outcomes, failure = fetch_in_turn(
            run, _urls(run.source), lambda url: fetch_file(run, url, name or url_name(url))
        )
        return KindResult(outcomes, failure=failure)

    def owns(self, source: Source, url: str) -> bool:
        return url in _urls(source)
