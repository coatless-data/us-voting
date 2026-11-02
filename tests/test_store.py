"""Tests for us_voting.store: release storage, atomic replace and recovery."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from us_voting import store
from us_voting.config import StoreConfig


def test_temporary_name_helpers_round_trip():
    assert store.next_name("counts.csv", "2026-09-15T1817Z", 1) == (
        "counts.csv.next-2026-09-15T1817Z-1"
    )
    assert store.old_name("counts.csv", "2026-09-15T1817Z") == ("counts.csv.old-2026-09-15T1817Z")
    assert store.split_temp_name("counts.csv.next-2026-09-15T1817Z-1") == (
        "counts.csv",
        "next",
    )
    assert store.split_temp_name("us-voting-all.parquet.old-run-2026-10-01T0300Z") == (
        "us-voting-all.parquet",
        "old",
    )
    assert store.split_temp_name("us-voting-2026-09-15.csv.gz") is None


def test_sha256_label_is_the_github_digest_format(tmp_path: Path):
    path = tmp_path / "rows.csv"
    path.write_bytes(b"capture_id,price\n2026-09-15T1817Z,3.999\n")
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert store.sha256_file(path) == expected
    assert store.sha256_label(path) == f"sha256:{expected}"


def test_ensure_release_creates_once_and_then_returns_it_untouched(tmp_path: Path):
    s = store.LocalReleaseStore(tmp_path / "releases")
    assert s.get_release("raw-2025-12") is None

    created = s.ensure_release("raw-2025-12", "Older month", "files", False, "true")
    assert created.tag == "raw-2025-12"
    assert created.title == "Older month"
    assert created.body == "files"
    assert created.prerelease is False
    assert created.make_latest == "true"
    assert created.is_latest is True
    assert created.immutable is False

    again = s.ensure_release("raw-2025-12", "other title", "other body", True, "false")
    assert again.title == "Older month"
    assert again.prerelease is False
    assert s.get_latest().tag == "raw-2025-12"


def test_update_release_and_list_releases(tmp_path: Path):
    s = store.LocalReleaseStore(tmp_path / "releases")
    s.ensure_release("raw-2025-12", "Older month", "b", False, "true")
    s.ensure_release("raw-2026-09", "September 2026", "b", True, "false")

    assert [r.tag for r in s.list_releases()] == ["raw-2025-12", "raw-2026-09"]

    closed = s.update_release("raw-2026-09", prerelease=False, body="closed month")
    assert closed.prerelease is False
    assert closed.body == "closed month"
    assert s.get_release("raw-2026-09").prerelease is False
    assert s.get_release("raw-2026-09").title == "September 2026"

    with pytest.raises(store.StorageError, match="release not found"):
        s.update_release("raw-2030-01", body="nope")


def test_is_latest_tracks_the_latest_marker(tmp_path: Path):
    # `make_latest` is the value a write asked for; `is_latest` is what the
    # store answers now for "which release is Latest?". Callers read
    # `is_latest` to decide whether the Latest pointer has drifted, so both
    # stores have to populate it.
    s = store.LocalReleaseStore(tmp_path / "releases")
    s.ensure_release("raw-2025-12", "Older month", "b", False, "true")
    s.ensure_release("raw-2026-09", "September 2026", "b", True, "false")

    assert s.get_release("raw-2025-12").is_latest is True
    assert s.get_release("raw-2026-09").is_latest is False
    assert {r.tag: r.is_latest for r in s.list_releases()} == {
        "raw-2025-12": True,
        "raw-2026-09": False,
    }

    moved = s.update_release("raw-2026-09", make_latest="true")

    assert moved.is_latest is True
    assert s.get_release("raw-2025-12").is_latest is False
    assert s.get_latest().tag == "raw-2026-09"


def test_ensure_release_refuses_an_immutable_release(tmp_path: Path):
    # Immutable releases must be disabled for the repository: a release that
    # cannot be edited can never receive a new daily file, so refuse loudly
    # instead of failing halfway through a publish.
    root = tmp_path / "releases"
    s = store.LocalReleaseStore(root)
    s.ensure_release("raw-2025-12", "Older month", "b", False, "true")

    sidecar = root / "raw-2025-12" / "_release.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["immutable"] = True
    sidecar.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(store.StorageError, match="immutable release: raw-2025-12"):
        s.ensure_release("raw-2025-12", "Older month", "b", False, "true")


def _seed(tmp_path: Path) -> tuple[store.LocalReleaseStore, Path]:
    root = tmp_path / "releases"
    # No real waiting: download retries a stale read on a backoff.
    s = store.LocalReleaseStore(root, sleep=lambda _seconds: None)
    s.ensure_release("raw-2026-09", "September 2026", "b", True, "false")
    return s, root


def test_upload_new_records_digest_size_and_state(tmp_path: Path):
    s, root = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"capture_id,price\n2026-09-15T1817Z,3.999\n")

    asset = s.upload_new("raw-2026-09", src, "us-voting-2026-09-15.csv.gz")
    assert asset.name == "us-voting-2026-09-15.csv.gz"
    assert asset.state == "uploaded"
    assert asset.size == src.stat().st_size
    assert asset.digest == store.sha256_label(src)
    assert asset.label is None

    listed = s.list_assets("raw-2026-09")
    assert [a.name for a in listed] == ["us-voting-2026-09-15.csv.gz"]
    assert listed[0].id == asset.id
    assert (root / "raw-2026-09" / "us-voting-2026-09-15.csv.gz").read_bytes() == (src.read_bytes())


def test_upload_new_keeps_an_explicit_label(tmp_path: Path):
    s, _ = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"rows-v1\n")
    asset = s.upload_new("raw-2026-09", src, "rows.csv.next-tok-1", label="sha256:abc")
    assert asset.label == "sha256:abc"


def test_upload_new_on_a_duplicate_name_reports_422(tmp_path: Path):
    s, _ = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"rows-v1\n")
    s.upload_new("raw-2026-09", src, "rows.csv")
    with pytest.raises(store.StorageError) as excinfo:
        s.upload_new("raw-2026-09", src, "rows.csv")
    assert excinfo.value.status == 422


def test_list_assets_raises_when_the_release_is_missing(tmp_path: Path):
    # A missing release must never look like "no assets": a wrong store spec
    # or a stale path would otherwise present exactly like "never published"
    # to `download`, which reads its listing through `list_assets`.
    s, _ = _seed(tmp_path)
    with pytest.raises(store.StorageError, match="release not found: raw-2030-01"):
        s.list_assets("raw-2030-01")


def test_created_at_is_strictly_increasing(tmp_path: Path):
    s, _ = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"rows-v1\n")
    first = s.upload_new("raw-2026-09", src, "a.csv")
    second = s.upload_new("raw-2026-09", src, "b.csv")
    assert second.created_at > first.created_at


def test_download_writes_the_bytes_and_creates_parents(tmp_path: Path):
    s, _ = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"rows-v1\n")
    s.upload_new("raw-2026-09", src, "rows.csv")

    dest = s.download("raw-2026-09", "rows.csv", tmp_path / "out" / "nested" / "d.csv")
    assert dest.read_bytes() == b"rows-v1\n"


def test_download_raises_asset_not_found_when_the_asset_is_missing(tmp_path: Path):
    s, _ = _seed(tmp_path)
    with pytest.raises(store.AssetNotFound):
        s.download("raw-2026-09", "rows.csv", tmp_path / "d.csv")


def test_download_propagates_release_not_found_instead_of_asset_not_found(
    tmp_path: Path,
):
    # A missing release must never present as "this file was never
    # published": callers treat AssetNotFound as "first publish" and would
    # otherwise start from empty and destroy history for what is really a
    # wrong store spec or a stale path.
    s, _ = _seed(tmp_path)
    with pytest.raises(store.StorageError, match="release not found: raw-2030-01"):
        s.download("raw-2030-01", "rows.csv", tmp_path / "d.csv")


def test_download_raises_storage_error_when_only_temporaries_exist(tmp_path: Path):
    # A missing <name> with a .next-* present is an interrupted replace, not a
    # file that was never published. Callers treat AssetNotFound as "first
    # publish" and would start from an empty frame, which would lose history.
    s, _ = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"rows-v1\n")
    s.upload_new("raw-2026-09", src, "rows.csv.next-tok-1", label=store.sha256_label(src))
    with pytest.raises(store.StorageError, match="recovery"):
        s.download("raw-2026-09", "rows.csv", tmp_path / "d.csv")


def test_download_detects_a_corrupted_asset(tmp_path: Path):
    s, root = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"rows-v1\n")
    s.upload_new("raw-2026-09", src, "rows.csv")
    # Same length, different bytes: the size still matches, the digest does not.
    (root / "raw-2026-09" / "rows.csv").write_bytes(b"rows-XX\n")

    with pytest.raises(store.StorageError, match="digest mismatch"):
        s.download("raw-2026-09", "rows.csv", tmp_path / "d.csv")


def test_download_retries_a_stale_read_and_returns_the_settled_bytes(tmp_path: Path):
    """An asset is replaced by name, and its two halves settle apart.

    For a few seconds the public download can still serve the bytes that were
    replaced while the listing already describes the new ones -- so publish
    rewriting a manifest and close-periods reading it moments later failed on a
    real run. The bytes must still verify; asking only once is what was wrong.
    """
    s, _ = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"rows-v2-longer\n")
    s.upload_new("raw-2026-09", src, "rows.csv")

    calls = {"n": 0}
    real_fetch = s._fetch_asset

    def fetch(tag, asset, dest):
        calls["n"] += 1
        if calls["n"] == 1:
            # The listing already describes v2; the download still hands back v1.
            Path(dest).write_bytes(b"rows-v1\n")
            return Path(dest)
        return real_fetch(tag, asset, dest)

    s._fetch_asset = fetch
    out = s.download("raw-2026-09", "rows.csv", tmp_path / "d.csv")

    assert out.read_bytes() == b"rows-v2-longer\n"
    assert calls["n"] == 2


def test_download_gives_up_when_the_bytes_never_settle(tmp_path: Path):
    """Retrying must not soften the guarantee: wrong bytes are never returned."""
    s, _ = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"rows-v2-longer\n")
    s.upload_new("raw-2026-09", src, "rows.csv")

    calls = {"n": 0}

    def fetch(tag, asset, dest):
        calls["n"] += 1
        Path(dest).write_bytes(b"rows-v1\n")
        return Path(dest)

    s._fetch_asset = fetch
    with pytest.raises(store.StorageError, match="size mismatch"):
        s.download("raw-2026-09", "rows.csv", tmp_path / "d.csv")
    assert calls["n"] == len(store.STALE_READ_RETRY_DELAYS) + 1


def test_rename_can_clear_the_label_and_delete_removes_the_asset(tmp_path: Path):
    s, root = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"rows-v1\n")
    uploaded = s.upload_new("raw-2026-09", src, "rows.csv.next-tok-1", label="sha256:abc")

    renamed = s.rename("raw-2026-09", uploaded.id, "rows.csv", label="")
    assert renamed.name == "rows.csv"
    assert renamed.label is None
    assert renamed.created_at == uploaded.created_at
    assert (root / "raw-2026-09" / "rows.csv").exists()
    assert not (root / "raw-2026-09" / "rows.csv.next-tok-1").exists()

    s.delete("raw-2026-09", renamed.id)
    assert s.list_assets("raw-2026-09") == []
    with pytest.raises(store.StorageError):
        s.delete("raw-2026-09", renamed.id)


def test_rename_onto_an_existing_name_reports_422(tmp_path: Path):
    s, _ = _seed(tmp_path)
    src = tmp_path / "rows.csv"
    src.write_bytes(b"rows-v1\n")
    s.upload_new("raw-2026-09", src, "rows.csv")
    other = s.upload_new("raw-2026-09", src, "rows.csv.next-tok-1")
    with pytest.raises(store.StorageError) as excinfo:
        s.rename("raw-2026-09", other.id, "rows.csv")
    assert excinfo.value.status == 422


def test_read_resolved_prefers_the_plain_name(tmp_path: Path):
    s, _ = _seed(tmp_path)
    live = tmp_path / "live.csv"
    live.write_bytes(b"live\n")
    stale = tmp_path / "stale.csv"
    stale.write_bytes(b"stale\n")
    s.upload_new("raw-2026-09", live, "counts.csv")
    s.upload_new(
        "raw-2026-09",
        stale,
        "counts.csv.next-tok-1",
        label=store.sha256_label(stale),
    )

    dest = s.read_resolved("raw-2026-09", "counts.csv", tmp_path / "r.csv")
    assert dest.read_bytes() == b"live\n"


def test_read_resolved_takes_the_newest_verifying_next(tmp_path: Path):
    s, _ = _seed(tmp_path)
    good = tmp_path / "good.csv"
    good.write_bytes(b"good\n")
    torn = tmp_path / "torn.csv"
    torn.write_bytes(b"torn\n")

    s.upload_new("raw-2026-09", good, "counts.csv.next-tok-1", label=store.sha256_label(good))
    # Uploaded later, but its label does not describe its bytes: a torn upload.
    s.upload_new("raw-2026-09", torn, "counts.csv.next-tok-2", label="sha256:" + "0" * 64)

    dest = s.read_resolved("raw-2026-09", "counts.csv", tmp_path / "r.csv")
    assert dest.read_bytes() == b"good\n"


def test_read_resolved_falls_back_to_the_newest_old(tmp_path: Path):
    s, _ = _seed(tmp_path)
    older = tmp_path / "older.csv"
    older.write_bytes(b"older\n")
    newer = tmp_path / "newer.csv"
    newer.write_bytes(b"newer\n")
    torn = tmp_path / "torn.csv"
    torn.write_bytes(b"torn\n")

    s.upload_new("raw-2026-09", older, "counts.csv.old-tok-a")
    s.upload_new("raw-2026-09", newer, "counts.csv.old-tok-b")
    s.upload_new("raw-2026-09", torn, "counts.csv.next-tok-1", label="sha256:" + "0" * 64)

    dest = s.read_resolved("raw-2026-09", "counts.csv", tmp_path / "r.csv")
    assert dest.read_bytes() == b"newer\n"


def test_read_resolved_raises_when_nothing_resolves(tmp_path: Path):
    s, _ = _seed(tmp_path)
    with pytest.raises(store.AssetNotFound):
        s.read_resolved("raw-2026-09", "counts.csv", tmp_path / "r.csv")
    with pytest.raises(store.AssetNotFound):
        s.read_resolved("raw-2030-01", "counts.csv", tmp_path / "r.csv")


def test_read_resolved_never_modifies_the_release(tmp_path: Path):
    s, _ = _seed(tmp_path)
    good = tmp_path / "good.csv"
    good.write_bytes(b"good\n")
    s.upload_new("raw-2026-09", good, "counts.csv.next-tok-1", label=store.sha256_label(good))
    before = [(a.name, a.id, a.label) for a in s.list_assets("raw-2026-09")]

    s.read_resolved("raw-2026-09", "counts.csv", tmp_path / "r.csv")

    assert [(a.name, a.id, a.label) for a in s.list_assets("raw-2026-09")] == before


class FakeClock:
    """A monotonic clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def test_replace_atomic_creates_the_asset_when_it_is_absent(tmp_path: Path):
    s, _ = _seed(tmp_path)
    src = tmp_path / "counts.csv"
    src.write_bytes(b"counts-v1\n")

    asset = s.replace_atomic("raw-2026-09", src, "counts.csv", "2026-09-15T1817Z")

    assert asset.name == "counts.csv"
    assert asset.label is None  # the sha256: label is cleared on promotion
    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]
    got = s.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-v1\n"


def test_replace_atomic_swaps_the_content_and_removes_the_old_copy(tmp_path: Path):
    s, _ = _seed(tmp_path)
    v1 = tmp_path / "v1.csv"
    v1.write_bytes(b"counts-v1\n")
    v2 = tmp_path / "v2.csv"
    v2.write_bytes(b"counts-v2\n")
    s.upload_new("raw-2026-09", v1, "counts.csv")

    s.replace_atomic("raw-2026-09", v2, "counts.csv", "2026-09-15T1817Z")

    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]
    got = s.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-v2\n"


def test_replace_atomic_never_reuses_a_temporary_name(tmp_path: Path):
    s, _ = _seed(tmp_path)
    v1 = tmp_path / "v1.csv"
    v1.write_bytes(b"counts-v1\n")
    v2 = tmp_path / "v2.csv"
    v2.write_bytes(b"counts-v2\n")
    leftover = tmp_path / "leftover.csv"
    leftover.write_bytes(b"leftover\n")

    s.upload_new("raw-2026-09", v1, "counts.csv")
    s.upload_new(
        "raw-2026-09",
        leftover,
        "counts.csv.next-tok-1",
        label=store.sha256_label(leftover),
    )

    s.replace_atomic("raw-2026-09", v2, "counts.csv", "tok")

    names = sorted(a.name for a in s.list_assets("raw-2026-09"))
    assert names == ["counts.csv", "counts.csv.next-tok-1"]
    got = s.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-v2\n"


def test_replace_atomic_deletes_an_upload_that_never_verifies(tmp_path: Path):
    class CorruptingStore(store.LocalReleaseStore):
        """Records a wrong digest for every upload, like a torn transfer."""

        def upload_new(self, tag, path, name, label=None):
            asset = super().upload_new(tag, path, name, label=label)
            data = self._read(tag)
            data["assets"][name]["digest"] = "sha256:" + "0" * 64
            self._write(tag, data)
            return asset

    root = tmp_path / "releases"
    seeder = store.LocalReleaseStore(root)
    seeder.ensure_release("raw-2026-09", "September 2026", "b", True, "false")
    v1 = tmp_path / "v1.csv"
    v1.write_bytes(b"counts-v1\n")
    seeder.upload_new("raw-2026-09", v1, "counts.csv")

    clock = FakeClock()
    s = CorruptingStore(root, sleep=clock.sleep, monotonic=clock.now)
    v2 = tmp_path / "v2.csv"
    v2.write_bytes(b"counts-v2\n")

    with pytest.raises(store.StorageError, match="did not verify"):
        s.replace_atomic("raw-2026-09", v2, "counts.csv", "tok")

    assert len(clock.slept) == 60  # polled once a second for 60 seconds
    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]
    got = seeder.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-v1\n"


def _set_state(root: Path, tag: str, name: str, state: str) -> None:
    """Force an asset's state in the sidecar, as a half-finished upload looks."""
    sidecar = root / tag / "_release.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["assets"][name]["state"] = state
    sidecar.write_text(json.dumps(data), encoding="utf-8")


def _write(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def test_recover_deletes_leftovers_when_the_live_name_is_present(tmp_path: Path):
    # Crash after step 1 of replace_atomic: <name> is still live and a verified
    # .next-* is sitting next to it.
    s, _ = _seed(tmp_path)
    v1 = _write(tmp_path, "v1.csv", b"counts-v1\n")
    v2 = _write(tmp_path, "v2.csv", b"counts-v2\n")
    s.upload_new("raw-2026-09", v1, "counts.csv")
    s.upload_new("raw-2026-09", v2, "counts.csv.next-tok-1", label=store.sha256_label(v2))

    actions = store.recover_temporaries(s, "raw-2026-09")

    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]
    assert actions == ["deleted-leftover:counts.csv.next-tok-1"]
    got = s.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-v1\n"


def test_recover_promotes_the_next_when_the_live_name_is_gone(tmp_path: Path):
    # Crash between step 3 and step 4: <name> was renamed away, the new upload
    # verifies, so it becomes <name> and the old copy is dropped.
    s, _ = _seed(tmp_path)
    v1 = _write(tmp_path, "v1.csv", b"counts-v1\n")
    v2 = _write(tmp_path, "v2.csv", b"counts-v2\n")
    s.upload_new("raw-2026-09", v1, "counts.csv.old-tok")
    s.upload_new("raw-2026-09", v2, "counts.csv.next-tok-1", label=store.sha256_label(v2))

    actions = store.recover_temporaries(s, "raw-2026-09")

    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]
    assert actions == [
        "promoted:counts.csv.next-tok-1",
        "deleted-leftover:counts.csv.old-tok",
    ]
    assert s.list_assets("raw-2026-09")[0].label is None
    got = s.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-v2\n"


def test_recover_deletes_the_old_copy_when_the_promotion_already_happened(
    tmp_path: Path,
):
    # Crash between step 4 and step 5.
    s, _ = _seed(tmp_path)
    v1 = _write(tmp_path, "v1.csv", b"counts-v1\n")
    v2 = _write(tmp_path, "v2.csv", b"counts-v2\n")
    s.upload_new("raw-2026-09", v2, "counts.csv")
    s.upload_new("raw-2026-09", v1, "counts.csv.old-tok")

    actions = store.recover_temporaries(s, "raw-2026-09")

    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]
    assert actions == ["deleted-leftover:counts.csv.old-tok"]
    got = s.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-v2\n"


def test_recover_restores_the_old_copy_when_no_next_verifies(tmp_path: Path):
    s, _ = _seed(tmp_path)
    v1 = _write(tmp_path, "v1.csv", b"counts-v1\n")
    torn = _write(tmp_path, "torn.csv", b"counts-XX\n")
    s.upload_new("raw-2026-09", v1, "counts.csv.old-tok")
    s.upload_new("raw-2026-09", torn, "counts.csv.next-tok-1", label="sha256:" + "0" * 64)

    actions = store.recover_temporaries(s, "raw-2026-09")

    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]
    assert actions == [
        "restored:counts.csv.old-tok",
        "deleted-leftover:counts.csv.next-tok-1",
    ]
    got = s.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-v1\n"


def test_recover_deletes_temporaries_that_never_finished_uploading(tmp_path: Path):
    s, root = _seed(tmp_path)
    v1 = _write(tmp_path, "v1.csv", b"counts-v1\n")
    half = _write(tmp_path, "half.csv", b"counts-v2\n")
    s.upload_new("raw-2026-09", v1, "counts.csv.old-tok")
    s.upload_new("raw-2026-09", half, "counts.csv.next-tok-1", label=store.sha256_label(half))
    _set_state(root, "raw-2026-09", "counts.csv.next-tok-1", "starter")

    actions = store.recover_temporaries(s, "raw-2026-09")

    assert actions == [
        "deleted-incomplete:counts.csv.next-tok-1",
        "restored:counts.csv.old-tok",
    ]
    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]
    got = s.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-v1\n"


def test_recover_takes_the_newest_verifying_next_and_clears_the_rest(tmp_path: Path):
    s, _ = _seed(tmp_path)
    old_try = _write(tmp_path, "a.csv", b"counts-v2\n")
    new_try = _write(tmp_path, "b.csv", b"counts-v3\n")
    previous = _write(tmp_path, "c.csv", b"counts-v1\n")
    s.upload_new("raw-2026-09", previous, "counts.csv.old-tok")
    s.upload_new(
        "raw-2026-09",
        old_try,
        "counts.csv.next-tok-1",
        label=store.sha256_label(old_try),
    )
    s.upload_new(
        "raw-2026-09",
        new_try,
        "counts.csv.next-tok-2",
        label=store.sha256_label(new_try),
    )

    store.recover_temporaries(s, "raw-2026-09")

    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]
    got = s.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-v3\n"


def test_recover_prefers_a_newer_old_over_a_stale_verifying_next(tmp_path: Path):
    # replace_atomic deliberately never deletes a foreign-token leftover, so a
    # self-consistent .next from an earlier, abandoned run can still verify
    # even though a later run already produced a newer .old. The newer one
    # must win by created_at -- not "any verifying .next beats any .old" --
    # or recovery would resurrect stale content over the run that came after.
    s, _ = _seed(tmp_path)
    stale = _write(tmp_path, "stale.csv", b"counts-stale\n")
    newer = _write(tmp_path, "newer.csv", b"counts-newer\n")
    s.upload_new(
        "raw-2026-09",
        stale,
        "counts.csv.next-stale-1",
        label=store.sha256_label(stale),
    )
    s.upload_new("raw-2026-09", newer, "counts.csv.old-tok")

    actions = store.recover_temporaries(s, "raw-2026-09")

    assert actions == [
        "restored:counts.csv.old-tok",
        "deleted-leftover:counts.csv.next-stale-1",
    ]
    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]
    got = s.download("raw-2026-09", "counts.csv", tmp_path / "got.csv")
    assert got.read_bytes() == b"counts-newer\n"


def test_recover_handles_several_names_and_a_closed_release(tmp_path: Path):
    s, _ = _seed(tmp_path)
    s.update_release("raw-2026-09", prerelease=False)  # a closed month
    rows = _write(tmp_path, "rows.csv", b"rows-v1\n")
    manifest = _write(tmp_path, "m.json", b"{}\n")
    s.upload_new("raw-2026-09", rows, "us-voting-2026-09-15.csv.gz")
    s.upload_new(
        "raw-2026-09",
        rows,
        "us-voting-2026-09-15.csv.gz.next-tok-1",
        label=store.sha256_label(rows),
    )
    s.upload_new(
        "raw-2026-09",
        manifest,
        "manifest-2026-09.json.next-tok-1",
        label=store.sha256_label(manifest),
    )

    store.recover_temporaries(s, "raw-2026-09")

    assert sorted(a.name for a in s.list_assets("raw-2026-09")) == [
        "manifest-2026-09.json",
        "us-voting-2026-09-15.csv.gz",
    ]
    assert s.get_release("raw-2026-09").prerelease is False


def test_recover_is_a_no_op_without_temporaries(tmp_path: Path):
    s, _ = _seed(tmp_path)
    rows = _write(tmp_path, "rows.csv", b"rows-v1\n")
    s.upload_new("raw-2026-09", rows, "rows.csv")
    assert store.recover_temporaries(s, "raw-2026-09") == []


def _seed_four_releases(tmp_path: Path) -> store.LocalReleaseStore:
    s = store.LocalReleaseStore(tmp_path / "releases")
    s.ensure_release("raw-2025-12", "Older month", "b", False, "true")
    s.ensure_release("raw-2026-09", "September 2026", "b", True, "false")
    s.ensure_release("raw-2026-10", "2026", "b", False, "false")
    s.ensure_release("notes", "not a raw release", "b", False, "false")
    return s


def test_recovery_tags_lists_every_raw_release(tmp_path: Path):
    s = _seed_four_releases(tmp_path)
    assert store.recovery_tags(s) == ["raw-2025-12", "raw-2026-09", "raw-2026-10"]


def test_recover_temporaries_over_recovery_tags_leaves_other_releases_alone(
    tmp_path: Path,
):
    s = _seed_four_releases(tmp_path)
    payload = _write(tmp_path, "p.csv", b"p\n")
    label = store.sha256_label(payload)
    s.upload_new("raw-2025-12", payload, "counts.csv.old-tok")
    s.upload_new("raw-2026-10", payload, "us-voting-2026.parquet.next-tok-1", label=label)
    s.upload_new("notes", payload, "readme.txt.old-tok")

    actions = {tag: store.recover_temporaries(s, tag) for tag in store.recovery_tags(s)}

    assert sorted(tag for tag, done in actions.items() if done) == [
        "raw-2025-12",
        "raw-2026-10",
    ]
    assert actions["raw-2026-09"] == []
    assert [a.name for a in s.list_assets("raw-2025-12")] == ["counts.csv"]
    assert [a.name for a in s.list_assets("raw-2026-10")] == ["us-voting-2026.parquet"]
    # `notes` is not a raw release, so recovery never looked at it.
    assert [a.name for a in s.list_assets("notes")] == ["readme.txt.old-tok"]


class FakeGitHub:
    """An in-memory stand-in for the GitHub Releases REST API.

    Served through httpx.MockTransport, so no test ever touches the network.
    Asset downloads are answered on a separate host, the way GitHub answers
    them from `browser_download_url`.
    """

    def __init__(self, owner: str = "acme", repo: str = "votes") -> None:
        self.owner = owner
        self.repo = repo
        self.base = f"/repos/{owner}/{repo}"
        self.releases: dict[int, dict] = {}
        self.assets: dict[int, dict] = {}
        self.latest_tag: str | None = None
        self.calls: list[dict] = []
        self.fail_promote: dict[str, int] = {}
        self.stale_downloads: dict[str, bytes] = {}
        self.rate_limit_queue: list[dict] = []
        self.null_digest = False
        self._next_release = 1
        self._next_asset = 1000
        self._clock = datetime(2026, 9, 15, 18, 0, 0, tzinfo=UTC)

    # -- seeding ---------------------------------------------------------
    def add_release(
        self,
        tag: str,
        *,
        prerelease: bool = False,
        immutable: bool = False,
        latest: bool = False,
    ) -> dict:
        release_id = self._next_release
        self._next_release += 1
        self.releases[release_id] = {
            "id": release_id,
            "tag_name": tag,
            "name": tag,
            "body": "",
            "prerelease": prerelease,
            "immutable": immutable,
            "html_url": f"https://example.invalid/{tag}",
        }
        if latest:
            self.latest_tag = tag
        return self.releases[release_id]

    def add_asset(
        self,
        tag: str,
        name: str,
        data: bytes,
        *,
        label: str | None = None,
        state: str = "uploaded",
    ) -> dict:
        release = self._release_by_tag(tag)
        assert release is not None
        return self._store_asset(release["id"], name, data, label=label, state=state)

    def _store_asset(
        self,
        release_id: int,
        name: str,
        data: bytes,
        *,
        label: str | None = None,
        state: str = "uploaded",
    ) -> dict:
        asset_id = self._next_asset
        self._next_asset += 1
        self._clock += timedelta(seconds=1)
        self.assets[asset_id] = {
            "id": asset_id,
            "release_id": release_id,
            "name": name,
            "label": label or "",
            "state": state,
            "body": data,
            "created_at": self._clock.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return self.assets[asset_id]

    def _release_by_tag(self, tag: str | None) -> dict | None:
        for release in self.releases.values():
            if release["tag_name"] == tag:
                return release
        return None

    def asset_names(self, tag: str) -> list[str]:
        release = self._release_by_tag(tag)
        assert release is not None
        return sorted(a["name"] for a in self.assets.values() if a["release_id"] == release["id"])

    def _asset(self, tag: str, name: str) -> dict:
        release = self._release_by_tag(tag)
        assert release is not None
        for asset in self.assets.values():
            if asset["release_id"] == release["id"] and asset["name"] == name:
                return asset
        raise KeyError(name)

    def body_of(self, tag: str, name: str) -> bytes:
        return self._asset(tag, name)["body"]

    def label_of(self, tag: str, name: str) -> str:
        return self._asset(tag, name)["label"]

    def corrupt_body(self, tag: str, name: str, data: bytes) -> None:
        """Overwrite a stored asset's bytes in place, as a torn transfer would.

        The caller is responsible for keeping `len(data)` equal to the
        original body so a size check alone cannot catch the corruption --
        only a digest or a hash of the actual bytes can.
        """
        self._asset(tag, name)["body"] = data

    # -- serialisation ---------------------------------------------------
    def _asset_json(self, asset: dict) -> dict:
        digest = None if self.null_digest else "sha256:" + hashlib.sha256(asset["body"]).hexdigest()
        return {
            "id": asset["id"],
            "name": asset["name"],
            "label": asset["label"],
            "state": asset["state"],
            "size": len(asset["body"]),
            "digest": digest,
            "created_at": asset["created_at"],
            "browser_download_url": f"https://downloads.invalid/assets/{asset['id']}",
        }

    # -- transport -------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "auth": request.headers.get("authorization"),
            }
        )
        path = request.url.path
        if request.url.host == "downloads.invalid":
            asset_id = int(path.rsplit("/", 1)[-1])
            name = self.assets[asset_id]["name"]
            # GitHub serves this by NAME: after a replace it can still answer
            # with the bytes of the asset that held the name before.
            if name in self.stale_downloads:
                return httpx.Response(200, content=self.stale_downloads[name])
            return httpx.Response(200, content=self.assets[asset_id]["body"])
        if request.method in ("POST", "PATCH", "DELETE") and self.rate_limit_queue:
            headers = self.rate_limit_queue.pop(0)
            return httpx.Response(429, headers=headers, json={"message": "rate limited"})

        if request.method == "GET" and path == f"{self.base}/releases/latest":
            release = self._release_by_tag(self.latest_tag)
            if release is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=release)
        if request.method == "GET" and path.startswith(f"{self.base}/releases/tags/"):
            release = self._release_by_tag(path.rsplit("/", 1)[-1])
            if release is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=release)
        if request.method == "GET" and path == f"{self.base}/releases":
            # Newest created first and paginated, the way the real endpoint
            # answers: the repository's oldest release -- `raw-2025-12` -- is the
            # last item, so it is the first thing an unpaginated read loses.
            items = list(reversed(self.releases.values()))
            page = int(request.url.params.get("page", "1"))
            per_page = int(request.url.params.get("per_page", "100"))
            return httpx.Response(200, json=items[(page - 1) * per_page : page * per_page])
        if request.method == "POST" and path == f"{self.base}/releases":
            payload = json.loads(request.content)
            release = self.add_release(
                payload["tag_name"],
                prerelease=bool(payload.get("prerelease")),
                latest=payload.get("make_latest") == "true",
            )
            release["name"] = payload.get("name", "")
            release["body"] = payload.get("body", "")
            return httpx.Response(201, json=release)

        match = re.fullmatch(rf"{re.escape(self.base)}/releases/(\d+)", path)
        if match and request.method == "PATCH":
            release = self.releases[int(match.group(1))]
            payload = json.loads(request.content)
            for key in ("name", "body", "prerelease"):
                if key in payload:
                    release[key] = payload[key]
            if payload.get("make_latest") == "true":
                self.latest_tag = release["tag_name"]
            return httpx.Response(200, json=release)

        match = re.fullmatch(rf"{re.escape(self.base)}/releases/(\d+)/assets", path)
        if match and request.method == "GET":
            release_id = int(match.group(1))
            items = [
                self._asset_json(a) for a in self.assets.values() if a["release_id"] == release_id
            ]
            page = int(request.url.params.get("page", "1"))
            per_page = int(request.url.params.get("per_page", "100"))
            return httpx.Response(200, json=items[(page - 1) * per_page : page * per_page])
        if match and request.method == "POST":
            release_id = int(match.group(1))
            name = request.url.params["name"]
            clash = any(
                a["release_id"] == release_id and a["name"] == name for a in self.assets.values()
            )
            if clash:
                return httpx.Response(422, json={"message": "Validation Failed"})
            asset = self._store_asset(
                release_id, name, request.content, label=request.url.params.get("label")
            )
            return httpx.Response(201, json=self._asset_json(asset))

        match = re.fullmatch(rf"{re.escape(self.base)}/releases/assets/(\d+)", path)
        if match and request.method == "GET":
            # Addressed by id: always this upload's own bytes.
            return httpx.Response(200, content=self.assets[int(match.group(1))]["body"])
        if match and request.method == "PATCH":
            asset = self.assets[int(match.group(1))]
            payload = json.loads(request.content)
            new_name = payload.get("name", asset["name"])
            remaining = self.fail_promote.get(new_name, 0)
            if remaining and asset["name"].startswith(f"{new_name}.next-"):
                self.fail_promote[new_name] = remaining - 1
                return httpx.Response(422, json={"message": "Validation Failed"})
            asset["name"] = new_name
            if "label" in payload:
                asset["label"] = payload["label"] or ""
            return httpx.Response(200, json=self._asset_json(asset))
        if match and request.method == "DELETE":
            self.assets.pop(int(match.group(1)), None)
            return httpx.Response(204)

        return httpx.Response(404, json={"message": f"no route for {request.method} {path}"})


@pytest.fixture
def writer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs-test-token")
    monkeypatch.setenv("US_VOTING_WRITER", "1")


def test_github_reads_need_no_token_and_no_auth_on_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake = FakeGitHub()
    fake.add_release("raw-2025-12", latest=True)
    fake.add_asset("raw-2025-12", "counts.csv", b"county,votes\nWake,12\n")
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())

    release = s.get_release("raw-2025-12")
    assert release is not None
    assert release.tag == "raw-2025-12"
    assert release.is_latest is True
    assert s.get_release("raw-2030-01") is None
    assert s.get_latest().tag == "raw-2025-12"
    assert [a.name for a in s.list_assets("raw-2025-12")] == ["counts.csv"]

    dest = s.download("raw-2025-12", "counts.csv", tmp_path / "counts.csv")
    assert dest.read_bytes() == b"county,votes\nWake,12\n"

    downloads = [c for c in fake.calls if "downloads.invalid" in c["url"]]
    assert len(downloads) == 1
    # browser_download_url is public: it costs no API quota, and the signed
    # redirect target rejects an Authorization header.
    assert downloads[0]["auth"] is None


def test_github_get_release_returns_none_when_the_release_is_missing(writer_env):
    fake = FakeGitHub()
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())
    assert s.get_release("raw-2030-01") is None


def test_github_list_assets_raises_when_the_release_is_missing(writer_env):
    fake = FakeGitHub()
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())
    with pytest.raises(store.StorageError, match="release not found: raw-2030-01"):
        s.list_assets("raw-2030-01")


def test_github_download_propagates_release_not_found_instead_of_asset_not_found(
    tmp_path: Path, writer_env
):
    # A 404 on the release itself -- a wrong repo, a renamed release, a token
    # that cannot see it -- must never look like "this file was never
    # published", or publish logic would happily start writing fresh history.
    fake = FakeGitHub()
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())
    with pytest.raises(store.StorageError, match="release not found: raw-2030-01"):
        s.download("raw-2030-01", "counts.csv", tmp_path / "d.csv")


def test_github_read_resolved_raises_asset_not_found_when_the_release_is_missing(
    tmp_path: Path, writer_env
):
    # read_resolved is the pre-recovery, pre-publish read path: the very
    # first publish legitimately finds no `raw-2025-12` release yet.
    fake = FakeGitHub()
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())
    with pytest.raises(store.AssetNotFound):
        s.read_resolved("raw-2025-12", "counts.csv", tmp_path / "d.csv")


def test_github_is_latest_comes_from_the_releases_latest_endpoint(writer_env):
    # The Latest pointer is repository-wide, so it cannot be read off the
    # release payload: the store asks GET /releases/latest and compares tags.
    fake = FakeGitHub()
    fake.add_release("raw-2026-09", prerelease=True)
    fake.add_release("raw-2025-12", latest=True)
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())

    assert s.get_release("raw-2025-12").is_latest is True
    assert s.get_release("raw-2026-09").is_latest is False
    assert s.get_latest().is_latest is True
    assert {r.tag: r.is_latest for r in s.list_releases()} == {
        "raw-2025-12": True,
        "raw-2026-09": False,
    }

    moved = s.update_release("raw-2026-09", make_latest="true")

    assert moved.make_latest == "true"
    assert moved.is_latest is True
    assert s.get_release("raw-2025-12").is_latest is False


def test_github_list_releases_reads_every_page(writer_env):
    """`GET /releases` is paginated, newest created first.

    `raw-2025-12` is created once, before the later monthly releases, and is
    never recreated, so it is the last item the endpoint returns. An unpaginated
    read drops it as soon as the repository holds more than one page of
    releases: `recovery_tags` would stop returning `raw-2025-12`, and recovery
    would never run again on it.
    """
    fake = FakeGitHub()
    fake.add_release("raw-2025-12", latest=True)
    months = [f"raw-{year}-{month:02d}" for year in range(2026, 2036) for month in range(1, 13)]
    for tag in months:
        fake.add_release(tag, prerelease=True)
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())

    tags = [r.tag for r in s.list_releases()]

    assert len(tags) == 121
    assert sorted(tags) == sorted(["raw-2025-12", *months])
    # `raw-2025-12` is only reachable on the second page, which is the whole point.
    assert "raw-2025-12" not in tags[:100]
    listings = [c["url"] for c in fake.calls if "/releases?" in c["url"]]
    assert listings == [
        "https://api.github.com/repos/acme/votes/releases?per_page=100&page=1",
        "https://api.github.com/repos/acme/votes/releases?per_page=100&page=2",
    ]
    # And the consequence the pages exist for: recovery still sees `raw-2025-12`.
    assert store.recovery_tags(s)[0] == "raw-2025-12"


def test_github_writes_require_the_token_and_the_writer_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake = FakeGitHub()
    fake.add_release("raw-2026-09", prerelease=True)
    payload = _write(tmp_path, "rows.csv", b"rows-v1\n")

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("US_VOTING_WRITER", "1")
    no_token = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())
    with pytest.raises(store.StorageError, match="GITHUB_TOKEN"):
        no_token.upload_new("raw-2026-09", payload, "rows.csv")

    monkeypatch.setenv("GITHUB_TOKEN", "ghs-test-token")
    monkeypatch.delenv("US_VOTING_WRITER", raising=False)
    no_flag = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())
    with pytest.raises(store.StorageError, match="US_VOTING_WRITER"):
        no_flag.ensure_release("raw-2025-12", "Older month", "b", False, "true")

    assert not [c for c in fake.calls if c["method"] in ("POST", "PATCH", "DELETE")]


def test_github_ensure_release_creates_updates_and_refuses_immutable(writer_env):
    fake = FakeGitHub()
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())

    created = s.ensure_release("raw-2026-09", "September 2026", "body", True, "false")
    assert created.tag == "raw-2026-09"
    assert created.prerelease is True
    assert created.make_latest == "false"
    assert created.is_latest is False
    assert s.ensure_release("raw-2026-09", "x", "y", False, "false").title == ("September 2026")

    closed = s.update_release("raw-2026-09", prerelease=False, body="closed")
    assert closed.prerelease is False
    assert closed.body == "closed"
    assert closed.is_latest is False
    assert [r.tag for r in s.list_releases()] == ["raw-2026-09"]

    fake.add_release("raw-2025-12", immutable=True)
    with pytest.raises(store.StorageError, match="immutable release: raw-2025-12"):
        s.ensure_release("raw-2025-12", "Older month", "b", False, "true")


def test_github_upload_new_reports_a_422_for_a_duplicate_name(tmp_path: Path, writer_env):
    fake = FakeGitHub()
    fake.add_release("raw-2026-09", prerelease=True)
    fake.add_asset("raw-2026-09", "capture-2026-09-15T1817Z.tar.gz", b"bundle")
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())
    payload = _write(tmp_path, "bundle.tar.gz", b"bundle")

    with pytest.raises(store.StorageError) as excinfo:
        s.upload_new("raw-2026-09", payload, "capture-2026-09-15T1817Z.tar.gz")
    assert excinfo.value.status == 422

    uploaded = s.upload_new("raw-2026-09", payload, "capture-2026-09-15T1818Z.tar.gz")
    assert uploaded.state == "uploaded"
    assert fake.body_of("raw-2026-09", "capture-2026-09-15T1818Z.tar.gz") == b"bundle"


def test_github_read_resolved_verifies_by_hashing_when_digest_is_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake = FakeGitHub()
    fake.null_digest = True  # older assets carry no digest
    fake.add_release("raw-2025-12")
    good = b"counts-v2\n"
    fake.add_asset(
        "raw-2025-12",
        "counts.csv.next-tok-1",
        good,
        label="sha256:" + hashlib.sha256(good).hexdigest(),
    )
    fake.add_asset("raw-2025-12", "counts.csv.next-tok-2", b"torn\n", label="sha256:" + "0" * 64)
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())

    dest = s.read_resolved("raw-2025-12", "counts.csv", tmp_path / "s.csv")
    assert dest.read_bytes() == good


def test_github_replace_atomic_accepts_a_null_digest_upload_that_hashes_correctly(
    tmp_path: Path, writer_env
):
    # `_verify_asset`'s download-and-hash fallback (for the null-digest case)
    # is only exercised through a poll loop like `replace_atomic`'s, not
    # through `read_resolved`'s own inline hashing. This proves it accepts a
    # genuine upload, and does so by hashing the bytes at most once.
    fake = FakeGitHub()
    fake.null_digest = True
    fake.add_release("raw-2025-12")
    fake.add_asset("raw-2025-12", "counts.csv", b"counts-v1\n")
    clock = FakeClock()
    s = store.GitHubReleaseStore(
        "acme", "votes", transport=fake.transport(), sleep=clock.sleep, monotonic=clock.now
    )
    payload = _write(tmp_path, "v2.csv", b"counts-v2\n")

    asset = s.replace_atomic("raw-2025-12", payload, "counts.csv", "tok")

    assert asset.name == "counts.csv"
    assert fake.body_of("raw-2025-12", "counts.csv") == b"counts-v2\n"
    downloads = [c for c in fake.calls if "downloads.invalid" in c["url"]]
    assert len(downloads) == 1  # hashed once, not re-downloaded on every poll
    assert clock.slept == []


def test_github_replace_atomic_rejects_a_null_digest_upload_that_hashes_wrong(
    tmp_path: Path, writer_env
):
    # A torn transfer that GitHub reports as `state: "uploaded"` with a
    # matching size but no digest must still be rejected -- and rejected
    # after hashing it exactly once, not after 60 repeated downloads.
    fake = FakeGitHub()
    fake.null_digest = True
    fake.add_release("raw-2025-12")
    fake.add_asset("raw-2025-12", "counts.csv", b"counts-v1\n")

    class CorruptingGitHubStore(store.GitHubReleaseStore):
        """The transport silently stores torn bytes for every upload."""

        def upload_new(self, tag, path, name, label=None):
            asset = super().upload_new(tag, path, name, label=label)
            fake.corrupt_body(tag, name, b"counts-XX\n")  # same length, wrong bytes
            return asset

    clock = FakeClock()
    s = CorruptingGitHubStore(
        "acme", "votes", transport=fake.transport(), sleep=clock.sleep, monotonic=clock.now
    )
    payload = _write(tmp_path, "v2.csv", b"counts-v2\n")

    with pytest.raises(store.StorageError, match="did not verify"):
        s.replace_atomic("raw-2025-12", payload, "counts.csv", "tok")

    downloads = [c for c in fake.calls if "downloads.invalid" in c["url"]]
    assert len(downloads) == 1  # rejected after hashing once, not retried
    assert clock.slept == []
    assert fake.body_of("raw-2025-12", "counts.csv") == b"counts-v1\n"


def test_github_replace_atomic_retries_a_422_promotion(tmp_path: Path, writer_env):
    fake = FakeGitHub()
    fake.add_release("raw-2025-12")
    fake.add_asset("raw-2025-12", "counts.csv", b"counts-v1\n")
    fake.fail_promote["counts.csv"] = 2
    clock = FakeClock()
    s = store.GitHubReleaseStore(
        "acme", "votes", transport=fake.transport(), sleep=clock.sleep, monotonic=clock.now
    )
    payload = _write(tmp_path, "v2.csv", b"counts-v2\n")

    asset = s.replace_atomic("raw-2025-12", payload, "counts.csv", "2026-09-15T1817Z")

    assert clock.slept == [5.0, 10.0]
    assert asset.name == "counts.csv"
    assert asset.label is None
    assert fake.asset_names("raw-2025-12") == ["counts.csv"]
    assert fake.body_of("raw-2025-12", "counts.csv") == b"counts-v2\n"
    assert fake.label_of("raw-2025-12", "counts.csv") == ""


def test_github_replace_atomic_rolls_back_when_the_promotion_keeps_failing(
    tmp_path: Path, writer_env
):
    fake = FakeGitHub()
    fake.add_release("raw-2025-12")
    fake.add_asset("raw-2025-12", "counts.csv", b"counts-v1\n")
    fake.fail_promote["counts.csv"] = 99
    clock = FakeClock()
    s = store.GitHubReleaseStore(
        "acme", "votes", transport=fake.transport(), sleep=clock.sleep, monotonic=clock.now
    )
    payload = _write(tmp_path, "v2.csv", b"counts-v2\n")

    with pytest.raises(store.StorageError, match="could not promote"):
        s.replace_atomic("raw-2025-12", payload, "counts.csv", "tok")

    assert clock.slept == [5.0, 10.0, 20.0, 40.0, 80.0]
    # The previous copy is back under its real name, so readers are never empty.
    assert fake.body_of("raw-2025-12", "counts.csv") == b"counts-v1\n"
    assert "counts.csv.next-tok-1" in fake.asset_names("raw-2025-12")
    # And the next recovery pass clears the unusable upload.
    store.recover_temporaries(s, "raw-2025-12")
    assert fake.asset_names("raw-2025-12") == ["counts.csv"]


def test_recover_promotes_the_next_when_it_ties_the_old_to_the_second(writer_env):
    """On an exact `created_at` tie the verified `.next` wins, not the `.old`.

    GitHub stamps an asset's `created_at` to the second, and step 3 and step 4
    of one `replace_atomic` -- rename the live copy to `.old`, rename the new
    copy in -- are two requests apart, so a tie is ordinary rather than
    hypothetical. A crash between them leaves exactly this state. The `.next`
    is the copy that run verified and was installing, and the `.old` is the
    copy it was replacing, so preferring the `.old` on a tie would undo a
    completed publish. Only a *newer* `.old`, from a later run, outranks a
    stale `.next`, which the test above covers.
    """
    fake = FakeGitHub()
    fake.add_release("raw-2025-12", latest=True)
    payload = b"counts-v2\n"
    label = "sha256:" + hashlib.sha256(payload).hexdigest()
    incoming = fake.add_asset("raw-2025-12", "counts.csv.next-tok-1", payload, label=label)
    previous = fake.add_asset("raw-2025-12", "counts.csv.old-tok", b"counts-v1\n")
    previous["created_at"] = incoming["created_at"]  # the same second
    s = store.GitHubReleaseStore("acme", "votes", transport=fake.transport())

    actions = store.recover_temporaries(s, "raw-2025-12")

    assert actions == [
        "promoted:counts.csv.next-tok-1",
        "deleted-leftover:counts.csv.old-tok",
    ]
    assert fake.asset_names("raw-2025-12") == ["counts.csv"]
    assert fake.body_of("raw-2025-12", "counts.csv") == payload


def test_open_store_builds_both_kinds(tmp_path: Path):
    local = store.open_store(f"local:{tmp_path / 'releases'}")
    assert isinstance(local, store.LocalReleaseStore)
    local.ensure_release("raw-2025-12", "Older month", "b", False, "true")
    assert (tmp_path / "releases" / "raw-2025-12" / "_release.json").exists()

    remote = store.open_store("github:coatless-data/us-voting")
    assert isinstance(remote, store.GitHubReleaseStore)
    assert store.DEFAULT_STORE == "github:coatless-data/us-voting"

    with pytest.raises(store.StorageError, match="unsupported store spec"):
        store.open_store("s3://bucket/prefix")
    with pytest.raises(store.StorageError, match="unsupported store spec"):
        store.open_store("github:no-slash")


def _paced_store(fake: FakeGitHub, clock: FakeClock, **kwargs) -> object:
    return store.GitHubReleaseStore(
        "acme",
        "votes",
        transport=fake.transport(),
        sleep=clock.sleep,
        monotonic=clock.now,
        **kwargs,
    )


def test_writes_are_paced_to_sixty_per_minute(writer_env):
    fake = FakeGitHub()
    fake.add_release("raw-2025-12")
    for i in range(61):
        fake.add_asset("raw-2025-12", f"asset-{i:03d}.bin", b"x")
    clock = FakeClock()
    s = _paced_store(fake, clock)

    for asset in s.list_assets("raw-2025-12"):
        s.delete("raw-2025-12", asset.id)

    # The 61st write waits for the first one to leave the 60-second window.
    assert clock.slept == [60.0]
    assert fake.asset_names("raw-2025-12") == []


def test_writes_are_paced_to_450_per_hour(writer_env):
    fake = FakeGitHub()
    fake.add_release("raw-2025-12")
    for i in range(451):
        fake.add_asset("raw-2025-12", f"asset-{i:03d}.bin", b"x")
    clock = FakeClock()
    s = _paced_store(fake, clock)

    for asset in s.list_assets("raw-2025-12"):
        s.delete("raw-2025-12", asset.id)

    assert clock.slept.count(60.0) == 7  # one per full minute of writes
    assert clock.slept[-1] == 3180.0  # the 451st waits out the rest of the hour
    assert fake.asset_names("raw-2025-12") == []


def test_retry_after_is_honoured(writer_env):
    fake = FakeGitHub()
    fake.add_release("raw-2025-12")
    fake.add_asset("raw-2025-12", "counts.csv", b"counts-v1\n")
    fake.rate_limit_queue.append({"retry-after": "7"})
    clock = FakeClock()
    s = _paced_store(fake, clock)

    asset_id = s.list_assets("raw-2025-12")[0].id
    s.delete("raw-2025-12", asset_id)

    assert clock.slept == [7.0]
    assert fake.asset_names("raw-2025-12") == []


def test_x_ratelimit_reset_is_honoured(writer_env):
    fake = FakeGitHub()
    fake.add_release("raw-2025-12")
    fake.add_asset("raw-2025-12", "counts.csv", b"counts-v1\n")
    epoch = 1_789_000_000.0
    fake.rate_limit_queue.append(
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(epoch) + 45)}
    )
    clock = FakeClock()
    s = _paced_store(fake, clock, now_epoch=lambda: epoch)

    asset_id = s.list_assets("raw-2025-12")[0].id
    s.delete("raw-2025-12", asset_id)

    assert clock.slept == [45.0]
    assert fake.asset_names("raw-2025-12") == []


def test_download_falls_back_to_the_asset_id_when_the_public_copy_is_stale(tmp_path: Path):
    """The public download is addressed by name, and a replace reuses the name.

    `replace_atomic` renames a temporary asset onto the name the old one held,
    so for a while afterwards that URL can still answer with the bytes it
    replaced -- which is how a real `close-periods` read a month manifest that
    the run before it had already rewritten, and kept reading it. Retrying the
    same URL cannot converge; an asset id belongs to one upload.
    """
    fake = FakeGitHub()
    fake.add_release("raw-2026-09", prerelease=True)
    fake.add_asset("raw-2026-09", "manifest-2026-09.json", b'{"v": 2, "settled": true}')
    fake.stale_downloads["manifest-2026-09.json"] = b'{"v": 1}'
    s = store.GitHubReleaseStore(
        "acme", "votes", transport=fake.transport(), sleep=lambda _seconds: None
    )

    out = s.download("raw-2026-09", "manifest-2026-09.json", tmp_path / "m.json")

    assert out.read_bytes() == b'{"v": 2, "settled": true}'
    # Exactly one public attempt, then the API by id: the public URL never
    # settles here, so a retry of it would have looped to the end and failed.
    fetches = [
        httpx.URL(c["url"]).host
        for c in fake.calls
        if re.search(r"(downloads\.invalid/assets|/releases/assets)/\d+$", c["url"])
    ]
    assert fetches == ["downloads.invalid", "api.github.com"]


# -- private repositories ------------------------------------------------------

SIGNED_HOST = "objects.example"


class PrivateRepo:
    """A private repository: asset bytes are only reachable through the API.

    `browser_download_url` points at github.com and would answer 404 to anyone,
    so any request to that host is a bug. The asset-by-id endpoint answers 302
    to a signed URL on another host, as the real API does.
    """

    def __init__(self, body: bytes = b"county,votes\nWake,12\n") -> None:
        self.body = body
        self.requests: list[httpx.Request] = []

    def asset_payload(self, *, with_digest: bool = True, name: str = "counts.csv") -> dict:
        return {
            "id": 7,
            "name": name,
            "size": len(self.body),
            "state": "uploaded",
            "digest": f"sha256:{hashlib.sha256(self.body).hexdigest()}" if with_digest else None,
            "label": None,
            "created_at": "2026-09-15T18:17:00Z",
            "browser_download_url": f"https://github.com/o/r/releases/download/raw-2026-09/{name}",
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "github.com":
            return httpx.Response(404)
        if request.url.host == SIGNED_HOST:
            return httpx.Response(200, content=self.body)
        if path == "/repos/o/r/releases/tags/raw-2026-09":
            return httpx.Response(
                200, json={"id": 1, "tag_name": "raw-2026-09", "name": "", "body": ""}
            )
        if path == "/repos/o/r/releases/1/assets":
            return httpx.Response(200, json=[self.asset_payload()])
        if path == "/repos/o/r/releases/assets/7":
            return httpx.Response(302, headers={"location": f"https://{SIGNED_HOST}/signed?x=1"})
        return httpx.Response(404)


def _private_store(repo: PrivateRepo, **kwargs) -> store.GitHubReleaseStore:
    return store.GitHubReleaseStore(
        "o",
        "r",
        token="t",
        private=True,
        transport=httpx.MockTransport(repo.handler),
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_private_download_uses_api_with_token(tmp_path: Path):
    repo = PrivateRepo()
    s = _private_store(repo)

    dest = s.download("raw-2026-09", "counts.csv", tmp_path / "d.csv")

    assert dest.read_bytes() == repo.body
    api = [r for r in repo.requests if r.url.path == "/repos/o/r/releases/assets/7"]
    assert api[-1].headers["accept"] == "application/octet-stream"
    assert api[-1].headers["authorization"].endswith("t")
    assert not any(r.url.host == "github.com" for r in repo.requests)


def test_private_reads_do_not_forward_the_token_to_the_signed_host(tmp_path: Path):
    repo = PrivateRepo()
    s = _private_store(repo)

    s.download("raw-2026-09", "counts.csv", tmp_path / "d.csv")

    signed = [r for r in repo.requests if r.url.host == SIGNED_HOST]
    assert len(signed) == 1
    assert "authorization" not in signed[0].headers
    api_calls = [r for r in repo.requests if r.url.host == "api.github.com"]
    assert api_calls
    assert all(r.headers["authorization"] == "Bearer t" for r in api_calls)


def test_private_listing_and_metadata_send_the_token():
    repo = PrivateRepo()
    s = _private_store(repo)

    assert s.get_release("raw-2026-09") is not None
    assert [a.name for a in s.list_assets("raw-2026-09")] == ["counts.csv"]

    assert repo.requests
    assert all(r.headers["authorization"] == "Bearer t" for r in repo.requests)


def test_private_read_resolved_never_uses_the_browser_url(tmp_path: Path):
    repo = PrivateRepo()
    s = _private_store(repo)

    dest = s.read_resolved("raw-2026-09", "counts.csv", tmp_path / "r.csv")

    assert dest.read_bytes() == repo.body
    assert not any(r.url.host == "github.com" for r in repo.requests)


def test_private_verification_fallback_never_uses_the_browser_url():
    repo = PrivateRepo()
    s = _private_store(repo)
    asset = s._to_asset(repo.asset_payload(with_digest=False))
    label = f"sha256:{hashlib.sha256(repo.body).hexdigest()}"

    assert s._verify_asset("raw-2026-09", asset, label, len(repo.body)) is True

    assert not any(r.url.host == "github.com" for r in repo.requests)
    assert any(r.url.path == "/repos/o/r/releases/assets/7" for r in repo.requests)


def test_private_store_without_a_token_fails_on_first_use_not_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A token for some other repository in the environment must not stand in.
    monkeypatch.setenv("GITHUB_TOKEN", "public-token")
    monkeypatch.setenv("US_VOTING_WRITER", "1")
    repo = PrivateRepo()
    s = store.GitHubReleaseStore(
        "o", "r", private=True, transport=httpx.MockTransport(repo.handler)
    )

    with pytest.raises(store.StorageError, match="private store requires a token"):
        s.get_release("raw-2026-09")
    with pytest.raises(store.StorageError, match="private store requires a token"):
        s.download("raw-2026-09", "counts.csv", tmp_path / "d.csv")
    payload = _write(tmp_path, "p.csv", b"p\n")
    with pytest.raises(store.StorageError, match="private store requires a token"):
        s.upload_new("raw-2026-09", payload, "p.csv")
    assert repo.requests == []


def test_public_reads_still_use_the_browser_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    repo = PrivateRepo()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            repo.requests.append(request)
            return httpx.Response(200, content=repo.body)
        return repo.handler(request)

    s = store.GitHubReleaseStore("o", "r", transport=httpx.MockTransport(handler))

    s.download("raw-2026-09", "counts.csv", tmp_path / "d.csv")

    assert any(r.url.host == "github.com" for r in repo.requests)
    assert not any(r.url.path == "/repos/o/r/releases/assets/7" for r in repo.requests)


def test_open_store_passes_token_and_private_to_github_stores():
    s = store.open_store("github:o/r", token="abc", private=True)
    assert isinstance(s, store.GitHubReleaseStore)
    assert s._token == "abc"
    assert s._private is True


def test_open_store_local_ignores_token_and_private(tmp_path: Path):
    s = store.open_store(f"local:{tmp_path / 'releases'}", token="abc", private=True)
    assert isinstance(s, store.LocalReleaseStore)


def test_open_configured_reads_token_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RAW_PRIVATE_TOKEN", "abc")
    s = store.open_configured(
        StoreConfig("github:coatless-data/us-voting-raw", "RAW_PRIVATE_TOKEN", True)
    )
    assert s._token == "abc" and s._private is True


def test_open_configured_treats_an_unset_or_empty_token_as_none(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("RAW_PRIVATE_TOKEN", "")
    s = store.open_configured(
        StoreConfig("github:coatless-data/us-voting-raw", "RAW_PRIVATE_TOKEN", True)
    )
    assert s._token is None
