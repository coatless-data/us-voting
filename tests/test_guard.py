import base64
import gzip
import io
import json
import zipfile
from itertools import count
from pathlib import Path

import pytest

from us_voting.guard import voter_level_reason

NC_VOTERS = (
    '"county_desc","voter_reg_num","ncid","voter_last_name","voter_first_name"\n'
    '"WAKE","1","AA1","DOE","JANE"\n'
)

_counter = count()


def zip_of(tmp_path: Path, members: dict[str, str]) -> Path:
    p = tmp_path / f"bundle{next(_counter)}.zip"
    with zipfile.ZipFile(p, "w") as z:
        for name, text in members.items():
            z.writestr(name, text)
    return p


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / f"file{next(_counter)}.csv"
    p.write_text(text, encoding="utf-8")
    return p


def xlsx_of(tmp_path: Path, strings: list[str]) -> Path:
    p = tmp_path / f"book{next(_counter)}.xlsx"
    items = "".join(f"<si><t>{s}</t></si>" for s in strings)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/sharedStrings.xml", f"<sst>{items}</sst>")
        z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
    return p


def test_zip_member_header(tmp_path):
    p = zip_of(tmp_path, {"absentee_20261103.csv": NC_VOTERS})
    assert voter_level_reason(p) == "absentee_20261103.csv: column 'voter_reg_num'"


def test_utf16_header(tmp_path):
    text = (
        "county_id\tcounty_name\telection_dt\tvoter_reg_num\tfull_name\r\n"
        "1\tWAKE\t11/03/2026\t1\tJANE DOE\r\n"
    )
    p = tmp_path / "provisional.txt"
    p.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    assert voter_level_reason(p) == "column 'voter_reg_num'"


def test_clark_and_pa_headers(tmp_path):
    assert (
        voter_level_reason(write(tmp_path, '"IDNUMBER","NAME","PRECINCT","EV SITE"\n'))
        == "column 'idnumber'"
    )
    assert (
        voter_level_reason(write(tmp_path, "County Name,Party,Date of Birth,Ballot Mailed Date\n"))
        == "column 'date_of_birth'"
    )


def test_headerless_person_names(tmp_path):
    rows = [
        f"EVtrs\tALA\t49894\t11/03/2026\tGeneral\t{100 + i}\tDoe, Jane Q\t47\tREP\tLibrary\n"
        for i in range(20)
    ]
    reason = voter_level_reason(write(tmp_path, "".join(rows)))
    assert reason == "person names in column 7"
    assert "Doe" not in reason


def test_quoted_names_in_comma_file(tmp_path):
    rows = [f'1,"Roe, Richard",{i}\n' for i in range(10)]
    assert voter_level_reason(write(tmp_path, "".join(rows))) == "person names in column 2"


@pytest.mark.parametrize(
    "header",
    [
        "county_name,abs_request_type,party_cd,gender,race,ethnicity,0",
        "ElectionNumber\tElectionDate\tElectionName\tCountyName\tStatType\tTotalRep\tGrandTotal",
        "COUNTY_CODE,COUNTY_NAME,Precinct,GENDER_CODE,AgeGroup,PARTY_CODE,EARLY_VOTE_CENTER,Day1",
        "County Name,Total",
    ],
)
def test_count_files_pass(tmp_path, header):
    assert voter_level_reason(write(tmp_path, header + "\n")) is None


def test_count_rows_pass(tmp_path):
    rows = "County,Total\n" + "".join(f"County{i},{i}\n" for i in range(30))
    assert voter_level_reason(write(tmp_path, rows)) is None


def test_allow_list(tmp_path):
    p = write(tmp_path, "election_dt\tcounty_name\tpolling_place_name\tstreet_address\tcity\n")
    assert voter_level_reason(p) == "column 'street_address'"
    assert voter_level_reason(p, allow=("street_address",)) is None
    assert voter_level_reason(p, allow=("Street Address",)) is None


def test_xlsx_shared_strings(tmp_path):
    assert voter_level_reason(xlsx_of(tmp_path, ["First Name", "Total"])) == "column 'first_name'"
    assert voter_level_reason(xlsx_of(tmp_path, ["County", "Total"])) is None


def test_json_keys(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('[{"county":"A","total":3}]')
    assert voter_level_reason(p) is None
    p = tmp_path / "b.json"
    p.write_text('[{"voter_id":1}]')
    assert voter_level_reason(p) == "column 'voter_id'"


def test_pdf_passes(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.6\nfirst_name last_name\n")
    assert voter_level_reason(p) is None


def test_gzip_header(tmp_path):
    p = tmp_path / "a.csv.gz"
    p.write_bytes(gzip.compress(NC_VOTERS.encode()))
    assert voter_level_reason(p) == "column 'voter_reg_num'"


def test_corrupt_zip_fails_closed(tmp_path):
    p = tmp_path / "bad.zip"
    p.write_bytes(b"PK\x03\x04" + b"garbage" * 20)
    assert voter_level_reason(p) == "unreadable zip"


def test_corrupt_gzip_fails_closed(tmp_path):
    p = tmp_path / "bad.gz"
    p.write_bytes(b"\x1f\x8b" + b"garbage" * 20)
    assert voter_level_reason(p) == "unreadable gzip"


def test_undecodable_bytes_do_not_raise(tmp_path):
    p = tmp_path / "junk.bin"
    p.write_bytes(bytes(range(256)) * 4)
    assert voter_level_reason(p) is None
    q = tmp_path / "junk2.csv"
    q.write_bytes(b"first_name,\xff\xfe\n")
    assert voter_level_reason(q) == "column 'first_name'"


def test_empty_file(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_bytes(b"")
    assert voter_level_reason(p) is None


def test_zip_member_headerless_names_and_prefix(tmp_path):
    rows = "".join(f"{i}\tDoe, Jane Q\n" for i in range(5))
    reason = voter_level_reason(zip_of(tmp_path, {"ok.csv": "County,Total\n", "list.txt": rows}))
    assert reason == "list.txt: person names in column 2"


def test_nested_zip_one_level(tmp_path):
    inner = zip_of(tmp_path, {"v.csv": NC_VOTERS})
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as z:
        z.write(inner, "inner.zip")
    assert voter_level_reason(outer) == "inner.zip: v.csv: column 'voter_reg_num'"


def test_curly_apostrophe_names(tmp_path):
    rows = "".join(f"{i}\tO{chr(0x2019)}Doe, Jane\n" for i in range(5))
    assert voter_level_reason(write(tmp_path, rows)) == "person names in column 2"


def test_preamble_blank_line_before_header(tmp_path):
    assert voter_level_reason(write(tmp_path, "\n" + NC_VOTERS)) == "column 'voter_reg_num'"


def test_preamble_title_before_header(tmp_path):
    text = "Absentee Report Wake County\n" + NC_VOTERS
    assert voter_level_reason(write(tmp_path, text)) == "column 'voter_reg_num'"


def test_preamble_before_tab_header(tmp_path):
    text = "Report for Wake, 2026\nvoter_id\tcounty\n1\tWAKE\n"
    assert voter_level_reason(write(tmp_path, text)) == "column 'voter_id'"


def test_pattern_hit_never_echoes_cell_value(tmp_path):
    reason = voter_level_reason(write(tmp_path, "123 Elm Street,Wake,5\n"))
    assert reason == "column matching 'street address'"
    assert "Elm" not in reason and "123" not in reason


def test_xlsx_pattern_hit_never_echoes_cell_value(tmp_path):
    reason = voter_level_reason(xlsx_of(tmp_path, ["County", "12 Oak Street"]))
    assert reason == "column matching 'street address'"
    assert "Oak" not in reason


def test_long_single_line_does_not_raise(tmp_path):
    text = ("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" * 6000)[:200_000]
    assert voter_level_reason(write(tmp_path, text)) is None


def test_unclosed_quote_does_not_raise(tmp_path):
    text = 'County,Total\n"Wake,5\nDurham,6\n'
    assert voter_level_reason(write(tmp_path, text)) is None


def test_stray_quote_does_not_hide_names(tmp_path):
    rows = ['EVtrs\tALA\t49894\t11/03/2026\tGeneral\t100\tDoe, Jane Q\t47\tREP\t"Lib\n']
    rows += [
        f"EVtrs\tALA\t49894\t11/03/2026\tGeneral\t{101 + i}\tDoe, Jane Q\t47\tREP\tLibrary\n"
        for i in range(19)
    ]
    assert voter_level_reason(write(tmp_path, "".join(rows))) == "person names in column 7"


def test_member_cap_reports_unchecked(tmp_path):
    members = {f"m{i}.csv": "County,Total\n" for i in range(50)}
    members["m50.csv"] = NC_VOTERS
    assert voter_level_reason(zip_of(tmp_path, members)) == "1 members not checked"


def test_member_within_cap_checked(tmp_path):
    members = {f"m{i}.csv": "County,Total\n" for i in range(49)}
    members["m49.csv"] = NC_VOTERS
    assert voter_level_reason(zip_of(tmp_path, members)) == "m49.csv: column 'voter_reg_num'"


def test_macosx_entries_skipped(tmp_path):
    members = {f"c{i}.csv": "County,Total\n" for i in range(3)}
    members.update({f"__MACOSX/._c{i}.csv": "junk" for i in range(3)})
    members["voters.csv"] = NC_VOTERS
    assert voter_level_reason(zip_of(tmp_path, members)) == "voters.csv: column 'voter_reg_num'"


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_zip_in_zip_in_zip_is_too_deep(tmp_path):
    inner = _zip_bytes({"v.csv": NC_VOTERS.encode()})
    mid = _zip_bytes({"inner.zip": inner})
    p = tmp_path / "deep.zip"
    p.write_bytes(_zip_bytes({"mid.zip": mid}))
    assert voter_level_reason(p) == "mid.zip: inner.zip: nested archive too deep"


def test_gzip_of_gzip_is_too_deep(tmp_path):
    p = tmp_path / "gg.gz"
    p.write_bytes(gzip.compress(gzip.compress(NC_VOTERS.encode())))
    assert voter_level_reason(p) == "nested archive too deep"


def test_xlsx_in_nested_zip_is_too_deep(tmp_path):
    book = _zip_bytes({"xl/workbook.xml": b"<w/>", "xl/sharedStrings.xml": b"<sst/>"})
    mid = _zip_bytes({"book.xlsx": book})
    p = tmp_path / "x.zip"
    p.write_bytes(_zip_bytes({"mid.zip": mid}))
    assert voter_level_reason(p) == "mid.zip: book.xlsx: nested archive too deep"


def test_base64_json_wrapper_flagged(tmp_path):
    blob = base64.b64encode((NC_VOTERS * 3).encode()).decode()
    p = tmp_path / "wrap.json"
    p.write_text(json.dumps({"upload": blob}))
    assert voter_level_reason(p) == "column 'voter_reg_num'"


def test_base64_json_wrapper_of_counts_passes(tmp_path):
    inner = json.dumps({"turnout_by_county": [{"name": "ANDERSON", "total": 5}]})
    p = tmp_path / "wrap.json"
    p.write_text(json.dumps({"upload": base64.b64encode(inner.encode()).decode()}))
    assert voter_level_reason(p) is None


def test_json_bare_name_key_ignored(tmp_path):
    p = tmp_path / "r.json"
    p.write_text('[{"name": "Governor", "votes": 3}]')
    assert voter_level_reason(p) is None


def test_json_other_keys_still_flagged(tmp_path):
    p = tmp_path / "r.json"
    p.write_text('[{"name": "x", "firstName": "y"}]')
    assert voter_level_reason(p) == "column 'first_name'"


def test_clark_name_column_still_flagged(tmp_path):
    assert voter_level_reason(write(tmp_path, '"NAME","PRECINCT"\n')) == "column 'name'"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("VoterRegNum,County", "column 'voter_reg_num'"),
        ("County,DateOfBirth", "column 'date_of_birth'"),
        ("County,FullName", "column 'full_name'"),
        ("IDNUMBER,County", "column 'idnumber'"),
        ("CountyName,Total", None),
    ],
)
def test_camel_case_headers(tmp_path, header, expected):
    assert voter_level_reason(write(tmp_path, header + "\n")) == expected


def test_legacy_excel_not_inspected(tmp_path):
    p = tmp_path / "old.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
    assert voter_level_reason(p) == "legacy Excel file not inspected"


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
def test_utf16_without_bom(tmp_path, codec):
    p = tmp_path / "nobom.txt"
    p.write_bytes("county_id\tvoter_reg_num\tfull_name\r\n1\t1\tJANE DOE\r\n".encode(codec))
    assert voter_level_reason(p) == "column 'voter_reg_num'"


def _wrapper_path(tmp_path, raw_json: str) -> Path:
    p = tmp_path / f"wrap{next(_counter)}.json"
    p.write_text(raw_json)
    return p


def _blob(count: int = 3) -> str:
    return base64.b64encode((NC_VOTERS * count).encode()).decode()


def test_escaped_slash_wrapper_with_slashes(tmp_path):
    # Bytes chosen so the base64 text contains "/" and "+".
    data = b"#" + b"\xfb\xef\xbf" * 20 + b"\n" + NC_VOTERS.encode()
    blob = base64.b64encode(data).decode()
    assert "/" in blob
    raw = json.dumps({"upload": blob}).replace("/", "\\/")
    assert "\\/" in raw
    reason = voter_level_reason(_wrapper_path(tmp_path, raw))
    assert reason == "column 'voter_reg_num'"


def test_unicode_escaped_plus_wrapper_flagged(tmp_path):
    data = b"\xfb\xef\xbe" * 30 + b"\n" + NC_VOTERS.encode()
    blob = base64.b64encode(data).decode()
    assert "+" in blob
    raw = json.dumps({"upload": blob}).replace("+", "\\u002B")
    assert voter_level_reason(_wrapper_path(tmp_path, raw)) == "column 'voter_reg_num'"


def test_mime_line_break_wrapper_flagged(tmp_path):
    blob = _blob()
    wrapped = "\n".join(blob[i : i + 76] for i in range(0, len(blob), 76))
    raw = json.dumps({"upload": wrapped})
    assert "\\n" in raw
    assert voter_level_reason(_wrapper_path(tmp_path, raw)) == "column 'voter_reg_num'"


def test_zip_with_wrapper_member_flagged(tmp_path):
    raw = json.dumps({"upload": _blob()})
    reason = voter_level_reason(zip_of(tmp_path, {"data.json": raw}))
    assert reason == "data.json: column 'voter_reg_num'"


def test_counts_wrapper_in_zip_passes(tmp_path):
    inner = json.dumps({"turnout_by_county": [{"name": "ANDERSON", "total": 5}]})
    raw = json.dumps({"upload": base64.b64encode(inner.encode()).decode()})
    assert voter_level_reason(zip_of(tmp_path, {"data.json": raw})) is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("fName,lName,precinct", "column 'fname'"),
        ("idNumber,party", "column 'idnumber'"),
        ("ncId,county", "column 'ncid'"),
        ("DoB,county", "column 'dob'"),
        ("CountyName,Total", None),
    ],
)
def test_compact_column_names(tmp_path, header, expected):
    assert voter_level_reason(write(tmp_path, header + "\n")) == expected


@pytest.mark.parametrize(
    ("text", "expected"), [('{"fName": 1}', "fname"), ('{"idNumber": 1}', "idnumber")]
)
def test_compact_json_keys(tmp_path, text, expected):
    p = tmp_path / f"k{next(_counter)}.json"
    p.write_text(text)
    assert voter_level_reason(p) == f"column '{expected}'"


def _escaped_wrapper(style: str, pad: int) -> str:
    data = NC_VOTERS.encode() + b"\xff\xff\xfe" * 9000
    blob = base64.b64encode(data).decode()
    if style == "newline":
        blob = "\n".join(blob[i : i + 76] for i in range(0, len(blob), 76))
    raw = json.dumps({"pad": "x" * pad, "upload": blob})
    if style == "plus":
        raw = raw.replace("+", "\\u002B")
    if style == "slash":
        raw = raw.replace("/", "\\/")
    return raw


@pytest.mark.parametrize("style", ["plus", "newline", "slash"])
@pytest.mark.parametrize("pad", range(12))
def test_wrapper_cut_inside_escape_is_flagged(tmp_path, monkeypatch, style, pad):
    monkeypatch.setattr("us_voting.guard.HEAD_BYTES", 4096)
    raw = _escaped_wrapper(style, pad)
    assert len(raw) > 4 * 4096
    assert voter_level_reason(_wrapper_path(tmp_path, raw)) == "column 'voter_reg_num'"


def test_cut_public_json_string_is_not_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr("us_voting.guard.HEAD_BYTES", 4096)
    raw = json.dumps({"note": "a\\b/c é " * 2000, "total": 5})
    assert voter_level_reason(_wrapper_path(tmp_path, raw)) is None
