import json
from pathlib import Path

import pytest

from app.config_spec import FormatConfig
from app.engine import ParseError, parse
from app.reader import read_document
from app.schema import Statement
from samples.generate import generate_all
from samples.sample_data import PASSWORDS

CONFIGS = Path(__file__).parent.parent / "configs"
NAMES = ["abc_pms", "xyz_wealth", "meridian_capital", "sterling_aif", "helix_aif"]


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
def test_full_parse_equals_expected(samples, name):
    got = parse(read(samples, name), FormatConfig.model_validate(cfg_dict(name)))
    expected = Statement.model_validate_json(
        (samples / "expected" / f"{name}.json").read_text())
    assert got.model_dump(exclude={"source_file_hash"}) == \
           expected.model_dump(exclude={"source_file_hash"})


def test_parsing_is_deterministic(samples):
    cfg = FormatConfig.model_validate(cfg_dict("abc_pms"))
    doc = read(samples, "abc_pms")
    assert parse(doc, cfg).model_dump_json() == parse(doc, cfg).model_dump_json()


def test_renamed_column_fails_loudly(samples):
    data = cfg_dict("abc_pms")
    data["holdings"]["columns"][2]["header"] = "Units"       # provider renamed Qty
    with pytest.raises(ParseError, match="Units"):
        parse(read(samples, "abc_pms"), FormatConfig.model_validate(data))


def test_blank_cell_row_is_placed_by_position(samples):
    stmt = parse(read(samples, "xyz_wealth"),
                 FormatConfig.model_validate(cfg_dict("xyz_wealth")))
    funds = stmt.transactions[0]
    assert funds.security_name == "Funds added"
    assert funds.quantity is None and str(funds.amount) == "100000.00"


def test_total_row_is_not_a_holding(samples):
    stmt = parse(read(samples, "meridian_capital"),
                 FormatConfig.model_validate(cfg_dict("meridian_capital")))
    assert [h.security_name for h in stmt.holdings] == [
        "Sun Pharmaceutical Industries", "Titan Company", "Axis Bank"]


def test_dr_cr_signs(samples):
    stmt = parse(read(samples, "meridian_capital"),
                 FormatConfig.model_validate(cfg_dict("meridian_capital")))
    amounts = [str(t.amount) for t in stmt.transactions]
    assert amounts == ["-57500.00", "4500.00", "67000.00", "-6000.00"]

def test_missing_table_is_reported_differently(samples):
    data = cfg_dict("abc_pms")
    data["holdings"]["header_starts_with"] = "Instrument"     # no such header in ABC
    with pytest.raises(ParseError, match="no header line"):
        parse(read(samples, "abc_pms"), FormatConfig.model_validate(data))