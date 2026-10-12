from pathlib import Path

import pytest

from app.config_spec import ColumnRule, FormatConfig, TableRule
from app.engine import ParseError, extract_table, parse
from app.reader import Cell, Document, Line, read_document, strip_page_furniture
from app.validation import validate
from samples.stress import write_stress_files

CONFIGS = Path(__file__).parent.parent / "configs"


# ====================================================================== page furniture
def L(page, top, text):
    return Line(page, float(top), (Cell(text, 36.0, 36.0 + 4 * len(text)),), float(top) + 8.0)


def texts(doc):
    return [ln.text for ln in doc.lines]


def test_running_headers_and_footers_are_removed_but_the_title_is_kept():
    doc = Document(lines=[
        L(1, 40, "ABC Portfolio Managers"), L(1, 300, "Opening Value"), L(1, 810, "Page 1 of 3"),
        L(2, 20, "ABC Portfolio Managers"), L(2, 300, "Some row"), L(2, 810, "Page 2 of 3"),
        L(3, 20, "ABC Portfolio Managers"), L(3, 810, "Page 3 of 3"),
    ], page_count=3, page_heights={1: 842.0, 2: 842.0, 3: 842.0})
    strip_page_furniture(doc)
    assert texts(doc) == ["ABC Portfolio Managers", "Opening Value", "Some row"]
    assert doc.lines[0].page == 1                       # the page-1 title survived


def test_text_seen_in_the_margin_of_only_one_page_is_kept():
    doc = Document(lines=[L(1, 300, "Body"), L(2, 20, "ABC Confidential"), L(2, 300, "Body 2")],
                   page_count=2, page_heights={1: 842.0, 2: 842.0})
    strip_page_furniture(doc)
    assert "ABC Confidential" in texts(doc)             # cannot tell furniture from content


def test_rows_that_merely_look_alike_are_not_furniture():
    doc = Document(lines=[L(1, 800, "Sample Company 17 Ltd 5"), L(2, 800, "Sample Company 46 Ltd 5")],
                   page_count=2, page_heights={1: 842.0, 2: 842.0})
    strip_page_furniture(doc)
    assert len(doc.lines) == 2                          # exact text differs, so both stay


def test_single_page_documents_are_untouched():
    doc = Document(lines=[L(1, 20, "Title"), L(1, 810, "Page 1")],
                   page_count=1, page_heights={1: 842.0})
    strip_page_furniture(doc)
    assert len(doc.lines) == 2


# ====================================================================== table logic
X = {"name": (36.0, 100.0), "isin": (150.0, 220.0), "qty": (260.0, 290.0), "value": (330.0, 400.0)}
HEADER = dict(name="Name", isin="ISIN", qty="Qty", value="Value")
RULE = TableRule(header_starts_with="Name", stop_at=["Total"], columns=[
    ColumnRule(header="Name", field="security_name"),
    ColumnRule(header="ISIN", field="isin", transform="isin"),
    ColumnRule(header="Qty", field="quantity", transform="number"),
    ColumnRule(header="Value", field="market_value", transform="number"),
])


def ln(page, top, **cells):                       # keyword order must run left to right
    return Line(page, float(top), tuple(Cell(t, *X[k]) for k, t in cells.items()),
                float(top) + 8.0)


def data(page, top, name, n):
    return ln(page, top, name=name, isin=f"INE00{n}A01010", qty=str(n * 10), value=f"{n},000.00")


def run(lines, pages=1, issues=None):
    issues = [] if issues is None else issues
    rows = extract_table(Document(lines=lines, page_count=pages), RULE, issues, "holdings")
    return [r["security_name"] for r in rows], issues


def test_table_continues_on_the_next_page_and_skips_the_repeated_header():
    names, issues = run([
        ln(1, 100, **HEADER), data(1, 116, "Alpha", 1), data(1, 132, "Beta", 2),
        ln(2, 40, **HEADER), data(2, 56, "Gamma", 3), ln(2, 72, name="Total", value="6,000.00"),
    ], pages=2)
    assert names == ["Alpha", "Beta", "Gamma"] and issues == []


def test_running_header_above_a_repeated_header_is_skipped():
    names, _ = run([
        ln(1, 100, **HEADER), data(1, 116, "Alpha", 1),
        ln(2, 20, name="ABC Portfolio Managers - Confidential"),
        ln(2, 40, **HEADER), data(2, 56, "Beta", 2),
    ], pages=2)
    assert names == ["Alpha", "Beta"]


def test_running_header_directly_above_a_data_row_is_skipped():
    names, _ = run([
        ln(1, 100, **HEADER), data(1, 116, "Alpha", 1),
        ln(2, 20, name="ABC Portfolio Managers - Confidential"), data(2, 40, "Beta", 2),
    ], pages=2)
    assert names == ["Alpha", "Beta"]


def test_continued_marker_does_not_end_the_table():
    names, _ = run([
        ln(1, 100, **HEADER), data(1, 116, "Alpha", 1), ln(1, 132, name="(continued)"),
        ln(2, 40, **HEADER), data(2, 56, "Beta", 2),
    ], pages=2)
    assert names == ["Alpha", "Beta"]


def test_name_wrapped_below_its_row_is_joined():
    names, issues = run([
        ln(1, 100, **HEADER),
        data(1, 116, "Consolidated Infrastructure", 1), ln(1, 128, name="Development Limited"),
        data(1, 144, "Beta", 2),
    ])
    assert names == ["Consolidated Infrastructure Development Limited", "Beta"] and issues == []


def test_name_wrapped_above_its_row_is_joined():
    names, _ = run([
        ln(1, 100, **HEADER),
        ln(1, 116, name="Consolidated Infrastructure"),
        ln(1, 128, name="Development Limited", isin="INE001A01010", qty="10", value="1,000.00"),
        data(1, 160, "Beta", 2),
    ])
    assert names == ["Consolidated Infrastructure Development Limited", "Beta"]


def test_name_wrapped_over_three_lines_is_joined():
    names, _ = run([
        ln(1, 100, **HEADER),
        ln(1, 116, name="One"), ln(1, 128, name="Two"),
        ln(1, 140, name="Three", isin="INE001A01010", qty="10", value="1,000.00"),
    ])
    assert names == ["One Two Three"]


def test_tightly_spaced_rows_with_numbers_are_never_merged():
    names, _ = run([
        ln(1, 100, **HEADER), data(1, 116, "Alpha", 1),
        data(1, 125.6, "Beta", 2), data(1, 135.2, "Gamma", 3),     # dense table
    ])
    assert names == ["Alpha", "Beta", "Gamma"]


def test_a_section_heading_still_ends_the_table():
    names, _ = run([
        ln(1, 100, **HEADER), data(1, 116, "Alpha", 1), data(1, 132, "Beta", 2),
        ln(1, 170, name="Cash Ledger"), data(1, 200, "Zeta", 9),
    ])
    assert names == ["Alpha", "Beta"]


def test_same_header_after_the_table_ended_is_an_error_not_a_guess():
    names, issues = run([
        ln(1, 100, **HEADER), data(1, 116, "Alpha", 1), ln(1, 132, name="Total", value="1,000.00"),
        ln(1, 200, **HEADER), data(1, 216, "Zeta", 9),
    ])
    assert names == ["Alpha"] and any("appears again" in i for i in issues)


def test_earlier_issues_no_longer_hide_a_table():
    issues = ["an earlier problem"]
    names, issues = run([ln(1, 100, **HEADER), data(1, 116, "Alpha", 1)], issues=issues)
    assert names == ["Alpha"] and issues == ["an earlier problem"]


# ====================================================================== real messy PDFs
@pytest.fixture(scope="module")
def stress(tmp_path_factory):
    return write_stress_files(tmp_path_factory.mktemp("stress"))


@pytest.fixture(scope="module")
def abc_config():
    return FormatConfig.model_validate_json((CONFIGS / "abc_pms.json").read_text())


@pytest.mark.parametrize("name", ["stress_long_abc", "stress_two_pages", "stress_wrapped_above"])
def test_messy_statement_parses_exactly_and_validates(stress, abc_config, name):
    path, expected = stress[name]
    got = parse(read_document(path), abc_config)
    assert got.model_dump(exclude={"source_file_hash"}) == \
           expected.model_dump(exclude={"source_file_hash"})
    assert validate(got).errors == []


def test_the_long_statement_really_spans_pages_and_loses_its_furniture(stress):
    doc = read_document(stress["stress_long_abc"][0])
    assert doc.page_count >= 3
    assert len({ln.page for ln in doc.lines if ln.text.startswith("Sample Company")}) >= 2
    assert not any(ln.text.startswith("Page ") for ln in doc.lines)


def test_two_page_statement_has_exactly_two_pages(stress):
    assert read_document(stress["stress_two_pages"][0]).page_count == 2


def test_without_furniture_removal_the_engine_fails_loudly_instead_of_dropping_rows(
        stress, abc_config):
    raw = read_document(stress["stress_long_abc"][0], strip_furniture=False)
    with pytest.raises(ParseError, match="appears again"):
        parse(raw, abc_config)

def test_text_lines_that_never_reach_a_data_row_still_end_the_table():
    names, _ = run([
        ln(1, 100, **HEADER), data(1, 116, "Alpha", 1),
        ln(1, 150, name="Notes"), ln(1, 162, name="Some footnote"),
    ])
    assert names == ["Alpha"]