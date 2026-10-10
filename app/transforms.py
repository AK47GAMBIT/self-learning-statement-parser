"""Whitelist of trusted value-parsing functions.

The LLM may only *name* these in a config. It can never add new ones.
Every transform takes a raw string and returns a clean value, or None if the
cell is empty. If the text can't be understood, it raises TransformError
(loud failure, never a silent wrong number).
"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from app.schema import TransactionType


class TransformError(ValueError):
    """The raw text could not be interpreted by this transform."""


class UnknownTransformError(KeyError):
    """A config named a transform that is not in the registry."""


_EMPTY_MARKERS = {"", "-", "--", "—", "–", "nil", "n/a", "na"}
_CURRENCY_RE = re.compile(r"(₹|rs\.?|inr)", re.IGNORECASE)
_PLAIN_NUMBER_RE = re.compile(r"^(\d+(\.\d*)?|\.\d+)$")


def _is_empty(raw: Optional[str]) -> bool:
    return raw is None or str(raw).strip().lower() in _EMPTY_MARKERS


# ---------- numbers ----------

def _to_decimal(raw: Optional[str], *, drcr: bool = False, eu: bool = False) -> Optional[Decimal]:
    if _is_empty(raw):
        return None

    s = _CURRENCY_RE.sub("", str(raw)).strip()
    negative = False

    # Trailing Dr/Cr, only when this transform explicitly allows it
    if drcr:
        m = re.search(r"(dr|cr)\.?$", s, re.IGNORECASE)
        if m:
            negative = m.group(1).lower() == "dr"
            s = s[: m.start()].strip()

    # Brackets mean negative: (1,234.00)
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    # Leading or trailing minus (including the unicode minus sign)
    if s and s[0] in "-−":
        negative = True
        s = s[1:].strip()
    elif s and s[-1] in "-−":
        negative = True
        s = s[:-1].strip()

    s = s.replace(" ", "")
    # Removing commas handles both 3,45,000 (Indian) and 345,000 (international).
    if eu:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")

    if not _PLAIN_NUMBER_RE.match(s):
        raise TransformError(f"Cannot read {raw!r} as a number")

    try:
        value = Decimal(s)
    except InvalidOperation as e:
        raise TransformError(f"Cannot read {raw!r} as a number") from e
    return -value if negative else value


def number(raw: Optional[str]) -> Optional[Decimal]:
    """1,23,456.78 | 123,456.78 | (1,234) | -1,234 | 1,234- | Rs. 500"""
    return _to_decimal(raw)


def number_drcr(raw: Optional[str]) -> Optional[Decimal]:
    """Like number, but a trailing Dr means negative and Cr means positive."""
    return _to_decimal(raw, drcr=True)


def number_eu(raw: Optional[str]) -> Optional[Decimal]:
    """European style: 1.234,56"""
    return _to_decimal(raw, eu=True)


def percent(raw: Optional[str]) -> Optional[Decimal]:
    """12.5% -> Decimal('12.5')  (kept as a percentage, not 0.125)"""
    if _is_empty(raw):
        return None
    return _to_decimal(str(raw).replace("%", ""))


# ---------- dates ----------

def _make_date_parser(fmt: str) -> Callable[[Optional[str]], Optional[date]]:
    def parse(raw: Optional[str]) -> Optional[date]:
        if _is_empty(raw):
            return None
        cleaned = " ".join(str(raw).split())
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError as e:
            raise TransformError(f"Cannot read {raw!r} as a date ({fmt})") from e

    return parse


# ---------- text ----------

def text(raw: Optional[str]) -> Optional[str]:
    if _is_empty(raw):
        return None
    return " ".join(str(raw).split())


def isin(raw: Optional[str]) -> Optional[str]:
    if _is_empty(raw):
        return None
    return str(raw).replace(" ", "").upper()


_TXN_KEYWORDS = [  # order matters: first match wins
    (TransactionType.DIVIDEND, ["dividend"]),
    (TransactionType.FEE, ["fee", "charge", "brokerage"]),
    (TransactionType.BUY, ["buy", "purchase", "bought"]),
    (TransactionType.SELL, ["sell", "sale", "sold"]),
    (TransactionType.INFLOW, ["inflow", "deposit", "contribution", "capital call",
                              "subscription", "funds added"]),
    (TransactionType.OUTFLOW, ["outflow", "withdraw", "redemption", "distribution"]),
]


def transaction_type(raw: Optional[str]) -> Optional[TransactionType]:
    if _is_empty(raw):
        return None
    label = str(raw).lower()
    for txn_type, keywords in _TXN_KEYWORDS:
        if any(k in label for k in keywords):
            return txn_type
    return TransactionType.OTHER  # Part 6 will flag statements containing OTHER


def number_abs(raw: Optional[str]) -> Optional[Decimal]:
    """Magnitude only. For fields we store as positive amounts (fees, outflows)
    that some providers print in brackets or with a minus sign."""
    value = _to_decimal(raw)
    return None if value is None else abs(value)


# ---------- the registry ----------

TRANSFORMS: dict[str, Callable[[Optional[str]], Any]] = {
    "text": text,
    "isin": isin,
    "number": number,
    "number_drcr": number_drcr,
    "number_eu": number_eu,
    "percent": percent,
    "txn_type": transaction_type,
    "date_dd_mon_yyyy": _make_date_parser("%d-%b-%Y"),       # 01-Apr-2026
    "date_dd_mm_yyyy": _make_date_parser("%d/%m/%Y"),        # 01/04/2026
    "date_dd_mm_yyyy_dash": _make_date_parser("%d-%m-%Y"),   # 01-04-2026
    "date_iso": _make_date_parser("%Y-%m-%d"),               # 2026-04-01
    "date_dd_month_yyyy": _make_date_parser("%d %B %Y"),     # 1 April 2026
    "date_month_dd_yyyy": _make_date_parser("%B %d, %Y"),    # April 1, 2026
    "number_abs": number_abs,
}


def apply_transform(name: str, raw: Optional[str]) -> Any:
    """The only way the engine runs a transform. Unknown names are rejected."""
    if name not in TRANSFORMS:
        raise UnknownTransformError(f"Transform {name!r} is not in the registry")
    return TRANSFORMS[name](raw)


def is_known_transform(name: str) -> bool:
    return name in TRANSFORMS