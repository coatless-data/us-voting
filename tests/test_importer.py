import gzip
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from conftest import write_config
from us_voting import cli
from us_voting.bundle import read_info
from us_voting.config import load_config
from us_voting.importer import LIMITS, ImportResult, import_archive

SOURCES = """
[[source]]
id = "pub"
state = "NC"
kind = "url"
raw = "public"
schedule = "results"
provides = ["results"]
url = "https://a.gov/p.csv"

[[source]]
id = "priv"
state = "NC"
kind = "url"
raw = "private"
schedule = "results"
provides = ["registration"]
url = "https://a.gov/v.csv"
"""
PUB = "https://a.gov/p.csv"
PRIV = "https://a.gov/v.csv"
CSV = b"county,votes\nA,1\n"
VOTER = b"county,voter_reg_num\nA,1\n"


def add(arch: Path, ts: str, url: str, data: bytes, **over) -> dict:
    """Write one gzip file and return its saved log entry."""
    stamp = ts.replace("-", "").replace(":", "").replace("+0000", "Z")[:16]
    stamp = stamp.replace("+00", "Z")
    name = url.rsplit("/", 1)[1]
    rel = f"x-source/{stamp}__{name}.gz"
    (arch / "x-source").mkdir(parents=True, exist_ok=True)
    (arch / rel).write_bytes(gzip.compress(data, mtime=0))
    entry = {
        "ts": ts,
        "source": "x-source",
        "url": url,
        "result": "saved",
        "status": 200,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": "text/csv",
        "last_modified": "Fri, 30 Oct 2026 13:00:00 GMT",
        "file": rel,
    }
    entry.update(over)
    return entry


def build(tmp_path: Path, entries: list[dict | str], name: str = "arch") -> Path:
    arch = tmp_path / name
    arch.mkdir(exist_ok=True)
    lines = [e if isinstance(e, str) else json.dumps(e) for e in entries]
    (arch / "_log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return arch


def standard(tmp_path: Path, name: str = "arch") -> Path:
    arch = tmp_path / name
    arch.mkdir(exist_ok=True)
    entries = [
        add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV),
        {"ts": "2026-10-30T15:00:00+00:00", "source": "x", "url": PUB, "result": "unchanged"},
        add(arch, "2026-10-31T09:00:00+00:00", PUB, CSV + b"B,2\n"),
        add(arch, "2026-10-31T10:00:00+00:00", PRIV, b"x,y\n1,2\n", content_type=None),
    ]
    return build(tmp_path, entries, name)


def cfg_for(tmp_path: Path):
    return load_config(write_config(tmp_path, SOURCES))


def test_bundles_per_privacy_and_day(tmp_path):
    arch, cfg, out = standard(tmp_path), cfg_for(tmp_path), tmp_path / "out"
    r = import_archive(arch, cfg, out, through=date(2026, 10, 31))
    assert isinstance(r, ImportResult)
    assert sorted(p.name for p in (out / "public").glob("*.tar.gz")) == [
        "import-2026-10-30.tar.gz",
        "import-2026-10-31.tar.gz",
    ]
    assert [p.name for p in (out / "private").glob("*.tar.gz")] == ["import-2026-10-31.tar.gz"]
    assert r.files == 3 and len(r.bundles) == 3 and r.skipped_after == 0
    info = read_info(out / "public/import-2026-10-30.json")
    assert info.origin == "import" and info.limits == list(LIMITS)
    assert info.files[0].received_at == "2026-10-30T14:00:00Z"
    assert info.files[0].headers == {
        "content-type": "text/csv",
        "last-modified": "Fri, 30 Oct 2026 13:00:00 GMT",
    }
    priv = read_info(out / "private/import-2026-10-31.json")
    assert priv.files[0].headers == {"last-modified": "Fri, 30 Oct 2026 13:00:00 GMT"}
    assert not (out / "work").exists()


def test_through_excludes_later_days(tmp_path):
    arch, cfg, out = standard(tmp_path), cfg_for(tmp_path), tmp_path / "out"
    r = import_archive(arch, cfg, out, through=date(2026, 10, 30))
    assert [p.name for p in (out / "public").glob("*.tar.gz")] == ["import-2026-10-30.tar.gz"]
    assert not (out / "private").exists()
    assert r.skipped_after == 2 and r.files == 1


def test_unmatched_writes_nothing(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    other = "https://zzz.gov/q.csv"
    build(tmp_path, [add(arch, "2026-10-30T14:00:00+00:00", other, CSV)])
    out = tmp_path / "out"
    r = import_archive(arch, cfg_for(tmp_path), out, through=date(2026, 10, 31))
    assert r.unmatched == [other] and r.bundles == []
    assert not (out / "public").exists() and not (out / "work").exists()


def test_ambiguous(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    build(tmp_path, [add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV)])
    both = SOURCES.replace(PRIV, PUB)
    cfg = load_config(write_config(tmp_path, both))
    out = tmp_path / "out"
    r = import_archive(arch, cfg, out, through=date(2026, 10, 31))
    assert r.ambiguous == [PUB] and r.bundles == [] and not (out / "public").exists()


def test_bad_hash(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    e = add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV, sha256="0" * 64)
    build(tmp_path, [e])
    out = tmp_path / "out"
    r = import_archive(arch, cfg_for(tmp_path), out, through=date(2026, 10, 31))
    assert r.bad_hash == [e["file"]] and not (out / "public").exists()


def test_missing_file_counts_as_bad_hash(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    e = add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV)
    (arch / e["file"]).unlink()
    build(tmp_path, [e])
    r = import_archive(arch, cfg_for(tmp_path), tmp_path / "out", through=date(2026, 10, 31))
    assert r.bad_hash == [f"{e['file']} (missing)"] and r.bundles == []


def test_rerouted_to_private(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    e = add(arch, "2026-10-30T14:00:00+00:00", PUB, VOTER)
    build(tmp_path, [e])
    out = tmp_path / "out"
    r = import_archive(arch, cfg_for(tmp_path), out, through=date(2026, 10, 31))
    assert r.rerouted == ["pub/20261030T140000Z__p.csv"]
    assert not (out / "public").exists()
    assert (out / "private/import-2026-10-30.tar.gz").exists()


def test_import_twice_is_identical(tmp_path):
    arch, cfg = standard(tmp_path), cfg_for(tmp_path)
    for n in ("o1", "o2"):
        import_archive(arch, cfg, tmp_path / n, through=date(2026, 10, 31))
    names = sorted(p.name for p in (tmp_path / "o1").rglob("*.tar.gz"))
    assert len(names) == 3
    for p in (tmp_path / "o1").rglob("*.tar.gz"):
        other = tmp_path / "o2" / p.relative_to(tmp_path / "o1")
        assert p.read_bytes() == other.read_bytes()


def test_ignores_non_saved_entries(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    junk = [
        {"ts": "2026-10-30T10:00:00+00:00", "url": "https://zzz.gov/", "result": r}
        for r in ("unchanged", "http_error", "soft_404", "exception")
    ]
    build(tmp_path, [*junk, "", add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV)])
    r = import_archive(arch, cfg_for(tmp_path), tmp_path / "out", through=date(2026, 10, 31))
    assert r.files == 1 and r.unmatched == []


def test_nonempty_output_refused(tmp_path):
    arch, cfg, out = standard(tmp_path), cfg_for(tmp_path), tmp_path / "out"
    (out / "public").mkdir(parents=True)
    (out / "public/keep").write_text("x")
    with pytest.raises(FileExistsError):
        import_archive(arch, cfg, out, through=date(2026, 10, 31))
    assert (out / "public/keep").exists()


def test_cli_success_and_failure(tmp_path, capsys):
    arch = standard(tmp_path)
    root = write_config(tmp_path, SOURCES)
    out = tmp_path / "out"
    args = ["import-archive", str(arch), "--out", str(out), "--config", str(root)]
    assert cli.main([*args, "--through", "2026-10-31"]) == 0
    text = capsys.readouterr().out
    assert "3 files" in text and "import-2026-10-30.tar.gz" in text
    bad = tmp_path / "bad"
    bad.mkdir()
    other = "https://zzz.gov/q.csv"
    build(tmp_path, [add(bad, "2026-10-30T14:00:00+00:00", other, CSV)], "bad")
    assert (
        cli.main(
            [
                "import-archive",
                str(bad),
                "--out",
                str(tmp_path / "o2"),
                "--config",
                str(root),
                "--through",
                "2026-10-31",
            ]
        )
        == 1
    )
    cap = capsys.readouterr()
    assert other in cap.out + cap.err
    assert cli.main([*args, "--through", "2026-10-31"]) == 1
