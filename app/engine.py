"""The generic parsing engine. It knows nothing about any provider.

All provider knowledge lives in a FormatConfig. Given the same Document and the
same config, the output is always identical, and nothing here calls an LLM.

Design rule: never guess. If a required value can't be found or read, record an
issue; at the end all issues are raised together as one ParseError.
"""
import re

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


if __name__ == "__main__":
    # python -m app.engine abc_pms samples/pdfs/abc_pms.pdf [password]
    import json
    import sys
    from pathlib import Path

    from app.reader import read_document

    config = FormatConfig.model_validate_json(
        (Path("configs") / f"{sys.argv[1]}.json").read_text())
    document = read_document(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print(json.dumps(json.loads(parse_head(document, config).model_dump_json()), indent=2))