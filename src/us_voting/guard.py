"""Detect raw files that look voter-level, so they are kept out of public releases.

Reasons name a column or a column number, prefixed with the zip member name when there is one.
They never contain a cell value.
"""

from __future__ import annotations

import base64
import binascii
import csv
import html
import io
import json
import re
import zipfile
import zlib
from collections.abc import Iterable
from pathlib import Path

HEAD_BYTES = 262_144
MAX_MEMBERS = 50
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
# Pattern hits report a fixed label, never the matched token, which could be a cell value.
VOTER_PATTERNS = (
    (re.compile(r"^(voter_)?(first|last|middle)_?name$"), "first/last/middle name"),
    (re.compile(r"_street(_address)?$"), "street address"),
    (re.compile(r"^res(idential)?_addr"), "residential address"),
)
PERSON_NAME = re.compile(
    r"^[A-Z][A-Za-z'\u2019\-]+(?: [A-Z][A-Za-z'\u2019\-]+)*, "
    r"[A-Z][A-Za-z'\u2019\-.]+(?: [A-Z][A-Za-z'\u2019\-.]*)*$"
)
JSON_KEY = re.compile(r'"([^"]{1,40})"\s*:')
XML_TEXT = re.compile(r"<t(?:\s[^>]*)?>([^<]*)</t>")
DELIMITERS = (",", "\t", "|", ";")
JSON_STRING = re.compile(r'"((?:[^"\\]|\\.)*\\?)("|$)')
PARTIAL_ESCAPE = re.compile(r"(\\+)(u[0-9A-Fa-f]{0,3})?$")
BASE64_TEXT = re.compile(r"[A-Za-z0-9+/=]{64,}")
BASE64_MAX_DEPTH = 2
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MAX_NAME_LEN = 100

csv.field_size_limit(2**31 - 1)


def normalize(name: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name.strip().strip('"'))
    return re.sub(r"[^a-z0-9]+", "_", spaced.lower()).strip("_")


def _compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().strip('"').lower()).strip("_")


def _variants(name: str) -> list[str]:
    """Normalized forms to check: camelCase split and unsplit (fName, DoB, idNumber)."""
    return list(dict.fromkeys((normalize(name), _compact(name))))


def _column_reason(raw: str, allow: frozenset[str], json_keys: bool = False) -> str | None:
    names = [v for v in _variants(raw) if v and v not in allow]
    if json_keys:
        names = [v for v in names if v != "name"]
    for column in names:
        if column in VOTER_COLUMNS:
            return f"column '{column}'"
    for column in names:
        for pattern, label in VOTER_PATTERNS:
            if pattern.search(column):
                return f"column matching '{label}'"
    return None


def _first_flagged(
    names: Iterable[str], allow: frozenset[str], json_keys: bool = False
) -> str | None:
    for raw in names:
        reason = _column_reason(raw, allow, json_keys)
        if reason:
            return reason
    return None


def _exact_hit(fields: Iterable[str], allow: frozenset[str]) -> str | None:
    for raw in fields:
        for column in _variants(raw):
            if column in VOTER_COLUMNS and column not in allow:
                return f"column '{column}'"
    return None


def _decode(data: bytes) -> str:
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    probe = data[:64]
    pairs = len(probe) // 2
    if pairs >= 2:
        odd, even = probe[1::2].count(0), probe[0::2].count(0)
        if odd > pairs / 2 and even <= pairs / 10:
            return data.decode("utf-16-le", errors="replace")
        if even > pairs / 2 and odd <= pairs / 10:
            return data.decode("utf-16-be", errors="replace")
    return data.decode("utf-8", errors="replace")


def _rows(lines: list[str], delimiter: str, quoting: int) -> list[list[str]]:
    rows: list[list[str]] = []
    try:
        for row in csv.reader(lines, delimiter=delimiter, quoting=quoting):
            if row:
                rows.append(row)
    except csv.Error:
        pass
    return rows


def _split(line: str, delimiter: str) -> list[str]:
    try:
        return next(csv.reader([line], delimiter=delimiter), [])
    except csv.Error:
        return line.split(delimiter)


def _check_text(text: str, allow: frozenset[str]) -> str | None:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    sample = lines[:NAME_SAMPLE_LINES]
    first = lines[0]
    delimiter = max(DELIMITERS, key=first.count)
    if first.count(delimiter) == 0:
        delimiter = ","
    reason = _first_flagged(_split(first, delimiter), allow)
    if reason:
        return reason

    # Headerless files: the first line may be ambiguous (a name contains a comma), so try
    # every delimiter, with and without quote handling (a stray quote can swallow lines).
    for sep in dict.fromkeys((delimiter, *DELIMITERS)):
        for quoting in (csv.QUOTE_MINIMAL, csv.QUOTE_NONE):
            rows = _rows(sample, sep, quoting)
            for index in range(max((len(r) for r in rows), default=0)):
                hits = sum(
                    1
                    for r in rows
                    if index < len(r)
                    and len(r[index]) <= MAX_NAME_LEN
                    and PERSON_NAME.match(r[index].strip())
                )
                if hits / len(rows) >= NAME_SHARE:
                    return f"person names in column {index + 1}"

    # A preamble can sit above the header: look for exact column names in the first lines.
    for line in sample:
        for sep in DELIMITERS:
            reason = _exact_hit(line.split(sep), allow)
            if reason:
                return reason
    return None


def _base64_candidates(text: str) -> Iterable[str]:
    for match in JSON_STRING.finditer(text):
        body = match.group(1)
        if len(body) < 64:
            continue
        if not match.group(2):
            # Cut off at the end of the head: drop a trailing partial escape.
            partial = PARTIAL_ESCAPE.search(body)
            if partial and len(partial.group(1)) % 2 == 1:
                body = body[: partial.start(1) + len(partial.group(1)) - 1]
        try:
            value = json.loads('"' + body + '"')
        except ValueError:
            value = re.sub(r"\\[nrt]", "", body.replace("\\/", "/"))
        value = "".join(value.split())
        if BASE64_TEXT.fullmatch(value):
            yield value


def _check_json(text: str, allow: frozenset[str], depth: int) -> str | None:
    reason = _first_flagged(JSON_KEY.findall(text), allow, json_keys=True)
    if reason:
        return reason
    for value in _base64_candidates(text):
        if depth >= BASE64_MAX_DEPTH:
            return "nested archive too deep"
        blob = value[:HEAD_BYTES]
        blob = blob[: len(blob) // 4 * 4]
        try:
            decoded = base64.b64decode(blob, validate=False)
        except (binascii.Error, ValueError):
            continue
        reason = _check_bytes(decoded[:HEAD_BYTES], allow, depth + 1)
        if reason:
            return reason
    return None


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
            infos = [
                i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")
            ]
            if "xl/workbook.xml" in {i.filename for i in infos}:
                return _check_xlsx(z, allow)
            for info in infos[:MAX_MEMBERS]:
                with z.open(info) as f:
                    head = f.read(HEAD_BYTES)
                    if head.startswith(b"PK"):
                        if depth >= 1:
                            return f"{info.filename}: nested archive too deep"
                        head += f.read(NESTED_ZIP_CAP)
                reason = _check_bytes(head, allow, depth + 1)
                if reason:
                    return f"{info.filename}: {reason}"
            if len(infos) > MAX_MEMBERS:
                return f"{len(infos) - MAX_MEMBERS} members not checked"
    except (zipfile.BadZipFile, OSError, EOFError, zlib.error, NotImplementedError, RuntimeError):
        return "unreadable zip"
    except (ValueError, csv.Error):
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
        if head.startswith((b"\x1f\x8b", b"PK")):
            return "nested archive too deep"
        return _check_bytes(head, allow, depth)
    if data.startswith(OLE2_MAGIC):
        return "legacy Excel file not inspected"
    if data.startswith(b"%PDF"):
        return None
    text = _decode(data)
    if text.lstrip()[:1] in ("{", "["):
        return _check_json(text, allow, depth)
    return _check_text(text, allow)


def voter_level_reason(path: Path, allow: Iterable[str] = ()) -> str | None:
    """Return why the file looks voter-level, or None. Unreadable input returns a reason."""
    allowed = frozenset(v for a in allow for v in _variants(a))
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
        if head.startswith(b"PK"):
            return _check_zip(path, allowed, 0)
        return _check_bytes(head, allowed)
    except Exception:
        return "unreadable file"
