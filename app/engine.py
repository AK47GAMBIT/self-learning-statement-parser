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
from typing import Optional

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
# A line is a WRAP of the previous one only if the gap between them is under this fraction
# of the line height. In our layouts a wrapped line sits half a line-height below (0.5) and
# a new row a full line-height below (1.0); 0.75 sits between them.
WRAP_GAP = 0.75
# These transforms accept almost any text, so they cannot prove that a line is a data row.
WEAK_TRANSFORMS = {"text", "isin", "txn_type"}


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


def _tight(a: Optional[Line], b: Optional[Line]) -> bool:
    """True if b sits directly under a, closer than a new row would."""
    if a is None or b is None or a.page != b.page:
        return False
    ha, hb = a.bottom - a.top, b.bottom - b.top
    if ha <= 0 or hb <= 0:
        return False                      # no vertical extent recorded: never guess
    return (b.top - a.bottom) < WRAP_GAP * min(ha, hb)


def _looks_like_data(ln: Line, columns: dict, rules: dict) -> bool:
    """Does this line parse as a data row (at least one number or date that reads cleanly)?"""
    try:
        placed = _assign(ln, columns)
    except TransformError:
        return False
    strong = [f for f in placed if rules[f].transform not in WEAK_TRANSFORMS]
    if not strong:
        return False
    try:
        values = [apply_transform(rules[f].transform, placed[f]) for f in strong]
    except TransformError:
        return False
    return any(v is not None for v in values)


def _append(row: dict, placed: dict) -> None:
    """Add a wrapped line's text after what the row already holds in each column."""
    for f, t in placed.items():
        row[f] = f"{row[f]} {t}" if row.get(f) else t

def _leads_to_data(lines: list, j: int, columns: dict, rules: dict, text_fields: set,
                   max_lines: int = 4) -> bool:
    """True if line j is followed, through lines that each sit tightly under the one above,
    by a data row. That is what a name wrapped over several lines above its row looks like.
    A heading followed by a far-away table, or by a text line that never reaches data,
    returns False."""
    k = j
    for _ in range(max_lines):
        if k + 1 >= len(lines) or not _tight(lines[k], lines[k + 1]):
            return False
        nxt = lines[k + 1]
        if _looks_like_data(nxt, columns, rules):
            return True
        try:
            placed = _assign(nxt, columns)
        except TransformError:
            return False
        if not all(f in text_fields for f in placed):
            return False
        k += 1
    return False

def extract_table(doc: Document, rule: TableRule, issues: list[str], label: str) -> list[dict]:
    problems: list[str] = []          # this table's own problems; never hidden by other tables
    idx = _find_header(doc, rule, problems, label)
    if idx is None:
        issues.extend(problems)
        return []
    header = doc.lines[idx]
    columns = _column_cells(header, rule, problems, label)
    if problems:
        issues.extend(problems)
        return []

    lines = doc.lines
    rules = {c.field: c for c in rule.columns}
    text_fields = {f for f, c in rules.items() if c.transform == "text"}
    stops = [_norm(s) for s in rule.stop_at]
    header_key = _norm(header.text)

    rows: list[tuple[dict, Line]] = []     # (raw text per field, line the row started on)
    last_row_line: Optional[Line] = None   # last line that belonged to a row
    pending: dict = {}                     # text-only lines waiting for the row below them
    pending_line: Optional[Line] = None
    ended = False

    for j in range(idx + 1, len(lines)):
        ln = lines[j]
        nxt = lines[j + 1] if j + 1 < len(lines) else None
        text = _norm(ln.text)
        single = len(ln.cells) == 1

        if ended:
            # After the table ended, the same header again is ambiguous: a continuation,
            # or a second table? We cannot tell, so we refuse to guess.
            if text == header_key:
                problems.append(
                    f"{label}: the header line appears again on page {ln.page} after the "
                    f"table ended; cannot tell a continuation from a second table")
                break
            continue

        if text == header_key:                          # header repeated on a later page
            pending, pending_line = {}, None
            continue
        if any(text.startswith(s) for s in stops):      # totals row: the table is over
            ended = True
            pending, pending_line = {}, None
            continue
        if single and "continued" in text:              # "(continued)" marker
            continue
        if (single and lines[j - 1].page != ln.page and nxt is not None
                and (_norm(nxt.text) == header_key or _looks_like_data(nxt, columns, rules))):
            continue                                    # running header at the top of a page

        try:
            placed = _assign(ln, columns)
        except TransformError as e:
            if single:
                ended = True                            # a heading far from every column
            else:
                problems.append(f"{label} row {ln.text!r}: {e}")
            continue

        if all(f in text_fields for f in placed):       # text only: a heading or a wrapped name
            if pending and _tight(pending_line, ln):    # a third line of a wrapped-above name
                _append(pending, placed)
                pending_line = ln
                continue
            if rows and not pending and _tight(last_row_line, ln):
                _append(rows[-1][0], placed)            # wrapped BELOW its row
                last_row_line = ln
                continue
            if _leads_to_data(lines, j, columns, rules, text_fields):
                pending, pending_line = dict(placed), ln    # wrapped ABOVE its row
                continue
            if single:                                  # a section heading: the table ended
                ended = True
                pending, pending_line = {}, None
                continue

        row = dict(placed)
        if pending:
            if _tight(pending_line, ln):
                for f, t in pending.items():
                    row[f] = f"{t} {row[f]}" if row.get(f) else t
            pending, pending_line = {}, None
        rows.append((row, ln))
        last_row_line = ln

    out: list[dict] = []
    for raw, first_line in rows:
        try:
            row = {f: apply_transform(rules[f].transform, raw.get(f)) for f in rules}
        except TransformError as e:
            problems.append(f"{label} row {first_line.text!r}: {e}")
            continue
        if any(row.get(f) is None for f in rule.skip_if_blank):
            continue
        out.append(row)

    if not out and not problems:
        problems.append(f"{label}: header found but no data rows")
    issues.extend(problems)
    return out

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