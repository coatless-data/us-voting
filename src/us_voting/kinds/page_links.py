"""Files linked from an HTML page, picked by a regular expression."""

import re
from urllib.parse import unquote, urljoin, urlsplit

from us_voting.config import Source
from us_voting.kinds.base import (
    KindResult,
    SourceRun,
    UrlOutcome,
    fetch_file,
    fetch_in_turn,
    safe_name,
)

LINK = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)
DEFAULT_MAX_LINKS = 60


def _pattern(source: Source) -> re.Pattern[str]:
    return re.compile(source.params["pattern"], re.I)


def _page_outcome(page: str, status: int | None, error: str | None) -> UrlOutcome:
    if error in ("blocked", "host_abandoned"):
        return UrlOutcome(page, "blocked", status, error)
    if error is not None:
        return UrlOutcome(page, "error", status, error)
    result = "missing" if status in (403, 404, 410) else "http_error"
    return UrlOutcome(page, result, status)


class PageLinksKind:
    def fetch(self, run: SourceRun) -> KindResult:
        page = run.source.params["page"]
        status, text, error = run.client.get_text(page)
        if error is not None or status != 200:
            return KindResult([_page_outcome(page, status, error)])
        pattern = _pattern(run.source)
        links = sorted({urljoin(page, h) for h in LINK.findall(text) if pattern.search(h)})
        links = links[: int(run.source.params.get("max_links", DEFAULT_MAX_LINKS))]
        if not links:
            return KindResult([], note="no_links")
        outcomes, failure = fetch_in_turn(
            run,
            links,
            lambda url: fetch_file(
                run, url, safe_name(unquote(urlsplit(url).path.rsplit("/", 1)[-1])) or "file"
            ),
        )
        return KindResult(outcomes, failure=failure)

    def owns(self, source: Source, url: str) -> bool:
        return _pattern(source).search(url) is not None
