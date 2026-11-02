"""Upload bundles to monthly `raw-YYYY-MM` releases and keep each release's manifest current."""

import json
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from us_voting.bundle import INFO_NAME, BundleInfo, info_to_dict, read_info, release_tag
from us_voting.config import Config
from us_voting.guard import voter_level_reason
from us_voting.store import (
    AssetNotFound,
    ReleaseStore,
    StorageError,
    recover_temporaries,
    sha256_file,
)

MANIFEST_NAME = "_manifest.json"
RELEASE_BODY = (
    "Official files exactly as captured, one bundle per capture run. `_manifest.json` lists "
    "every file with its source, URL, capture time and SHA-256."
)


class PublishConflict(Exception):
    """A bundle with the same name but different bytes is already published."""


class PublishRefused(Exception):
    """A public bundle holds a file that looks voter-level."""


@dataclass
class PublishResult:
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    events: list[tuple[str, str, str]] = field(default_factory=list)  # (verb, name, tag)


def _check_public(tar_path: Path, info: BundleInfo, cfg: Config) -> None:
    entries = {f.member: f for f in info.files}
    with tarfile.open(tar_path) as tar:
        for member in tar.getmembers():
            if member.name == INFO_NAME or not member.isfile():
                continue
            entry = entries.get(member.name)
            source = cfg.sources.get(entry.source_id) if entry else None
            allow = source.guard_allow if source else ()
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "member"
                src = tar.extractfile(member)
                if src is None:
                    continue
                with src, dest.open("wb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                reason = voter_level_reason(dest, allow)
            if reason is not None:
                raise PublishRefused(
                    f"{info.name}: {member.name} looks voter-level ({reason}); "
                    "nothing was uploaded for this bundle"
                )


def _read_manifest(store: ReleaseStore, tag: str, work: Path) -> dict:
    try:
        path = store.download(tag, MANIFEST_NAME, work / "manifest.in")
    except AssetNotFound:
        return {"tag": tag, "bundles": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _upload(store: ReleaseStore, tag: str, tar_path: Path, name: str, digest: str) -> None:
    try:
        store.upload_new(tag, tar_path, name)
    except StorageError as exc:
        if exc.status != 422:
            raise
        existing = next((a for a in store.list_assets(tag) if a.name == name), None)
        if existing is None or existing.digest != f"sha256:{digest}":
            raise


def _publish_one(store: ReleaseStore, sidecar: Path, cfg: Config, public: bool) -> tuple[bool, str]:
    info = read_info(sidecar)
    tar_path = sidecar.with_suffix(".tar.gz")
    tag = release_tag(info)
    store.ensure_release(tag, f"Raw captures {tag[4:]}", RELEASE_BODY, False, "false")
    recover_temporaries(store, tag)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        manifest = _read_manifest(store, tag, work)
        digest = sha256_file(tar_path)
        for existing in manifest["bundles"]:
            if existing["name"] == info.name:
                if existing["sha256"] == digest:
                    return False, tag
                raise PublishConflict(
                    f"{info.name} is already published in {tag} with different content"
                )
        if public:
            _check_public(tar_path, info, cfg)
        _upload(store, tag, tar_path, info.name, digest)
        entry = info_to_dict(info) | {"sha256": digest, "bytes": tar_path.stat().st_size}
        manifest["bundles"] = sorted([*manifest["bundles"], entry], key=lambda b: b["name"])
        out = work / MANIFEST_NAME
        out.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        store.replace_atomic(tag, out, MANIFEST_NAME, token=info.capture_id)
    return True, tag


def publish_dir(
    capture_dir: Path, cfg: Config, public: ReleaseStore, private: ReleaseStore
) -> PublishResult:
    result = PublishResult()
    for sub, store, is_public in (("public", public, True), ("private", private, False)):
        folder = capture_dir / sub
        if not folder.is_dir():
            continue
        for sidecar in sorted(folder.glob("*.json")):
            name = f"{sidecar.stem}.tar.gz"
            done, tag = _publish_one(store, sidecar, cfg, is_public)
            (result.uploaded if done else result.skipped).append(name)
            result.events.append(("uploaded" if done else "skipped", name, tag))
    return result


__all__ = ["MANIFEST_NAME", "PublishConflict", "PublishRefused", "PublishResult", "publish_dir"]
