"""Detect raw files that look voter-level, so they are kept out of public releases.

Reasons name a column or a column number, prefixed with the zip member name when there is one.
They never contain a cell value.
"""

from __future__ import annotations

import csv
import html
import io
import re
import zipfile
import zlib
from collections.abc import Iterable
from pathlib import Path

HEAD_BYTES = 262_144
MAX_MEMBERS = 5
NESTED_ZIP_CAP = 64 * 1024 * 1024
NAME_SAMPLE_LINES = 20
NAME_SHARE = 0.6

VOTER_COLUMNS = frozenset(
    {
        "name",
        "full_name",
        "voter_name",
        "first_name",
        "last_name",
        "middle_name",
        "fname",
        "lname",
        "date_of_birth",
        "dob",
        "birth_date",
        "birthdate",
        "voter_reg_num",
        "ncid",
        "vuid",
        "idnumber",
        "voter_id",
        "voterid",
        "registration_number",
        "street_address",
        "residential_address",
        "mailing_address",
        "res_addr_street",
        "latitude",
        "longitude",
        "phone",
    }
)
VOTER_PATTERNS = (
    re.compile(r"^(voter_)?(first|last|middle)_?name$"),
    re.compile(r"_street(_address)?$"),
    re.compile(r"^res(idential)?_addr"),
)
PERSON_NAME = re.compile(
    r"^[A-Z][A-Za-z'\u2019\-]+(?: [A-Z][A-Za-z'\u2019\-]+)*, "
    r"[A-Z][A-Za-z'\u2019\-.]+(?: [A-Z][A-Za-z'\u2019\-.]*)*$"
)
JSON_KEY = re.compile(r'"([^"]{1,40})"\s*:')
XML_TEXT = re.compile(r"<t(?:\s[^>]*)?>([^<]*)</t>")
DELIMITERS = (",", "\t", "|", ";")


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().strip('"').lower()).strip("_")


def _flagged(column: str, allow: frozenset[str]) -> bool:
    if not column or column in allow:
        return False
    return column in VOTER_COLUMNS or any(p.search(column) for p in VOTER_PATTERNS)


def _first_flagged(names: Iterable[str], allow: frozenset[str]) -> str | None:
    for raw in names:
        column = normalize(raw)
        if _flagged(column, allow):
            return f"column '{column}'"
    return None


def _decode(data: bytes) -> str:
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")


def _check_text(text: str, allow: frozenset[str]) -> str | None:
    lines = text.splitlines()
    if not lines:
        return None
    first = lines[0]
    delimiter = max(DELIMITERS, key=first.count)
    if first.count(delimiter) == 0:
        delimiter = ","
    header = next(csv.reader([first], delimiter=delimiter), [])
    reason = _first_flagged(header, allow)
    if reason:
        return reason

    # Headerless files: the first line may be ambiguous (a name contains a comma), so try
    # every delimiter and flag if any of them yields a column of person names.
    for sep in dict.fromkeys((delimiter, *DELIMITERS)):
        rows = [r for r in csv.reader(lines[:NAME_SAMPLE_LINES], delimiter=sep) if r]
        for index in range(max((len(r) for r in rows), default=0)):
            hits = sum(1 for r in rows if index < len(r) and PERSON_NAME.match(r[index].strip()))
            if hits / len(rows) >= NAME_SHARE:
                return f"person names in column {index + 1}"
    return None


def _check_json(text: str, allow: frozenset[str]) -> str | None:
    return _first_flagged(JSON_KEY.findall(text), allow)


def _check_xlsx(z: zipfile.ZipFile, allow: frozenset[str]) -> str | None:
    # Column names only: strings are checked against the column rules. The headerless
    # person-name check is not applied to spreadsheets.
    strings: list[str] = []
    for part in ("xl/sharedStrings.xml", "xl/worksheets/sheet1.xml"):
        try:
            with z.open(part) as f:
                head = f.read(HEAD_BYTES)
        except KeyError:
            continue
        strings.extend(html.unescape(s) for s in XML_TEXT.findall(_decode(head)))
    return _first_flagged(strings, allow)


def _check_zip(source, allow: frozenset[str], depth: int) -> str | None:
    try:
        with zipfile.ZipFile(source) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if "xl/workbook.xml" in {i.filename for i in infos}:
                return _check_xlsx(z, allow)
            for info in infos[:MAX_MEMBERS]:
                with z.open(info) as f:
                    head = f.read(HEAD_BYTES)
                    if head.startswith(b"PK"):
                        if depth >= 1:
                            continue
                        head += f.read(NESTED_ZIP_CAP)
                reason = _check_bytes(head, allow, depth + 1)
                if reason:
                    return f"{info.filename}: {reason}"
    except (zipfile.BadZipFile, OSError, EOFError, zlib.error, NotImplementedError, RuntimeError):
        return "unreadable zip"
    return None


def _check_bytes(data: bytes, allow: frozenset[str], depth: int = 0) -> str | None:
    if data.startswith(b"PK"):
        return _check_zip(io.BytesIO(data), allow, depth)
    if data.startswith(b"\x1f\x8b"):
        try:
            head = zlib.decompressobj(31).decompress(data, HEAD_BYTES)
        except zlib.error:
            return "unreadable gzip"
        return _check_bytes(head, allow, depth) if not head.startswith(b"\x1f\x8b") else None
    if data.startswith(b"%PDF"):
        return None
    text = _decode(data)
    if text.lstrip()[:1] in ("{", "["):
        return _check_json(text, allow)
    return _check_text(text, allow)


def voter_level_reason(path: Path, allow: Iterable[str] = ()) -> str | None:
    """Return why the file looks voter-level, or None. Unreadable input returns a reason."""
    allowed = frozenset(normalize(a) for a in allow)
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
        if head.startswith(b"PK"):
            return _check_zip(path, allowed, 0)
        return _check_bytes(head, allowed)
    except OSError:
        return "unreadable file"
