"""Fictional statement data. The PDFs and expected.json are both generated from this."""
from datetime import date
from decimal import Decimal as D

from app.isin_util import make_isin
from app.schema import (
    Holding, Statement, StatementType, Summary, Transaction, TransactionType,
)

T = TransactionType


def _holdings(rows, total_value):
    """rows: (name, isin_first_11, qty, price, cost). Weight is computed from value."""
    out = []
    for name, isin11, qty, price, cost in rows:
        value = (D(qty) * D(price)).quantize(D("0.01"))
        out.append(Holding(
            security_name=name,
            isin=make_isin(isin11),
            quantity=D(qty),
            cost=D(cost),
            market_price=D(price),
            market_value=value,
            weight_pct=(value / total_value * 100).quantize(D("0.01")),
        ))
    return out


# ---------------------------------------------------------------- 1. ABC (PMS)
def abc_pms() -> Statement:
    rows = [
        ("Reliance Industries", "INE002A0101", 120, "2875.00", "310000.00"),
        ("HDFC Bank", "INE040A0103", 200, "1550.00", "290000.00"),
        ("Infosys", "INE009A0102", 150, "1480.00", "205000.00"),
        ("Tata Consultancy Services", "INE467B0102", 40, "3950.00", "140000.00"),
        ("ITC", "INE154A0102", 500, "420.00", "190000.00"),
    ]
    equity = sum(D(q) * D(p) for _, _, q, p, _ in rows)      # 1,215,000
    cash = D("85000.00")
    closing = equity + cash                                   # 1,300,000
    hold = _holdings(rows, closing)
    return Statement(
        statement_type=StatementType.PMS,
        provider="ABC Portfolio Managers",
        strategy_name="ABC Large Cap Growth",
        client_id="AB1234",
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
        holdings=hold,
        transactions=[
            Transaction(date=date(2026, 4, 6), type=T.BUY, security_name="Infosys",
                        quantity=D("50"), amount=D("72000.00")),
            Transaction(date=date(2026, 4, 15), type=T.SELL, security_name="ITC",
                        quantity=D("100"), amount=D("41000.00")),
            Transaction(date=date(2026, 4, 22), type=T.DIVIDEND, security_name="HDFC Bank",
                        amount=D("3000.00")),
            Transaction(date=date(2026, 4, 30), type=T.FEE,
                        security_name="Management fee", amount=D("8000.00")),
        ],
        summary=Summary(
            opening_value=D("1200000.00"),
            closing_value=closing,
            cash_balance=cash,
            fees_charged=D("8000.00"),
            capital_inflows=D("50000.00"),
            capital_outflows=D("0.00"),
            net_gain=closing - D("1200000.00") - D("50000.00") + D("8000.00"),  # 58,000
        ),
    )


# ---------------------------------------------------------------- 2. XYZ (PMS)
def xyz_wealth() -> Statement:
    rows = [
        ("Bajaj Finance", "INE296A0102", 60, "7100.00", "390000.00"),
        ("Larsen & Toubro", "INE018A0103", 90, "3600.00", "270000.00"),
        ("Asian Paints", "INE021A0102", 70, "2400.00", "150000.00"),
        ("Maruti Suzuki India", "INE585B0101", 25, "12500.00", "280000.00"),
    ]
    equity = sum(D(q) * D(p) for _, _, q, p, _ in rows)      # 1,375,000
    cash = D("125000.00")
    closing = equity + cash                                   # 1,500,000
    hold = _holdings(rows, closing)
    return Statement(
        statement_type=StatementType.PMS,
        provider="XYZ Wealth",
        strategy_name="XYZ Multicap Opportunities",
        client_id="98765",
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
        holdings=hold,
        transactions=[
            Transaction(date=date(2026, 4, 3), type=T.INFLOW,
                        security_name="Funds added", amount=D("100000.00")),
            Transaction(date=date(2026, 4, 9), type=T.BUY, security_name="Asian Paints",
                        quantity=D("20"), amount=D("47000.00")),
            Transaction(date=date(2026, 4, 17), type=T.OUTFLOW,
                        security_name="Withdrawal", amount=D("-30000.00")),
            Transaction(date=date(2026, 4, 30), type=T.FEE,
                        security_name="Management fee", amount=D("-12000.00")),
        ],
        summary=Summary(
            opening_value=D("1350000.00"),
            closing_value=closing,
            cash_balance=cash,
            fees_charged=D("12000.00"),
            capital_inflows=D("100000.00"),
            capital_outflows=D("30000.00"),
            net_gain=closing - D("1350000.00") - D("100000.00") + D("30000.00") + D("12000.00"),  # 92,000
        ),
    )


# ---------------------------------------------------------------- 3. Sterling (AIF)
def sterling_aif() -> Statement:
    units = D("1000.0000")
    nav = D("1285.4300")
    closing = (units * nav).quantize(D("0.01"))               # 1,285,430.00
    opening = D("1100000.00")
    called = D("100000.00")
    distributed = D("0.00")
    fees = D("5000.00")
    gain = closing - opening - called + distributed + fees    # 90,430
    return Statement(
        statement_type=StatementType.AIF,
        provider="Sterling Growth Fund",
        strategy_name="Sterling Growth Fund - Category II",
        client_id="SGF-00457",
        period_start=date(2026, 1, 1), period_end=date(2026, 3, 31),
        transactions=[
            Transaction(date=date(2026, 2, 10), type=T.INFLOW,
                        security_name="Capital call - 3rd drawdown", amount=called),
            Transaction(date=date(2026, 3, 31), type=T.FEE,
                        security_name="Management fee", amount=fees),
        ],
        summary=Summary(
            opening_value=opening,
            closing_value=closing,
            nav_per_unit=nav,
            units_held=units,
            fees_charged=fees,
            capital_inflows=called,
            capital_outflows=distributed,
            net_gain=gain,
        ),
    )


# ---------------------------------------------------------------- 4. Meridian (PMS)
def meridian_capital() -> Statement:
    rows = [
        ("Sun Pharmaceutical Industries", "INE044A0103", 180, "1650.00", "240000.00"),
        ("Titan Company", "INE280A0102", 60, "3400.00", "175000.00"),
        ("Axis Bank", "INE238A0103", 250, "1180.00", "260000.00"),
    ]
    equity = sum(D(q) * D(p) for _, _, q, p, _ in rows)      # 797,000
    cash = D("203000.00")
    closing = equity + cash                                   # 1,000,000
    hold = _holdings(rows, closing)
    return Statement(
        statement_type=StatementType.PMS,
        provider="Meridian Capital",
        strategy_name="Meridian Value Equity",
        client_id="MC/2291/B",
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
        holdings=hold,
        transactions=[
            Transaction(date=date(2026, 4, 2), type=T.BUY, security_name="Axis Bank",
                        quantity=D("50"), amount=D("-57500.00")),
            Transaction(date=date(2026, 4, 14), type=T.DIVIDEND, security_name="Sun Pharmaceutical Industries",
                        amount=D("4500.00")),
            Transaction(date=date(2026, 4, 28), type=T.SELL, security_name="Titan Company",
                        quantity=D("20"), amount=D("67000.00")),
            Transaction(date=date(2026, 4, 30), type=T.FEE,
                        security_name="Performance fee", amount=D("-6000.00")),
        ],
        summary=Summary(
            opening_value=D("950000.00"),
            closing_value=closing,
            cash_balance=cash,
            fees_charged=D("6000.00"),
            capital_inflows=D("0.00"),
            capital_outflows=D("0.00"),
            net_gain=closing - D("950000.00") + D("6000.00"),  # 56,000
        ),
    )

# ---------------------------------------------------------------- 5. Helix (AIF)
def helix_aif() -> Statement:
    units = D("2500.0000")
    nav = D("1320.4000")
    closing = (units * nav).quantize(D("0.01"))               # 3,301,000.00
    opening = D("2600000.00")
    called = D("400000.00")
    distributed = D("50000.00")
    fees = D("45000.00")
    gain = closing - opening - called + distributed + fees    # 396,000
    return Statement(
        statement_type=StatementType.AIF,
        provider="Helix Capital Partners",
        strategy_name="Helix Private Credit Fund II",
        client_id="HCF-2024-118",
        period_start=date(2025, 10, 1), period_end=date(2026, 3, 31),
        transactions=[
            Transaction(date=date(2025, 11, 15), type=T.INFLOW,
                        security_name="Capital call - 4th drawdown", amount=called),
            Transaction(date=date(2026, 1, 20), type=T.OUTFLOW,
                        security_name="Distribution of income", amount=distributed),
            Transaction(date=date(2026, 3, 31), type=T.FEE,
                        security_name="Management fee", amount=fees),
        ],
        summary=Summary(
            opening_value=opening,
            closing_value=closing,
            nav_per_unit=nav,
            units_held=units,
            fees_charged=fees,
            capital_inflows=called,
            capital_outflows=distributed,
            net_gain=gain,
            capital_committed=D("5000000.00"),
            capital_uncalled=D("2000000.00"),   # 3,000,000 called to date
        ),
    )

ALL_SAMPLES = {
    "abc_pms": abc_pms,
    "xyz_wealth": xyz_wealth,
    "meridian_capital": meridian_capital,
    "sterling_aif": sterling_aif,
    "helix_aif": helix_aif,
}

# Passwords are fake, for demo only. Real ones would never be committed.
PASSWORDS = {"xyz_wealth": "demo1234"}