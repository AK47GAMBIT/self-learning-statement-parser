from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schema import Holding, Statement, StatementType, Summary


def make_statement(**overrides):
    data = dict(
        statement_type=StatementType.PMS,
        provider="ABC Portfolio Managers",
        client_id="AB1234",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
    )
    data.update(overrides)
    return Statement(**data)


def test_minimal_pms_statement():
    s = make_statement(holdings=[
        Holding(security_name="Reliance Industries", isin="ine002a01018",
                quantity=Decimal("120"), market_value=Decimal("345000.00")),
    ])
    assert s.currency == "INR"
    assert s.holdings[0].isin == "INE002A01018"       # normalised
    assert s.summary.nav_per_unit is None             # PMS has no NAV


def test_aif_statement_without_holdings():
    s = make_statement(
        statement_type=StatementType.AIF,
        summary=Summary(nav_per_unit=Decimal("1234.5678"), units_held=Decimal("1000")),
    )
    assert s.holdings == []
    assert s.summary.nav_per_unit == Decimal("1234.5678")


def test_period_end_before_start_is_rejected():
    with pytest.raises(ValidationError):
        make_statement(period_start=date(2026, 5, 1), period_end=date(2026, 4, 1))


def test_missing_client_id_is_rejected():
    with pytest.raises(ValidationError):
        make_statement(client_id="")


def test_bad_isin_is_kept_not_rejected():
    # Flagging bad ISINs is validation's job (Part 6), not the schema's.
    h = Holding(security_name="X", isin="NOTANISIN", market_value=Decimal("1"))
    assert h.isin == "NOTANISIN"


def test_decimal_survives_json_round_trip():
    s = make_statement(summary=Summary(closing_value=Decimal("655000.10")))
    restored = Statement.model_validate_json(s.model_dump_json())
    assert restored.summary.closing_value == Decimal("655000.10")