"""Reads a PDF into a normalised form: pages -> lines -> cells (with x-positions).

Nothing outside this module touches pdfplumber.
"""
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

# Words closer than this (in PDF points) belong to the same cell. A normal space at
# 8-9pt is about 2-3pt; the gap between adjacent table cells is at least ~12pt.
CELL_GAP = 5.0
# Words whose vertical positions differ by less than this are on the same line.
LINE_TOLERANCE = 3.0
# Lines this close to the top or bottom edge of a page can be repeating page furniture.
MARGIN = 60.0


class ReaderError(Exception):
    """Base class for all reader failures."""


class PasswordRequiredError(ReaderError):
    """The PDF is encrypted and no password was supplied."""


class WrongPasswordError(ReaderError):
    """A password was supplied but it does not open the PDF."""


class NoTextLayerError(ReaderError):
    """The PDF has no extractable text (probably a scan). OCR is out of scope."""


class UnreadableDocumentError(ReaderError):
    """The file is corrupt or not a PDF."""


@dataclass(frozen=True)
class Cell:
    text: str
    x0: float
    x1: float


@dataclass(frozen=True)
class Line:
    page: int          # 1-based
    top: float
    cells: tuple[Cell, ...]
    bottom: float = 0.0    # 0.0 = unknown; the engine then never guesses about wrapping

    @property
    def text(self) -> str:
        return " ".join(c.text for c in self.cells)


@dataclass
class Document:
    lines: list[Line] = field(default_factory=list)
    page_count: int = 0
    page_heights: dict = field(default_factory=dict)    # page number -> height in points

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def file_sha256(path) -> str:
    """Hash of the raw bytes. Used to avoid processing the same file twice."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _group_into_lines(words: list[dict], page_no: int, gap: float) -> list[Line]:
    lines: list[Line] = []
    current: list[dict] = []
    current_top: Optional[float] = None

    def flush():
        if not current:
            return
        current.sort(key=lambda w: w["x0"])
        cells: list[list[dict]] = [[current[0]]]
        for w in current[1:]:
            if w["x0"] - cells[-1][-1]["x1"] > gap:
                cells.append([w])
            else:
                cells[-1].append(w)
        lines.append(Line(
            page=page_no,
            top=round(min(w["top"] for w in current), 2),
            cells=tuple(
                Cell(text=" ".join(w["text"] for w in group),
                     x0=round(group[0]["x0"], 2), x1=round(group[-1]["x1"], 2))
                for group in cells
            ),
            bottom=round(max(w["bottom"] for w in current), 2),
        ))

    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if current_top is None or abs(w["top"] - current_top) > LINE_TOLERANCE:
            flush()
            current, current_top = [w], w["top"]
        else:
            current.append(w)
    flush()
    return lines


# ------------------------------------------------------------------ page furniture
_PAGE_MARKS = [
    (re.compile(r"\bpage\s*\d+(\s*(of|/)\s*\d+)?\b"), "page #"),    # Page 2 / Page 2 of 3
    (re.compile(r"^\W*\d+\s*(of|/)\s*\d+\W*$"), "# of #"),          # 2 of 3 / 2/3
    (re.compile(r"^\W*\d{1,4}\W*$"), "#"),                          # a lone page number
]


def _furniture_key(text: str) -> str:
    """Exact lower-cased text, except that page numbers are made equal on every page.
    Other digits are NOT normalised, so two different holdings never look alike."""
    t = " ".join(text.lower().split())
    for pattern, replacement in _PAGE_MARKS:
        t = pattern.sub(replacement, t)
    return t


def strip_page_furniture(doc: Document, margin: float = MARGIN) -> None:
    """Remove running headers and footers: margin lines whose text repeats on 2+ pages.

    Top margin:    the first page's copy is kept (it is usually the real title).
    Bottom margin: every copy is removed (footers and page numbers carry no data).
    A line that appears in the margin of only ONE page is never removed.
    """
    if doc.page_count < 2:
        return

    def zone(ln: Line) -> Optional[str]:
        if ln.top < margin:
            return "top"
        height = doc.page_heights.get(ln.page)
        if height and ln.bottom > height - margin:
            return "bottom"
        return None

    seen: dict = defaultdict(set)
    for ln in doc.lines:
        z = zone(ln)
        if z:
            seen[(z, _furniture_key(ln.text))].add(ln.page)
    repeating = {key for key, pages in seen.items() if len(pages) >= 2}

    def is_furniture(ln: Line) -> bool:
        z = zone(ln)
        if not z or (z, _furniture_key(ln.text)) not in repeating:
            return False
        return z == "bottom" or ln.page > 1

    doc.lines = [ln for ln in doc.lines if not is_furniture(ln)]


def read_document(path, password: Optional[str] = None, gap: float = CELL_GAP,
                  strip_furniture: bool = True) -> Document:
    path = Path(path)
    try:
        pdf = pdfplumber.open(path, password=password or "")
    except PdfminerException as e:
        # pdfplumber wraps the real pdfminer error; the original is in e.args[0].
        cause = e.args[0] if e.args else None
        if isinstance(cause, PDFPasswordIncorrect):
            if password:
                raise WrongPasswordError(f"The password does not open {path.name}") from e
            raise PasswordRequiredError(f"{path.name} is password protected") from e
        raise UnreadableDocumentError(f"{path.name} could not be read: {cause or e}") from e
    except (OSError, ValueError) as e:
        raise UnreadableDocumentError(f"{path.name} could not be read: {e}") from e

    doc = Document()
    with pdf:
        doc.page_count = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, start=1):
            doc.page_heights[page_no] = float(page.height)
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            doc.lines.extend(_group_into_lines(words, page_no, gap))

    if not doc.lines:
        raise NoTextLayerError(f"{path.name} has no extractable text (scanned PDF?)")
    if strip_furniture:
        strip_page_furniture(doc)
    return doc


if __name__ == "__main__":
    # Quick look at what the reader sees:
    #   python -m app.reader samples/pdfs/abc_pms.pdf
    #   python -m app.reader samples/pdfs/xyz_wealth.pdf demo1234
    import sys

    document = read_document(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    for ln in document.lines:
        print(f"p{ln.page} y={ln.top:6.1f} | " + " | ".join(c.text for c in ln.cells))