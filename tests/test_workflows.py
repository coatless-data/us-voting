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


def step(name: str, workflow: str = "capture.yml") -> str:
    """The text of one step, from its `- name:` line up to the next step."""
    lines = read(workflow).splitlines(keepends=True)
    start = lines.index(f"      - name: {name}\n")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("      - name: ")),
        len(lines),
    )
    return "".join(lines[start:end])


def step_names(workflow: str = "capture.yml") -> list[str]:
    prefix = "      - name: "
    return [line[len(prefix) :] for line in read(workflow).splitlines() if line.startswith(prefix)]


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
    text = step("Gate")
    assert "        if: ${{ failure() && steps.publish.outcome == 'failure' }}\n" in text
    assert (
        '          echo "::error::captured files were not published; '
        'fetch state was not advanced"\n'
        "          exit 1\n"
    ) in text


def test_hourly_cron_and_serial_concurrency():
    text = read("capture.yml")
    assert 'cron: "23 * * * *"' in text
    assert "concurrency:\n  group: us-voting-capture\n  cancel-in-progress: false\n" in text


def test_publish_runs_only_with_files():
    assert "if: ${{ inputs.dry_run != true && steps.capture.outputs.files != '0' }}" in read(
        "capture.yml"
    )


def test_status_commit_waits_for_publish():
    """Schedule state is committed whatever publish did; fetch state only after a publish."""
    text = step("Commit status/latest.json")
    assert (
        "        if: ${{ !cancelled() && inputs.dry_run != true"
        " && steps.capture.outcome == 'success' }}\n"
    ) in text
    assert "          PUBLISH_OUTCOME: ${{ steps.publish.outcome }}\n" in text
    hold = (
        '          if [ "$PUBLISH_OUTCOME" = "failure" ]; then\n'
        "            uv run us-voting hold-fetch-state status/latest.json out/capture/status.json\n"
        "          fi\n"
    )
    assert hold in text
    assert text.index("set -euo pipefail") < text.index(hold)
    assert text.index(hold) < text.index("cp out/capture/status.json status/latest.json")


def test_status_commit_fails_when_every_push_is_rejected():
    text = step("Commit status/latest.json")
    assert "continue-on-error" not in text
    assert "::warning::" not in text
    assert text.endswith(
        '          echo "::error::could not push status/latest.json after 3 attempts"\n'
        "          exit 1\n"
        "\n"
    )


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


def test_capture_keeps_its_step_timeout():
    assert step("Capture") == (
        "      - name: Capture\n"
        "        id: capture\n"
        "        timeout-minutes: 60\n"
        '        run: uv run us-voting capture --out out --only "$ONLY"\n'
        "\n"
    )


def test_capture_failure_opens_an_issue():
    text = step("Report a capture failure")
    assert (
        "        if: ${{ !cancelled() && inputs.dry_run != true"
        " && steps.capture.outcome == 'failure' }}\n"
    ) in text
    assert "        run: uv run us-voting alerts --capture-outcome failure\n" in text
    names = step_names()
    assert names.index("Capture") + 1 == names.index("Report a capture failure")


def test_alerts_close_the_capture_issue_after_a_good_capture():
    text = step("Alerts")
    assert (
        "        if: ${{ !cancelled() && inputs.dry_run != true"
        " && steps.capture.outcome == 'success' }}\n"
    ) in text
    assert '--capture-outcome "${{ steps.capture.outcome }}"' in text


def test_github_token_only_in_steps_that_call_the_api():
    text = read("capture.yml")
    assert "    env:\n      ONLY: ${{ inputs.only || '' }}\n    steps:\n" in text
    token = "          GITHUB_TOKEN: ${{ github.token }}\n"
    users = ["Report a capture failure", "Publish", "Alerts"]
    assert [name for name in step_names() if token in step(name)] == users
    assert text.count("GITHUB_TOKEN") == len(users)
