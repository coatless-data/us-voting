import gzip
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


def test_only_first_five_members_checked(tmp_path):
    members = {f"m{i}.csv": "County,Total\n" for i in range(5)}
    members["m5.csv"] = NC_VOTERS
    assert voter_level_reason(zip_of(tmp_path, members)) is None


def test_curly_apostrophe_names(tmp_path):
    rows = "".join(f"{i}\tO{chr(0x2019)}Doe, Jane\n" for i in range(5))
    assert voter_level_reason(write(tmp_path, rows)) == "person names in column 2"
