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


def test_html_entry_with_missing_file_is_skipped(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    ok = add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV)
    html = add(arch, "2026-10-30T14:01:00+00:00", "https://a.gov/p.csv?x=1", b"<html>")
    html["content_type"] = "Text/HTML; charset=utf-8"
    (arch / html["file"]).unlink()
    build(tmp_path, [ok, html])
    out = tmp_path / "out"
    r = import_archive(arch, cfg_for(tmp_path), out, through=date(2026, 10, 31))
    assert r.skipped_html == ["https://a.gov/p.csv?x=1"]
    assert r.bad_hash == [] and r.unmatched == []
    assert r.files == 1 and len(r.bundles) == 1


@pytest.mark.parametrize("body", [b"<html>", b"\xef\xbb\xbf \r\n\t<!DOCTYPE html><html>"])
def test_html_entry_with_an_html_body_is_skipped(tmp_path, body):
    arch = tmp_path / "arch"
    arch.mkdir()
    ok = add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV)
    html = add(arch, "2026-10-30T14:01:00+00:00", "https://zzz.gov/gone", body)
    html["content_type"] = "text/html"
    build(tmp_path, [ok, html])
    r = import_archive(arch, cfg_for(tmp_path), tmp_path / "out", through=date(2026, 10, 31))
    assert r.skipped_html == ["https://zzz.gov/gone"] and r.unmatched == [] and r.files == 1


def test_html_typed_entry_holding_csv_is_imported(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    e = add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV, content_type="TEXT/HTML; charset=utf-8")
    build(tmp_path, [e])
    out = tmp_path / "out"
    r = import_archive(arch, cfg_for(tmp_path), out, through=date(2026, 10, 31))
    assert r.skipped_html == [] and r.files == 1 and r.bad_hash == []
    info = read_info(out / "public/import-2026-10-30.json")
    assert info.files[0].headers["content-type"] == "TEXT/HTML; charset=utf-8"


def test_html_typed_entry_outside_the_archive_is_not_read(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    outside = tmp_path / "outside.gz"
    outside.write_bytes(gzip.compress(b"<html>", mtime=0))
    e = add(arch, "2026-10-30T14:00:00+00:00", PUB, b"<html>", content_type="text/html")
    e["file"] = "../outside.gz"
    build(tmp_path, [e])
    r = run(arch, tmp_path)
    assert r.skipped_html == [] and r.bad_hash == ["../outside.gz"]


def test_cli_lists_html_skips(tmp_path, capsys):
    arch = tmp_path / "arch"
    arch.mkdir()
    ok = add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV)
    pages = [
        add(
            arch,
            f"2026-10-30T14:0{n}:00+00:00",
            "https://zzz.gov/gone",
            b"<p>",
            content_type="text/html",
        )
        for n in (1, 2)
    ]
    build(tmp_path, [ok, *pages])
    root = write_config(tmp_path, SOURCES)
    argv = ["import-archive", str(arch), "--out", str(tmp_path / "out"), "--config", str(root)]
    assert cli.main([*argv, "--through", "2026-10-31"]) == 0
    out = capsys.readouterr().out
    assert "2 skipped as html" in out
    assert out.count("skipped as html: https://zzz.gov/gone") == 1


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


def run(arch, tmp_path):
    return import_archive(arch, cfg_for(tmp_path), tmp_path / "out", through=date(2026, 10, 31))


def test_corrupt_gzip_body(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    e = add(arch, "2026-10-30T14:00:00+00:00", PUB, bytes(range(256)) * 40)
    path = arch / e["file"]
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    path.write_bytes(bytes(raw))
    build(tmp_path, [e])
    r = run(arch, tmp_path)
    assert r.bad_hash == [f"{e['file']} (unreadable)"] and r.bundles == []
    assert not (tmp_path / "out/public").exists()


@pytest.mark.parametrize("kind", ["absolute", "parent"])
def test_path_escaping_archive(tmp_path, kind):
    arch = tmp_path / "arch"
    arch.mkdir()
    e = add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV)
    target = arch / e["file"]
    outside = tmp_path / "outside.gz"
    outside.write_bytes(target.read_bytes())
    e["file"] = str(outside) if kind == "absolute" else "../outside.gz"
    build(tmp_path, [e])
    r = run(arch, tmp_path)
    assert r.bad_hash == [e["file"]] and r.bundles == []


def test_torn_final_line_is_skipped(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    good = json.dumps(add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV))
    (arch / "_log.jsonl").write_text(good + '\n{"ts": "2026-10-30T1', encoding="utf-8")
    r = run(arch, tmp_path)
    assert r.files == 1 and r.bad_hash == []


def test_bad_json_line_names_line_number(tmp_path, capsys):
    arch = tmp_path / "arch"
    arch.mkdir()
    good = json.dumps(add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV))
    (arch / "_log.jsonl").write_text(f"{good}\n{{oops\n{good}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="log line 2"):
        run(arch, tmp_path)
    root = write_config(tmp_path / "c2", SOURCES)
    out = str(tmp_path / "o")
    args = ["import-archive", str(arch), "--out", out, "--config", str(root)]
    code = cli.main([*args, "--through", "2026-10-31"])
    assert code == 1 and "log line 2" in capsys.readouterr().err


def test_malformed_saved_entries(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    ok = add(arch, "2026-10-30T14:00:00+00:00", PUB, CSV)
    entries = [
        {k: v for k, v in ok.items() if k != "file"},
        {k: v for k, v in ok.items() if k != "sha256"},
        {k: v for k, v in ok.items() if k != "url"},
        [1, 2],
        "text",
        {**ok, "file": 5},
    ]
    build(tmp_path, [json.dumps(e) for e in entries])
    r = run(arch, tmp_path)
    assert r.bad_hash == [f"log line {i}" for i in range(1, 7)] and r.bundles == []


def test_cli_rejects_partial_day(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 11, 1))
    arch = standard(tmp_path)
    root = write_config(tmp_path, SOURCES)
    base = ["import-archive", str(arch), "--config", str(root)]
    for extra in (["--through", "2026-11-01"], ["--through", "2026-11-05"]):
        assert cli.main([*base, "--out", str(tmp_path / "o"), *extra]) == 1
        assert "not a finished UTC day" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()
    assert cli.main([*base, "--out", str(tmp_path / "o")]) == 0


def test_unmatched_and_ambiguous_listed_once(tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    other, other2 = "https://zzz.gov/q.csv", "https://zzz.gov/r.csv"
    entries = [
        add(arch, "2026-10-30T14:00:00+00:00", other, CSV),
        add(arch, "2026-10-30T15:00:00+00:00", other2, CSV),
        add(arch, "2026-10-30T16:00:00+00:00", other, CSV + b"1\n"),
    ]
    build(tmp_path, entries)
    assert run(arch, tmp_path).unmatched == [other, other2]
    arch2 = tmp_path / "arch2"
    arch2.mkdir()
    build(
        tmp_path,
        [
            add(arch2, "2026-10-30T14:00:00+00:00", PUB, CSV),
            add(arch2, "2026-10-30T15:00:00+00:00", PUB, CSV + b"1\n"),
        ],
        "arch2",
    )
    cfg = load_config(write_config(tmp_path / "amb", SOURCES.replace(PRIV, PUB)))
    r = import_archive(arch2, cfg, tmp_path / "out2", through=date(2026, 10, 31))
    assert r.ambiguous == [PUB]


def test_disk_write_errors_propagate(tmp_path, monkeypatch):
    arch = standard(tmp_path)
    real = Path.open

    def failing(self, mode="r", *a, **kw):
        if "work" in self.parts and "w" in mode:
            raise OSError(28, "No space left on device")
        return real(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", failing)
    with pytest.raises(OSError, match="No space"):
        run(arch, tmp_path)
    assert not (tmp_path / "out/work").exists()
