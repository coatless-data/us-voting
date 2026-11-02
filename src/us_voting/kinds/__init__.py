"""Capture kinds: each knows how to fetch one style of source."""

from us_voting.kinds.base import Kind
from us_voting.kinds.clarity import ClarityKind
from us_voting.kinds.dated import DatedKind
from us_voting.kinds.enhanced import EnhancedKind
from us_voting.kinds.page_links import PageLinksKind
from us_voting.kinds.url import UrlKind

KINDS: dict[str, Kind] = {
    "url": UrlKind(),
    "dated": DatedKind(),
    "page_links": PageLinksKind(),
    "clarity": ClarityKind(),
    "enhanced": EnhancedKind(),
}
