"""Contract tests for the GitHub Actions workflows.

These assert on the literal text of the workflow files rather than on a parsed
YAML tree, because the rules being enforced are about the literal text: GitHub
rejects a bare `!` at the start of an `if:` value, and a CLI call is only
correct if the subcommand and flags are spelled exactly as their subparser
defines them. A YAML parser normalizes quoting and would hide both.
"""

import re
import shlex
from pathlib import Path

from us_voting.cli import build_parser

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def unwrapped_if_expressions(name: str) -> list[str]:
    """`if:` values that GitHub would not parse, i.e. those starting with `!`."""
    bad = []
    for line in read(name).splitlines():
        match = re.match(r"\s*if:\s*(.*)$", line)
        if match and match.group(1).startswith("!"):
            bad.append(line.strip())
    return bad


def test_no_artifacts_anywhere():
    assert all("upload-artifact" not in read(p.name) for p in WORKFLOWS.glob("*.yml"))


def test_private_token_only_from_secrets():
    text = read("capture.yml")
    assert "RAW_PRIVATE_TOKEN: ${{ secrets.RAW_PRIVATE_TOKEN }}" in text
    assert text.count("RAW_PRIVATE_TOKEN") == 2


def test_private_token_scoped_to_publish():
    lines = read("capture.yml").splitlines()
    publish = lines.index("      - name: Publish")
    alerts = lines.index("      - name: Alerts")
    token = [i for i, line in enumerate(lines) if "secrets.RAW_PRIVATE_TOKEN" in line]
    assert len(token) == 1
    assert publish < token[0] < alerts


def test_gate_runs_after_publish_failure():
    assert "        if: ${{ failure() && steps.publish.outcome == 'failure' }}\n" in read(
        "capture.yml"
    )


def test_hourly_cron_and_serial_concurrency():
    text = read("capture.yml")
    assert 'cron: "23 * * * *"' in text
    assert "concurrency:\n  group: us-voting-capture\n  cancel-in-progress: false\n" in text


def test_publish_runs_only_with_files():
    assert "if: ${{ inputs.dry_run != true && steps.capture.outputs.files != '0' }}" in read(
        "capture.yml"
    )


def test_status_commit_waits_for_publish():
    line = next(
        line
        for line in read("capture.yml").splitlines()
        if "steps.publish.outcome != 'failure'" in line
    )
    assert "inputs.dry_run != true" in line
    assert "steps.capture.outcome == 'success'" in line


def test_no_bare_bang_if():
    for path in WORKFLOWS.glob("*.yml"):
        assert unwrapped_if_expressions(path.name) == [], path.name


def test_cli_calls_match_parser():
    """Every `uv run us-voting ...` line in capture.yml parses under build_parser()."""
    calls = [
        line.split("uv run us-voting ", 1)[1]
        for line in read("capture.yml").splitlines()
        if "uv run us-voting " in line
    ]
    assert calls, "capture.yml has no `uv run us-voting` calls"
    parser = build_parser()
    for call in calls:
        # Expressions and shell variables stand in for runtime values; only the
        # subcommand and flag spelling is under test here.
        text = re.sub(r'"\$\{\{.*?\}\}"', "X", call)
        text = re.sub(r'"\$[A-Z_]+"', "X", text)
        tokens = shlex.split(text)
        parser.parse_args(tokens)
