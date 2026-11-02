"""Command-line entry point."""

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from us_voting import __version__
from us_voting import capture as capture_mod
from us_voting.config import ConfigError, load_config
from us_voting.status import load_status


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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    return args.func(args)
