from datetime import date

import pytest

from conftest import CONFIG_DIR, one_source, write_config
from us_voting.cli import main
from us_voting.config import ConfigError, StoreConfig, Window, load_config

VALID = {
    "id": '"nc-x"',
    "state": '"NC"',
    "kind": '"url"',
    "raw": '"public"',
    "schedule": '"results"',
    "provides": '["results"]',
    "url": '"https://a.gov/f.zip"',
}


def body(**edit: str | None) -> str:
    fields = {**VALID, **edit}
    return "".join(f"{k} = {v}\n" for k, v in fields.items() if v is not None)


def test_repo_config_loads():
    cfg = load_config(CONFIG_DIR)
    assert set(cfg.schedules) == {"results", "early_vote", "early_vote_overwrite", "daily"}
    assert cfg.schedules["results"].windows == (Window(23, 39, 1),)
    assert cfg.stores["private"] == StoreConfig(
        "github:coatless-data/us-voting-raw", "RAW_PRIVATE_TOKEN", True
    )
    assert cfg.stores["public"].private is False
    assert cfg.max_bundle_bytes == 1_800_000_000
    assert cfg.workers == 6


def test_source_fields(tmp_path):
    src = one_source(tmp_path, body())
    assert src.election.date == date(2026, 11, 3)
    assert src.raw == "public" and src.schedule.name == "results"
    assert dict(src.params) == {"url": "https://a.gov/f.zip"}
    assert src.guard_allow == () and src.expect_missing_until is None


@pytest.mark.parametrize(
    ("edit", "fragment"),
    [
        ({"raw": None}, "raw"),
        ({"raw": '"secret"'}, "raw"),
        ({"kind": '"ftp"'}, "kind"),
        ({"schedule": '"weekly"'}, "schedule"),
        ({"raw_privacy": '"public"'}, "raw_privacy"),
        ({"state": '"XX"'}, "state"),
        ({"urls": '["https://a.gov/g"]'}, "url"),
        ({"provides": '["magic"]'}, "provides"),
        ({"daily_at_utc": "24"}, "daily_at_utc"),
    ],
)
def test_invalid_source(tmp_path, edit, fragment):
    root = write_config(tmp_path, "[[source]]\n" + body(**edit))
    with pytest.raises(ConfigError, match=fragment) as exc:
        load_config(root)
    assert "nc-x" in str(exc.value) or fragment == "id"
    assert "test.toml" in str(exc.value)


def test_duplicate_id_across_files(tmp_path):
    root = write_config(tmp_path, "[[source]]\n" + body())
    (root / "sources" / "other.toml").write_text(
        'election = "2026-11-03-general"\n[[source]]\n' + body(), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(root)


def test_clarity_needs_eid_or_discover_date(tmp_path):
    extra = 'base = "https://x.gov"\nfiles = ["a.zip"]\n'
    root = write_config(tmp_path, "[[source]]\n" + body(url=None, kind='"clarity"') + extra)
    with pytest.raises(ConfigError, match="eid"):
        load_config(root)


def test_page_links_pattern_must_compile(tmp_path):
    extra = "page = \"https://a.gov/p\"\npattern = '''('''\n"
    root = write_config(tmp_path, "[[source]]\n" + body(url=None, kind='"page_links"') + extra)
    with pytest.raises(ConfigError, match="pattern"):
        load_config(root)


def test_missing_sources_dir_is_valid(tmp_path):
    root = write_config(tmp_path, "")
    (root / "sources" / "test.toml").unlink()
    (root / "sources").rmdir()
    assert load_config(root).sources == {}


def test_unknown_top_level_key(tmp_path):
    root = write_config(tmp_path, "colour = 1\n")
    with pytest.raises(ConfigError, match="colour"):
        load_config(root)


def test_user_agent_for():
    http = load_config(CONFIG_DIR).http
    assert http.user_agent_for("sos-prod.tnsosgovfiles.com") == http.browser_user_agent
    assert http.user_agent_for("tnsosgovfiles.com") == http.browser_user_agent
    assert http.user_agent_for("eviltnsosgovfiles.com") == http.project_user_agent
    assert http.user_agent_for("dl.ncsbe.gov") == http.project_user_agent


def test_check_config_cli(tmp_path, capsys):
    root = write_config(tmp_path, "[[source]]\n" + body())
    assert main(["check-config", "--config", str(root)]) == 0
    assert capsys.readouterr().out.strip() == "1 sources, 1 elections"
    bad = write_config(tmp_path / "bad", "[[source]]\n" + body(raw='"secret"'))
    assert main(["check-config", "--config", str(bad)]) == 1
    assert "raw" in capsys.readouterr().err


def test_no_command_returns_usage_error(capsys):
    assert main([]) == 2
