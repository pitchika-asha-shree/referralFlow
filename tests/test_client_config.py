import copy
from pathlib import Path

import pytest

from app.client_config import ConfigError, load_client_configs, parse_client_config
from tests.helpers import CONFIG

ROOT = Path(__file__).resolve().parent.parent


def test_repo_configs_are_valid():
    configs = load_client_configs(ROOT / "configs" / "clients")
    assert {"acme_ortho", "sunrise_dme"} <= set(configs)


def test_secret_comes_from_environment(monkeypatch):
    monkeypatch.setenv("TEST_ACME_SECRET", "s3cret")
    assert parse_client_config(CONFIG).webhook.secret == "s3cret"


def _broken(mutate):
    cfg = copy.deepcopy(CONFIG)
    mutate(cfg)
    return cfg


def test_linter_catches_typos():
    cases = {
        "unknown op": lambda c: c["routing"]["rules"][0]["when"].update(op="equal"),
        "unknown field": lambda c: c["routing"]["rules"][0]["when"].update(field="urgncy"),
        "needs a 'value'": lambda c: c["routing"]["rules"][0]["when"].pop("value"),
        "unknown required_fields": lambda c: c["required_fields"].append("ssn"),
        "min_confidence": lambda c: c.update(min_confidence=5),
        "missing required key 'webhook'": lambda c: c.pop("webhook"),
        "field_map has unknown": lambda c: c["field_map"].update(foo="bar"),
    }
    for expected, mutate in cases.items():
        with pytest.raises(ConfigError, match=expected):
            parse_client_config(_broken(mutate))


def test_duplicate_client_ids(tmp_path):
    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text("client_id: same\nwebhook: {url: http://x, secret_env: X}\n")
    with pytest.raises(ConfigError, match="duplicate"):
        load_client_configs(tmp_path)
