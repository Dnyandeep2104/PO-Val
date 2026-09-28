"""Line-item extraction by column geometry and record grouping.

The idea that makes this work across vendors: **a line item is not a row.**

  WWT      one item = 4 rows (part, description, supplier item, note)
  SYNNEX   one item = up to 7 rows, with the real F5 SKU on row 6
  SAP      one item = 4 rows (part, description, dates, sales order)
  Ariba    one item = ~60 rows, with tax and accounting tables inside
  Carahsoft one item = 1-2 rows

So we find the header, derive column x-bands, then decide which rows are
*anchors* (start a new item) and treat everything up to the next anchor
as part of that item. Fields are harvested from the whole record, not one
row. Wrapped descriptions and continuation SKUs stop being a problem.

Tables are found per *region*, not once per document. Ariba reprints the
column header before every single line item; SYNNEX and Carahsoft reprint
it on every page. One header, one region, records read within it.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Optional

from .layout import (INT_RE, MONEYISH_RE, QTY_RE, PdfView, Row, Word,
                     is_money, join_split_number, to_decimal)

log = logging.getLogger(__name__)

# A part-number-shaped token: has a letter and a digit, no spaces.
SKU_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9][A-Za-z0-9\-\+\./_]{3,}$")
# F5's own SKUs. Used to choose between a distributor's item number and
# the F5 SKU when a record carries both (SYNNEX prints both).
F5_SKU_RE = re.compile(r"^F5[N]?[-–][A-Z0-9\-\+\.]{3,}$", re.I)
STRICT_F5_RE = re.compile(r"^F5[-–][A-Z0-9\-\+\.]{3,}$", re.I)

DATE_TOKEN_RE = re.compile(
    r"^\d{1,2}[/\-][A-Za-z0-9]{1,4}[/\-]\d{2,4}$|^\d{4}-\d{2}-\d{2}$")
LINE_NO_RE = re.compile(r"^\d{1,4}(?:\.\d{1,3})?$")

# Column header vocabulary. Order inside a field does not matter; conflicts
# between fields are resolved by specificity (longest matching label wins).
HEADER_SYNONYMS: dict[str, list[str]] = {
    "line_no": [
        r"ln", r"ln\s*#", r"line", r"line\s*#", r"line\s*no\.?",
        r"item", r"item\s*#", r"pos", r"no\.?", r"#", r"seq", r"s\.?no\.?",
    ],
    "part_number": [
        r"part\s*(?:#|no\.?|number)?", r"part\s*#\s*/\s*description",
        r"item\s*(?:code|number)", r"product\s*(?:code|number|#|id)?",
        r"material(?:\s*(?:no\.?|number))?", r"sku", r"model",
        r"vendor\s*item\s*#?", r"mfr\.?\s*part(?:\s*#)?", r"catalog(?:\s*#)?",
        r"manufacturer\s*part(?:\s*#)?", r"supplier\s*(?:part|item)\s*#?",
        r"item\s*id", r"article",
    ],
    # Combined columns: one band holds both the SKU and its description.
    "part_number_description": [
        r"part\s*#?\s*/\s*description", r"item\s*/\s*description",
        r"description\s*/\s*part(?:\s*#)?", r"part\s*number\s*/\s*description",
        r"product\s*/\s*description", r"sku\s*/\s*description",
    ],
    "description": [
        r"description", r"item\s*description", r"product\s*(?:name|description)",
        r"desc\.?", r"details",
    ],
    "quantity": [
        r"qty\.?", r"quantity", r"qty\s*\(?unit\)?", r"qty\s*ord(?:ered)?",
        r"order\s*qty", r"units?", r"qty\s*/\s*uom", r"open\s*qty",
    ],
    "unit_price": [
        r"unit\s*(?:price|cost)", r"unit\s*net", r"price\s*(?:each|/\s*ea)?",
        r"each", r"rate", r"unit", r"list\s*price",
        r"unit\s*sell", r"price", r"cost",
    ],
    "total_price": [
        r"ext(?:ended)?\.?\s*(?:price|amount|cost)?", r"extension",
        r"line\s*(?:total|amount|value)", r"total\s*(?:price|amount|net)?",
        r"amount", r"net\s*amount", r"net\s*price", r"value", r"subtotal",
        r"sub\s*total",
        r"total", r"ext\.?",
    ],
    "uom": [r"uom", r"u/m", r"unit\s*of\s*measure", r"um"],
}

# Rows that end a table region.
STOP_RE = re.compile(
    r"^\s*(?:sub\s*total|subtotal|grand\s*total|total\s*(?:due|amount|net|order|"
    r"purchase\s*order)|p\.?o\.?\s*total|order\s*total|total\s*\(|freight\b|"
    r"shipping\s*(?:and|&)|sales\s*tax|^tax$|terms\s*(?:and|&)\s*conditions|"
    r"remit\s*to|authorized\s*signature|continued\s*on\s*next|"
    r"vendor\s*comments|comments\s*:|please\s*send|thank\s*you)", re.I)

# Text that is never a line item, even inside the table region.
NOISE_RE = re.compile(
    r"^(?:note|notes|comment|status|accounting|other\s+information|"
    r"additional\s+information|tax\s+category|req\.?\s*line|requester|"
    r"pr\s*no|naics|classification|company|percentage|cost\s*center|"
    r"account|purchasing\s*unit|concatenated|unconfirmed|local\s*currency|"
    r"is\s*it\s*a\s*resale|contract\s*length|please\s*confirm|sales\s*order)"
    r"\b", re.I)


class Column:
    __slots__ = ("field", "x0", "x1", "label")

    def __init__(self, field: str, x0: float, x1: float, label: str = ""):
        self.field, self.x0, self.x1, self.label = field, x0, x1, label

    def __repr__(self):
        return f"<{self.field} {self.x0:.0f}-{self.x1:.0f} {self.label!r}>"


class Region:
    """One header plus the rows beneath it, on one page."""

    def __init__(self, page: int, header_rows: list[Row],
                 columns: dict[str, Column], body: list[Row], score: int):
        self.page = page
        self.header_rows = header_rows
        self.columns = columns
        self.body = body
        self.score = score

    def __repr__(self):
        return (f"<Region p{self.page} score={self.score} "
                f"cols={sorted(self.columns)} rows={len(self.body)}>")


# ---------------------------------------------------------------------------
# header detection
# ---------------------------------------------------------------------------

def _label_matches(label: str) -> list[tuple[str, int]]:
    """Which fields could this header text be, and how specific is the match?"""
    norm = " ".join(label.split()).strip(" :.#-").lower()
    if not norm:
        return []
    out = []
    for field, patterns in HEADER_SYNONYMS.items():
        best = -1
        for pat in patterns:
            if re.fullmatch(pat, norm, re.I):
                best = max(best, len(pat))
        if best >= 0:
            out.append((field, best))
    return out


MAX_HEADER_WORDS = 20


def merge_header_rows(header_rows: list[Row]) -> list[Word]:
    """Fuse vertically stacked header words into one composite label.

    SYNNEX writes 'Unit' above 'Price' and 'Total' above 'Price' at the
    same x. Treated as separate rows they become two competing claims for
    one column and the total column is lost. Joined by x-overlap they read
    as 'Unit Price' and 'Total Price', which is what a human sees.
    """
    if not header_rows:
        return []

    def clean(words):
        """Drop column-separator pipes. Some templates (Ingram's among
        them) draw the header as 'LINE | QTY | UOM | PART NUMBER'. Counted
        as words, the separators halve the label coverage and the header
        fails the prose filter."""
        out = []
        for w in words:
            text = w.text.strip("|").strip()
            if text:
                out.append(Word(text, w.x0, w.x1, w.top, w.bottom, w.page))
        return out

    header_rows = [Row(clean(r.words), r.top, r.page) for r in header_rows]
    if not header_rows[0].words:
        return []
    base = [Word(w.text, w.x0, w.x1, w.top, w.bottom, w.page)
            for w in header_rows[0].words]
    for row in header_rows[1:]:
        for w in row.words:
            best, best_overlap = None, 0.0
            for b in base:
                overlap = min(b.x1, w.x1) - max(b.x0, w.x0)
                if overlap > best_overlap:
                    best, best_overlap = b, overlap
            # Require real horizontal overlap, not a shared edge.
            if best is not None and best_overlap > 1.0:
                best.text = f"{best.text} {w.text}"
                best.x0 = min(best.x0, w.x0)
                best.x1 = max(best.x1, w.x1)
            else:
                base.append(Word(w.text, w.x0, w.x1, w.top, w.bottom, w.page))
    return sorted(base, key=lambda w: w.x0)


def find_columns(rows: list[Row], idx: int,
                 lookahead: int = 1) -> tuple[dict[str, Column], list[Row], int]:
    """Try to read rows[idx], optionally merged with the next row, as a
    table header. Returns columns, the header rows consumed, and a score."""
    best: tuple[dict[str, Column], list[Row], int] = ({}, [], 0)

    for extra in range(lookahead + 1):
        header_rows = rows[idx: idx + 1 + extra]
        if len(header_rows) < 1 + extra:
            break
        # Prose runs long. A real column header is a handful of short
        # labels; this is what stops PayPal's terms-and-conditions
        # paragraph ("...product numbers, a description and the quantity
        # of each of the Items shipped...") registering as a table.
        if any(len(r.words) > MAX_HEADER_WORDS for r in header_rows):
            continue
        # Never absorb a data row into the header. A column heading does
        # not contain prices; the first line item does. Without this the
        # two-row lookahead fuses "LINE" with "001" and the line-number
        # and quantity columns disappear.
        if extra and any(is_money(w.text.strip("|"))
                         for r in header_rows[1:] for w in r.words):
            continue

        words = merge_header_rows(list(header_rows))
        if len(words) < 3:
            continue

        claims: list[tuple[str, int, float, float, str, int]] = []
        for n in (4, 3, 2, 1):
            for i in range(len(words) - n + 1):
                chunk = words[i:i + n]
                label = " ".join(w.text for w in chunk)
                for field, spec in _label_matches(label):
                    claims.append((field, spec + n * 10,
                                   min(w.x0 for w in chunk),
                                   max(w.x1 for w in chunk), label, n))

        if not claims:
            continue

        claims.sort(key=lambda c: -c[1])
        columns: dict[str, Column] = {}
        taken: list[tuple[float, float]] = []
        covered = 0
        for field, spec, x0, x1, label, n in claims:
            if field in columns:
                continue
            if any(not (x1 < tx0 or x0 > tx1) for tx0, tx1 in taken):
                continue
            columns[field] = Column(field, x0, x1, label)
            taken.append((x0, x1))
            covered += n

        # What fraction of the header's words are actually column labels?
        coverage = covered / max(len(words), 1)
        if coverage < 0.45:
            continue

        score = _score_header(columns)
        if score:
            score += int(coverage * 4) - extra   # prefer tight, single-row headers
        if score > best[2]:
            best = (columns, list(header_rows), score)

    return best


def _score_header(columns: dict[str, Column]) -> int:
    """A header needs a way to identify the item and a way to price it."""
    if not columns:
        return 0
    combo = "part_number_description" in columns
    score = len(columns)
    has_id = combo or bool({"part_number", "description"} & set(columns))
    has_num = bool({"quantity", "unit_price", "total_price"} & set(columns))
    if not (has_id and has_num):
        return 0
    if combo or "part_number" in columns:
        score += 3
    if "quantity" in columns:
        score += 2
    if "total_price" in columns:
        score += 2
    if "unit_price" in columns:
        score += 1
    return score


def widen(columns: dict[str, Column], page_width: float) -> dict[str, Column]:
    """Expand each header's x-span to the midpoint between neighbours.

    Header text almost never spans its column. Right-aligned numbers in
    particular sit well right of their heading. Carahsoft's
    Item|Line|Description|Qty|Unit|Extended only reads correctly once the
    bands meet.
    """
    ordered = sorted(columns.values(), key=lambda c: c.x0)
    out: dict[str, Column] = {}
    for i, col in enumerate(ordered):
        left = 0.0 if i == 0 else (ordered[i - 1].x1 + col.x0) / 2
        right = (page_width + 50 if i == len(ordered) - 1
                 else (col.x1 + ordered[i + 1].x0) / 2)
        out[col.field] = Column(col.field, left, right, col.label)
    return out


# ---------------------------------------------------------------------------
# region discovery
# ---------------------------------------------------------------------------

def expand_combined(columns: dict[str, Column]) -> dict[str, Column]:
    """Split a widened 'Part # / Description' band into two identical
    bands. Must run *after* widen(): registering them as two columns
    beforehand makes widen() place a boundary between them and cut the
    combined column in half, which is how Dell's SKU cell ('Not
    Available') lost its second word to the description."""
    combo = columns.pop("part_number_description", None)
    if combo is not None:
        columns.setdefault("part_number",
                           Column("part_number", combo.x0, combo.x1, combo.label))
        columns.setdefault("description",
                           Column("description", combo.x0, combo.x1, combo.label))
    return columns


def find_regions(view: PdfView, min_score: int = 6) -> list[Region]:
    header_keys, footer_keys = view.furniture
    regions: list[Region] = []

    for page in view.pages:
        rows = page.rows
        candidates: list[tuple[int, dict, list[Row], int]] = []
        for i in range(len(rows)):
            columns, hrows, score = find_columns(rows, i)
            if score >= min_score:
                candidates.append((i, columns, hrows, score))

        # Evaluating every row (rather than skipping past the first hit)
        # matters: a weak pseudo-header a line or two above the real one
        # would otherwise consume it and the strong header would never be
        # tried. Score decides, not document order.
        if not candidates:
            continue
        best_score = max(c[3] for c in candidates)
        header_positions: list[tuple[int, dict, list[Row], int]] = []
        for cand in sorted(candidates, key=lambda c: (-c[3], c[0])):
            if cand[3] < best_score - 3:
                continue
            # Drop candidates that start inside an already-accepted header.
            if any(kept[0] <= cand[0] < kept[0] + len(kept[2])
                   or cand[0] <= kept[0] < cand[0] + len(cand[2])
                   for kept in header_positions):
                continue
            header_positions.append(cand)
        header_positions.sort(key=lambda c: c[0])

        for n, (start, columns, hrows, score) in enumerate(header_positions):
            body_start = start + len(hrows)
            body_end = (header_positions[n + 1][0]
                        if n + 1 < len(header_positions) else len(rows))
            body = []
            for r in rows[body_start:body_end]:
                if STOP_RE.match(r.norm()):
                    break
                if r.norm() in footer_keys:
                    continue
                body.append(r)
            if body:
                regions.append(Region(
                    page.index, hrows,
                    expand_combined(widen(columns, page.width)),
                    body, score))
    return regions


# ---------------------------------------------------------------------------
# record grouping
# ---------------------------------------------------------------------------

def is_anchor(row: Row, cols: dict[str, Column]) -> bool:
    """Does this row start a new line item?

    An anchor carries an identifier in the leftmost key column plus at
    least one number. Requiring both keeps wrapped description rows and
    embedded tax tables from spawning phantom items.
    """
    if NOISE_RE.match(row.norm()):
        return False

    key_field = "line_no" if "line_no" in cols else "part_number"
    key_col = cols.get(key_field)
    if key_col is None:
        return False
    key_tokens = [w.text for w in row.words_in(key_col.x0, key_col.x1)]
    if not key_tokens:
        return False

    if key_field == "line_no":
        has_key = any(LINE_NO_RE.match(t) for t in key_tokens)
    else:
        has_key = any(SKU_RE.match(t) and not is_money(t) for t in key_tokens)
        # SAP prints the line number and the SKU in the same band.
        has_key = has_key or any(LINE_NO_RE.match(t) for t in key_tokens)
    if not has_key:
        return False

    # ...plus a number somewhere in a numeric column.
    for field in ("quantity", "unit_price", "total_price"):
        col = cols.get(field)
        if not col:
            continue
        for w in row.words_in(col.x0, col.x1):
            t = w.text.strip()
            if DATE_TOKEN_RE.match(t):
                continue
            if is_money(t) or QTY_RE.match(t) or INT_RE.match(t):
                return True
    return False


def group_records(body: list[Row], cols: dict[str, Column]) -> list[list[Row]]:
    records: list[list[Row]] = []
    current: list[Row] = []
    for row in body:
        if is_anchor(row, cols):
            if current:
                records.append(current)
            current = [row]
        elif current:
            current.append(row)
    if current:
        records.append(current)
    return records


# ---------------------------------------------------------------------------
# field harvesting
# ---------------------------------------------------------------------------

def band_tokens(record: list[Row], col: Optional[Column]) -> list[list[str]]:
    """Tokens inside one column, grouped per row, in row order."""
    if col is None:
        return []
    out = []
    for row in record:
        # Split on pipes as well as whitespace: pipe-delimited templates
        # glue the separator to the value ("|F5-BIG-VE-BTA-25MV18",
        # "| 10,082.52"), which otherwise hides both from the matchers.
        toks = []
        for w in row.words_in(col.x0, col.x1):
            for piece in w.text.split("|"):
                piece = piece.strip()
                if piece:
                    toks.append(piece)
        if toks:
            out.append(toks)
    return out


def pick_part_number(record: list[Row], cols: dict[str, Column]) -> Optional[str]:
    """Choose the F5 SKU.

    SYNNEX prints its own item number (F5N-BIGLTM-VE-25MV18) on the anchor
    row and the actual F5 SKU (F5-BIG-LTM-VE-25MV18) on a continuation
    row. Booking against the distributor's number would be wrong, so an
    exact F5- prefix wins over anything else.
    """
    shares_band = (cols.get("part_number") is not None
                   and cols.get("description") is not None
                   and cols["part_number"].x0 == cols["description"].x0)

    # When one column holds both the SKU and the description ("Part # /
    # Description"), the SKU is on the record's first row and the
    # description wraps beneath it. Reading the whole record would let a
    # word out of the product blurb pose as a part number: Dell's PO says
    # "Not Available" and the description mentions QSFP28, which is a
    # plausible-looking SKU that appears nowhere as a part number.
    part_rows = record[:1] if shares_band else record
    part_band = band_tokens(part_rows, cols.get("part_number"))
    desc_band = band_tokens(record, cols.get("description"))

    def skus(bands) -> list[str]:
        out = []
        for toks in bands:
            for t in toks:
                t = t.strip(" ,;:|")
                if SKU_RE.match(t) and not is_money(t) and not DATE_TOKEN_RE.match(t):
                    out.append(t)
        return out

    candidates = skus(part_band)
    # Only look in the description when the part column is genuinely empty,
    # or when the two share one band. Ariba prints "Not Available" as the
    # part number; scavenging a token out of the description there invents
    # a SKU that is not on the document ("QSFP28" from the product blurb).
    if not candidates and not shares_band and not part_band:
        candidates = skus(desc_band)

    if not candidates:
        return None
    strict = [c for c in candidates if STRICT_F5_RE.match(c)]
    if strict:
        return max(strict, key=len)
    loose = [c for c in candidates if F5_SKU_RE.match(c)]
    if loose:
        return max(loose, key=len)
    # No F5-shaped SKU: fall back to the first plausible token, but only
    # from the part column, never from free-text description.
    return candidates[0]


def money_candidates(record: list[Row], cols: dict[str, Column],
                     field: str) -> list[Decimal]:
    """All plausible readings of a money column, best guess first.

    Returns candidates rather than one value so reconcile() can pick the
    reading that makes the arithmetic work. A split price yields both the
    truncated and the repaired form; the one where unit x qty equals the
    line total is the right one, and that is a far stronger signal than
    any heuristic about which token looked more price-like.
    """
    col = cols.get(field)
    if col is None:
        return []
    rows_tokens = band_tokens(record, col)
    out: list[Decimal] = []
    for i, toks in enumerate(rows_tokens):
        for t in toks:
            if DATE_TOKEN_RE.match(t) or not is_money(t):
                continue
            base = to_decimal(t)
            if base is not None and base not in out:
                out.append(base)
            # The same value with digits recovered from the next row.
            if i + 1 < len(rows_tokens):
                for tail in rows_tokens[i + 1][:2]:
                    joined = join_split_number(t, tail)
                    jd = to_decimal(joined) if joined else None
                    if jd is not None and jd not in out:
                        out.append(jd)
            break   # first money token in a row is the column's value
    return out


def qty_candidates(record: list[Row], cols: dict[str, Column]) -> list[int]:
    col = cols.get("quantity")
    if col is None:
        return []
    out: list[int] = []
    for toks in band_tokens(record, col):
        for t in toks:
            t = re.sub(r"\((?:ea|each|pc|pcs|unit)\)", "", t, flags=re.I).strip(" ()")
            if not t or DATE_TOKEN_RE.match(t) or not QTY_RE.match(t):
                continue
            d = to_decimal(t)
            if (d is not None and d == d.to_integral_value()
                    and 0 < d < 10_000_000 and int(d) not in out):
                out.append(int(d))
    return out


def reconcile(units: list[Decimal], qtys: list[int],
              totals: list[Decimal]) -> tuple[Optional[Decimal], Optional[int],
                                              Optional[Decimal], bool]:
    """Pick the reading of unit / qty / total where unit x qty == total.

    This is the single most valuable check in the extractor. Every PO line
    carries the same fact three times, so the document validates our parse
    for us. When the three agree we know the columns were read correctly;
    when only two are present we derive the third; when they disagree we
    say so instead of guessing.

    Returns (unit, quantity, total, exact) where exact means the identity
    held to the cent.
    """
    tol = Decimal("0.05")

    # 1. A combination that satisfies the identity outright.
    for u in units:
        for q in qtys:
            for t in totals:
                if abs(u * q - t) <= tol:
                    return u, q, t, True

    # 2. Two of three: derive the missing one.
    if units and qtys and not totals:
        return units[0], qtys[0], (units[0] * qtys[0]).quantize(Decimal("0.01")), True
    if totals and qtys and not units and qtys[0]:
        derived = (totals[0] / qtys[0]).quantize(Decimal("0.0001"))
        return derived, qtys[0], totals[0], True
    if totals and units and not qtys and units[0]:
        ratio = totals[0] / units[0]
        if abs(ratio - round(ratio)) < Decimal("0.01") and 0 < round(ratio) < 10_000_000:
            return units[0], int(round(ratio)), totals[0], True

    # 3. No consistent reading. Hand back best guesses and flag it, so the
    #    line surfaces for review rather than being silently wrong.
    return (units[0] if units else None,
            qtys[0] if qtys else None,
            totals[0] if totals else None,
            False)


def record_text(record: list[Row], cols: dict[str, Column]) -> Optional[str]:
    col = cols.get("description")
    if col is None:
        return " ".join(r.text for r in record)[:300] or None
    parts = [" ".join(t) for t in band_tokens(record, col)]
    return " ".join(parts)[:300] or None


def read_records(view: PdfView) -> tuple[list[dict], dict]:
    """Extract every line item in the document.

    Returns the items plus diagnostics, because when this gets something
    wrong the person debugging needs to know which header it locked onto.
    """
    regions = find_regions(view)
    items: list[dict] = []
    diag = {"regions": len(regions), "region_detail": []}

    for region in regions:
        records = group_records(region.body, region.columns)
        found = 0
        for rec in records:
            part = pick_part_number(rec, region.columns)
            unit, qty, total, exact = reconcile(
                money_candidates(rec, region.columns, "unit_price"),
                qty_candidates(rec, region.columns),
                money_candidates(rec, region.columns, "total_price"))

            # Keep a record with no SKU as long as it has commercial
            # substance. Ariba prints "Not Available" as the part number,
            # and the checklist explicitly covers that case: the quote
            # number becomes required instead. Dropping the line would
            # hide the very condition we must report.
            if part is None and qty is None and total is None:
                continue
            if part is None and total is None:
                continue

            items.append({
                "part_number": part,
                "quantity": qty,
                "unit_price": unit,
                "total_price": total,
                "description": record_text(rec, region.columns),
                "line_no": _line_no(rec, region.columns),
                "raw": rec[0].text[:200],
                "page": region.page,
                "arithmetic_ok": exact,
            })
            found += 1
        diag["region_detail"].append(
            {"page": region.page, "score": region.score,
             "columns": {f: [round(c.x0), round(c.x1), c.label]
                         for f, c in region.columns.items()},
             "records": len(records), "items": found})

    return dedupe(items), diag


def _line_no(record: list[Row], cols: dict[str, Column]) -> Optional[str]:
    for toks in band_tokens(record, cols.get("line_no")):
        for t in toks:
            if LINE_NO_RE.match(t):
                return t
    return None


def dedupe(items: list[dict]) -> list[dict]:
    """Drop items repeated verbatim across pages.

    Careful: SYNNEX legitimately lists the same SKU twice with different
    prices (lines 3 and 4 are both F5-SVC-BIG-VE+STDL13). Only an exact
    match on every commercial field plus line number is a duplicate.
    """
    seen, out = set(), []
    for it in items:
        key = (it["part_number"], it["quantity"], str(it["unit_price"]),
               str(it["total_price"]), it["line_no"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out
