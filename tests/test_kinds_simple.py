from datetime import UTC, datetime

import httpx

from conftest import make_run
from us_voting.kinds import KINDS
from us_voting.kinds.base import source_result
from us_voting.kinds.dated import DatedKind
from us_voting.kinds.page_links import PageLinksKind
from us_voting.kinds.url import UrlKind

HEAD = 'id = "s"\nstate = "NC"\nraw = "public"\nschedule = "results"\nprovides = ["results"]\n'


def recorder(requested, content=b"data"):
    def h(r):
        requested.append(str(r.url))
        return httpx.Response(200, content=content + str(r.url).encode())

    return h


def test_registry():
    assert set(KINDS) >= {"url", "dated", "page_links"}


def test_url_names_and_owns(tmp_path):
    body = (
        HEAD
        + 'kind = "url"\nurls = ["https://a.gov/x/results_pct_20261103.zip",'
        + ' "https://a.gov/x/resultsCSV.aspx?text=All&type=All&map=CTY"]\n'
    )
    requested = []
    run = make_run(tmp_path, recorder(requested), body=body)
    kr = UrlKind().fetch(run)
    assert [f.name for f in kr.files] == [
        "results_pct_20261103.zip",
        "resultsCSV.aspx_text=All_type=All_map=CTY",
    ]
    assert UrlKind().owns(run.source, "https://a.gov/x/results_pct_20261103.zip")
    assert not UrlKind().owns(run.source, "https://a.gov/x/results_pct_20261103.zip.bak")


def test_url_name_param(tmp_path):
    body = HEAD + 'kind = "url"\nurl = "https://a.gov/x/y"\nname = "custom.csv"\n'
    run = make_run(tmp_path, recorder([]), body=body)
    (f,) = UrlKind().fetch(run).files
    assert f.name == "custom.csv" and f.source_id == "s" and f.url == "https://a.gov/x/y"


def test_dated_dates_capped_at_today(tmp_path):
    t = "https://ca.gov/bsr-statistics-{d:%m-%d-%y}.xlsx"
    body = HEAD + f'kind = "dated"\ntemplate = "{t}"\nstart = "2026-10-28"\nend = "2026-11-12"\n'
    requested = []
    run = make_run(
        tmp_path, recorder(requested), body=body, now=datetime(2026, 10, 30, 12, tzinfo=UTC)
    )
    DatedKind().fetch(run)
    assert requested == [
        "https://ca.gov/bsr-statistics-10-28-26.xlsx",
        "https://ca.gov/bsr-statistics-10-29-26.xlsx",
        "https://ca.gov/bsr-statistics-10-30-26.xlsx",
    ]


def test_dated_counter_templates(tmp_path):
    body = (
        HEAD
        + 'kind = "dated"\ntemplates = ["https://a.gov/a{n}.csv", "https://a.gov/b{n}.csv"]\n'
        + "n_range = [1, 3]\n"
    )
    requested = []
    run = make_run(tmp_path, recorder(requested), body=body)
    kr = DatedKind().fetch(run)
    assert len(requested) == 6 and len(kr.outcomes) == 6


def test_dated_owns(tmp_path):
    ca = make_run(
        tmp_path,
        recorder([]),
        body=HEAD
        + 'kind = "dated"\ntemplate = "https://ca.gov/bsr-statistics-{d:%m-%d-%y}.xlsx"\n'
        + 'start = "2026-10-28"\nend = "2026-11-12"\n',
    ).source
    tx_t = (
        "https://goelect.txelections.civixapps.com/api-ivis-system/api/v1/getFile"
        "?type=EVR_EARLYVOTING&electionId=53815&electionDate={d:%m/%d/%Y}"
    )
    tx = make_run(
        tmp_path / "tx",
        recorder([]),
        body=HEAD
        + f'kind = "dated"\ntemplate = "{tx_t}"\nstart = "2026-10-14"\nend = "2026-10-14"\n',
    ).source
    assert DatedKind().owns(ca, "https://ca.gov/bsr-statistics-10-29-26.xlsx")
    assert not DatedKind().owns(ca, "https://ca.gov/bsr-statistics-10-29-26.xlsx.bak")
    assert DatedKind().owns(
        tx,
        "https://goelect.txelections.civixapps.com/api-ivis-system/api/v1/getFile"
        "?type=EVR_EARLYVOTING&electionId=53815&electionDate=10/14/2026",
    )


def test_dated_owns_exact_urls_only(tmp_path):
    clark = "https://c.gov/EVMB/ev_{d:%Y%m%d}.zip"
    src = make_run(
        tmp_path,
        recorder([]),
        body=HEAD
        + f'kind = "dated"\ntemplate = "{clark}"\nstart = "2026-10-17"\nend = "2026-10-30"\n',
        now=datetime(2026, 10, 20, 12, tzinfo=UTC),
    ).source
    assert DatedKind().owns(src, "https://c.gov/EVMB/ev_20261017.zip")
    assert DatedKind().owns(src, "https://c.gov/EVMB/ev_20261030.zip")
    assert not DatedKind().owns(src, "https://c.gov/EVMB/ev_26GE.zip")
    assert not DatedKind().owns(src, "https://c.gov/EVMB/ev_20261016.zip")
    assert not DatedKind().owns(src, "https://c.gov/EVMB/ev_20261031.zip")


def test_dated_owns_counter_range(tmp_path):
    src = make_run(
        tmp_path,
        recorder([]),
        body=HEAD
        + 'kind = "dated"\ntemplates = ["https://a.gov/a{n}.csv", "https://a.gov/b{n}.csv"]\n'
        + "n_range = [1, 3]\n",
    ).source
    assert DatedKind().owns(src, "https://a.gov/a1.csv")
    assert DatedKind().owns(src, "https://a.gov/b3.csv")
    assert not DatedKind().owns(src, "https://a.gov/a4.csv")
    assert not DatedKind().owns(src, "https://a.gov/a0.csv")
    assert not DatedKind().owns(src, "https://a.gov/ax.csv")


PAGE = (
    '<a href="/press_room/2026_stats/GG26/a.csv">a</a> <a href="b.xlsx">b</a> '
    "<img src='https://x.gov/2026_stats/GG26/c.xlsx'>"
)
PL = (
    HEAD
    + 'kind = "page_links"\npage = "https://x.gov/press/index.html"\n'
    + "pattern = '''2026_stats/GG26/[^\"']+\\.(csv|xlsx)$'''\n"
)


def page_handler(requested, status=200, page=PAGE):
    def h(r):
        requested.append(str(r.url))
        if str(r.url) == "https://x.gov/press/index.html":
            return httpx.Response(
                status, content=page.encode(), headers={"content-type": "text/html"}
            )
        return httpx.Response(200, content=b"d" + str(r.url).encode())

    return h


def test_page_links(tmp_path):
    requested = []
    run = make_run(tmp_path, page_handler(requested), body=PL)
    kr = PageLinksKind().fetch(run)
    assert requested[1:] == [
        "https://x.gov/2026_stats/GG26/c.xlsx",
        "https://x.gov/press_room/2026_stats/GG26/a.csv",
    ]
    assert [f.name for f in kr.files] == ["c.xlsx", "a.csv"]
    assert PageLinksKind().owns(run.source, "https://x.gov/press_room/2026_stats/GG26/a.csv")
    assert not PageLinksKind().owns(run.source, "https://x.gov/other.csv")


def test_page_links_max_links_and_no_links(tmp_path):
    requested = []
    run = make_run(tmp_path, page_handler(requested), body=PL + "max_links = 1\n")
    PageLinksKind().fetch(run)
    assert len(requested) == 2
    run = make_run(tmp_path / "n", page_handler([], page="<p>none</p>"), body=PL)
    kr = PageLinksKind().fetch(run)
    assert kr.note == "no_links" and kr.outcomes == []


def test_page_links_page_error(tmp_path):
    run = make_run(tmp_path, page_handler([], status=500), body=PL)
    kr = PageLinksKind().fetch(run)
    assert [(o.url, o.result) for o in kr.outcomes] == [
        ("https://x.gov/press/index.html", "http_error")
    ]


def test_page_links_page_missing(tmp_path):
    run = make_run(tmp_path, page_handler([], status=404), body=PL)
    assert PageLinksKind().fetch(run).outcomes[0].result == "missing"


def test_page_links_blocked_page_is_blocked_not_waiting(tmp_path):
    run = make_run(tmp_path, lambda r: httpx.Response(429), body=PL)
    kr = PageLinksKind().fetch(run)
    result, error = source_result(run.source, kr, run.now)
    assert kr.outcomes[0].result == "blocked"
    assert result == "blocked" and error == "https://x.gov/press/index.html"
