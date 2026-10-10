from datetime import date
from decimal import Decimal as D

import pytest

from app.config_spec import FormatConfig
from app.engine import parse
from app.reader import read_document
from app.schema import Statement, TransactionType
from app.validation import Status, validate
from samples.generate import generate_all
from samples.sample_data import PASSWORDS

import json
from pathlib import Path

CONFIGS = Path(__file__).parent.parent / "configs"
NAMES = ["abc_pms", "xyz_wealth", "meridian_capital", "sterling_aif", "helix_aif"]


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    generate_all(root)
    return root


def expected(samples, name) -> Statement:
    return Statement.model_validate_json((samples / "expected" / f"{name}.json").read_text())


def codes(report):
    return {i.code for i in report.issues}


# ---------------------------------------------------------------- clean data passes
@pytest.mark.parametrize("name", NAMES)
def test_clean_statement_has_no_issues(samples, name):
    cfg = FormatConfig.model_validate(json.loads((CONFIGS / f"{name}.json").read_text()))
    doc = read_document(samples / "pdfs" / f"{name}.pdf", PASSWORDS.get(name))
    report = validate(parse(doc, cfg))          # full chain: PDF -> engine -> validation
    assert report.errors == [], report.errors
    assert report.warnings == [], report.warnings
    assert report.status == Status.PARSED_OK


def test_skipped_checks_are_reported(samples):
    report = validate(expected(samples, "meridian_capital"))
    assert "reconciliation" in report.skipped          # Meridian prints no flows or gain
    assert "holding_arithmetic" in report.passed


def test_xyz_skips_price_check(samples):
    report = validate(expected(samples, "xyz_wealth"))
    assert "holding_arithmetic" in report.skipped      # XYZ prints no per-share price


# ---------------------------------------------------------------- corrupted data is caught
def test_inflated_market_value_is_caught(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    s.holdings[0].market_value += D("10000")
    report = validate(s)
    assert report.status == Status.NEEDS_REVIEW
    assert {"holding_arithmetic", "holdings_total_mismatch"} <= codes(report)


def test_wrong_closing_value_is_caught(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    s.summary.closing_value += D("50000")
    assert {"holdings_total_mismatch", "reconciliation_mismatch"} <= codes(validate(s))


def test_bad_isin_checksum_is_caught(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    last = s.holdings[0].isin[-1]
    s.holdings[0].isin = s.holdings[0].isin[:-1] + ("0" if last != "0" else "1")
    assert "bad_isin" in codes(validate(s))


def test_wrong_weight_is_caught(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    s.holdings[0].weight_pct = D("40.00")
    assert "weight_mismatch" in codes(validate(s))


def test_transaction_outside_period_is_caught(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    s.transactions[0].date = date(2026, 6, 1)
    assert "date_out_of_period" in codes(validate(s))


def test_unrecognised_transaction_type_is_caught(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    s.transactions[0].type = TransactionType.OTHER
    assert "unrecognised_transaction_type" in codes(validate(s))


def test_aif_units_nav_mismatch_is_caught(samples):
    s = expected(samples, "sterling_aif").model_copy(deep=True)
    s.summary.nav_per_unit += D("10")
    assert "units_nav_mismatch" in codes(validate(s))


def test_uncalled_above_commitment_is_caught(samples):
    s = expected(samples, "helix_aif").model_copy(deep=True)
    s.summary.capital_uncalled = D("6000000")
    assert "commitment_inconsistent" in codes(validate(s))


def test_missing_closing_value_is_caught(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    s.summary.closing_value = None
    assert "missing_closing_value" in codes(validate(s))


def test_pms_without_holdings_is_caught(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    s.holdings = []
    assert "pms_without_holdings" in codes(validate(s))


# ---------------------------------------------------------------- tolerance and warnings
def test_rounding_difference_is_tolerated(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    s.summary.closing_value += D("0.50")
    assert validate(s).errors == []


def test_fee_mismatch_is_only_a_warning(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    s.summary.fees_charged = D("9000.00")
    report = validate(s)
    assert "fee_mismatch" in codes(report)
    assert "fee_mismatch" in {i.code for i in report.warnings}


def test_non_inr_currency_is_a_warning(samples):
    s = expected(samples, "abc_pms").model_copy(deep=True)
    s.currency = "USD"
    report = validate(s)
    assert "non_inr_currency" in codes(report)
    assert report.status == Status.PARSED_OK