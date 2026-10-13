"""Party blocks: who the PO is from, to, billed to, shipped to.

The SOS checklist is mostly about parties, not prices. It asks whether the
Ship To has a physical address rather than a PO Box, whether the entity
name is clearly labelled and free of "c/o" verbiage, whether there is a
named contact with an email and a phone, and whether more than one Bill To
appears. None of that is answerable without segmenting the document into
labelled address blocks.

Blocks are found geometrically. "Bill To:" and "Ship To:" sit side by side
in five of the six sample POs; a line-based reader merges them into one
smear of text. We locate each label, take the horizontal band it owns
(bounded by the next label to its right), and read down until the block
ends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .layout import EMAIL_RE, PHONE_RE, PdfView, Row, Word

# Role -> label patterns.
#
# Split into SPECIFIC (unambiguous enough to trust anywhere) and GENERIC
# (real words that occur constantly inside terms-and-conditions prose).
# A bare "to", "from", "vendor" or "seller" matched mid-sentence produced
# roughly 180 phantom parties per document on the PayPal and NTT POs,
# every one of which would have been a checklist finding.
PARTY_LABELS: dict[str, list[str]] = {
    "sold_to": [r"vendor\s*address", r"sold\s*to", r"vendor\s*name",
                r"vendor", r"to", r"supplier", r"seller"],
    "freight_bill_to": [r"freight\s*bill\s*to"],
    "bill_to": [r"(?:please\s+)?(?:(?:regd|corp)[-/\s]*)*bill[-\s]*to(?:[-/\s]*(?:company\s*name|address|customer\s*name))?",
                r"(?:please\s+submit\s+)?(?:invoices?\s+to|send\s+invoices?\s+to)",
                r"(?:please\s+)?bill[-\s]*to(?:\s*(?:company\s*name|address|customer\s*name))?",
                r"billing\s*address", r"invoice\s*to",
                r"bill\s*to\s*name", r"send\s*invoices?\s*to", r"submit\s+invoices?\s+to",
                r"bill\s*(?:to)?\s*(?:/\s*)?importer\s*of\s*record",
                r"bill\s*(?:to)?\s*/\s*ship\s*to(?:\s*(?:company\s*name|address))?"],
    "ship_to": [r"(?:please\s+)?ship[-\s]*to(?:\s*\([^\)]*\))?", r"ship\s*all\s*items\s*to",
                r"(?:please\s+)?deliver\s*to(?:\s*\([^\)]*\))?",
                r"shipping\s*address", r"delivery\s*address",
                r"ship\s*to\s*address", r"ship\s*to\s*customer\s*name",
                r"ship\s*(?:to)?\s*(?:/\s*)?consignee",
                r"ship[-\s]*(?:to)?\s*(?:/[-/\s]*)?importer\s*of\s*record",
                r"bill\s*(?:to)?\s*/\s*ship\s*to(?:\s*(?:company\s*name|address))?",
                r"bill\s*/\s*ship\s*to"],
    "end_user": [r"end[-\s]*user", r"end[-\s]*customer", r"end[-\s]*user\s*info",
                 r"end[-\s]*(?:user|customer)\s*(?:details|info)?",
                 r"ship\s*to\s*/\s*end[-\s]*user\s*info", r"ship\s*to\s*/\s*end[-\s]*user",
                 r"end[-\s]*customer\s*name", r"end[-\s]*user\s*company\s*name",
                 r"end[-\s]*user\s*name",
                 r"end[-\s]*customer\s*address", r"\beu\s*:", r"\beu\s*#",
                 r"customer\s*contact(?:\s*information)?", r"customer\s*information"],
    "reseller": [r"reseller", r"re[-\s]*seller", r"distributor",
                 r"reseller\s*(?:details|info)?",
                 r"reseller\s*company\s*name", r"reseller\s*name",
                 r"partner(?:\s*name|\s*po|\s*company)?", r"partner\s*:\s*[A-Za-z0-9]"],
    "remit_to": [r"remit\s*to", r"payment\s*address"],
    "from": [r"from", r"buyer", r"purchaser"],
}

# Single words that are also ordinary English. These only count as labels
# when punctuated or standing alone on a label-only row.
GENERIC = {"to", "from", "vendor", "supplier", "seller", "buyer",
           "purchaser", "reseller", "distributor"}

MAX_LABEL_ROW_WORDS = 14

LABEL_RE = re.compile(
    r"^\s*\**\s*(" + "|".join(
        p for pats in PARTY_LABELS.values() for p in pats) + r")\s*\**\s*:?\s*$",
    re.I)

# Checklist predicates.
PO_BOX_RE = re.compile(
    r"\b(?:p\.?\s*o\.?\s*box|post\s*office\s*box|postal\s*box|"
    r"p\.?\s*o\.?\s*b\.?\s*\d|box\s+\d{1,6}\b(?!\s*\d{2,}))", re.I)
CARE_OF_RE = re.compile(r"(?:^|[\s,;])(?:c/o|c\.o\.|care\s+of|attn\s*:?\s*c/o)\b", re.I)
# A person's name: two or three capitalised words, not an org.
PERSON_RE = re.compile(
    r"\b([A-Z][a-z]{1,15}(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]{1,15})\b|"
    r"\b([A-Z][a-z]{1,15},\s*[A-Z][a-z]{1,15})\b")
ORG_WORDS = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|company|co|gmbh|s\.?a\.?|s\.?r\.?o\.?|pte|private|plc|"
    r"technologies|technology|systems|solutions|services|logistics|group|holdings|university|authority|"
    r"county|department|bank|assurance|ulc|lp|llp|ag|bv|nv|pty|srl|kk|a/s|aktieselskab|oy|"
    r"sas|sarl|spa|aps|ab|command)\b", re.I)

# Address lines, so they are not mistaken for a person's name.
STREET_RE = re.compile(
    r"\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|way|"
    r"court|ct|place|pl|parkway|pkwy|highway|hwy|suite|ste|floor|fl|unit|"
    r"building|bldg|center|centre|commerce|industrial|park|plaza|circle|cir|"
    r"terrace|trail|square|sq|route|rte)\b\.?", re.I)

# A city/state/postcode line. Excluded from contact-name detection: the
# city in "San Jose, CA 95131" is two capitalised words and otherwise
# reads exactly like a person's name.
CITY_LINE_RE = re.compile(
    r"\b[A-Z]{2}\b[\s,]*\d{5}(?:-\d{4})?\b"          # US state + ZIP
    r"|\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b"                  # Canadian postcode
    r"|\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b"         # UK postcode
    r"|\b(?:Country\s*)?(?:USA|U\.S\.A\.|US|United States|Canada|Mexico|India|Singapore|"
    r"Australia|Ireland|Germany|France|Netherlands)\b", re.I)

# Real phone numbers, not part numbers or zip codes.
PHONE_STRICT_RE = re.compile(
    r"(?:(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}"
    r"|\+\d{1,3}\s?\d{6,14}|\b\d{10,11}\b)")


MAX_BAND_WIDTH = 235.0     # a party block is a column, not half a page
MAX_TAIL_GAP = 200.0       # key-value layouts put the value far right;
                           # MAX_BAND_WIDTH is what keeps a neighbouring
                           # column out, not this


@dataclass
class Party:
    role: str
    label: str = ""
    lines: list[str] = field(default_factory=list)
    page: int = 0
    top: float = 0.0
    x0: float = 0.0

    @property
    def text(self) -> str:
        return " / ".join(self.lines)

    @property
    def name(self) -> Optional[str]:
        """The legal entity name.

        Prefers a line carrying a company suffix over merely the first
        line, because a block often opens with a stray word picked up from
        an adjacent column or a key fragment ("Loc Name", "Purchase").
        """
        def usable(ln: str) -> Optional[str]:
            s = re.sub(r"\(cid:\s*\d+\)", "", ln).strip(" :*,\t")
            if not s or len(s) < 3:
                return None
            s = re.sub(r"^[|/\s:*,_\-]+|[|/\s:*,_\-]+$", "", s)
            is_explicit_company = False
            m_comp = re.search(r"\bCompany\s*:\s*([A-Za-z0-9 &.,\'()\-]+)", s, re.I)
            if m_comp:
                s = m_comp.group(1).strip(" :*,\t")
                is_explicit_company = True
            s = re.sub(r"^(?:(?:company|partner|customer|reseller|vendor)\s*[:\-]?\s+|(?:company\s*)?name|end[-\s]*(?:user|customer)|reseller|bill[-\s]*to|ship[-\s]*to)\s*[:\-]?\s*", "", s, flags=re.I)
            s = re.sub(r"^(?:company|partner|reseller|vendor)\s+", "", s, flags=re.I)
            s = re.sub(r"\s*(?:quote\s*(?:#|no\.?|number)?|f5\s*quote).*$", "", s, flags=re.I).strip(" :*,\t")
            s = re.sub(r"\s+(?:Shipping\s+Terms|Payment\s+Terms|Fax\s*(?:No\.?|#)?|Phone\s*(?:No\.?|#)?|Tel\s*(?:No\.?|#)?)\b.*$", "", s, flags=re.I).strip(" :*,\t")
            s = re.sub(r"\s+(?:FCA|FOB|CIP|CIF|DAP|DDP|EXW|CPT)\b.*$", "", s, flags=re.I).strip(" :*,\t")
            if not s or len(s) < 3:
                return None
            if re.match(r"^(?:na|n/a|none|not\s*applicable|na\s+payment)\b", s, re.I):
                return None
            if re.search(r"[@]|thank\s*you|please\s+(?:send|submit|enter|contact)|invoicing\s*terms|internal\s*resale", s, re.I):
                return None
            if re.match(r"^(attn|attention|c/o|po\s*(?:#|no\.?|number)?|ph#|phone|email|e-?mail(?:\s*addr)?|tel|fax|"
                        r"date|page|loc\s*name|code|agency\s*name|"
                        r"buyer\s*tax|tax\s*reg|tax\s*id|vat|gst|pan\s*no|reg\s*no|co\.\s*reg|"
                        r"this\s*purchase\s*is\s*made|all\s*purchases\s*made|"
                        r"questions|contact\s*(?:person|name|phone|email)?|"
                        r"terms\b|terms\s*of\s*delivery|payment\s*terms|"
                        r"incoterms|carrier\s*details|quote\s*id|"
                        r"reference\s*eu|ship\s*to|importer\s*of\s*record|consignee|purchase\s*order|supplier\s*tax)\b", s, re.I):
                return None
            if re.search(r"\b(?:details\s+carrier\s+details|carrier\s+details)\b", s, re.I):
                return None
            if re.search(r"\b(?:to\s*provide|status\s*on|department\s*at|services\s*department|when\s*shipment|provide\s*documents)\b", s, re.I):
                return None
            if re.search(r"\b(?:at|the|of|for|to|by|in|on|with|from)$", s, re.I):
                return None
            if not any(c.isalpha() for c in s):
                return None
            if re.fullmatch(r"[\+\(\)\d\s\.\-\/]+", s) or re.search(r"^\+?[\d\s\(\)\.\-]{7,}$", s):
                return None
            if re.search(r"\b(?:technology\s+park|industrial\s+park|business\s+park)\b", s, re.I):
                return None
            if re.search(r",\s*[A-Za-z\s]+(?:\s+\d{4,6})?,\s*(?:Egypt|USA|Canada|UK|UAE|Saudi|Singapore|India)\b", s, re.I):
                return None
            if re.search(r"\b(?:building|bldg|suite|ste|floor|fl)\b.*,\s*[A-Za-z\s]+", s, re.I):
                return None
            if not is_explicit_company and (STREET_RE.search(s) or CITY_LINE_RE.search(s) or re.match(r"^\d+\s+[A-Za-z]", s)) and not ORG_WORDS.search(s):
                return None
            if s.upper() in ("LLC", "INC", "LTD", "CORP", "CO", "GMBH", "PLC", "LP", "ULC"):
                return None
            return s

        candidates = [c for c in (usable(l) for l in self.lines) if c]
        if not candidates:
            return None
        for c in candidates:
            if ORG_WORDS.search(c):
                return c
        for c in candidates:
            if len(c.split()) >= 2 and not EMAIL_RE.search(c):
                return c
        return candidates[0]

    @property
    def emails(self) -> list[str]:
        return _uniq(EMAIL_RE.findall(self.text))

    @property
    def phones(self) -> list[str]:
        out = []
        for m in PHONE_STRICT_RE.finditer(self.text):
            v = m.group(0).strip()
            digits = re.sub(r"\D", "", v)
            # Reject zips, years, and quantities masquerading as phones.
            if 10 <= len(digits) <= 15:
                out.append(v)
        return _uniq(out)

    @property
    def contact_name(self) -> Optional[str]:
        for ln in self.lines:
            m = re.search(r"(?:attn|attention|contact|poc|person)\s*:?\s*(.+)", ln, re.I)
            if m:
                cand = m.group(1).strip(" .:,")
                cand = re.sub(r"^(?:person|to|name|poc)\s*[:\-]?\s*", "", cand, flags=re.I).strip(" .:,")
                cand = re.sub(r"\(.*?\)", "", cand).strip()
                if cand and not ORG_WORDS.search(cand):
                    return cand[:80]
            # Dell PO format: "Harris, Byran L (Bryan.L.Harris@dell.com)" or "John Doe <jdoe@x.com>"
            m_del = re.match(r"^([A-Z][a-z]+(?:,\s*[A-Z][a-z]+(?:\s+[A-Z]\.?)?|\s+[A-Z][a-z]+))\s*[\(<][^>@\)]+@[^>@\)]+[\)>]", ln.strip())
            if m_del:
                return m_del.group(1).strip()
        for ln in self.lines[1:]:
            if ORG_WORDS.search(ln) or EMAIL_RE.search(ln):
                continue
            # Skip address lines.
            if (STREET_RE.search(ln) or CITY_LINE_RE.search(ln)
                    or re.match(r"^\s*\d", ln)):
                continue
            m = PERSON_RE.search(ln)
            if m:
                cand = (m.group(1) or m.group(2)).strip()
                cand = re.sub(r"^(?:person|to|name|poc)\s*[:\-]?\s*", "", cand, flags=re.I).strip(" .:,")
                return cand
        return None

    @property
    def has_po_box(self) -> bool:
        return bool(PO_BOX_RE.search(self.text))

    @property
    def has_care_of(self) -> bool:
        for ln in self.lines:
            if re.search(r"\b(?:c/o|care\s+of)\s*f5\b", ln, re.I):
                continue
            if CARE_OF_RE.search(ln):
                return True
        return False

    @property
    def has_street_address(self) -> bool:
        """A number followed by words, or a recognisable street suffix."""
        return bool(re.search(
            r"\b\d{1,6}\s+[A-Za-z][\w'.\-]*(?:\s+[\w'.\-]+){0,4}\b", self.text)
            and not self.has_po_box)

    def contact_gaps(self) -> list[str]:
        gaps = []
        if not self.contact_name:
            gaps.append("contact name")
        if not self.emails:
            gaps.append("email")
        if not self.phones:
            gaps.append("phone")
        return gaps

    def to_dict(self) -> dict:
        return {"role": self.role, "label": self.label, "name": self.name,
                "lines": self.lines, "emails": self.emails,
                "phones": self.phones, "contact_name": self.contact_name,
                "po_box": self.has_po_box, "care_of": self.has_care_of,
                "page": self.page}


def _uniq(items) -> list[str]:
    seen, out = set(), []
    for i in items:
        k = i.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(i.strip())
    return out


# ---------------------------------------------------------------------------
# block detection
# ---------------------------------------------------------------------------

def _labels_in_row(row: Row) -> list[tuple[str, str, float, float]]:
    """Every party label in one row, left to right.

    Handles 'Bill To:  Ship To:' and Ariba's three-across
    'SHIP ALL ITEMS TO   BILL TO   DELIVER TO'.

    A candidate only counts as a label if it looks structurally like one:
    punctuated with a colon, sitting on a row that is nothing but labels,
    or opening a short row. Otherwise the word "to" in a sentence becomes
    a Ship To block.
    """
    if len(row.words) > 25:
        return []

    found = []
    used: set[int] = set()
    for n in (8, 7, 6, 5, 4, 3, 2, 1):
        for i in range(len(row.words) - n + 1):
            if any(j in used for j in range(i, i + n)):
                continue
            chunk = row.words[i:i + n]
            text = " ".join(w.text for w in chunk)
            if not LABEL_RE.match(text):
                continue
            norm = text.strip(" *:").lower()
            if any(k in norm for k in ("contact person", "contact name", "contact phone", "contact email")):
                continue
            next_w = row.words[i + n].text.lower().strip(" :*,-#/") if i + n < len(row.words) else ""
            if next_w in ("contact", "loc", "phone", "email", "address", "city", "state", "zip", "country", "fax", "po", "po#", "person", "mobile"):
                continue
            role = next((r for r, pats in PARTY_LABELS.items()
                         if any(re.fullmatch(p, norm, re.I) for p in pats)), None)
            if role is None:
                continue

            colon = text.rstrip().endswith(":") or (
                i + n < len(row.words) and row.words[i + n].text.startswith(":"))
            starts_row = i == 0
            is_generic = norm in GENERIC

            if is_generic and not (colon or starts_row):
                continue
            if not (colon or starts_row or n >= 2):
                continue

            found.append((role, text.strip(" *:"),
                          min(w.x0 for w in chunk),
                          max(w.x1 for w in chunk), i, n, colon, is_generic))
            used.update(range(i, i + n))

    # A row of nothing but labels ("Vendor    Ship To") is always a header
    # for side-by-side blocks. Otherwise generic single words need a colon.
    covered = sum(f[5] for f in found)
    pure_label_row = covered >= len(row.words)

    out = []
    for role, label, x0, x1, i, n, colon, is_generic in found:
        if is_generic and not colon and not pure_label_row:
            continue
        out.append((role, label, x0, x1))

    # If the row has more than MAX_LABEL_ROW_WORDS (14), only keep it if
    # it contains at least 2 distinct side-by-side party labels.
    if len(row.words) > MAX_LABEL_ROW_WORDS and len(out) < 2:
        return []

    return sorted(out, key=lambda f: f[2])


def _normalize_row_words(row: Row) -> Row:
    new_words = []
    for w in row.words:
        if ":" in w.text and not w.text.endswith(":") and not w.text.startswith("http") and not re.match(r"^\d+:\d+", w.text):
            idx = w.text.find(":") + 1
            w1_text = w.text[:idx]
            w2_text = w.text[idx:]
            split_x = w.x0 + (w.x1 - w.x0) * (len(w1_text) / max(len(w.text), 1))
            new_words.append(Word(w1_text, w.x0, split_x, w.top, w.bottom))
            new_words.append(Word(w2_text, split_x, w.x1, w.top, w.bottom))
        elif "/" in w.text and any(k in w.text.lower() for k in ["bill", "ship"]):
            parts = w.text.split("/")
            cur_x = w.x0
            dx = (w.x1 - w.x0) / max(len(parts), 1)
            for part in parts:
                new_words.append(Word(part, cur_x, cur_x + dx, w.top, w.bottom))
                cur_x += dx
        else:
            new_words.append(w)
    return Row(new_words, row.top, row.bottom)


def extract_parties(view: PdfView, max_lines: int = 16) -> list[Party]:
    header_keys, _ = view.furniture
    parties: list[Party] = []
    seen_blocks: set[tuple] = set()

    for page in view.pages:
        rows = [_normalize_row_words(r) for r in page.rows]
        for i, row in enumerate(rows):
            labels = _labels_in_row(row)
            if not labels:
                continue

            for k, (role, label, lx0, lx1) in enumerate(labels):
                has_next = (k + 1 < len(labels))
                tail_band_x1 = labels[k + 1][2] - 8 if has_next else page.width + 40
                is_full_width_header = bool(re.search(r"customer\s*(?:contact|information)", label, re.I))
                band_x0 = 0.0 if k == 0 else (lx0 - 8)
                band_x1 = page.width - 36 if is_full_width_header else min(tail_band_x1, lx0 + MAX_BAND_WIDTH)

                lines: list[str] = []
                tail_words = sorted(
                    [w for w in row.words if w.x0 >= lx1 - 1 and w.xmid <= tail_band_x1],
                    key=lambda w: w.x0)
                if tail_words and tail_words[0].x0 - lx1 <= MAX_TAIL_GAP:
                    filtered_tail = []
                    for tw in tail_words:
                        if filtered_tail and (tw.x0 - filtered_tail[-1].x1 > 25.0):
                            break
                        if re.match(r"^(?:incoterms|carrier\s*details|quote\s*id|fca)\b", tw.text, re.I):
                            break
                        filtered_tail.append(tw)
                    if filtered_tail:
                        tail = re.sub(r"\(cid:\s*\d+\)", "", " ".join(w.text for w in filtered_tail)).strip(" :*,\t")
                        if tail:
                            lines.append(tail)

                prev_bottom = row.bottom
                for nxt in rows[i + 1: i + 1 + max_lines * 2]:
                    full_row_text = " ".join(w.text for w in nxt.words)
                    # Stop if we hit an opposing party block header
                    if re.match(r"^\**\s*(?:reseller|end[-\s]*(?:user|customer)|ship[-\s]*to|bill[-\s]*to|vendor|sold[-\s]*to)\b", full_row_text, re.I):
                        if role == "end_user" and re.match(r"^\**\s*reseller\b", full_row_text, re.I):
                            break
                        if role == "reseller" and re.match(r"^\**\s*end[-\s]*(?:user|customer)\b", full_row_text, re.I):
                            break

                    sub_pat = (
                        r"^reseller\s+(?:company\s*name|customer\s*name|name|contact\s*name|contact\s*phone|contact\s*email|loc\s*name|address|city[,\s]+state[,\s]+zip|country|phone|email)\s*(.*)"
                        if role == "reseller" else
                        r"^end[-\s]*(?:user|customer)\s+(?:company\s*name|customer\s*name|name|contact\s*name|contact\s*phone|contact\s*email|loc\s*name|address|city[,\s]+state[,\s]+zip|country|phone|email)\s*(.*)"
                    )
                    m_sub = re.match(sub_pat, full_row_text, re.I)
                    if m_sub and role in ("reseller", "end_user"):
                        sub_val = re.sub(r"\(cid:\s*\d+\)", "", m_sub.group(1)).strip(" :*,\t")
                        sub_val = re.sub(r"\s+(?:carrier\s*name|carrier\s*details|incoterms|quote\s*id).*$", "", sub_val, flags=re.I).strip(" :*,\t")
                        if sub_val:
                            lines.append(sub_val)
                        prev_bottom = nxt.bottom
                        continue

                    words = [w for w in nxt.words if band_x0 <= w.xmid <= band_x1]
                    if not words:
                        # One blank line inside a block is normal; two ends it.
                        if nxt.top - prev_bottom > 26:
                            break
                        continue
                    text = " ".join(w.text for w in sorted(words, key=lambda w: w.x0))
                    if re.match(r"^(ship\s*method|payment\s*term|line\s*item|"
                                r"item\s*(?:code|number)|ln\b|part\s*#|"
                                r"terms\s*and|purchase\s*order\s*number|"
                                r"subject\s*to\s*state|subject\s*to\s*sales\s*tax|"
                                r"via\s*for\s*po|for\s*po\s*questions|"
                                r"remarks?\b|notes?\b|internal\s*ref|f5\s*sales|f5\s*quote|"
                                r"invoicing\s*terms|this\s*purchase\s*is\s*made|"
                                r"qty\b|quantity\b|unit\s*price|extended\s*price|"
                                r"comments\b|total\s*(?:purchase|order)|payment\s*in|"
                                r"incoterms\b|carrier\s*details\b|quote\s*id\b|"
                                r"please\s*(?:send|electronic|contact))",
                                text, re.I):
                        break
                    # Stop if we hit another party block header (supporting asterisks like **END USER INFO**)
                    if re.match(r"^\**\s*(?:reseller|end[-\s]*(?:user|customer)|ship[-\s]*to|bill[-\s]*to|vendor|sold[-\s]*to)\b", text, re.I):
                        break
                    if re.search(r"\b(qty|quantity)\b.*\b(unit\s*price|extended\s*price|price)\b", text, re.I):
                        break
                    if re.search(r"\b\d+\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2}\b", text):
                        break
                    clean_text = re.sub(r"\(cid:\s*\d+\)", "", text).strip(" :*,\t")
                    if clean_text:
                        lines.append(clean_text)
                    prev_bottom = nxt.bottom
                    if len(lines) >= max_lines:
                        break

                if not lines:
                    continue
                party = Party(role=role, label=label, lines=lines,
                              page=page.index, top=row.top, x0=lx0)
                # Repeated page furniture would otherwise produce one Bill
                # To per page and trip the "multiple Bill To" rule on every
                # multi-page PO.
                key = (role, tuple(x.lower() for x in lines[:3]))
                if key not in seen_blocks:
                    seen_blocks.add(key)
                    parties.append(party)

                # Compound Bill To / Ship To block clones into both roles only if
                # the label itself explicitly contains both 'bill' and 'ship'.
                is_compound = ("ship" in label.lower() and "bill" in label.lower())
                if is_compound:
                    if role == "bill_to":
                        ship_key = ("ship_to", tuple(x.lower() for x in lines[:3]))
                        if ship_key not in seen_blocks:
                            seen_blocks.add(ship_key)
                            parties.append(Party(role="ship_to", label=label, lines=list(lines),
                                                 page=page.index, top=row.top, x0=lx0))
                    elif role == "ship_to":
                        bill_key = ("bill_to", tuple(x.lower() for x in lines[:3]))
                        if bill_key not in seen_blocks:
                            seen_blocks.add(bill_key)
                            parties.append(Party(role="bill_to", label=label, lines=list(lines),
                                                 page=page.index, top=row.top, x0=lx0))

    return merge_adjacent(parties)


def merge_adjacent(parties: list[Party], y_gap: float = 46.0,
                   x_tol: float = 34.0) -> list[Party]:
    """Fold a stacked key-value block into one party.

    SYNNEX writes the end user as eight consecutive labelled rows
    ("End User Company Name", "End User Contact Phone", "End User
    Address"...). Each is a legitimate label, so each becomes its own
    party, and the checklist then sees eight end users. They are one
    block: same role, same column, a line apart.
    """
    if not parties:
        return []
    ordered = sorted(parties, key=lambda p: (p.page, p.top, p.x0))
    out: list[Party] = [ordered[0]]
    for p in ordered[1:]:
        last = out[-1]
        if (p.role == last.role and p.page == last.page
                and abs(p.x0 - last.x0) <= x_tol
                and 0 <= p.top - last.top <= y_gap):
            last.lines.extend(p.lines)
            last.top = p.top
        else:
            out.append(p)
    return out


def by_role(parties: list[Party], role: str) -> list[Party]:
    return [p for p in parties if p.role == role]


def primary(parties: list[Party], role: str) -> Optional[Party]:
    found = by_role(parties, role)
    return found[0] if found else None
