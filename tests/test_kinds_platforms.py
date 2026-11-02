import json

import httpx

from conftest import Clock, make_run, ticking
from us_voting.kinds import KINDS
from us_voting.kinds.base import source_result
from us_voting.kinds.clarity import ClarityKind
from us_voting.kinds.enhanced import EnhancedKind

HEAD = 'id = "s"\nstate = "{st}"\nraw = "public"\nschedule = "results"\nprovides = ["results"]\n'
BASE = "https://electionresults.iowa.gov/IA"
FILES = '["json/en/summary.json", "reports/detailxml.zip"]'
CLARITY = HEAD.format(st="IA") + f'kind = "clarity"\nbase = "{BASE}"\nfiles = {FILES}\n'
CLARITY_EID = CLARITY + 'eid = "127077"\n'
CLARITY_DISC = CLARITY + 'discover_date = "11/3/2026"\n'
HOST = "https://enr.elections.virginia.gov"
ENH = HEAD.format(st="VA") + f'kind = "enhanced"\nhost = "{HOST}"\njurisdiction = "Virginia"\n'
ENH_EL = ENH + 'election = "2026-November-General"\n'
ENH_DISC = ENH + 'discover_date = "2026-11-03"\n'
JUR = f"{HOST}/results/public/api/jurisdictions/Virginia"
META = f"{HOST}/results/public/api/elections/Virginia/2026-November-General"
EXPORT = f"{HOST}/cdn/results/VA/export-2026-November-General.json"
MD = {"publishVersionId": "d503e85b-12f1", "lastUpdated": "t1", "asOf": "a1"}


def clarity_handler(requested, version="382865\n", elections=None, ver_status=200):
    def h(r):
        url = str(r.url)
        requested.append((url, dict(r.headers)))
        if url.endswith("elections.json"):
            return httpx.Response(200, json=elections)
        if url.endswith("current_ver.txt"):
            return httpx.Response(ver_status, text=version)
        return httpx.Response(200, content=b"data" + url.encode())

    return h


def urls(requested):
    return [u for u, _ in requested]


def test_registry():
    assert set(KINDS) >= {"clarity", "enhanced"}


def test_clarity_new_version(tmp_path):
    requested = []
    run = make_run(tmp_path, clarity_handler(requested), body=CLARITY_EID)
    kr = ClarityKind().fetch(run)
    assert urls(requested)[1:] == [
        f"{BASE}/127077/382865/json/en/summary.json",
        f"{BASE}/127077/382865/reports/detailxml.zip",
    ]
    assert [o.file.name for o in kr.outcomes] == [
        "e127077_v382865__json_en_summary.json",
        "e127077_v382865__reports_detailxml.zip",
    ]
    assert kr.version == "382865"
    assert all(f.version == "382865" for f in kr.files)
    for _, headers in requested[1:]:
        assert "if-none-match" not in headers and "if-modified-since" not in headers


def test_clarity_same_version(tmp_path):
    requested = []
    run = make_run(tmp_path, clarity_handler(requested), body=CLARITY_EID)
    run.status.version = "382865"
    kr = ClarityKind().fetch(run)
    assert kr.note == "same_version" and kr.outcomes == []
    assert len(requested) == 1


def test_clarity_discover(tmp_path):
    elections = [
        {"EID": "999", "Date": "11/3/2026", "County": "Adams"},
        {"EID": "127300", "Date": "11/3/2026 12:00:00 AM"},
    ]
    requested = []
    run = make_run(tmp_path, clarity_handler(requested, elections=elections), body=CLARITY_DISC)
    kr = ClarityKind().fetch(run)
    assert "/127300/" in urls(requested)[2]
    assert kr.version == "382865"


def test_clarity_not_listed(tmp_path):
    run = make_run(
        tmp_path,
        clarity_handler([], elections=[{"EID": "1", "Date": "5/5/2026"}]),
        body=CLARITY_DISC,
    )
    kr = ClarityKind().fetch(run)
    assert kr.note == "not_listed_yet" and kr.outcomes == []
    assert source_result(run.source, kr, run.now) == ("waiting", None)


def test_clarity_discover_bad_shape(tmp_path):
    run = make_run(tmp_path, clarity_handler([], elections={"a": 1}), body=CLARITY_DISC)
    (o,) = ClarityKind().fetch(run).outcomes
    assert (o.result, o.status, o.error) == ("error", 200, "unparseable")


def test_clarity_partial_failure_keeps_old_version(tmp_path):
    def h(r):
        url = str(r.url)
        if url.endswith("current_ver.txt"):
            return httpx.Response(200, text="5")
        if url.endswith("detailxml.zip"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"x")

    run = make_run(tmp_path, h, body=CLARITY_EID)
    kr = ClarityKind().fetch(run)
    assert kr.version is None
    assert [o.result for o in kr.outcomes] == ["saved", "missing"]


def test_clarity_blocked_version_maps_to_blocked(tmp_path, monkeypatch):
    run = make_run(tmp_path, lambda r: httpx.Response(200), body=CLARITY_EID)
    monkeypatch.setattr(run.client, "get_text", lambda url: (None, "", "blocked"))
    kr = ClarityKind().fetch(run)
    assert kr.outcomes[0].result == "blocked"
    assert source_result(run.source, kr, run.now)[0] == "blocked"


def test_clarity_empty_version_unparseable(tmp_path):
    run = make_run(tmp_path, clarity_handler([], version="  \n"), body=CLARITY_EID)
    (o,) = ClarityKind().fetch(run).outcomes
    assert (o.result, o.error) == ("error", "unparseable")


def test_clarity_owns(tmp_path):
    run = make_run(tmp_path, clarity_handler([]), body=CLARITY_EID)
    k = ClarityKind()
    assert k.owns(run.source, f"{BASE}/127077/1/json/en/summary.json")
    assert not k.owns(run.source, "https://other.gov/IA/127077/1/x")
    assert not k.owns(run.source, f"{BASE}/555/1/x")
    disc = make_run(tmp_path / "d", clarity_handler([]), body=CLARITY_DISC)
    assert k.owns(disc.source, f"{BASE}/555/1/x")


def enhanced_handler(requested, md=MD, jur=None, meta_status=200):
    jur = {"shortName": "VA", "elections": []} if jur is None else jur

    def h(r):
        url = str(r.url)
        requested.append((url, dict(r.headers)))
        if url == JUR:
            return httpx.Response(200, json=jur)
        if url.endswith("/api/elections/Virginia/G1") or url == META:
            return httpx.Response(meta_status, json=md)
        return httpx.Response(200, content=b"export" + url.encode())

    return h


def test_enhanced_new_version(tmp_path):
    requested = []
    run = make_run(tmp_path, enhanced_handler(requested), body=ENH_EL)
    kr = EnhancedKind().fetch(run)
    assert kr.version == "d503e85b-12f1|t1|a1"
    assert [o.file.name for o in kr.outcomes] == [
        "2026-November-General_d503e85b-12f__meta.json",
        "2026-November-General_d503e85b-12f__export.json",
    ]
    assert urls(requested)[-1] == EXPORT
    assert all(f.version == kr.version for f in kr.files)


def test_enhanced_same_version(tmp_path):
    requested = []
    run = make_run(tmp_path, enhanced_handler(requested), body=ENH_EL)
    run.status.version = "d503e85b-12f1|t1|a1"
    kr = EnhancedKind().fetch(run)
    assert kr.note == "same_version" and kr.outcomes == []
    assert urls(requested) == [JUR, META]


def test_enhanced_discover_skips_special(tmp_path):
    jur = {
        "shortName": "VA",
        "elections": [
            {"publicElectionId": "S1", "electionDate": "2026-11-03", "name": "Special Election"},
            {"publicElectionId": "G1", "electionDate": "2026-11-03", "name": "General Election"},
        ],
    }
    requested = []
    run = make_run(tmp_path, enhanced_handler(requested, jur=jur), body=ENH_DISC)
    kr = EnhancedKind().fetch(run)
    assert urls(requested)[1].endswith("/Virginia/G1")
    assert kr.version is not None


def test_enhanced_not_listed(tmp_path):
    run = make_run(tmp_path, enhanced_handler([]), body=ENH_DISC)
    kr = EnhancedKind().fetch(run)
    assert kr.note == "not_listed_yet" and kr.outcomes == []


def test_enhanced_short_name_fallback(tmp_path):
    requested = []
    run = make_run(tmp_path, enhanced_handler(requested, jur={}), body=ENH_EL)
    EnhancedKind().fetch(run)
    assert urls(requested)[-1].endswith("/cdn/results/Virginia/export-2026-November-General.json")


def test_enhanced_metadata_500_is_http_error(tmp_path):
    run = make_run(tmp_path, enhanced_handler([], meta_status=500), body=ENH_EL)
    (o,) = EnhancedKind().fetch(run).outcomes
    assert (o.url, o.result, o.status) == (META, "http_error", 500)


def test_enhanced_bad_json_unparseable(tmp_path):
    def h(r):
        return httpx.Response(200, content=json.dumps([1]).encode())

    run = make_run(tmp_path, h, body=ENH_EL)
    (o,) = EnhancedKind().fetch(run).outcomes
    assert (o.result, o.status, o.error) == ("error", 200, "unparseable")


def test_enhanced_owns(tmp_path):
    run = make_run(tmp_path, enhanced_handler([]), body=ENH_EL)
    k = EnhancedKind()
    assert k.owns(run.source, META)
    assert k.owns(run.source, EXPORT)
    assert not k.owns(run.source, "https://enr.elections.other.gov/cdn/results/VA/export-x.json")
    assert not k.owns(run.source, f"{HOST}/other/page")


def test_clarity_cut_off_by_the_deadline_records_no_version(tmp_path):
    clock, requested = Clock(), []
    handler = ticking(clarity_handler(requested), clock, 10)
    run = make_run(tmp_path, handler, body=CLARITY_EID, monotonic=clock)
    run.client.set_deadline(20)
    kr = ClarityKind().fetch(run)
    assert len(requested) == 2 and len(kr.files) == 1
    assert kr.version is None
    assert kr.failure == "capture deadline reached after 1 of 2 URLs"


def test_enhanced_cut_off_by_the_deadline_records_no_version(tmp_path):
    clock, requested = Clock(), []
    handler = ticking(enhanced_handler(requested), clock, 10)
    run = make_run(tmp_path, handler, body=ENH_EL, monotonic=clock)
    run.client.set_deadline(30)
    kr = EnhancedKind().fetch(run)
    assert urls(requested) == [JUR, META, META]
    assert kr.version is None
    assert kr.failure == "capture deadline reached after 1 of 2 URLs"
