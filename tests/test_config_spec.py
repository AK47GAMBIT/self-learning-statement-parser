import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config_spec import FormatConfig

CONFIGS = Path(__file__).parent.parent / "configs"


def load(name):
    return json.loads((CONFIGS / f"{name}.json").read_text())


def test_abc_config_loads():
    cfg = FormatConfig.model_validate(load("abc_pms"))
    assert cfg.provider == "ABC Portfolio Managers"
    assert cfg.holdings.columns[0].field == "security_name"


def test_unknown_transform_rejected():
    data = load("abc_pms")
    data["summary"]["net_gain"]["transform"] = "do_something_clever"
    with pytest.raises(ValidationError):
        FormatConfig.model_validate(data)


def test_unknown_key_rejected():
    data = load("abc_pms")
    data["run_this"] = "import os"
    with pytest.raises(ValidationError):
        FormatConfig.model_validate(data)


def test_missing_required_field_rejected():
    data = load("abc_pms")
    del data["fields"]["client_id"]
    with pytest.raises(ValidationError):
        FormatConfig.model_validate(data)

def test_unknown_field_name_rejected():
    data = load("abc_pms")
    data["fields"]["favourite_colour"] = {"regex": "(x)"}
    with pytest.raises(ValidationError):
        FormatConfig.model_validate(data)


def test_unknown_summary_field_rejected():
    data = load("abc_pms")
    data["summary"]["profit_margin"] = {"label": "Profit"}
    with pytest.raises(ValidationError):
        FormatConfig.model_validate(data)


def test_bad_regex_rejected():
    data = load("abc_pms")
    data["fields"]["client_id"]["regex"] = "Client Code:(\\S+"      # unbalanced bracket
    with pytest.raises(ValidationError):
        FormatConfig.model_validate(data)


def test_regex_without_capture_group_rejected():
    data = load("abc_pms")
    data["fields"]["client_id"]["regex"] = "Client Code:"           # no group
    with pytest.raises(ValidationError):
        FormatConfig.model_validate(data)