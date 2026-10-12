"""Deliberately messy variants of the ABC statement, to harden the engine.

    python -m samples.stress           # writes samples/stress_pdfs/
"""
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from app.schema import Statement, StatementType, Summary, Transaction, TransactionType
from samples.generate import render_pdf
from samples.sample_data import _holdings

HERE = Path(__file__).parent


def long_statement(n: int = 80) -> Statement:
    """n holdings; every fifth has a long name that wraps onto two lines in the PDF."""
    rows = []
    for i in range(n):
        name = (f"Consolidated Infrastructure Development {i:02d} Limited" if i % 5 == 0
                else f"Sample Company {i:02d} Ltd")
        qty = 10 + i
        price = D(100 + 7 * i).quantize(D("0.01"))
        cost = (D(qty) * price * D("0.9")).quantize(D("0.01"))
        rows.append((name, f"INE9{i:02d}A0101", qty, str(price), str(cost)))

    equity = sum(D(q) * D(p) for _, _, q, p, _ in rows)
    cash = D("100000.00")
    closing = equity + cash
    opening, inflows, outflows, fees = D("1500000.00"), D("300000.00"), D("0.00"), D("12000.00")
    return Statement(
        statement_type=StatementType.PMS,
        provider="ABC Portfolio Managers",
        strategy_name="ABC Large Cap Growth",
        client_id="AB1234",
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
        holdings=_holdings(rows, closing),
        transactions=[
            Transaction(date=date(2026, 4, 6), type=TransactionType.BUY,
                        security_name="Sample Company 01 Ltd", quantity=D("5"),
                        amount=D("1250.00")),
            Transaction(date=date(2026, 4, 22), type=TransactionType.DIVIDEND,
                        security_name="Sample Company 02 Ltd", amount=D("3000.00")),
            Transaction(date=date(2026, 4, 30), type=TransactionType.FEE,
                        security_name="Management fee", amount=fees),
        ],
        summary=Summary(
            opening_value=opening, closing_value=closing, cash_balance=cash,
            fees_charged=fees, capital_inflows=inflows, capital_outflows=outflows,
            net_gain=closing - opening - inflows + outflows + fees,
        ),
    )


def build_all() -> dict:
    return {
        # 3+ pages, header repeated on each page, running header + page footers,
        # long names wrapped BELOW their row
        "stress_long_abc": (long_statement(80), dict(
            furniture=True, repeat_header=True, wrap=True, valign="TOP")),
        # 2 pages, NO repeated header: only a running header on page 2
        "stress_two_pages": (long_statement(45), dict(
            furniture=True, wrap=True, valign="TOP")),
        # one page, long names wrapped ABOVE their row (bottom-aligned cells)
        "stress_wrapped_above": (long_statement(12), dict(wrap=True)),
    }


def write_stress_files(out_dir=HERE / "stress_pdfs") -> dict:
    """Returns {name: (pdf_path, expected Statement)}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, (statement, options) in build_all().items():
        path = out / f"{name}.pdf"
        render_pdf("abc_pms", statement, path, **options)
        result[name] = (path, statement)
    return result


if __name__ == "__main__":
    for name, (path, _) in write_stress_files().items():
        print("wrote", path)