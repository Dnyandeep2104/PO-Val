"""Layout engine: turn a PDF into geometry we can reason about.

Written against six real reseller POs (WWT, TD SYNNEX, Carahsoft, NTT/SAP,
Dell/Ariba, PayPal/Ariba). Each defeats a different naive assumption:

  WWT       label and value separated by three rows, not on one line
  SYNNEX    4-decimal prices, prices split across two rows, dual SKUs,
            header block repeated on every page
  Carahsoft column order Item|Line|Description|Qty|Unit|Extended, so a
            naive left-to-right read puts the line number in quantity
  SAP/NTT   per-page subtotals that look exactly like a grand total
  Ariba     part number literally "Not Available", records separated by
            hundreds of points of tax and accounting tables

So: words with coordinates, grouped into rows, grouped into pages, with
repeated page furniture identified and removable. Everything above this
layer works on geometry, not on string shape.
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional

import pdfplumber

log = logging.getLogger(__name__)

# Money with 2 to 6 decimals. SYNNEX quotes unit prices to 4 places
# (1,858.2550) and the prototype's \d{2} anchor silently dropped them.
MONEY_RE = re.compile(
    r"^\(?\s*(?:USD|EUR|GBP|CAD|AUD|SGD|JPY|CHF|INR)?\s*[\$€£¥]?\s*"
    r"-?\d{1,3}(?:,\d{3})*(?:\.\d{1,6})?\s*\)?$")
# Must contain a decimal point or a thousands separator to count as money;
# stops bare integers like a line number being read as a price.
MONEYISH_RE = re.compile(r"\d[\d,]*\.\d{1,6}|\d{1,3}(?:,\d{3})+")
INT_RE = re.compile(r"^\d{1,7}$")
QTY_RE = re.compile(r"^\d{1,3}(?:,\d{3})*(?:\.0+)?$")

OCR_DPI = 300
OCR_PSM_MODES = (3, 4)      # auto, and single-column-of-blocks
OCR_MIN_CONF = 30

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{3}[\s.\-]?\d{3,4}"
    r"(?:[\s.\-]?\d{2,4})?")


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------

@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page: int = 0

    @property
    def xmid(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class Row:
    words: list[Word]
    top: float
    page: int = 0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return min((w.x0 for w in self.words), default=0.0)

    @property
    def x1(self) -> float:
        return max((w.x1 for w in self.words), default=0.0)

    @property
    def bottom(self) -> float:
        return max((w.bottom for w in self.words), default=self.top)

    def words_in(self, x0: float, x1: float) -> list[Word]:
        """Words whose horizontal midpoint sits inside a column band.

        Midpoint rather than full containment: a value can overhang its
        header's band (right-aligned numbers routinely do) and still
        belong to that column.
        """
        return [w for w in self.words if x0 <= w.xmid <= x1]

    def cell(self, x0: float, x1: float) -> str:
        return " ".join(w.text for w in self.words_in(x0, x1))

    def norm(self) -> str:
        return " ".join(self.text.split()).strip().lower()


@dataclass
class Page:
    index: int
    rows: list[Row]
    width: float = 612.0
    height: float = 792.0
    is_ocr: bool = False

    @property
    def text(self) -> str:
        return "\n".join(r.text for r in self.rows)

    def body_rows(self, header_rows: set[str], footer_rows: set[str]) -> list[Row]:
        return [r for r in self.rows
                if r.norm() not in header_rows and r.norm() not in footer_rows]


# ---------------------------------------------------------------------------
# the view
# ---------------------------------------------------------------------------

class PdfView:
    def __init__(self, data: bytes, name: str = "", ocr_if_needed: bool = True):
        self.data = data
        self.name = name
        self.ocr_if_needed = ocr_if_needed
        self._pages: Optional[list[Page]] = None
        self._raw: Optional[str] = None
        self._layout: Optional[str] = None
        self._furniture: Optional[tuple[set[str], set[str]]] = None
        self.notes: list[str] = []

    # -- pages --------------------------------------------------------------
    @property
    def pages(self) -> list[Page]:
        if self._pages is None:
            self._pages = self._load_pages()
        return self._pages

    def _load_pages(self) -> list[Page]:
        pages: list[Page] = []
        try:
            with pdfplumber.open(io.BytesIO(self.data)) as pdf:
                for i, page in enumerate(pdf.pages):
                    words = page.extract_words(use_text_flow=False,
                                               keep_blank_chars=False)
                    rows = cluster_rows(
                        [Word(w["text"], w["x0"], w["x1"], w["top"],
                              w["bottom"], i) for w in words], page=i)
                    pages.append(Page(i, rows, page.width, page.height))
        except Exception as exc:
            log.warning("pdfplumber failed on %s: %s", self.name, exc)
            self.notes.append(f"pdfplumber error: {exc}")

        # A page with no text layer is a scan. Rasterize and OCR it.
        if self.ocr_if_needed:
            for i, pg in enumerate(pages):
                if len(pg.rows) < 3:
                    ocr = self._ocr_page(i)
                    if ocr is not None:
                        pages[i] = ocr
                        self.notes.append(f"page {i + 1} had no text layer; used OCR")
        if not pages and self.ocr_if_needed:
            ocr = self._ocr_page(0)
            if ocr:
                pages = [ocr]
                self.notes.append("no text layer at all; used OCR")
        return pages

    def _ocr_page(self, index: int) -> Optional[Page]:
        """Rasterize one page at 300 DPI, clean it up, and OCR it into
        positioned words.

        The preprocessing is not decoration. On a scanned PO, a median
        filter to kill speckle plus an autocontrast stretch is the
        difference between reading the part numbers and reading nothing;
        raw Tesseract on an unprocessed 200 DPI office scan of the
        Carahsoft PO finds neither the PO number nor any SKU.

        Words come back with real coordinates, so the table reader,
        the party segmenter and every rule work unchanged on a scan.
        """
        try:
            import pytesseract
            from PIL import Image, ImageFilter, ImageOps
        except ImportError:
            self.notes.append("OCR needed but pytesseract/Pillow not installed")
            return None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "in.pdf"
                src.write_bytes(self.data)
                subprocess.run(
                    ["pdftoppm", "-r", str(OCR_DPI), "-gray", "-png",
                     "-f", str(index + 1), "-l", str(index + 1),
                     str(src), str(Path(tmp) / "pg")],
                    check=True, capture_output=True, timeout=240)
                images = sorted(Path(tmp).glob("pg-*.png"))
                if not images:
                    return None
                img = Image.open(images[0])
                img = ImageOps.autocontrast(img.filter(ImageFilter.MedianFilter(3)))

                scale = 72.0 / OCR_DPI
                best_words: list[Word] = []
                for psm in OCR_PSM_MODES:
                    data = pytesseract.image_to_data(
                        img, config=f"--psm {psm}",
                        output_type=pytesseract.Output.DICT)
                    words = []
                    for j, txt in enumerate(data["text"]):
                        txt = (txt or "").strip()
                        try:
                            conf = float(data["conf"][j])
                        except (TypeError, ValueError):
                            conf = -1
                        if not txt or conf < OCR_MIN_CONF:
                            continue
                        x, y = data["left"][j] * scale, data["top"][j] * scale
                        w, h = data["width"][j] * scale, data["height"][j] * scale
                        words.append(Word(txt, x, x + w, y, y + h, index))
                    # Page segmentation mode changes results a lot on
                    # noisy scans; keep whichever reads more.
                    if len(words) > len(best_words):
                        best_words = words
                if not best_words:
                    return None
                return Page(index, cluster_rows(best_words, page=index),
                            img.width * scale, img.height * scale, is_ocr=True)
        except Exception as exc:
            log.warning("OCR failed on page %s of %s: %s", index, self.name, exc)
            self.notes.append(f"OCR failed: {exc}")
            return None

    @property
    def is_ocr(self) -> bool:
        return any(p.is_ocr for p in self.pages)

    # -- flat views ---------------------------------------------------------
    @property
    def rows(self) -> list[Row]:
        return [r for p in self.pages for r in p.rows]

    @property
    def raw_text(self) -> str:
        if self._raw is None:
            self._raw = "\n".join(p.text for p in self.pages)
        return self._raw

    @property
    def layout_text(self) -> str:
        """Column-preserving text. Kept because some label/value pairs are
        far easier to regex when horizontal whitespace survives."""
        if self._layout is None:
            chunks = []
            try:
                with pdfplumber.open(io.BytesIO(self.data)) as pdf:
                    for page in pdf.pages:
                        chunks.append(page.extract_text(layout=True) or "")
            except Exception:
                chunks = [self.raw_text]
            self._layout = "\n".join(chunks)
        return self._layout

    @property
    def text(self) -> str:
        return self.raw_text + "\n" + self.layout_text

    # -- repeated page furniture -------------------------------------------
    @property
    def furniture(self) -> tuple[set[str], set[str]]:
        """Rows that repeat identically near the top or bottom of most pages.

        SYNNEX and Carahsoft reprint the whole Bill To / Ship To / vendor
        block on every page. Without this, a three-page PO yields three
        copies of every header value and the "is there more than one Bill
        To?" checklist rule fires on every multi-page document.
        """
        if self._furniture is None:
            self._furniture = self._detect_furniture()
        return self._furniture

    def _detect_furniture(self) -> tuple[set[str], set[str]]:
        if len(self.pages) < 2:
            return set(), set()
        counts: dict[str, int] = {}
        positions: dict[str, list[float]] = {}
        for pg in self.pages:
            seen_on_page = set()
            for r in pg.rows:
                key = r.norm()
                if not key or len(key) < 3 or key in seen_on_page:
                    continue
                seen_on_page.add(key)
                counts[key] = counts.get(key, 0) + 1
                positions.setdefault(key, []).append(
                    r.top / max(pg.height, 1.0))

        threshold = max(2, int(len(self.pages) * 0.6))
        header, footer = set(), set()
        for key, n in counts.items():
            if n < threshold:
                continue
            avg = sum(positions[key]) / len(positions[key])
            if avg < 0.42:
                header.add(key)
            elif avg > 0.75:
                footer.add(key)
        return header, footer

    def body_rows(self, page: Optional[int] = None) -> list[Row]:
        """Rows with repeated page furniture removed."""
        header, footer = self.furniture
        out = []
        for pg in self.pages:
            if page is not None and pg.index != page:
                continue
            out.extend(pg.body_rows(header, footer))
        return out

    # -- label/value lookup -------------------------------------------------
    def label_value(self, patterns: Iterable[str], *,
                    max_right: float = 420.0, max_below: int = 4,
                    value_re: Optional[re.Pattern] = None,
                    stop_re: Optional[re.Pattern] = None) -> Optional[str]:
        """Find a label, then look right and then down for its value.

        WWT prints "PO#" as a column heading with 4527709 three rows
        below it. A same-line regex returns whatever text happens to
        follow, which is how the old code produced po_number="Primary".
        Searching right-then-down in the label's own x-band fixes it.
        """
        for pat in patterns:
            rx = re.compile(pat, re.I)
            for pg in self.pages:
                for i, row in enumerate(pg.rows):
                    hit = self._match_in_row(row, rx)
                    if hit is None:
                        continue
                    label_words, label_x0, label_x1 = hit

                    # 1. to the right, same row
                    right = [w for w in row.words
                             if w.x0 >= label_x1 - 1
                             and w.x0 <= label_x1 + max_right
                             and w not in label_words]
                    val = _pick(right, value_re, stop_re)
                    if val:
                        return val

                    # 2. below, overlapping the label's x-band
                    for nxt in pg.rows[i + 1: i + 1 + max_below]:
                        band = [w for w in nxt.words
                                if w.x1 >= label_x0 - 12 and w.x0 <= label_x1 + 90]
                        val = _pick(band, value_re, stop_re)
                        if val:
                            return val
        return None

    @staticmethod
    def _match_in_row(row: Row, rx: re.Pattern):
        """Locate a label that may span several words ('Purchase Order Number')."""
        for n in (4, 3, 2, 1):
            for i in range(len(row.words) - n + 1):
                chunk = row.words[i:i + n]
                joined = " ".join(w.text for w in chunk).strip(" :#-")
                if rx.fullmatch(joined):
                    return chunk, min(w.x0 for w in chunk), max(w.x1 for w in chunk)
        return None

    def search(self, pattern: str, flags: int = re.I) -> Optional[re.Match]:
        m = re.search(pattern, self.raw_text, flags)
        return m or re.search(pattern, self.layout_text, flags)

    def find_all(self, pattern: str, flags: int = re.I) -> list[str]:
        seen, out = set(), []
        for hay in (self.raw_text, self.layout_text):
            for m in re.finditer(pattern, hay, flags):
                v = m.group(0)
                if v.lower() not in seen:
                    seen.add(v.lower())
                    out.append(v)
        return out


def _pick(words: list[Word], value_re: Optional[re.Pattern],
          stop_re: Optional[re.Pattern]) -> Optional[str]:
    if not words:
        return None
    words = sorted(words, key=lambda w: w.x0)
    text = " ".join(w.text for w in words).strip(" :#-\u2013")
    if not text:
        return None
    if stop_re and stop_re.search(text):
        return None
    if value_re:
        m = value_re.search(text)
        return m.group(0) if m else None
    return text


# ---------------------------------------------------------------------------
# row clustering
# ---------------------------------------------------------------------------

def cluster_rows(words: list[Word], page: int = 0,
                 tol: Optional[float] = None) -> list[Row]:
    """Group words into visual rows.

    Tolerance adapts to the document's own font size. A fixed 2.5pt splits
    rows in 12pt documents and merges them in 6pt ones; six vendors means
    six font sizes.
    """
    if not words:
        return []
    heights = sorted(w.height for w in words if w.height > 0)
    median_h = heights[len(heights) // 2] if heights else 8.0
    tol = tol if tol is not None else max(1.5, median_h * 0.45)

    words = sorted(words, key=lambda w: (round(w.top, 1), w.x0))
    rows: list[Row] = []
    current = [words[0]]
    anchor = words[0].top
    for w in words[1:]:
        if abs(w.top - anchor) <= tol:
            current.append(w)
            anchor = min(anchor, w.top)
        else:
            rows.append(Row(sorted(current, key=lambda x: x.x0), anchor, page))
            current, anchor = [w], w.top
    rows.append(Row(sorted(current, key=lambda x: x.x0), anchor, page))
    return rows


# ---------------------------------------------------------------------------
# number parsing
# ---------------------------------------------------------------------------

def to_decimal(text: Optional[str]) -> Optional[Decimal]:
    """Parse a money-ish token. Tolerates currency codes, symbols,
    thousands separators, trailing minus, and accounting parentheses."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.endswith("-")
    s = re.sub(r"(?i)\b(?:USD|EUR|GBP|CAD|AUD|SGD|JPY|CHF|INR)\b", "", s)
    s = s.replace("(", "").replace(")", "").rstrip("-")
    s = re.sub(r"[\$€£¥,\s]", "", s)
    if not s or not re.fullmatch(r"-?\d*\.?\d+", s):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg and d > 0 else d


def is_money(token: str) -> bool:
    return bool(MONEY_RE.match(token.strip())) and bool(MONEYISH_RE.search(token))


MAX_PRICE_DECIMALS = 4


def decimals_of(text: str) -> int:
    m = re.search(r"\.(\d+)$", text.strip())
    return len(m.group(1)) if m else 0


def join_split_number(head: str, tail: str) -> Optional[str]:
    """Repair a number broken across two rows inside one column.

    SYNNEX renders 1,858.2550 as "1,858.255" on one row and "0" on the
    next. Read independently that is a price of 1858.255 plus a stray 0.

    Strictly bounded: the head must already be money with fewer than four
    decimals, and the tail may only supply enough digits to reach four.
    Without that bound the "36" from a neighbouring "Contract Length 36"
    row gets welded onto a price and turns 912.7600 into 912.760036.
    The caller still arithmetic-checks the result.
    """
    head, tail = head.strip(), tail.strip()
    if not is_money(head):
        return None
    have = decimals_of(head)
    if have == 0 or have >= MAX_PRICE_DECIMALS:
        return None
    room = MAX_PRICE_DECIMALS - have
    if not re.fullmatch(r"\d{1,%d}" % room, tail):
        return None
    return head + tail
