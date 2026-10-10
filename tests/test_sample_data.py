from decimal import Decimal as D

from app.isin_util import is_valid_isin
from samples.sample_data import ALL_SAMPLES


def test_all_isins_valid():
    for name, build in ALL_SAMPLES.items():
        for h in build().holdings:
            assert is_valid_isin(h.isin), f"{name}: {h.security_name} has bad ISIN {h.isin}"


def test_holdings_plus_cash_equal_closing():
    for name, build in ALL_SAMPLES.items():
        s = build()
        if not s.holdings:
            continue
        total = sum(h.market_value for h in s.holdings) + s.summary.cash_balance
        assert total == s.summary.closing_value, name


def test_weights_sum_close_to_equity_share():
    for name, build in ALL_SAMPLES.items():
        s = build()
        if not s.holdings:
            continue
        equity_pct = sum(h.weight_pct for h in s.holdings)
        expected = sum(h.market_value for h in s.holdings) / s.summary.closing_value * 100
        assert abs(equity_pct - expected) < D("0.1"), name

def test_aif_closing_is_units_times_nav():
    for name in ("sterling_aif", "helix_aif"):
        s = ALL_SAMPLES[name]()
        assert s.summary.units_held * s.summary.nav_per_unit == s.summary.closing_value, name