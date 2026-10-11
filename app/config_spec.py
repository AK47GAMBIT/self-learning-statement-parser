"""The shape of a learned format config. The LLM (Part 7) must produce exactly this.

Everything here is data. There is no code, so nothing in a config can execute.
"""
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schema import Holding, Summary, Transaction
from app.transforms import is_known_transform

ALLOWED_FIELDS = {"client_id", "strategy_name", "period_start", "period_end", "currency"}
SUMMARY_FIELDS = set(Summary.model_fields)
HOLDING_FIELDS = set(Holding.model_fields)
TRANSACTION_FIELDS = set(Transaction.model_fields)


class _Strict(BaseModel):
    # Unknown keys are errors, so a hallucinated field is rejected, not ignored.
    model_config = ConfigDict(extra="forbid")


def _check_transform(v: str) -> str:
    if not is_known_transform(v):
        raise ValueError(f"unknown transform {v!r}")
    return v


class FieldRule(_Strict):
    """Find a single value in the page text using a regex with a capture group."""
    regex: str
    transform: str = "text"
    group: int = 1
    optional: bool = False

    @field_validator("transform")
    @classmethod
    def _known(cls, v):
        return _check_transform(v)

    @model_validator(mode="after")
    def _regex_ok(self):
        try:
            compiled = re.compile(self.regex)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}")
        if compiled.groups < self.group:
            raise ValueError(
                f"regex has {compiled.groups} capture group(s) but group={self.group}")
        return self


class SummaryRule(_Strict):
    """Find the line whose FIRST CELL equals `label` (case-insensitive); read one cell."""
    label: str
    transform: str = "number"
    cell: int = -1             # -1 = last cell on the line
    optional: bool = False

    @field_validator("transform")
    @classmethod
    def _known(cls, v):
        return _check_transform(v)


class ColumnRule(_Strict):
    header: str                # header cell text, matched case-insensitively
    field: str
    transform: str = "text"
    optional: bool = False     # True: a missing column is allowed

    @field_validator("transform")
    @classmethod
    def _known(cls, v):
        return _check_transform(v)


class TableRule(_Strict):
    header_starts_with: str    # text the header line starts with
    stop_at: list[str] = Field(default_factory=list)   # line prefixes ending the table
    columns: list[ColumnRule]
    skip_if_blank: list[str] = Field(default_factory=list)  # fields that must be non-empty

    @model_validator(mode="after")
    def _has_columns(self):
        if not self.columns:
            raise ValueError("a table needs at least one column")
        return self


class FormatConfig(_Strict):
    format_name: str
    statement_type: Literal["pms", "aif"]
    provider: str
    currency: str = "INR"      # default, used if no 'currency' field rule is given
    fields: dict[str, FieldRule]
    summary: dict[str, SummaryRule] = Field(default_factory=dict)
    holdings: Optional[TableRule] = None
    transactions: Optional[TableRule] = None

    @field_validator("fields")
    @classmethod
    def _check_fields(cls, v):
        missing = {"client_id", "period_start", "period_end"} - set(v)
        if missing:
            raise ValueError(f"missing required fields: {sorted(missing)}")
        unknown = set(v) - ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"unknown field names: {sorted(unknown)}")
        return v

    @field_validator("summary")
    @classmethod
    def _check_summary_keys(cls, v):
        unknown = set(v) - SUMMARY_FIELDS
        if unknown:
            raise ValueError(f"unknown summary fields: {sorted(unknown)}")
        return v

    @model_validator(mode="after")
    def _check_table_fields(self):
        for label, table, allowed in (("holdings", self.holdings, HOLDING_FIELDS),
                                      ("transactions", self.transactions, TRANSACTION_FIELDS)):
            if table is None:
                continue
            names = [c.field for c in table.columns]
            unknown = set(names) - allowed
            if unknown:
                raise ValueError(f"{label}: unknown column fields {sorted(unknown)}")
            if len(names) != len(set(names)):
                raise ValueError(f"{label}: a field is mapped by more than one column")
        return self