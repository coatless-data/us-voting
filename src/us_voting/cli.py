"""Command-line entry point."""

import argparse

from us_voting import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="us-voting", description=__doc__)
    parser.add_argument("--version", action="version", version=f"us-voting {__version__}")
    parser.add_subparsers(dest="command", metavar="command")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    return 0
