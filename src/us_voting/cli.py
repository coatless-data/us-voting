"""Command-line entry point."""

import argparse
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from us_voting import __version__
from us_voting import capture as capture_mod
from us_voting.alerts import run_alerts
from us_voting.config import ConfigError, load_config
from us_voting.importer import import_archive
from us_voting.issues import Issues
from us_voting.publish import PublishConflict, PublishRefused, publish_dir
from us_voting.status import load_status
from us_voting.store import StorageError, open_configured, open_store


def _check_config(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(Path(args.config))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{len(cfg.sources)} sources, {len(cfg.elections)} elections")
    return 0


def _capture(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(Path(args.config))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    only = [i.strip() for i in args.only.split(",") if i.strip()] or None
    client = capture_mod.Client(cfg.http)
    try:
        status = load_status(Path(args.status))
        result = capture_mod.run_capture(
            cfg, status, Path(args.out), datetime.now(UTC), client, only=only
        )
    except (ValueError, FileExistsError) as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        client.close()
    for source_id in result.ran:
        error = status.sources[source_id].error
        line = f"{source_id}: {result.results[source_id]}"
        print(f"{line} ({error})" if error else line)
    print(f"{result.capture_id}: {result.files} files, {len(result.bundles)} bundles")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"capture_id={result.capture_id}\n")
            fh.write(f"files={result.files}\n")
            fh.write(f"all_failed={'true' if result.all_failed else 'false'}\n")
    return 0


def _publish(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(Path(args.config))
        overrides = {"public": args.public_store, "private": args.private_store}
        opened = {}
        for key, spec in overrides.items():
            sc = cfg.stores[key]
            if spec:
                token = os.environ.get(sc.token_env) or None
                opened[key] = open_store(spec, token=token, private=sc.private)
            else:
                opened[key] = open_configured(sc)
        result = publish_dir(Path(args.dir), cfg, opened["public"], opened["private"])
    except (PublishConflict, PublishRefused, StorageError, ConfigError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 1
    for verb, name, tag in result.events:
        print(f"{verb} {name} ({tag})")
    return 0


def _alerts(args: argparse.Namespace) -> int:
    """Alerting never fails the workflow, so every path returns 0."""
    try:
        issues = Issues(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN"))
        run_alerts(
            load_status(Path(args.status_json)), issues, publish_outcome=args.publish_outcome
        )
    except Exception as exc:
        print(f"::warning::alerts failed: {type(exc).__name__}")
    return 0


def _today() -> date:
    return datetime.now(UTC).date()


def _import_archive(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(Path(args.config))
        today = _today()
        through = date.fromisoformat(args.through) if args.through else today - timedelta(days=1)
        if through >= today:
            print(
                f"--through {through} is not a finished UTC day (today is {today})",
                file=sys.stderr,
            )
            return 1
        result = import_archive(Path(args.archive), cfg, Path(args.out), through=through)
    except (ConfigError, ValueError, FileExistsError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 1
    problems = (
        ("no source owns", result.unmatched),
        ("more than one source owns", result.ambiguous),
        ("could not be verified", result.bad_hash),
    )
    failed = False
    for label, items in problems:
        for item in items:
            failed = True
            print(f"{label}: {item}", file=sys.stderr)
    if failed:
        print("nothing was written", file=sys.stderr)
        return 1
    print(f"{result.files} files imported, {len(result.bundles)} bundles")
    for path in result.bundles:
        print(f"  {path.parent.name}/{path.name}")
    print(
        f"{result.skipped_after} skipped after {through}, "
        f"{result.skipped_html} skipped as html, {len(result.rerouted)} rerouted"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="us-voting", description=__doc__)
    parser.add_argument("--version", action="version", version=f"us-voting {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    check = sub.add_parser("check-config", help="validate the files in the config directory")
    check.add_argument("--config", default="config", help="config directory (default: config)")
    check.set_defaults(func=_check_config)
    cap = sub.add_parser("capture", help="fetch due sources into public and private bundles")
    cap.add_argument("--out", required=True, help="output directory")
    cap.add_argument("--only", default="", help="comma-separated source ids to run regardless")
    cap.add_argument("--status", default="status/latest.json", help="status file to start from")
    cap.add_argument("--config", default="config", help="config directory (default: config)")
    cap.set_defaults(func=_capture)
    pub = sub.add_parser("publish", help="upload bundles to the release stores")
    pub.add_argument("dir", help="directory holding public/ and private/ bundles")
    pub.add_argument("--public-store", default="", help="store spec overriding the config")
    pub.add_argument("--private-store", default="", help="store spec overriding the config")
    pub.add_argument("--config", default="config", help="config directory (default: config)")
    pub.set_defaults(func=_publish)
    alerts = sub.add_parser("alerts", help="open, refresh and close GitHub issue alerts")
    alerts.add_argument("status_json", help="status file written by capture")
    alerts.add_argument("--publish-outcome", default="", help="publish step outcome, any string")
    alerts.set_defaults(func=_alerts)
    imp = sub.add_parser("import-archive", help="import a local archive of raw captures")
    imp.add_argument("archive", help="archive directory holding _log.jsonl")
    imp.add_argument("--out", required=True, help="output directory")
    imp.add_argument("--through", default="", help="last UTC day to import (default: yesterday)")
    imp.add_argument("--config", default="config", help="config directory (default: config)")
    imp.set_defaults(func=_import_archive)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    return args.func(args)
