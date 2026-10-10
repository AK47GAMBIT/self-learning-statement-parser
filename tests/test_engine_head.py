import json
from pathlib import Path

import pytest

from app.config_spec import FormatConfig
from app.engine import ParseError, parse_head
from app.reader import read_document
from app.schema import Statement
from samples.generate import generate_all
from samples.sample_data import PASSWORDS

CONFIGS = Path(__file__).parent.parent / "configs"
NAMES = ["abc_pms", "xyz_wealth", "meridian_capital", "sterling_aif", "helix_aif"]
HEAD_ONLY = {"holdings", "transactions", "source_file_hash"}   # excluded until 5c


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    generate_all(root)
    return root


def cfg_dict(name):
    return json.loads((CONFIGS / f"{name}.json").read_text())


def read(samples, name):
    return read_document(samples / "pdfs" / f"{name}.pdf", PASSWORDS.get(name))


@pytest.mark.parametrize("name", NAMES)
def test_head_matches_expected(samples, name):
    got = parse_head(read(samples, name), FormatConfig.model_validate(cfg_dict(name)))
    expected = Statement.model_validate_json(
        (samples / "expected" / f"{name}.json").read_text())
    assert got.model_dump(exclude=HEAD_ONLY) == expected.model_dump(exclude=HEAD_ONLY)


def test_missing_label_fails_loudly(samples):
    data = cfg_dict("abc_pms")
    data["summary"]["opening_value"]["label"] = "Starting Value"   # layout "changed"
    with pytest.raises(ParseError, match="opening_value"):
        parse_head(read(samples, "abc_pms"), FormatConfig.model_validate(data))


def test_wrong_date_transform_fails_loudly(samples):
    data = cfg_dict("abc_pms")
    data["fields"]["period_start"]["transform"] = "date_iso"      # wrong style
    with pytest.raises(ParseError, match="period_start"):
        parse_head(read(samples, "abc_pms"), FormatConfig.model_validate(data))


def test_config_for_another_provider_is_rejected(samples):
    # Applying Meridian's config to an ABC statement must not produce a statement.
    with pytest.raises(ParseError) as exc:
        parse_head(read(samples, "abc_pms"),
                   FormatConfig.model_validate(cfg_dict("meridian_capital")))
    assert len(exc.value.issues) >= 3          # every problem reported, not just the first


def test_optional_rule_may_be_missing(samples):
    data = cfg_dict("abc_pms")
    data["summary"]["nav_per_unit"] = {"label": "NAV per Unit", "optional": True}
    stmt = parse_head(read(samples, "abc_pms"), FormatConfig.model_validate(data))
    assert stmt.summary.nav_per_unit is None
    