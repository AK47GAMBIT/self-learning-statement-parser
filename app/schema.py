"""Standard output structure. Every provider's statement is converted into this."""
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StatementType(str, Enum):
    PMS = "pms"
    AIF = "aif"


class TransactionType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    FEE = "fee"
    INFLOW = "inflow"
    OUTFLOW = "outflow"
    OTHER = "other"  # unrecognised label; validation (Part 6) will flag these


class Holding(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    security_name: str = Field(min_length=1)
    isin: Optional[str] = None
    quantity: Optional[Decimal] = None
    cost: Optional[Decimal] = None
    market_price: Optional[Decimal] = None  # used later for qty x price check
    market_value: Decimal
    weight_pct: Optional[Decimal] = None  # 12.5 means 12.5%

    @field_validator("isin", mode="before")
    @classmethod
    def _normalise_isin(cls, v):
        # Only normalise here. Format and checksum are checked in validation (Part 6),
        # so a bad ISIN gets flagged instead of crashing the whole parse.
        if v is None:
            return None
        v = str(v).replace(" ", "").upper()
        return v or None


class Transaction(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    date: date
    type: TransactionType
    security_name: Optional[str] = None  # fees and inflows may have no security
    quantity: Optional[Decimal] = None
    amount: Decimal


class Summary(BaseModel):
    opening_value: Optional[Decimal] = None
    closing_value: Optional[Decimal] = None
    cash_balance: Optional[Decimal] = None
    fees_charged: Optional[Decimal] = None
    nav_per_unit: Optional[Decimal] = None   # AIF
    units_held: Optional[Decimal] = None     # AIF
    # Optional extras, used by the reconciliation check in Part 6:
    capital_inflows: Optional[Decimal] = None
    capital_outflows: Optional[Decimal] = None
    net_gain: Optional[Decimal] = None
        # AIF commitment tracking:
    capital_committed: Optional[Decimal] = None
    capital_uncalled: Optional[Decimal] = None


class Statement(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    statement_type: StatementType
    provider: str = Field(min_length=1)
    strategy_name: Optional[str] = None
    client_id: str = Field(min_length=1)
    period_start: date
    period_end: date
    currency: str = "INR"

    summary: Summary = Field(default_factory=Summary)
    holdings: list[Holding] = Field(default_factory=list)
    transactions: list[Transaction] = Field(default_factory=list)

    source_file_hash: Optional[str] = None  # filled by the pipeline later

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _check_period(self):
        if self.period_end < self.period_start:
            raise ValueError("period_end is before period_start")
        return self