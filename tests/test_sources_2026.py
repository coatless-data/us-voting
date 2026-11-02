from conftest import CONFIG_DIR
from us_voting.config import load_config


def test_private_sources_are_exactly():
    cfg = load_config(CONFIG_DIR)
    assert {s.id for s in cfg.sources.values() if s.raw == "private"} == {
        "nc-2026-11-03-absentee-voters",
        "fl-2026-11-03-ev-voters",
        "nv-2026-11-03-clark-ev-voters",
        "nv-2026-11-03-clark-ev-voters-cumulative",
        "pa-2026-11-03-mail-voters",
    }
    assert len(cfg.sources) == 31
