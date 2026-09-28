"""Document-level field extraction, driven by the SOS checklist.

The checklist is mostly not about prices. It asks which F5 entity the PO
was issued to, whether payment terms are stated, whether Ship To is a
physical address with a named contact, whether Inco Terms are acceptable,
and whether shipping carrier and account details are present. Each of
those needs a field here before a rule can test it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from ..models import LineItem, ParsedPO, sha256_bytes
from . import parties as party_mod
from .layout import PdfView, to_decimal
from .tables import read_records

QUOTE_RE = r"\bF5Q-\d{8}\b"

# The four F5 contracting entities named at the top of the checklist.
F5_ENTITIES: list[tuple[str, str]] = [
    ("GSLLC", r"F5\s+Government\s+Solutions,?\s*LLC"),
    ("EMEA",  r"F5\s+Networks\s+Limited"),
    ("SG",    r"F5\s+Networks\s+Singapore\s+Pte\.?\s*Ltd"),
    ("CORP",  r"F5,\s*Inc\.?"),
    ("CORP",  r"F5\s+Networks,?\s*Inc\.?"),
    ("CORP",  r"\bF5\s+Inc\.?"),
]

PO_LABELS = [
    r"purchase\s*order\s*(?:no\.?|number|#)?", r"p\.?\s?o\.?\s*(?:no\.?|number|#)",
    r"po\s*#", r"order\s*(?:no\.?|number|#)", r"document\s*(?:no\.?|number)",
]
PO_VALUE_RE = re.compile(r"(?=[A-Z0-9\-_/]*\d)[A-Z0-9][A-Z0-9\-_/]{3,}")

DATE_LABELS = [r"(?:p\.?\s?o\.?|purchase\s*order|order|document|print)\s*date",
               r"date"]
DATE_VALUE_RE = re.compile(
    r"\d{1,4}[/\-]\d{1,2}[/\-]\d{2,4}|\d{1,2}[\s\-][A-Za-z]{3,9}[\s\-,]+\d{2,4}"
    r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}")
DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y",
                "%d-%b-%y", "%b %d, %Y", "%d %B %Y", "%B %d, %Y", "%d %b %Y"]

PAYMENT_TERMS_RE = re.compile(
    r"(?:payment\s*terms?|terms\s*of\s*payment|pmt\s*terms?|\bterms\b)\s*[:\-]?\s*"
    r"(net\s*\d{1,3}|\d{1,3}\s*days?\s*net|net\s*\d{1,3}\s*days?|"
    r"\bn\s?\d{2,3}\b|due\s*on\s*receipt|prepaid|\bcod\b|"
    r"\d+%\s*\d+\s*net\s*\d+)", re.I)

# Inco Terms. The checklist names FCA Ship Point, Origin and EXW as
# acceptable and says Destination terms need prior approval.
INCOTERMS = ["EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP",
             "DAP", "DPU", "DDP", "DAT"]
INCO_RE = re.compile(
    r"\b(?:" + "|".join(INCOTERMS) + r")\b"
    r"(?:\s+(?:free\s+carrier|ship\s*point|origin|destination|on\s+board|"
    r"named\s+place)(?:\s+\w+){0,3})?", re.I)
ACCEPTABLE_INCO_RE = re.compile(r"\b(?:FCA|EXW)\b|ship\s*point|\borigin\b", re.I)
DESTINATION_INCO_RE = re.compile(r"\bdestination\b|\b(?:DDP|DAP|DPU)\b", re.I)

CARRIERS = {"FEDEX": r"fed\s*ex|fedex", "UPS": r"\bUPS\b",
            "DHL": r"\bDHL\b", "USPS": r"\bUSPS\b"}
# A terms value, used to validate whatever a label lookup returns. SYNNEX
# and WWT put "Payment Term" in a header row whose neighbours are "Buyer"
# and "Ship Via"; without this the terms field fills with column headings.
TERMS_VALUE_RE = re.compile(
    r"(?:net\s*\d{1,3}|\d{1,3}\s*days?\s*net|n\s?\d{2,3}\b|"
    r"due\s*on\s*receipt|prepaid|\bcod\b|\d+%\s*\d+\s*net\s*\d+)", re.I)

CARRIER_ACCT_RE = re.compile(
    r"(?:carrier|freight|shipping|ups|fedex|fed\s*ex|dhl)\s*"
    r"(?:acct|account)\s*#?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{4,})", re.I)
EORI_RE = re.compile(
    r"\bEORI\s*(?:number|no\.?|#)?\s*[:\-]?\s*([A-Z]{2}[A-Z0-9]{5,15})", re.I)
IOR_RE = re.compile(r"importer\s*of\s*record\s*[:\-]?\s*(.{3,80})", re.I)

CURRENCY_RE = re.compile(
    r"\b(USD|EUR|GBP|CAD|AUD|JPY|SGD|CHF|SEK|INR|MXN|BRL|ZAR)\b")

# Grand-total labels, scored. A per-page "Sub Total" is not the order
# total: NTT's PO prints one on every page.
TOTAL_PATTERNS: list[tuple[int, str]] = [
    (10, r"total\s*purchase\s*order"),
    (10, r"p\.?\s?o\.?\s*total(?:\s*in\s*[a-z .()]*)?"),
    (9,  r"grand\s*total"),
    (8,  r"order\s*total"),
    (7,  r"total\s*\((?:USD|[A-Z]{3})\)"),
    (7,  r"total\s*amount"),
    (6,  r"amount\s*:"),
    (5,  r"net\s*total"),
    (4,  r"total\s*due"),
    (2,  r"sub\s*total"),
]
MONEY_VALUE_RE = r"-?\d{1,3}(?:,\d{3})*\.\d{2,6}|-?\d+\.\d{2,6}"


class BaseExtractor:
    """One extractor handles all layouts. Vendor subclasses exist only to
    override a field a particular reseller renders unusually."""

    name = "generic"
    fingerprints: list[str] = []

    def matches(self, view: PdfView) -> float:
        if not self.fingerprints:
            return 0.15
        hay = view.text.lower()
        hits = sum(1 for fp in self.fingerprints if fp.lower() in hay)
        return hits / len(self.fingerprints)

    # -------------------------------------------------------------- extract
    def extract(self, view: PdfView, source_id: str) -> ParsedPO:
        po = ParsedPO(source_id=source_id,
                      content_hash=sha256_bytes(view.data),
                      layout=self.name,
                      raw_text=view.raw_text)

        po.quote_number = self.quote_number(view)
        po.quote_numbers = self.all_quote_numbers(view)
        po.po_number = self.po_number(view)
        po.po_date = self.po_date(view)
        po.currency = self.currency(view)
        po.payment_terms = self.payment_terms(view)

        po.f5_entity, po.f5_entity_name = self.f5_entity(view)
        po.parties = [p.to_dict() for p in party_mod.extract_parties(view)]

        po.inco_terms, po.inco_terms_acceptable = self.inco_terms(view)
        po.carriers = self.carriers(view)
        po.carrier_account = self.carrier_account(view)
        po.eori = self.first(view, EORI_RE)
        po.importer_of_record = self.first(view, IOR_RE)

        items, diag = read_records(view)
        po.line_items = [
            LineItem(part_number=i["part_number"], quantity=i["quantity"],
                     unit_price=i["unit_price"], total_price=i["total_price"],
                     description=i["description"], line_no=i["line_no"],
                     raw=i["raw"])
            for i in items]
        po.po_total = self.po_total(view, line_sum=po.computed_total)
        po.total_candidates = [(sc, str(v), lb)
                               for sc, v, lb in self.total_candidates(view)]
        po.extraction_diagnostics = diag
        po.is_ocr = view.is_ocr
        po.arithmetic_failures = [i["raw"][:80] for i in items
                                  if not i.get("arithmetic_ok", True)]
        po.warnings.extend(view.notes)
        po.confidence = self.score(po)
        po.warnings.extend(self.self_check(po))
        return po

    # ----------------------------------------------------------- the fields
    def quote_number(self, view: PdfView) -> Optional[str]:
        found = view.find_all(QUOTE_RE)
        return found[0].upper() if found else None

    def all_quote_numbers(self, view: PdfView) -> list[str]:
        return [q.upper() for q in view.find_all(QUOTE_RE)]

    def po_number(self, view: PdfView) -> Optional[str]:
        val = view.label_value(PO_LABELS, value_re=PO_VALUE_RE,
                               stop_re=re.compile(QUOTE_RE, re.I))
        if val:
            v = val.strip(" .:#-")
            if not re.fullmatch(QUOTE_RE, v, re.I) and not v.isalpha():
                return v
        m = view.search(r"purchase\s*order\s*[:#]\s*([A-Z0-9][A-Z0-9\-_/]{3,})")
        return m.group(1) if m else None

    def po_date(self, view: PdfView) -> Optional[date]:
        val = view.label_value(DATE_LABELS, value_re=DATE_VALUE_RE)
        return parse_date(val) if val else None

    def currency(self, view: PdfView) -> Optional[str]:
        m = CURRENCY_RE.search(view.raw_text) or CURRENCY_RE.search(view.layout_text)
        return m.group(1).upper() if m else None

    def payment_terms(self, view: PdfView) -> Optional[str]:
        m = PAYMENT_TERMS_RE.search(view.text)
        if m:
            return " ".join(m.group(1).split()).upper()
        val = view.label_value(
            [r"payment\s*terms?", r"terms\s*of\s*payment", r"pmt\s*terms?",
             r"terms"],
            value_re=TERMS_VALUE_RE)
        return " ".join(val.split())[:40].upper() if val else None

    def f5_entity(self, view: PdfView) -> tuple[Optional[str], Optional[str]]:
        """Which F5 entity is this PO issued to? Checklist item one.

        Looks in the Vendor / Sold To block first: a PO can mention F5
        anywhere in body text without being issued to F5.
        """
        blocks = [p.text for p in party_mod.extract_parties(view)
                  if p.role in ("sold_to", "remit_to")]
        for hay in blocks + [view.raw_text, view.layout_text]:
            for code, pattern in F5_ENTITIES:
                m = re.search(pattern, hay, re.I)
                if m:
                    return code, " ".join(m.group(0).split())
        return None, None

    def inco_terms(self, view: PdfView) -> tuple[Optional[str], Optional[bool]]:
        labelled = view.label_value(
            [r"inco\s*terms?", r"international\s*commercial\s*terms",
             r"terms\s*of\s*delivery", r"ship\s*terms", r"delivery\s*terms",
             r"freight\s*terms", r"fob"])
        candidate = None
        if labelled and INCO_RE.search(labelled):
            candidate = " ".join(labelled.split())[:60]
        else:
            m = INCO_RE.search(view.layout_text) or INCO_RE.search(view.raw_text)
            if m:
                candidate = " ".join(m.group(0).split())
        if not candidate:
            return None, None
        if DESTINATION_INCO_RE.search(candidate):
            return candidate, False
        return candidate, bool(ACCEPTABLE_INCO_RE.search(candidate))

    def carriers(self, view: PdfView) -> list[str]:
        hay = view.text
        return [name for name, pat in CARRIERS.items() if re.search(pat, hay, re.I)]

    def carrier_account(self, view: PdfView) -> Optional[str]:
        val = self.first(view, CARRIER_ACCT_RE)
        # "Freight Acct#  Payment Term" is a header row, not an account.
        if val and re.search(r"\d", val) and not val.isalpha():
            return val
        return None

    @staticmethod
    def first(view: PdfView, pattern: re.Pattern) -> Optional[str]:
        m = pattern.search(view.layout_text) or pattern.search(view.raw_text)
        return " ".join(m.group(1).split())[:100] if m else None

    def po_total(self, view: PdfView,
                 line_sum: Optional[Decimal] = None) -> Optional[Decimal]:
        """The order total, chosen by label strength rather than position.

        A labelled grand total beats a per-page subtotal. Among equally
        strong candidates take the largest, since a running subtotal is
        never greater than the total it rolls into.
        """
        candidates = self.total_candidates(view)
        if not candidates:
            return None
        # Prefer the candidate the line items agree with. Dell's PO prints
        # "Grand Total: $81,019.50" (tax included) and "Amount: $74,844.80"
        # (goods only); the lines sum to the latter, and that is the figure
        # a quote comparison must use. Tax is not on the quote.
        if line_sum is not None:
            for _score, val, _label in candidates:
                if abs(val - line_sum) <= 1:
                    return val
        return candidates[0][1]

    def total_candidates(self, view: PdfView) -> list[tuple[int, Decimal, str]]:
        found: list[tuple[int, Decimal, str]] = []
        seen: set[tuple[int, Decimal]] = set()
        for hay in (view.layout_text, view.raw_text):
            for score, label in TOTAL_PATTERNS:
                rx = label + r"[^\n\d\-]{0,40}?(" + MONEY_VALUE_RE + r")"
                for m in re.finditer(rx, hay, re.I):
                    val = to_decimal(m.group(1))
                    if val is None or (score, val) in seen:
                        continue
                    seen.add((score, val))
                    found.append((score, val, " ".join(m.group(0).split())[:60]))
        # Strongest label first; among equals the largest, since a running
        # subtotal is never greater than the total it rolls into.
        found.sort(key=lambda c: (-c[0], -c[1]))
        return found

    # ----------------------------------------------------------- confidence
    def score(self, po: ParsedPO) -> float:
        s = 0.0
        if po.line_items:
            s += 0.25
            complete = sum(1 for li in po.line_items
                           if li.quantity is not None and li.total_price is not None)
            s += 0.20 * (complete / len(po.line_items))
        if po.quote_number:
            s += 0.10
        if po.po_number:
            s += 0.10
        if po.f5_entity:
            s += 0.05
        if any(p["role"] in ("ship_to", "bill_to") for p in po.parties):
            s += 0.05
        if po.po_total is not None:
            s += 0.05
        # The strongest signal available: our lines add up to the total the
        # document itself prints.
        if po.po_total is not None and po.computed_total is not None:
            if abs(po.po_total - po.computed_total) <= 1:
                s += 0.20
        if po.arithmetic_failures:
            s -= 0.10
        if po.is_ocr:
            s -= 0.10
        return round(max(0.0, min(s, 1.0)), 3)

    def self_check(self, po: ParsedPO) -> list[str]:
        w: list[str] = []
        if not po.line_items:
            w.append("No line items extracted.")
        if po.po_total is not None and po.computed_total is not None:
            diff = po.computed_total - po.po_total
            if abs(diff) > 1:
                w.append(f"Extracted line totals ({po.computed_total}) do not sum "
                         f"to the PO's stated total ({po.po_total}); off by {diff}.")
        if po.arithmetic_failures:
            w.append(f"{len(po.arithmetic_failures)} line(s) where unit price x "
                     f"quantity does not equal the line total.")
        return w


def parse_date(s: str) -> Optional[date]:
    s = " ".join(str(s).split()).strip(" ,")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2})$", s)
    if m:
        mm, dd, yy = (int(x) for x in m.groups())
        try:
            return date(2000 + yy, mm, dd)
        except ValueError:
            return None
    return None
