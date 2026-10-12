"""Draws the sample PDFs and writes the matching expected.json answer keys.

Run from the project root:   python -m samples.generate
"""
import os
from decimal import Decimal as D
from pathlib import Path

import pikepdf
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

from app.schema import Statement, TransactionType as T
from samples.sample_data import ALL_SAMPLES, PASSWORDS
from typing import Optional

HERE = Path(__file__).parent


# ============================================================ number / date formatting
def indian(x, dp=2) -> str:
    """1234567.5 -> 12,34,567.50  (lakh/crore grouping)"""
    q = D(x).quantize(D(1).scaleb(-dp))
    sign = "-" if q < 0 else ""
    int_part, _, frac = f"{abs(q):.{dp}f}".partition(".")
    if len(int_part) > 3:
        head, tail = int_part[:-3], int_part[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        int_part = ",".join(groups + [tail])
    return sign + int_part + (f".{frac}" if dp else "")


def intl(x, dp=2) -> str:
    """1234567.5 -> 1,234,567.50  (international grouping)"""
    return f"{D(x).quantize(D(1).scaleb(-dp)):,.{dp}f}"


def brackets(x, fmt) -> str:
    return f"({fmt(abs(x))})" if x < 0 else fmt(x)


def drcr(x, fmt) -> str:
    return f"{fmt(abs(x))} {'Dr' if x < 0 else 'Cr'}"


def qty(x) -> str:
    return "" if x is None else f"{x.normalize():f}"


def d_mon(d):   return d.strftime("%d-%b-%Y")        # 01-Apr-2026
def d_slash(d): return d.strftime("%d/%m/%Y")        # 01/04/2026
def d_dash(d):  return d.strftime("%d-%m-%Y")        # 01-04-2026
def d_iso(d):   return d.isoformat()                 # 2026-04-01
def d_long(d):  return f"{d.day:02d} {d:%B} {d.year}"  # 01 April 2026
def d_long_np(d): return f"{d.day} {d:%B} {d.year}"    # 1 April 2026


# ============================================================ drawing helpers
_ss = getSampleStyleSheet()
TITLE = ParagraphStyle("title", parent=_ss["Title"], fontSize=18, alignment=0, spaceAfter=2)
H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontSize=11, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("body", parent=_ss["BodyText"], fontSize=9)


def P(text, style=BODY):
    return Paragraph(text, style)


def make_table(rows, widths, right_cols=(), header=True, total_row=False, box=False, font=8,
               repeat_rows=0, valign=None):
    t = Table(rows, colWidths=widths, hAlign="LEFT", repeatRows=repeat_rows)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), font),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for c in right_cols:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    if header:
        style += [("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                  ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black)]
    if total_row:
        style += [("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                  ("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black)]
    if box:
        style += [("BOX", (0, 0), (-1, -1), 1, colors.black),
                  ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke)]
    if valign:
        style.append(("VALIGN", (0, 0), (-1, -1), valign))
    t.setStyle(TableStyle(style))
    return t


def kv_table(pairs, widths=(220, 120), box=False):
    return make_table([list(p) for p in pairs], list(widths), right_cols=(1,),
                      header=False, box=box, font=9)


def info_row(left, right):
    return make_table([[left, right]], [230, 270], header=False, font=9)


def txn_table(s, labels, date_fn, amount_fn, headers, widths, with_qty=True):
    rows = [headers]
    for t in s.transactions:
        row = [date_fn(t.date), labels[t.type], t.security_name or ""]
        if with_qty:
            row.append(qty(t.quantity))
        row.append(amount_fn(t.amount))
        rows.append(row)
    return make_table(rows, widths, right_cols=(len(headers) - 1,) if not with_qty
                      else (len(headers) - 2, len(headers) - 1))


# ============================================================ 1. ABC (PMS): baseline
def _wrap_name(name: str, limit: int = 28) -> str:
    """Break a long name over two lines (the PDF shows two lines; the data stays one name)."""
    if len(name) <= limit:
        return name
    cut = name.rfind(" ", 0, limit)
    return name if cut <= 0 else name[:cut] + "\n" + name[cut + 1:]


def abc_story(s: Statement, qty_header: str = "Qty", wrap: bool = False,
              repeat_header: bool = False, valign: Optional[str] = None):
    sm = s.summary
    rows = [["Security", "ISIN", qty_header, "Cost", "Mkt Price", "Mkt Value", "Weight %"]]
    for h in s.holdings:
        name = _wrap_name(h.security_name) if wrap else h.security_name
        rows.append([name, h.isin, qty(h.quantity), indian(h.cost),
                     indian(h.market_price), indian(h.market_value), str(h.weight_pct)])
    rows.append(["Total", "", "", indian(sum(h.cost for h in s.holdings)), "",
                 indian(sum(h.market_value for h in s.holdings)),
                 str(sum(h.weight_pct for h in s.holdings))])
    labels = {T.BUY: "Buy", T.SELL: "Sell", T.DIVIDEND: "Dividend",
              T.FEE: "Fee", T.INFLOW: "Inflow", T.OUTFLOW: "Outflow"}
    return [
        P("ABC Portfolio Managers", TITLE),
        P("Portfolio Statement", H2),
        info_row(f"Client Code: {s.client_id}",
                 f"Period: {d_mon(s.period_start)} to {d_mon(s.period_end)}"),
        info_row(f"Strategy: {s.strategy_name}", f"Currency: {s.currency}"),
        P("Account Summary", H2),
        kv_table([
            ("Opening Value", indian(sm.opening_value)),
            ("Add: Inflows", indian(sm.capital_inflows)),
            ("Less: Outflows", indian(sm.capital_outflows)),
            ("Less: Fees Charged", indian(sm.fees_charged)),
            ("Net Gain", indian(sm.net_gain)),
            ("Closing Value", indian(sm.closing_value)),
            ("Cash Balance", indian(sm.cash_balance)),
        ]),
        P("Equity Holdings", H2),
        make_table(rows, [135, 80, 38, 68, 58, 72, 50], right_cols=(2, 3, 4, 5, 6),
                   total_row=True, repeat_rows=1 if repeat_header else 0, valign=valign),
        P("Transactions", H2),
        txn_table(s, labels, d_mon, indian,
                  ["Date", "Type", "Security", "Qty", "Amount"], [70, 60, 190, 50, 90]),
    ]


# ============================================================ 2. XYZ (PMS): brackets, dd/mm/yyyy
def xyz_story(s: Statement):
    sm = s.summary
    rows = [["Stock Name", "Units Held", "Cost (INR)", "Value (INR)", "ISIN Code", "% of Portfolio"]]
    for h in s.holdings:
        rows.append([h.security_name, qty(h.quantity), intl(h.cost), intl(h.market_value),
                     h.isin, f"{h.weight_pct}%"])
    labels = {T.BUY: "Purchase", T.SELL: "Sale", T.DIVIDEND: "Dividend",
              T.FEE: "Fee", T.INFLOW: "Deposit", T.OUTFLOW: "Withdrawal"}
    return [
        P("XYZ Wealth", TITLE),
        P(f"Statement for {s.period_end:%B %Y}", H2),
        P(f"Investor ID ....... {s.client_id}"),
        P(f"Scheme : {s.strategy_name}"),
        P(f"Statement Period : {d_slash(s.period_start)} to {d_slash(s.period_end)}"),
        P(f"All amounts in {s.currency}"),
        P("Account Summary", H2),
        kv_table([
            ("Beginning Balance", intl(sm.opening_value)),
            ("Deposits", intl(sm.capital_inflows)),
            ("Withdrawals", brackets(-sm.capital_outflows, intl)),
            ("Fees & Charges", brackets(-sm.fees_charged, intl)),
            ("Gain for the Period", intl(sm.net_gain)),
            ("Ending Balance", intl(sm.closing_value)),
            ("Cash / Liquid Balance", intl(sm.cash_balance)),
        ]),
        P("Portfolio Holdings", H2),
        make_table(rows, [140, 55, 75, 75, 80, 70], right_cols=(1, 2, 3, 5)),
        P("Transaction History", H2),
        txn_table(s, labels, d_slash, lambda a: brackets(a, intl),
                  ["Txn Date", "Transaction", "Scrip", "Units", "Amount (INR)"],
                  [70, 70, 170, 45, 90]),
    ]


# ============================================================ 3. Meridian (PMS): summary box + Dr/Cr
def meridian_story(s: Statement):
    sm = s.summary
    rows = [["Instrument", "ISIN", "No. of Shares", "Book Cost", "Rate", "Market Value", "Wt %"]]
    for h in s.holdings:
        rows.append([h.security_name, h.isin, qty(h.quantity), indian(h.cost),
                     indian(h.market_price), indian(h.market_value), str(h.weight_pct)])
    rows.append(["Total Equity", "", "", indian(sum(h.cost for h in s.holdings)), "",
                 indian(sum(h.market_value for h in s.holdings)), ""])
    labels = {T.BUY: "Purchase", T.SELL: "Sale", T.DIVIDEND: "Dividend Received",
              T.FEE: "Fee", T.INFLOW: "Inflow", T.OUTFLOW: "Outflow"}
    return [
        P("Meridian Capital", TITLE),
        P("Portfolio Valuation Report", H2),
        kv_table([
            ("Portfolio value at start", indian(sm.opening_value)),
            ("Portfolio value at end", indian(sm.closing_value)),
            ("Cash balance", indian(sm.cash_balance)),
            ("Charges for the period", indian(sm.fees_charged)),
        ], widths=(220, 120), box=True),
        P(f"Client Ref: {s.client_id}"),
        P(f"Mandate: {s.strategy_name}"),
        P(f"From {d_long(s.period_start)} to {d_long(s.period_end)}"),
        P(f"Reporting currency: {s.currency}"),
        P("Securities Held", H2),
        make_table(rows, [140, 80, 60, 65, 50, 70, 35], right_cols=(2, 3, 4, 5, 6),
                   total_row=True),
        P("Cash Ledger", H2),
        txn_table(s, labels, d_long, lambda a: drcr(a, indian),
                  ["Value Date", "Nature", "Particulars", "Qty", "Amount"],
                  [90, 85, 170, 40, 90]),
    ]


# ============================================================ 4. Sterling (AIF): quarterly capital account
def sterling_story(s: Statement):
    sm = s.summary
    labels = {T.INFLOW: "Capital Call", T.FEE: "Fee", T.OUTFLOW: "Distribution"}
    return [
        P("Sterling Growth Fund", TITLE),
        P("Capital Account Statement", H2),
        info_row(f"Investor Code: {s.client_id}", f"Currency: {s.currency}"),
        P(f"Fund: {s.strategy_name}"),
        P(f"Period: {d_iso(s.period_start)} to {d_iso(s.period_end)}"),
        P("Capital Account", H2),
        kv_table([
            ("Opening Capital Account Balance", intl(sm.opening_value)),
            ("Add: Capital Contributions", intl(sm.capital_inflows)),
            ("Less: Distributions", intl(sm.capital_outflows)),
            ("Less: Management Fees", intl(sm.fees_charged)),
            ("Add: Net Investment Gain", intl(sm.net_gain)),
            ("Closing Capital Account Balance", intl(sm.closing_value)),
        ], widths=(250, 120)),
        P("Unit Information", H2),
        kv_table([
            ("Units Held", intl(sm.units_held, 4)),
            ("NAV per Unit (INR)", intl(sm.nav_per_unit, 4)),
        ], widths=(250, 120)),
        P("Capital Movements", H2),
        txn_table(s, labels, d_iso, intl,
                  ["Date", "Type", "Description", "Amount (INR)"],
                  [80, 80, 200, 90], with_qty=False),
    ]


# ============================================================ 5. Helix (AIF): sectioned, half-yearly
def helix_story(s: Statement):
    sm = s.summary
    called = sm.capital_committed - sm.capital_uncalled
    labels = {T.INFLOW: "Capital Call", T.OUTFLOW: "Distribution", T.FEE: "Fee"}
    return [
        P("Helix Capital Partners", TITLE),
        P("Half-Yearly Investor Statement", H2),
        P(f"Investor Code: {s.client_id}"),
        P(f"Fund: {s.strategy_name}"),
        P(f"Reporting period: {d_long_np(s.period_start)} to {d_long_np(s.period_end)}"),
        P(f"Currency: {s.currency}"),
        P("SECTION A - COMMITMENT", H2),
        kv_table([
            ("Total Commitment", intl(sm.capital_committed)),
            ("Capital Called to Date", intl(called)),
            ("Uncalled Commitment", intl(sm.capital_uncalled)),
        ], widths=(250, 120)),
        P("SECTION B - CAPITAL ACCOUNT", H2),
        kv_table([
            ("Opening Balance", intl(sm.opening_value)),
            ("Drawdowns during the period", intl(sm.capital_inflows)),
            ("Distributions during the period", intl(sm.capital_outflows)),
            ("Fees and expenses", intl(sm.fees_charged)),
            ("Net income / gain", intl(sm.net_gain)),
            ("Closing Balance", intl(sm.closing_value)),
        ], widths=(250, 120)),
        P("SECTION C - UNIT INFORMATION", H2),
        kv_table([
            ("Units outstanding", intl(sm.units_held, 4)),
            ("NAV per unit", intl(sm.nav_per_unit, 4)),
        ], widths=(250, 120)),
        P("SECTION D - CASH MOVEMENTS", H2),
        txn_table(s, labels, d_dash, intl,
                  ["Date", "Type", "Narration", "Amount"],
                  [80, 80, 200, 90], with_qty=False),
    ]


STORIES = {
    "abc_pms": abc_story,
    "xyz_wealth": xyz_story,
    "meridian_capital": meridian_story,
    "sterling_aif": sterling_story,
    "helix_aif": helix_story,
}

# The answer key must contain only what the PDF actually shows.
# XYZ prints no per-share price; Meridian prints no flow breakdown or net gain.
NOT_SHOWN = {
    "xyz_wealth": {"holding": ["market_price"]},
    "meridian_capital": {"summary": ["capital_inflows", "capital_outflows", "net_gain"]},
}


def visible_copy(name: str, s: Statement) -> Statement:
    s2 = s.model_copy(deep=True)
    hidden = NOT_SHOWN.get(name, {})
    for field in hidden.get("holding", []):
        for h in s2.holdings:
            setattr(h, field, None)
    for field in hidden.get("summary", []):
        setattr(s2.summary, field, None)
    return s2


def encrypt_pdf(path: Path, password: str):
    tmp = path.with_suffix(".tmp")
    with pikepdf.open(path) as pdf:
        pdf.save(tmp, encryption=pikepdf.Encryption(user=password, owner=password + "-owner"))
    os.replace(tmp, path)

def _furniture(canvas, doc):
    """A running header on pages 2+ and a 'Page n' footer on every page."""
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    if doc.page > 1:
        canvas.drawString(36, A4[1] - 22, "ABC Portfolio Managers - Confidential")
    canvas.drawString(36, 20, f"Page {doc.page}")
    canvas.restoreState()


def render_pdf(name: str, s: Statement, path: Path, furniture: bool = False, **story_kwargs):
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=36, rightMargin=36,
                            topMargin=36, bottomMargin=36, title=s.provider)
    story = STORIES[name](s, **story_kwargs)
    if furniture:
        doc.build(story, onFirstPage=_furniture, onLaterPages=_furniture)
    else:
        doc.build(story)


def generate_all(out_dir=HERE):
    out = Path(out_dir)
    pdf_dir, exp_dir = out / "pdfs", out / "expected"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    exp_dir.mkdir(parents=True, exist_ok=True)

    for name, build in ALL_SAMPLES.items():
        s = build()
        path = pdf_dir / f"{name}.pdf"
        render_pdf(name, s, path)
        if name in PASSWORDS:
            encrypt_pdf(path, PASSWORDS[name])
        (exp_dir / f"{name}.json").write_text(
            visible_copy(name, s).model_dump_json(indent=2), encoding="utf-8")
        print(f"generated {name}" + ("  (password protected)" if name in PASSWORDS else ""))


if __name__ == "__main__":
    generate_all()