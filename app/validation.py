"""Consistency checks on a parsed Statement. No LLM, no guessing: pure arithmetic.

Errors  -> the numbers contradict each other  -> status needs_review
Warnings-> unusual but not provably wrong      -> status stays parsed_ok
Skipped -> not enough data to run the check    -> reported, never hidden
"""
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from app.isin_util import is_valid_isin
from app.schema import Statement, StatementType, TransactionType

REL_TOL = Decimal("0.001")        # 0.1%
ABS_TOL = Decimal("1.00")         # one rupee
WEIGHT_TOL = Decimal("0.10")      # percentage points
WEIGHT_SUM_MAX = Decimal("100.5")


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class Status(str, Enum):
    PARSED_OK = "parsed_ok"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def status(self) -> Status:
        return Status.NEEDS_REVIEW if self.errors else Status.PARSED_OK

    def error(self, code: str, message: str):
        self.issues.append(Issue(code, Severity.ERROR, message))

    def warn(self, code: str, message: str):
        self.issues.append(Issue(code, Severity.WARNING, message))

    def ok(self, check: str):
        self.passed.append(check)

    def skip(self, check: str, reason: str):
        self.skipped[check] = reason

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "issues": [{"code": i.code, "severity": i.severity.value, "message": i.message}
                       for i in self.issues],
            "passed": self.passed,
            "skipped": self.skipped,
        }


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(ABS_TOL, REL_TOL * max(abs(a), abs(b)))


def _names(items, limit=3) -> str:
    items = list(items)
    shown = ", ".join(str(i) for i in items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


# ------------------------------------------------------------------ individual checks
def _check_required(s: Statement, rep: ValidationReport):
    sm = s.summary
    if sm.closing_value is None:
        rep.error("missing_closing_value", "closing value was not extracted")
    elif sm.closing_value < 0:
        rep.error("negative_closing_value", f"closing value is negative ({sm.closing_value})")
    else:
        rep.ok("closing_value_present")

    if s.statement_type == StatementType.PMS and not s.holdings:
        rep.error("pms_without_holdings", "a PMS statement must contain holdings")
    if s.statement_type == StatementType.AIF:
        if sm.nav_per_unit is None or sm.units_held is None:
            rep.warn("aif_missing_units_or_nav", "AIF statement has no units held or NAV per unit")


def _check_isins(s: Statement, rep: ValidationReport):
    missing = [h.security_name for h in s.holdings if h.isin is None]
    bad = [f"{h.security_name} ({h.isin})" for h in s.holdings
           if h.isin is not None and not is_valid_isin(h.isin)]
    if bad:
        rep.error("bad_isin", f"ISIN failed format or checksum: {_names(bad)}")
    if missing:
        rep.warn("missing_isin", f"no ISIN for: {_names(missing)}")
    if not bad and not missing:
        rep.ok("isin_checksums")


def _check_duplicate_isins(s: Statement, rep: ValidationReport):
    counts = Counter(h.isin for h in s.holdings if h.isin)
    dups = [i for i, n in counts.items() if n > 1]
    if dups:
        rep.warn("duplicate_isin", f"same ISIN appears in several rows: {_names(dups)}")


def _check_holding_arithmetic(s: Statement, rep: ValidationReport):
    rows = [h for h in s.holdings if h.quantity is not None and h.market_price is not None]
    if not rows:
        rep.skip("holding_arithmetic", "no holdings with both quantity and price")
        return
    bad = [h.security_name for h in rows
           if not _close(h.quantity * h.market_price, h.market_value)]
    if bad:
        rep.error("holding_arithmetic",
                  f"quantity x price does not equal market value for: {_names(bad)}")
    else:
        rep.ok("holding_arithmetic")


def _check_holdings_total(s: Statement, rep: ValidationReport):
    sm = s.summary
    if sm.closing_value is None or sm.cash_balance is None:
        rep.skip("holdings_total", "closing value or cash balance missing")
        return
    total = sum(h.market_value for h in s.holdings)
    if _close(total + sm.cash_balance, sm.closing_value):
        rep.ok("holdings_total")
    else:
        rep.error("holdings_total_mismatch",
                  f"holdings {total} + cash {sm.cash_balance} = {total + sm.cash_balance}, "
                  f"but closing value is {sm.closing_value}")


def _check_weights(s: Statement, rep: ValidationReport):
    rows = [h for h in s.holdings if h.weight_pct is not None]
    if not rows:
        rep.skip("weights", "no weights in statement")
        return

    total_mv = sum(h.market_value for h in s.holdings)
    bases = {}
    if s.summary.closing_value and s.summary.closing_value > 0:
        bases["closing value"] = s.summary.closing_value
    if total_mv > 0:
        bases["holdings total"] = total_mv

    fits = any(
        all(abs(h.market_value / base * 100 - h.weight_pct) <= WEIGHT_TOL for h in rows)
        for base in bases.values()
    )
    if not fits:
        rep.error("weight_mismatch",
                  "weights do not match market value / closing value or / holdings total")
    else:
        rep.ok("weights_match_values")

    weight_sum = sum(h.weight_pct for h in rows)
    if weight_sum > WEIGHT_SUM_MAX:
        rep.error("weight_sum_over_100", f"weights add up to {weight_sum}%")
    else:
        rep.ok("weight_sum")


def _check_units_nav(s: Statement, rep: ValidationReport):
    sm = s.summary
    if sm.units_held is None or sm.nav_per_unit is None or sm.closing_value is None:
        rep.skip("units_x_nav", "units, NAV or closing value missing")
        return
    if _close(sm.units_held * sm.nav_per_unit, sm.closing_value):
        rep.ok("units_x_nav")
    else:
        rep.error("units_nav_mismatch",
                  f"units {sm.units_held} x NAV {sm.nav_per_unit} = "
                  f"{sm.units_held * sm.nav_per_unit}, but closing value is {sm.closing_value}")


def _check_commitment(s: Statement, rep: ValidationReport):
    sm = s.summary
    if sm.capital_committed is None or sm.capital_uncalled is None:
        return  # only some AIFs print this; nothing to report
    if sm.capital_uncalled < 0 or sm.capital_uncalled > sm.capital_committed:
        rep.error("commitment_inconsistent",
                  f"uncalled {sm.capital_uncalled} is outside 0..committed {sm.capital_committed}")
    else:
        rep.ok("commitment")


def _check_reconciliation(s: Statement, rep: ValidationReport):
    sm = s.summary
    needed = ["opening_value", "closing_value", "capital_inflows",
              "capital_outflows", "fees_charged", "net_gain"]
    missing = [n for n in needed if getattr(sm, n) is None]
    if missing:
        rep.skip("reconciliation", f"statement does not show: {', '.join(missing)}")
        return
    base = sm.opening_value + sm.capital_inflows - sm.capital_outflows
    with_fees = base - sm.fees_charged + sm.net_gain     # gain is before fees
    gain_net = base + sm.net_gain                        # gain already net of fees
    if _close(with_fees, sm.closing_value) or _close(gain_net, sm.closing_value):
        rep.ok("reconciliation")
    else:
        rep.error("reconciliation_mismatch",
                  f"opening {sm.opening_value} + inflows {sm.capital_inflows} - outflows "
                  f"{sm.capital_outflows} - fees {sm.fees_charged} + gain {sm.net_gain} = "
                  f"{with_fees}, but closing value is {sm.closing_value}")


def _check_dates(s: Statement, rep: ValidationReport):
    if not s.transactions:
        return
    outside = [f"{t.date}" for t in s.transactions
               if not (s.period_start <= t.date <= s.period_end)]
    if outside:
        rep.error("date_out_of_period",
                  f"transactions dated outside {s.period_start}..{s.period_end}: {_names(outside)}")
    else:
        rep.ok("transaction_dates")


def _check_txn_types(s: Statement, rep: ValidationReport):
    unknown = [t.security_name or str(t.date) for t in s.transactions
               if t.type == TransactionType.OTHER]
    if unknown:
        rep.error("unrecognised_transaction_type",
                  f"{len(unknown)} transaction(s) have an unrecognised type: {_names(unknown)}")


def _check_fees(s: Statement, rep: ValidationReport):
    fee_txns = [t for t in s.transactions if t.type == TransactionType.FEE]
    if s.summary.fees_charged is None or not fee_txns:
        rep.skip("fees", "no fee total or no fee transactions to compare")
        return
    total = sum(abs(t.amount) for t in fee_txns)      # sign conventions differ by provider
    if _close(total, abs(s.summary.fees_charged)):
        rep.ok("fees")
    else:
        rep.warn("fee_mismatch",
                 f"fee transactions add up to {total}, summary says {s.summary.fees_charged}")


def _check_currency(s: Statement, rep: ValidationReport):
    if s.currency != "INR":
        rep.warn("non_inr_currency", f"statement currency is {s.currency}")


# ------------------------------------------------------------------ entry point
def validate(s: Statement) -> ValidationReport:
    rep = ValidationReport()
    _check_required(s, rep)
    if s.holdings:
        _check_isins(s, rep)
        _check_duplicate_isins(s, rep)
        _check_holding_arithmetic(s, rep)
        _check_holdings_total(s, rep)
        _check_weights(s, rep)
    if s.statement_type == StatementType.AIF:
        _check_units_nav(s, rep)
    _check_commitment(s, rep)
    _check_reconciliation(s, rep)
    _check_dates(s, rep)
    _check_txn_types(s, rep)
    _check_fees(s, rep)
    _check_currency(s, rep)
    return rep


if __name__ == "__main__":
    # python -m app.validation xyz_wealth samples/pdfs/xyz_wealth.pdf [password]
    import json
    import sys
    from pathlib import Path

    from app.config_spec import FormatConfig
    from app.engine import parse
    from app.reader import read_document

    config = FormatConfig.model_validate_json(
        (Path("configs") / f"{sys.argv[1]}.json").read_text())
    document = read_document(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print(json.dumps(validate(parse(document, config)).to_dict(), indent=2))