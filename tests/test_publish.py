import gzip
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from dataclasses import replace
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
from us_voting.store import LocalReleaseStore, StorageError, sha256_file, sha256_label

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


class RecordingStore(LocalReleaseStore):
    def __init__(self, root, log, label, *, fail=False):
        super().__init__(root)
        self.log, self.label, self.fail = log, label, fail

    def upload_new(self, tag, path, name, label=None):
        if name.endswith(".tar.gz"):
            self.log.append(self.label)
            if self.fail:
                raise StorageError("upload failed", status=500)
        return super().upload_new(tag, path, name, label)


def test_private_bundles_are_uploaded_first(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap")
    log = []
    publish_dir(
        cap,
        cfg,
        RecordingStore(tmp_path / "pub-store", log, "public"),
        RecordingStore(tmp_path / "prv-store", log, "private"),
    )
    assert log == ["private", "public"]


def test_failed_private_upload_uploads_no_public_bundle(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap")
    log = []
    pub_store = RecordingStore(tmp_path / "pub-store", log, "public")
    with pytest.raises(StorageError):
        publish_dir(
            cap, cfg, pub_store, RecordingStore(tmp_path / "prv", log, "private", fail=True)
        )
    assert log == ["private"]
    assert pub_store.get_release(TAG) is None or names(pub_store, TAG) == []


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


def recompress(tar_path: Path) -> None:
    """Rewrite a bundle's gzip layer so its bytes change while its files do not."""
    raw = gzip.decompress(tar_path.read_bytes())
    before = tar_path.read_bytes()
    tar_path.write_bytes(gzip.compress(raw, compresslevel=1, mtime=1))
    assert tar_path.read_bytes() != before


def two_files(tmp_path, cfg, cap, second=b"x\n2\n"):
    items = [
        make_file(tmp_path / cap, cfg, "pub", b"a,b\n1,2\n", name="f.csv"),
        make_file(tmp_path / cap, cfg, "pub", second, name="g.csv"),
    ]
    write_bundles(IDENT, "capture", "public", items, tmp_path / cap / "public", max_bytes=10**9)
    return tmp_path / cap


@pytest.mark.parametrize("folder", ["public", "private"])
def test_same_files_with_different_gzip_bytes_are_skipped(tmp_path, folder):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap")
    pub_store, prv_store = stores(tmp_path)
    publish_dir(cap, cfg, pub_store, prv_store)
    store = pub_store if folder == "public" else prv_store
    before_m, before_a = manifest(store, TAG), names(store, TAG)
    cap2 = tmp_path / "cap2"
    shutil.copytree(cap, cap2)
    recompress(cap2 / folder / "capture-2026-11-03T2323Z.tar.gz")
    r = publish_dir(cap2, cfg, pub_store, prv_store)
    assert r.uploaded == [] and len(r.skipped) == 2
    assert manifest(store, TAG) == before_m and names(store, TAG) == before_a


def test_one_member_with_a_different_sha_is_a_conflict(tmp_path):
    cfg = cfg_for(tmp_path)
    pub_store, prv_store = stores(tmp_path)
    publish_dir(two_files(tmp_path, cfg, "cap"), cfg, pub_store, prv_store)
    before_m, before_a = manifest(pub_store, TAG), names(pub_store, TAG)
    with pytest.raises(PublishConflict, match="different content"):
        publish_dir(two_files(tmp_path, cfg, "cap2", b"x\n3\n"), cfg, pub_store, prv_store)
    assert manifest(pub_store, TAG) == before_m and names(pub_store, TAG) == before_a


def test_a_missing_member_is_a_conflict(tmp_path):
    cfg = cfg_for(tmp_path)
    pub_store, prv_store = stores(tmp_path)
    publish_dir(two_files(tmp_path, cfg, "cap"), cfg, pub_store, prv_store)
    with pytest.raises(PublishConflict):
        publish_dir(build(tmp_path, cfg, "cap2", prv=None), cfg, pub_store, prv_store)


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
    (tmp_path / "empty").mkdir()
    assert publish_dir(tmp_path / "empty", cfg, pub_store, prv_store).uploaded == []


def test_missing_dir_fails(tmp_path, capsys):
    cfg = cfg_for(tmp_path)
    pub_store, prv_store = stores(tmp_path)
    with pytest.raises(FileNotFoundError):
        publish_dir(tmp_path / "nope", cfg, pub_store, prv_store)
    config = tmp_path / "config"
    argv = ["publish", str(tmp_path / "nope"), "--config", str(config)]
    argv += [
        "--public-store",
        f"local:{tmp_path / 'p'}",
        "--private-store",
        f"local:{tmp_path / 'q'}",
    ]
    assert cli.main(argv) == 1
    assert "nope" in capsys.readouterr().err


def _assert_refused_untouched(tmp_path, cfg, cap, match):
    pub_store, prv_store = stores(tmp_path)
    with pytest.raises(PublishRefused, match=match) as exc:
        publish_dir(cap, cfg, pub_store, prv_store)
    assert "1,2" not in str(exc.value)
    assert pub_store.get_release(TAG) is None or names(pub_store, TAG) == []


def test_private_marked_bundle_in_public_folder_is_refused(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap", pub=None)
    (cap / "public").mkdir()
    for f in (cap / "private").iterdir():
        shutil.copy(f, cap / "public" / f.name)
    _assert_refused_untouched(tmp_path, cfg, cap, "marked 'private'")


def test_bundle_from_private_source_in_public_folder_is_refused(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = tmp_path / "cap"
    write_bundles(
        IDENT, "capture", "public", [make_file(tmp_path, cfg, "prv", b"c\n3\n")], cap / "public",
        max_bytes=10**9,
    )  # fmt: skip
    _assert_refused_untouched(tmp_path, cfg, cap, "private source prv")


def test_non_regular_member_in_public_bundle_is_refused(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap", prv=None)
    tar_path = cap / "public" / "capture-2026-11-03T2323Z.tar.gz"
    with tarfile.open(tar_path) as t:
        members = [(m, t.extractfile(m).read()) for m in t.getmembers()]
    with tarfile.open(tar_path, "w:gz") as t:
        for m, data in members:
            t.addfile(m, io.BytesIO(data))
        link = tarfile.TarInfo("pub/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        t.addfile(link)
    _assert_refused_untouched(tmp_path, cfg, cap, "not a regular file")


class Exists422(LocalReleaseStore):
    """Pretends the first upload landed but reports 422, as GitHub does after a lost response."""

    def __init__(self, root, *, strip_digest=False):
        super().__init__(root)
        self.strip_digest = strip_digest

    def upload_new(self, tag, path, name, label=None):
        asset = super().upload_new(tag, path, name, label)
        if not name.endswith(".tar.gz"):
            return asset
        raise StorageError("asset exists", status=422)

    def list_assets(self, tag):
        assets = super().list_assets(tag)
        if self.strip_digest:
            assets = [replace(a, digest=None) for a in assets]
        return assets


def test_422_with_matching_digest_counts_as_uploaded(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap", prv=None)
    store = Exists422(tmp_path / "s")
    r = publish_dir(cap, cfg, store, LocalReleaseStore(tmp_path / "q"))
    assert r.uploaded == ["capture-2026-11-03T2323Z.tar.gz"]
    assert len(manifest(store, TAG)["bundles"]) == 1


def test_422_with_matching_content_and_no_digest_counts_as_uploaded(tmp_path):
    cfg = cfg_for(tmp_path)
    cap = build(tmp_path, cfg, "cap", prv=None)
    store = Exists422(tmp_path / "s", strip_digest=True)
    r = publish_dir(cap, cfg, store, LocalReleaseStore(tmp_path / "q"))
    assert r.uploaded == ["capture-2026-11-03T2323Z.tar.gz"]


def _stale_then_422(tmp_path, strip_digest):
    cfg = cfg_for(tmp_path)
    store = Exists422(tmp_path / "s", strip_digest=strip_digest)
    store.ensure_release(TAG, "t", "b", False, "false")
    other = tmp_path / "other.bin"
    other.write_bytes(b"different")
    LocalReleaseStore.upload_new(store, TAG, other, "capture-2026-11-03T2323Z.tar.gz")
    cap = build(tmp_path, cfg, "cap", prv=None)
    return cfg, cap, store


@pytest.mark.parametrize("strip_digest", [False, True])
def test_422_with_different_content_reraises(tmp_path, strip_digest):
    cfg, cap, store = _stale_then_422(tmp_path, strip_digest)
    with pytest.raises(StorageError) as exc:
        publish_dir(cap, cfg, store, LocalReleaseStore(tmp_path / "q"))
    assert exc.value.status == 422
    assert not [a for a in store.list_assets(TAG) if a.name == MANIFEST_NAME]


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


@pytest.mark.parametrize(
    "flags",
    [
        ["--public-store", "github:Coatless-Data/US-Voting-Raw"],
        ["--private-store", "github:COATLESS-DATA/us-voting"],
        ["--public-store", "local:{d}/same", "--private-store", "local:{d}/same"],
    ],
)
def test_cli_refuses_one_repo_for_both_stores(tmp_path, capsys, monkeypatch, flags):
    def never(*args, **kwargs):
        raise AssertionError("a store was opened")

    monkeypatch.setattr(cli, "open_store", never)
    monkeypatch.setattr(cli, "open_configured", never)
    config = write_config(tmp_path, SOURCES)
    cap = build(tmp_path, cfg_for(tmp_path / "c2"), "cap")
    argv = ["publish", str(cap), "--config", str(config)]
    argv += [f.format(d=tmp_path) for f in flags]
    assert cli.main(argv) == 1
    assert "same" in capsys.readouterr().err
    assert not (tmp_path / "same").exists()


def test_import_refuses_one_repo_for_both_stores(tmp_path, capsys):
    config = write_config(tmp_path, SOURCES)
    text = (config / "stores.toml").read_text(encoding="utf-8")
    (config / "stores.toml").write_text(
        text.replace("coatless-data/us-voting-raw", "coatless-data/US-voting"), encoding="utf-8"
    )
    (tmp_path / "arch").mkdir()
    (tmp_path / "arch" / "_log.jsonl").write_text("", encoding="utf-8")
    argv = ["import-archive", str(tmp_path / "arch"), "--out", str(tmp_path / "out")]
    argv += ["--config", str(config), "--through", "2026-10-31"]
    assert cli.main(argv) == 1
    assert "same" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
