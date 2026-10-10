import shutil

import pytest

from app.reader import (
    NoTextLayerError, PasswordRequiredError, UnreadableDocumentError,
    WrongPasswordError, file_sha256, read_document,
)
from samples.generate import generate_all
from samples.sample_data import PASSWORDS


@pytest.fixture(scope="module")
def pdfs(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    generate_all(root)
    return root / "pdfs"


def find_line(doc, starts_with):
    return next(ln for ln in doc.lines if ln.text.startswith(starts_with))


def test_reads_plain_pdf(pdfs):
    doc = read_document(pdfs / "abc_pms.pdf")
    assert doc.page_count >= 1
    assert "ABC Portfolio Managers" in doc.text


def test_table_row_is_split_into_cells(pdfs):
    doc = read_document(pdfs / "abc_pms.pdf")
    row = find_line(doc, "Reliance Industries")
    assert [c.text for c in row.cells] == [
        "Reliance Industries", "INE002A01018", "120",
        "3,10,000.00", "2,875.00", "3,45,000.00", "25.94",
    ]


def test_label_value_row_has_two_cells(pdfs):
    doc = read_document(pdfs / "xyz_wealth.pdf", PASSWORDS["xyz_wealth"])
    row = find_line(doc, "Beginning Balance")
    assert row.cells[-1].text == "1,350,000.00"


def test_blank_cell_keeps_positions(pdfs):
    # "Funds added" has no Units value, so the row has one cell fewer than the
    # header. x-positions are how the engine will still map cells to columns.
    doc = read_document(pdfs / "xyz_wealth.pdf", PASSWORDS["xyz_wealth"])
    header = find_line(doc, "Txn Date")
    row = find_line(doc, "03/04/2026")
    assert len(row.cells) == len(header.cells) - 1


def test_password_required(pdfs):
    with pytest.raises(PasswordRequiredError):
        read_document(pdfs / "xyz_wealth.pdf")


def test_wrong_password(pdfs):
    with pytest.raises(WrongPasswordError):
        read_document(pdfs / "xyz_wealth.pdf", "not-the-password")


def test_correct_password(pdfs):
    doc = read_document(pdfs / "xyz_wealth.pdf", PASSWORDS["xyz_wealth"])
    assert "XYZ Wealth" in doc.text


def test_not_a_pdf(tmp_path):
    bad = tmp_path / "fake.pdf"
    bad.write_text("this is not a pdf")
    with pytest.raises(UnreadableDocumentError):
        read_document(bad)


def test_pdf_without_text_is_rejected(tmp_path):
    from reportlab.pdfgen import canvas
    blank = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(blank))
    c.rect(50, 50, 100, 100)   # a shape, but no text
    c.save()
    with pytest.raises(NoTextLayerError):
        read_document(blank)


def test_file_hash_is_stable_and_content_based(pdfs, tmp_path):
    copy = tmp_path / "copy.pdf"
    shutil.copy(pdfs / "abc_pms.pdf", copy)
    assert file_sha256(copy) == file_sha256(pdfs / "abc_pms.pdf")
    assert file_sha256(pdfs / "abc_pms.pdf") != file_sha256(pdfs / "helix_aif.pdf")