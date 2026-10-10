"""Turns a Document into a stable fingerprint: the set of 'label-like' lines.

Digits are the signal for 'this changes monthly', so only the text before the first
digit-bearing cell is kept from each line.
"""
import hashlib
import re
from dataclasses import dataclass

from app.reader import Document

_DIGIT = re.compile(r"\d")
_MONTHS = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_CURRENCY = re.compile(r"\(?\b(inr|usd|eur|gbp|rs)\b\.?\)?|[₹$€£]", re.IGNORECASE)
MATCH_THRESHOLD = 0.85
DRIFT_THRESHOLD = 0.50



@dataclass(frozen=True)
class Fingerprint:
    labels: frozenset
    digest: str


def _normalise(text: str) -> str:
    text = text.strip().lower()
    text = _CURRENCY.sub(" ", text)          # currency is data, not layout
    text = _SPACES.sub(" ", text).strip()
    return text.rstrip(" :.-")

def _line_label(cells) -> str:
    """Stable label for a line, or '' if the line is monthly data."""
    texts = [c.text for c in cells]
    has_digit = [bool(_DIGIT.search(t)) for t in texts]

    if not any(has_digit):
        joined = " ".join(t for t in texts)
        if ":" in joined:
            # 'Strategy: ABC Large Cap Growth | Currency: INR' -> keep only the keys
            keys = [t.split(":")[0] for t in texts if ":" in t]
            return " | ".join(_normalise(k) for k in keys if _normalise(k))
        if len(texts) <= 3 and len(joined) < 40:
            return _normalise(joined)          # heading, e.g. "ABC Portfolio Managers"
        return " | ".join(_normalise(t) for t in texts if _normalise(t))

    # Mixed line. Only the 'label | value' shape is stable (value changes, label doesn't).
    if len(texts) == 2 and has_digit[1] and not has_digit[0]:
        return _normalise(texts[0])

    # 'Client Code: AB1234 | Period: ...' style: keep text before each colon.
    prefixes = []
    for t in texts:
        if ":" in t:
            p = t.split(":")[0]
            if p and not _DIGIT.search(p):
                prefixes.append(_normalise(p))
    return " | ".join(prefixes)


def compute_fingerprint(doc: Document) -> Fingerprint:
    labels = set()
    for line in doc.lines:
        label = _line_label(line.cells)
        if not label or len(label) < 3:
            continue
        if _MONTHS.search(label) and len(label.split()) <= 4:
            continue  # e.g. "statement for april" changes monthly
        labels.add(label)
    frozen = frozenset(labels)
    digest = hashlib.sha256("\n".join(sorted(frozen)).encode()).hexdigest()
    return Fingerprint(labels=frozen, digest=digest)


def similarity(a: frozenset, b: frozenset) -> float:
    """Jaccard similarity: shared labels / all labels."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


