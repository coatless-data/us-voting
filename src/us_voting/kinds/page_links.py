"""Files linked from an HTML page, picked by a regular expression."""

import re
from urllib.parse import unquote, urljoin, urlsplit

from us_voting.config import Source
from us_voting.kinds.base import (
    KindResult,
    SourceRun,
    fetch_file,
    fetch_in_turn,
    safe_name,
    text_failure,
)

LINK = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)
DEFAULT_MAX_LINKS = 60


def _pattern(source: Source) -> re.Pattern[str]:
    return re.compile(source.params["pattern"], re.I)


class PageLinksKind:
    def fetch(self, run: SourceRun) -> KindResult:
        page = run.source.params["page"]
        status, text, error = run.client.get_text(page)
        if error is not None or status != 200:
            return KindResult([text_failure(page, status, error)])
        pattern = _pattern(run.source)
        matched = sorted({urljoin(page, h) for h in LINK.findall(text) if pattern.search(h)})
        if not matched:
            return KindResult([], note="no_links")
        max_links = int(run.source.params.get("max_links", DEFAULT_MAX_LINKS))
        links = matched[:max_links]
        outcomes, cut_off = fetch_in_turn(
            run,
            links,
            lambda url: fetch_file(
                run, url, safe_name(unquote(urlsplit(url).path.rsplit("/", 1)[-1])) or "file"
            ),
        )
        # Links past max_links are never fetched, so the source fails until the config
        # is fixed rather than silently missing new files.
        truncated = (
            f"{len(matched)} links matched; max_links is {max_links}"
            if len(matched) > max_links
            else None
        )
        failure = "; ".join(m for m in (truncated, cut_off) if m) or None
        return KindResult(outcomes, failure=failure)

    def owns(self, source: Source, url: str) -> bool:
        return _pattern(source).search(url) is not None
