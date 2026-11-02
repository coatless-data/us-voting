"""Command-line entry point."""

import argparse
import sys
from pathlib import Path

from us_voting import __version__
from us_voting.config import ConfigError, load_config


def _check_config(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(Path(args.config))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{len(cfg.sources)} sources, {len(cfg.elections)} elections")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="us-voting", description=__doc__)
    parser.add_argument("--version", action="version", version=f"us-voting {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    check = sub.add_parser("check-config", help="validate the files in the config directory")
    check.add_argument("--config", default="config", help="config directory (default: config)")
    check.set_defaults(func=_check_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    return args.func(args)
