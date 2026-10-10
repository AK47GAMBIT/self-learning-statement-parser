"""Reads a PDF into a normalised form: pages -> lines -> cells (with x-positions).

Nothing outside this module touches pdfplumber.
"""
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError

# Words closer than this (in PDF points) belong to the same cell. A normal space at
# 8-9pt is about 2-3pt; the gap between adjacent table cells is at least ~12pt.
CELL_GAP = 5.0
# Words whose vertical positions differ by less than this are on the same line.
LINE_TOLERANCE = 3.0


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

    @property
    def text(self) -> str:
        return " ".join(c.text for c in self.cells)


@dataclass
class Document:
    lines: list[Line] = field(default_factory=list)
    page_count: int = 0

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
        ))

    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if current_top is None or abs(w["top"] - current_top) > LINE_TOLERANCE:
            flush()
            current, current_top = [w], w["top"]
        else:
            current.append(w)
    flush()
    return lines


def read_document(path, password: Optional[str] = None, gap: float = CELL_GAP) -> Document:
    path = Path(path)
    try:
        pdf = pdfplumber.open(path, password=password or "")
    except PDFPasswordIncorrect:
        if password:
            raise WrongPasswordError(f"The password does not open {path.name}")
        raise PasswordRequiredError(f"{path.name} is password protected")
    except (PDFSyntaxError, ValueError) as e:
        raise UnreadableDocumentError(f"{path.name} could not be read: {e}") from e

    doc = Document()
    with pdf:
        doc.page_count = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            doc.lines.extend(_group_into_lines(words, page_no, gap))

    if not doc.lines:
        raise NoTextLayerError(f"{path.name} has no extractable text (scanned PDF?)")
    return doc


if __name__ == "__main__":
    # Quick look at what the reader sees:
    #   python -m app.reader samples/pdfs/abc_pms.pdf
    #   python -m app.reader samples/pdfs/xyz_wealth.pdf demo1234
    import sys

    document = read_document(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    for ln in document.lines:
        print(f"p{ln.page} y={ln.top:6.1f} | " + " | ".join(c.text for c in ln.cells))