import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from conftest import NOW, write_config
from us_voting import cli
from us_voting.bundle import write_bundles
from us_voting.config import Config, load_config
from us_voting.kinds.base import CapturedFile
from us_voting.publish import (
    MANIFEST_NAME,
    PublishConflict,
    PublishRefused,
    publish_dir,
)
from us_voting.store import LocalReleaseStore, sha256_file, sha256_label

SOURCES = """
[[source]]
id = "pub"
state = "NC"
kind = "url"
raw = "public"
schedule = "results"
provides = ["results"]
url = "https://a.gov/f.csv"

[[source]]
id = "prv"
state = "NC"
kind = "url"
raw = "private"
schedule = "results"
provides = ["results"]
url = "https://a.gov/g.csv"
"""
IDENT = "2026-11-03T2323Z"
TAG = "raw-2026-11"


def cfg_for(tmp_path: Path) -> Config:
    return load_config(write_config(tmp_path, SOURCES))


def make_file(tmp_path: Path, cfg: Config, source_id: str, data: bytes, name="f.csv"):
    folder = tmp_path / "in" / source_id / hashlib.sha256(data).hexdigest()[:8]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    f = CapturedFile(
        source_id=source_id,
        url=f"https://a.gov/{name}",
        name=name,
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        received_at=NOW,
        status=200,
        headers={},
    )
    return f, cfg.sources[source_id]


def build(tmp_path, cfg, cap, *, pub=b"a,b\n1,2\n", prv=b"c\n3\n", ident=IDENT, origin="capture"):
    out = tmp_path / cap
    if pub is not None:
        write_bundles(
            ident, origin, "public", [make_file(tmp_path, cfg, "pub", pub)], out / "public",
            max_bytes=10**9,
        )  # fmt: skip
    if prv is not None:
        write_bundles(
            ident, origin, "private", [make_file(tmp_path, cfg, "prv", prv)], out / "private",
            max_bytes=10**9,
        )  # fmt: skip
    return out


def stores(tmp_path):
    return LocalReleaseStore(tmp_path / "pub-store"), LocalReleaseStore(tmp_path / "prv-store")


def manifest(store, tag):
    with tempfile.TemporaryDirectory() as tmp:
        dest = store.download(tag, MANIFEST_NAME, Path(tmp) / "m.json")
        return json.loads(dest.read_text(encoding="utf-8"))


def names(store, tag):
    return sorted(a.name for a in store.list_assets(tag))


def test_publish_to_both_stores(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap")
    pub_store, prv_store = stores(tmp_path)
    r = publish_dir(cap, cfg, pub_store, prv_store)
    assert r.uploaded == ["capture-2026-11-03T2323Z.tar.gz"] * 2
    m = manifest(pub_store, TAG)
    assert m["tag"] == TAG
    assert [b["name"] for b in m["bundles"]] == ["capture-2026-11-03T2323Z.tar.gz"]
    pub_tar = cap / "public" / "capture-2026-11-03T2323Z.tar.gz"
    assert m["bundles"][0]["sha256"] == sha256_file(pub_tar)
    assert m["bundles"][0]["bytes"] == pub_tar.stat().st_size
    assert m["bundles"][0]["files"][0]["source_id"] == "pub"
    pm = manifest(prv_store, TAG)
    ids = [f["source_id"] for b in pm["bundles"] for f in b["files"]]
    assert ids == ["prv"]
    assert "pub" not in {f["source_id"] for b in pm["bundles"] for f in b["files"]}


def test_republish_skips(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap")
    pub_store, prv_store = stores(tmp_path)
    publish_dir(cap, cfg, pub_store, prv_store)
    r = publish_dir(cap, cfg, pub_store, prv_store)
    assert r.uploaded == []
    assert len(r.skipped) == 2
    assert len(manifest(pub_store, TAG)["bundles"]) == 1


def test_conflict_changes_nothing(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap")
    pub_store, prv_store = stores(tmp_path)
    publish_dir(cap, cfg, pub_store, prv_store)
    before_m = manifest(pub_store, TAG)
    before_a = names(pub_store, TAG)
    cap2 = build(tmp_path, cfg, "cap2", pub=b"a,b\n9,9\n", prv=None)
    with pytest.raises(PublishConflict):
        publish_dir(cap2, cfg, pub_store, prv_store)
    assert manifest(pub_store, TAG) == before_m
    assert names(pub_store, TAG) == before_a


def test_public_bundle_with_voter_member_is_refused(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap", pub=b"voter_reg_num,x\n1,2\n", prv=None)
    pub_store, prv_store = stores(tmp_path)
    with pytest.raises(PublishRefused) as exc:
        publish_dir(cap, cfg, pub_store, prv_store)
    assert "pub/20261103T232300Z__f.csv" in str(exc.value)
    assert "1,2" not in str(exc.value)
    assert pub_store.get_release(TAG) is None or pub_store.list_assets(TAG) == []


def test_import_bundle_goes_to_its_month(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap", ident="2026-10-17", origin="import", prv=None)
    pub_store, prv_store = stores(tmp_path)
    r = publish_dir(cap, cfg, pub_store, prv_store)
    assert r.uploaded == ["import-2026-10-17.tar.gz"]
    assert manifest(pub_store, "raw-2026-10")["bundles"][0]["origin"] == "import"


def test_manifest_sorted_by_name(tmp_path):
    cfg = cfg_for(tmp_path)
    pub_store, prv_store = stores(tmp_path)
    for ident in ("2026-11-05T0000Z", "2026-11-03T2323Z"):
        publish_dir(
            build(tmp_path, cfg, f"c{ident}", ident=ident, prv=None), cfg, pub_store, prv_store
        )
    got = [b["name"] for b in manifest(pub_store, TAG)["bundles"]]
    assert got == sorted(got) and len(got) == 2


def test_leftover_temporary_does_not_block_publish(tmp_path):
    cfg = cfg_for(tmp_path)
    pub_store, prv_store = stores(tmp_path)
    pub_store.ensure_release(TAG, "t", "b", False, "false")
    leftover = tmp_path / "left.json"
    leftover.write_text(json.dumps({"tag": TAG, "bundles": []}), encoding="utf-8")
    pub_store.upload_new(TAG, leftover, f"{MANIFEST_NAME}.next-tok-1", label=sha256_label(leftover))
    cap = build(tmp_path, cfg, "cap", prv=None)
    r = publish_dir(cap, cfg, pub_store, prv_store)
    assert r.uploaded == ["capture-2026-11-03T2323Z.tar.gz"]
    assert len(manifest(pub_store, TAG)["bundles"]) == 1


def test_missing_subfolder_is_fine(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap", prv=None)
    pub_store, prv_store = stores(tmp_path)
    assert publish_dir(cap, cfg, pub_store, prv_store).uploaded != []
    assert publish_dir(tmp_path / "empty", cfg, pub_store, prv_store).uploaded == []


def test_cli_exit_codes(tmp_path, capsys):
    config = write_config(tmp_path, SOURCES)
    cfg = load_config(config)
    cap = build(tmp_path, cfg, "cap")
    argv = [
        "publish", str(cap), "--config", str(config),
        "--public-store", f"local:{tmp_path / 'p'}",
        "--private-store", f"local:{tmp_path / 'q'}",
    ]  # fmt: skip
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "skipped" not in out and out.count("uploaded") == 2 and TAG in out
    assert cli.main(argv) == 0
    assert "skipped" in capsys.readouterr().out
    cap2 = build(tmp_path, cfg, "cap2", pub=b"a,b\n9,9\n", prv=None)
    argv[1] = str(cap2)
    assert cli.main(argv) == 1
    assert capsys.readouterr().err
    bad = [*argv[:-1], "bogus:x"]
    assert cli.main(bad) == 1
