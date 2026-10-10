import pytest
from app.isin_util import is_valid_isin, make_isin


@pytest.mark.parametrize("isin", [
    "INE002A01018",  # Reliance Industries
    "INE040A01034",  # HDFC Bank
    "INE009A01021",  # Infosys
    "US0378331005",  # Apple
])
def test_known_real_isins_are_valid(isin):
    assert is_valid_isin(isin)


@pytest.mark.parametrize("isin", [
    "INE002A01019",   # wrong check digit
    "INE002A0101",    # too short
    "12E002A01018",   # country code must be letters
    "",
])
def test_bad_isins_are_rejected(isin):
    assert not is_valid_isin(isin)


def test_make_isin_round_trip():
    assert make_isin("INE002A0101") == "INE002A01018"
    assert is_valid_isin(make_isin("INE123A0100"))