"""The generic parsing engine. It knows nothing about any provider.

All provider knowledge lives in a FormatConfig. Given the same Document and the
same config, the output is always identical, and nothing here calls an LLM.

Design rule: never guess. If a required value can't be found or read, record an
issue; at the end all issues are raised together as one ParseError.
"""
import re
from app.config_spec import ColumnRule, TableRule
from app.reader import Cell
from app.schema import Holding, Transaction
from pydantic import ValidationError

from app.config_spec import FormatConfig, SummaryRule
from app.reader import Document, Line
from app.schema import Statement, StatementType, Summary
from app.transforms import TransformError, apply_transform

_SPACES = re.compile(r"\s+")


class ParseError(Exception):
    """The config could not be applied to this document. Carries every problem found."""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


def _norm(text: str) -> str:
    return _SPACES.sub(" ", text.strip().lower())


# ------------------------------------------------------------------ header fields
def extract_fields(doc: Document, cfg: FormatConfig, issues: list[str]) -> dict:
    text = doc.text
    out = {}
    for name, rule in cfg.fields.items():
        match = re.search(rule.regex, text)
        if not match:
            if not rule.optional:
                issues.append(f"field {name!r}: pattern not found in document")
            continue
        try:
            value = apply_transform(rule.transform, match.group(rule.group))
        except TransformError as e:
            issues.append(f"field {name!r}: {e}")
            continue
        if value is None:
            if not rule.optional:
                issues.append(f"field {name!r}: matched but the value was empty")
            continue
        out[name] = value
    return out


# ------------------------------------------------------------------ summary rows
def _lines_with_label(doc: Document, label: str) -> list[Line]:
    target = _norm(label)
    return [ln for ln in doc.lines
            if len(ln.cells) >= 2 and _norm(ln.cells[0].text) == target]


def _read_summary_rule(doc: Document, name: str, rule: SummaryRule, issues: list[str]):
    lines = _lines_with_label(doc, rule.label)
    if not lines:
        if not rule.optional:
            issues.append(f"summary {name!r}: no line labelled {rule.label!r}")
        return None

    values = set()
    for ln in lines:
        try:
            raw = ln.cells[rule.cell].text
        except IndexError:
            issues.append(f"summary {name!r}: line {ln.text!r} has no cell {rule.cell}")
            return None
        try:
            values.add(apply_transform(rule.transform, raw))
        except TransformError as e:
            issues.append(f"summary {name!r}: {e}")
            return None

    if len(values) > 1:
        # The same label printed twice with different numbers: don't pick one silently.
        issues.append(f"summary {name!r}: label {rule.label!r} appears with "
                      f"different values {sorted(map(str, values))}")
        return None
    return values.pop()


def extract_summary(doc: Document, cfg: FormatConfig, issues: list[str]) -> dict:
    out = {}
    for name, rule in cfg.summary.items():
        value = _read_summary_rule(doc, name, rule, issues)
        if value is not None:
            out[name] = value
    return out


# ------------------------------------------------------------------ tables
def _overlap(a: Cell, b: Cell) -> float:
    return max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))


def _find_header(doc: Document, rule: TableRule, issues: list[str], label: str):
    """Index of the first line starting with header_starts_with that holds all columns.

    If a line starts correctly but lacks some required columns, the error names
    exactly which ones (the likeliest sign that the provider renamed a column).
    """
    start = _norm(rule.header_starts_with)
    required = [c.header for c in rule.columns if not c.optional]
    best_missing = None
    for i, ln in enumerate(doc.lines):
        if not _norm(ln.text).startswith(start):
            continue
        texts = {_norm(c.text) for c in ln.cells}
        missing = [h for h in required if _norm(h) not in texts]
        if not missing:
            return i
        if best_missing is None or len(missing) < len(best_missing):
            best_missing = missing

    if best_missing:
        issues.append(f"{label}: header line found but column(s) {best_missing} "
                      f"are missing (layout may have changed)")
    else:
        issues.append(f"{label}: no header line starting with "
                      f"{rule.header_starts_with!r}")
    return None

def _column_cells(header: Line, rule: TableRule, issues: list[str], label: str) -> dict:
    """Map each ColumnRule.field -> its header Cell (None if optional and absent)."""
    by_text = {_norm(c.text): c for c in header.cells}
    out = {}
    for col in rule.columns:
        cell = by_text.get(_norm(col.header))
        if cell is None and not col.optional:
            issues.append(f"{label}: column {col.header!r} not in header")
        out[col.field] = cell
    return out


def _assign(line: Line, columns: dict) -> dict:
    """Assign each cell on the line to the column it overlaps most. Raises on ambiguity."""
    assigned: dict = {}
    for cell in line.cells:
        best_field, best = None, 0.0
        for field_name, hdr in columns.items():
            if hdr is None:
                continue
            o = _overlap(cell, hdr)
            if o > best:
                best_field, best = field_name, o
        if best_field is None:
            raise TransformError(f"cell {cell.text!r} sits under no known column")
        if best_field in assigned:
            raise TransformError(
                f"two cells ({assigned[best_field]!r}, {cell.text!r}) fall in column "
                f"{best_field!r}")
        assigned[best_field] = cell.text
    return assigned


def extract_table(doc: Document, rule: TableRule, issues: list[str], label: str) -> list[dict]:
    idx = _find_header(doc, rule, issues, label)
    if idx is None:
        return []
    header = doc.lines[idx]
    columns = _column_cells(header, rule, issues, label)
    if issues:
        return []

    stops = [_norm(s) for s in rule.stop_at]
    rules = {c.field: c for c in rule.columns}
    rows: list[dict] = []

    for ln in doc.lines[idx + 1:]:
        if ln.page != header.page:
            break                                   # multi-page tables: see limitations
        text = _norm(ln.text)
        if any(text.startswith(s) for s in stops):
            break
        # A line with a single cell is a new section heading, so the table has ended.
        if len(ln.cells) < 2:
            break
        try:
            raw = _assign(ln, columns)
            row = {f: apply_transform(rules[f].transform, raw.get(f)) for f in rules}
        except TransformError as e:
            issues.append(f"{label} row {ln.text!r}: {e}")
            continue
        blank_required = [f for f in rule.skip_if_blank if row.get(f) is None]
        if blank_required:
            continue
        rows.append(row)

    if not rows and not issues:
        issues.append(f"{label}: header found but no data rows")
    return rows


# ------------------------------------------------------------------ assembly
def parse_head(doc: Document, cfg: FormatConfig) -> Statement:
    """Statement-level fields and summary only. 5c adds holdings and transactions."""
    issues: list[str] = []
    fields = extract_fields(doc, cfg, issues)
    summary_values = extract_summary(doc, cfg, issues)
    if issues:
        raise ParseError(issues)

    currency = fields.pop("currency", None) or cfg.currency
    try:
        return Statement(
            statement_type=StatementType(cfg.statement_type),
            provider=cfg.provider,
            currency=currency,
            summary=Summary(**summary_values),
            **fields,
        )
    except ValidationError as e:
        raise ParseError([f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
                          for err in e.errors()]) from e

def parse(doc: Document, cfg: FormatConfig) -> Statement:
    """Full deterministic parse: header, summary, holdings, transactions."""
    issues: list[str] = []
    fields = extract_fields(doc, cfg, issues)
    summary_values = extract_summary(doc, cfg, issues)
    holdings = (extract_table(doc, cfg.holdings, issues, "holdings")
                if cfg.holdings else [])
    transactions = (extract_table(doc, cfg.transactions, issues, "transactions")
                    if cfg.transactions else [])
    if issues:
        raise ParseError(issues)

    currency = fields.pop("currency", None) or cfg.currency
    try:
        return Statement(
            statement_type=StatementType(cfg.statement_type),
            provider=cfg.provider,
            currency=currency,
            summary=Summary(**summary_values),
            holdings=[Holding(**h) for h in holdings],
            transactions=[Transaction(**t) for t in transactions],
            **fields,
        )
    except ValidationError as e:
        raise ParseError([f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
                          for err in e.errors()]) from e

if __name__ == "__main__":
    # python -m app.engine abc_pms samples/pdfs/abc_pms.pdf [password]
    import json
    import sys
    from pathlib import Path

    from app.reader import read_document

    config = FormatConfig.model_validate_json(
        (Path("configs") / f"{sys.argv[1]}.json").read_text())
    document = read_document(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print(json.dumps(json.loads(parse(document, config).model_dump_json()), indent=2))