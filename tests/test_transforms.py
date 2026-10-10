from datetime import date
from decimal import Decimal

import pytest

from app.schema import TransactionType
from app.transforms import (
    TransformError, UnknownTransformError, apply_transform, is_known_transform,
)


@pytest.mark.parametrize("raw, expected", [
    ("3,45,000.00", Decimal("345000.00")),   # Indian grouping
    ("345,000.00", Decimal("345000.00")),    # international grouping
    ("(1,234.00)", Decimal("-1234.00")),     # brackets
    ("-1,234.50", Decimal("-1234.50")),      # leading minus
    ("1,234.50-", Decimal("-1234.50")),      # trailing minus
    ("₹ 1,00,000", Decimal("100000")),
    ("Rs. 5,000.50", Decimal("5000.50")),
    ("  120  ", Decimal("120")),
    ("-", None),
    ("", None),
    (None, None),
])
def test_number(raw, expected):
    assert apply_transform("number", raw) == expected


@pytest.mark.parametrize("raw", ["abc", "12.5.3", "1,2x4", "Dr 500"])
def test_number_rejects_garbage(raw):
    with pytest.raises(TransformError):
        apply_transform("number", raw)


def test_number_does_not_silently_accept_drcr():
    # Dr/Cr must be opted into via number_drcr. Otherwise it fails loudly.
    with pytest.raises(TransformError):
        apply_transform("number", "1,234.00 Dr")


@pytest.mark.parametrize("raw, expected", [
    ("1,234.00 Dr", Decimal("-1234.00")),
    ("1,234.00 Cr", Decimal("1234.00")),
    ("500 CR", Decimal("500")),
    ("500", Decimal("500")),
])
def test_number_drcr(raw, expected):
    assert apply_transform("number_drcr", raw) == expected


def test_number_eu():
    assert apply_transform("number_eu", "1.234,56") == Decimal("1234.56")


@pytest.mark.parametrize("raw, expected", [
    ("12.5%", Decimal("12.5")),
    ("12.5 %", Decimal("12.5")),
    ("(2.5)%", Decimal("-2.5")),
    ("-", None),
])
def test_percent(raw, expected):
    assert apply_transform("percent", raw) == expected


@pytest.mark.parametrize("name, raw, expected", [
    ("date_dd_mon_yyyy", "01-Apr-2026", date(2026, 4, 1)),
    ("date_dd_mon_yyyy", "01-APR-2026", date(2026, 4, 1)),
    ("date_dd_mm_yyyy", "01/04/2026", date(2026, 4, 1)),
    ("date_dd_mm_yyyy_dash", "01-04-2026", date(2026, 4, 1)),
    ("date_iso", "2026-04-01", date(2026, 4, 1)),
    ("date_dd_month_yyyy", "1 April 2026", date(2026, 4, 1)),
    ("date_month_dd_yyyy", "April 1, 2026", date(2026, 4, 1)),
    ("date_iso", "", None),
])
def test_dates(name, raw, expected):
    assert apply_transform(name, raw) == expected


@pytest.mark.parametrize("name, raw", [
    ("date_dd_mm_yyyy", "31/02/2026"),   # impossible date
    ("date_dd_mm_yyyy", "2026-04-01"),   # wrong style for this transform
    ("date_dd_mon_yyyy", "hello"),
])
def test_dates_reject_bad_input(name, raw):
    with pytest.raises(TransformError):
        apply_transform(name, raw)


def test_dd_mm_is_not_mm_dd():
    # 04/05/2026 is 4 May under dd/mm. The config must pick the right one.
    assert apply_transform("date_dd_mm_yyyy", "04/05/2026") == date(2026, 5, 4)


@pytest.mark.parametrize("raw, expected", [
    ("Purchase", TransactionType.BUY),
    ("SALE", TransactionType.SELL),
    ("Dividend Received", TransactionType.DIVIDEND),
    ("Management Fee", TransactionType.FEE),
    ("Capital Call", TransactionType.INFLOW),
    ("Withdrawal", TransactionType.OUTFLOW),
    ("something weird", TransactionType.OTHER),
])
def test_txn_type(raw, expected):
    assert apply_transform("txn_type", raw) == expected


def test_isin_and_text():
    assert apply_transform("isin", "ine002a01018 ") == "INE002A01018"
    assert apply_transform("text", "  Reliance   Industries ") == "Reliance Industries"


def test_unknown_transform_is_rejected():
    assert not is_known_transform("do_something_clever")
    with pytest.raises(UnknownTransformError):
        apply_transform("do_something_clever", "x")