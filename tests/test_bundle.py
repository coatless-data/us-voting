import hashlib
import os
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import NOW, one_source
from us_voting.bundle import (
    BundleTooLarge,
    info_to_dict,
    member_name,
    read_info,
    release_tag,
    write_bundles,
)
from us_voting.kinds.base import CapturedFile

BODY = (
    'id = "nc-x"\nstate = "NC"\nkind = "url"\nraw = "public"\n'
    'schedule = "results"\nprovides = ["results"]\nurl = "https://a.gov/f.csv"\n'
)


def make_item(tmp_path: Path, name: str, data: bytes, dirname: str = "in"):
    source = one_source(tmp_path / f"cfg-{dirname}-{name}", BODY)
    folder = tmp_path / dirname
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    f = CapturedFile(
        source_id=source.id,
        url=f"https://a.gov/{name}",
        name=name,
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        received_at=NOW,
        status=200,
        headers={"etag": "x"},
    )
    return f, source


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_roundtrip(tmp_path):
    item_a = make_item(tmp_path, "a.csv", b"a,b\n1,2\n")
    item_b = make_item(tmp_path, "b.csv", b"c\n3\n")
    out = tmp_path / "out"
    paths = write_bundles(
        "2026-11-03T2323Z", "capture", "public", [item_b, item_a], out, max_bytes=10**9
    )
    assert [p.name for p in paths] == ["capture-2026-11-03T2323Z.tar.gz"]
    with tarfile.open(paths[0]) as t:
        assert t.getnames() == [
            "_bundle.json",
            "nc-x/20261103T232300Z__a.csv",
            "nc-x/20261103T232300Z__b.csv",
        ]
        sidecar_in_tar = t.extractfile("_bundle.json").read()
    sidecar = out / "capture-2026-11-03T2323Z.json"
    assert sidecar.read_bytes() == sidecar_in_tar
    info = read_info(sidecar)
    assert info.files[0].sha256 == item_a[0].sha256
    assert info.files[0].election == "2026-11-03-general"
    assert info.files[0].state == "NC"
    assert info.files[0].received_at == "2026-11-03T23:23:00Z"
    assert info.name == "capture-2026-11-03T2323Z.tar.gz"
    assert read_info(sidecar) == info
    assert info_to_dict(info)["origin"] == "capture"


def test_bundles_are_deterministic(tmp_path):
    items = [make_item(tmp_path, "a.csv", b"x\n1\n"), make_item(tmp_path, "b.csv", b"y\n")]
    a = write_bundles("X", "capture", "public", items, tmp_path / "1", max_bytes=10**9)[0]
    b = write_bundles("X", "capture", "public", items, tmp_path / "2", max_bytes=10**9)[0]
    assert sha256_file(a) == sha256_file(b)


def test_split_and_too_large(tmp_path):
    items = [
        make_item(tmp_path, "a.csv", b"a" * 6),
        make_item(tmp_path, "b.csv", b"b" * 6),
        make_item(tmp_path, "c.csv", b"c" * 3),
    ]
    out = tmp_path / "out"
    paths = write_bundles("X", "capture", "public", items, out, max_bytes=10)
    assert [p.name for p in paths] == ["capture-X.tar.gz", "capture-X-2.tar.gz"]
    first = read_info(out / "capture-X.json")
    second = read_info(out / "capture-X-2.json")
    assert second.name == "capture-X-2.tar.gz"
    assert [f.bytes for f in first.files] == [6]
    assert [f.bytes for f in second.files] == [6, 3]
    big = [make_item(tmp_path, "big.csv", b"z" * 11)]
    with pytest.raises(BundleTooLarge):
        write_bundles("Y", "capture", "public", big, tmp_path / "o2", max_bytes=10)


def test_member_collision_gets_suffix(tmp_path):
    one = make_item(tmp_path, "a.csv", b"1", "d1")
    two = make_item(tmp_path, "a.csv", b"2", "d2")
    three = make_item(tmp_path, "a.csv", b"3", "d3")
    (p,) = write_bundles("X", "capture", "public", [one, two, three], tmp_path / "o", max_bytes=99)
    with tarfile.open(p) as t:
        assert t.getnames()[1:] == [
            "nc-x/20261103T232300Z__a-2.csv",
            "nc-x/20261103T232300Z__a-3.csv",
            "nc-x/20261103T232300Z__a.csv",
        ]
    assert member_name(one[0]) == "nc-x/20261103T232300Z__a.csv"


def test_empty_writes_nothing(tmp_path):
    assert write_bundles("X", "capture", "public", [], tmp_path / "o", max_bytes=9) == []
    assert not (tmp_path / "o").exists() or not list((tmp_path / "o").iterdir())


def test_import_names_and_tags(tmp_path):
    item = make_item(tmp_path, "a.csv", b"1")
    out = tmp_path / "out"
    (p,) = write_bundles("2026-10-17", "import", "public", [item], out, max_bytes=99)
    assert p.name == "import-2026-10-17.tar.gz"
    assert release_tag(read_info(out / "import-2026-10-17.json")) == "raw-2026-10"
    (q,) = write_bundles("2026-11-03T2323Z", "capture", "public", [item], out, max_bytes=99)
    assert release_tag(read_info(out / f"{q.name[:-7]}.json")) == "raw-2026-11"


def test_tar_headers_are_normalized(tmp_path):
    item = make_item(tmp_path, "a.csv", b"1")
    (p,) = write_bundles("X", "capture", "public", [item], tmp_path / "o", max_bytes=99)
    with tarfile.open(p) as t:
        for m in t.getmembers():
            assert (m.uid, m.gid, m.uname, m.gname, m.mode) == (0, 0, "", "", 0o644)
            assert m.mtime == int(datetime(2026, 11, 3, 23, 23, tzinfo=UTC).timestamp())


def _collision_items(tmp_path, dirname):
    return [
        make_item(tmp_path, "a.csv", b"1", f"{dirname}1"),
        make_item(tmp_path, "a.csv", b"2", f"{dirname}2"),
        make_item(tmp_path, "b.csv", b"3" * 6, f"{dirname}3"),
        make_item(tmp_path, "c.csv", b"4" * 6, f"{dirname}4"),
    ]


def test_input_order_does_not_change_bundles(tmp_path):
    items = _collision_items(tmp_path, "d")
    fwd = write_bundles("X", "capture", "public", items, tmp_path / "f", max_bytes=8)
    rev = write_bundles("X", "capture", "public", items[::-1], tmp_path / "r", max_bytes=8)
    assert [p.name for p in fwd] == [p.name for p in rev]
    assert len(fwd) == 2
    assert [sha256_file(p) for p in fwd] == [sha256_file(p) for p in rev]
    entries = read_info(tmp_path / "f" / "capture-X.json").files
    plain = next(e for e in entries if e.member.endswith("__a.csv"))
    suffixed = next(e for e in entries if e.member.endswith("__a-2.csv"))
    first, second = sorted(items[:2], key=lambda i: i[0].sha256)
    assert plain.sha256 == first[0].sha256
    assert suffixed.sha256 == second[0].sha256


def test_paths_and_mtimes_do_not_change_bundles(tmp_path):
    items = _collision_items(tmp_path, "p")
    copies = _collision_items(tmp_path, "q")
    for i, (f, _) in enumerate(copies):
        os.utime(f.path, (1_000_000 + i, 1_000_000 + i))
    a = write_bundles("X", "capture", "public", items, tmp_path / "1", max_bytes=99)
    b = write_bundles("X", "capture", "public", copies, tmp_path / "2", max_bytes=99)
    assert [sha256_file(p) for p in a] == [sha256_file(p) for p in b]
