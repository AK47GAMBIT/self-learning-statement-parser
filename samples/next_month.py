"""A 'next month' ABC statement, plus two broken variants, for tests and demos.

    python -m samples.next_month
"""
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from app.schema import Statement, StatementType, Summary, Transaction, TransactionType
from samples.generate import render_pdf
from samples.sample_data import _holdings

HERE = Path(__file__).parent


def abc_may(tamper: bool = False) -> Statement:
    rows = [
        ("Reliance Industries", "INE002A0101", 100, "3000.00", "310000.00"),
        ("HDFC Bank", "INE040A0103", 220, "1600.00", "290000.00"),
    ]
    equity = sum(D(q) * D(p) for _, _, q, p, _ in rows)      # 652,000
    cash = D("50000.00")
    closing = equity + cash                                   # 702,000
    s = Statement(
        statement_type=StatementType.PMS,
        provider="ABC Portfolio Managers",
        strategy_name="ABC Large Cap Growth",
        client_id="AB1234",
        period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
        holdings=_holdings(rows, closing),
        transactions=[
            Transaction(date=date(2026, 5, 29), type=TransactionType.FEE,
                        security_name="Management fee", amount=D("5000.00")),
        ],
        summary=Summary(
            opening_value=D("650000.00"), closing_value=closing, cash_balance=cash,
            fees_charged=D("5000.00"), capital_inflows=D("0.00"),
            capital_outflows=D("0.00"), net_gain=D("57000.00"),
        ),
    )
    if tamper:                       # printed value no longer equals quantity x price
        s.holdings[0].market_value += D("10000")
    return s


def write_demo_files(out_dir=HERE / "pdfs"):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    render_pdf("abc_pms", abc_may(), out / "abc_pms_may.pdf")
    render_pdf("abc_pms", abc_may(tamper=True), out / "abc_pms_bad_numbers.pdf")
    render_pdf("abc_pms", abc_may(), out / "abc_pms_units.pdf", qty_header="Units")
    print("wrote abc_pms_may.pdf, abc_pms_bad_numbers.pdf, abc_pms_units.pdf")


if __name__ == "__main__":
    write_demo_files()
    