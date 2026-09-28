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
    "bill_to": [r"bill\s*to", r"billing\s*address", r"invoice\s*to",
                r"bill\s*to\s*address", r"bill\s*to\s*name",
                r"send\s*invoices?\s*to"],
    "ship_to": [r"ship\s*to", r"ship\s*all\s*items\s*to", r"deliver\s*to",
                r"shipping\s*address", r"delivery\s*address",
                r"ship\s*to\s*address", r"ship\s*to\s*customer\s*name"],
    "end_user": [r"end\s*user", r"end\s*customer", r"end\s*user\s*info",
                 r"ship\s*to\s*/\s*end\s*user\s*info", r"ship\s*to\s*/\s*end\s*user",
                 r"end\s*customer\s*name", r"end\s*user\s*company\s*name",
                 r"end\s*user\s*name", r"end\s*user\s*address",
                 r"end\s*customer\s*address"],
    "reseller": [r"reseller", r"re\s*seller", r"distributor",
                 r"reseller\s*company\s*name", r"reseller\s*name",
                 r"reseller\s*address"],
    "remit_to": [r"remit\s*to", r"payment\s*address"],
    "from": [r"from", r"buyer", r"purchaser"],
}

# Single words that are also ordinary English. These only count as labels
# when punctuated or standing alone on a label-only row.
GENERIC = {"to", "from", "vendor", "supplier", "seller", "buyer",
           "purchaser", "reseller", "distributor"}

MAX_LABEL_ROW_WORDS = 12

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
    r"\b(inc|llc|ltd|corp|corporation|company|co|gmbh|s\.?a\.?|pte|plc|"
    r"technologies|technology|systems|solutions|services|logistics|group|holdings|university|"
    r"county|department|bank|assurance|ulc|lp|llp|ag|bv|nv|pty|srl|kk|as|oy|"
    r"sas|sarl|spa|aps|ab)\b", re.I)

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
    r"|\b(?:USA|U\.S\.A\.|United States|Canada|Mexico|India|Singapore|"
    r"Australia|Ireland|Germany|France|Netherlands)\b", re.I)

# Real phone numbers, not part numbers or zip codes.
PHONE_STRICT_RE = re.compile(
    r"(?:(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}"
    r"|\+\d{1,3}\s?\d{6,14}|\b\d{10,11}\b)")


MAX_BAND_WIDTH = 285.0     # a party block is a column, not half a page
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
            s = re.sub(r"\(cid:\d+\)", "", ln).strip(" :*,\t")
            if not s or len(s) < 3:
                return None
            if re.match(r"^(attn|attention|c/o|po#|ph#|phone|email|tel|fax|"
                        r"date|page|loc\s*name|code|agency\s*name|"
                        r"reference\s*eu|ship\s*to)\b", s, re.I):
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
            m = re.search(r"(?:attn|attention|contact|poc)\s*:?\s*(.+)", ln, re.I)
            if m:
                cand = m.group(1).strip(" .:,")
                cand = re.sub(r"\(.*?\)", "", cand).strip()
                if cand and not ORG_WORDS.search(cand):
                    return cand[:80]
        for ln in self.lines[1:]:
            if ORG_WORDS.search(ln) or EMAIL_RE.search(ln):
                continue
            # Skip address lines. "2211 North First Street" and "One Dell
            # Way" both match a two-capitalised-words pattern, which is how
            # "North First", "One Dell" and "Gateway Commerce" ended up
            # reported as the Ship To contact on four of the six real POs.
            if (STREET_RE.search(ln) or CITY_LINE_RE.search(ln)
                    or re.match(r"^\s*\d", ln)):
                continue
            m = PERSON_RE.search(ln)
            if m:
                return (m.group(1) or m.group(2)).strip()
        return None

    @property
    def has_po_box(self) -> bool:
        return bool(PO_BOX_RE.search(self.text))

    @property
    def has_care_of(self) -> bool:
        return bool(CARE_OF_RE.search(self.text))

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
    if len(row.words) > MAX_LABEL_ROW_WORDS:
        return []

    found = []
    used: set[int] = set()
    for n in (5, 4, 3, 2, 1):
        for i in range(len(row.words) - n + 1):
            if any(j in used for j in range(i, i + n)):
                continue
            chunk = row.words[i:i + n]
            text = " ".join(w.text for w in chunk)
            if not LABEL_RE.match(text):
                continue
            norm = text.strip(" *:").lower()
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
    return sorted(out, key=lambda f: f[2])


def extract_parties(view: PdfView, max_lines: int = 16) -> list[Party]:
    header_keys, _ = view.furniture
    parties: list[Party] = []
    seen_blocks: set[tuple] = set()

    for page in view.pages:
        rows = page.rows
        for i, row in enumerate(rows):
            labels = _labels_in_row(row)
            if not labels:
                continue

            for k, (role, label, lx0, lx1) in enumerate(labels):
                # The band this label owns: from just left of it to just
                # left of the next label on the same row.
                band_x0 = lx0 - 8
                band_x1 = min(
                    labels[k + 1][2] - 8 if k + 1 < len(labels) else page.width + 40,
                    lx0 + MAX_BAND_WIDTH)

                lines: list[str] = []
                # Text on the label's own row, right of the label — but only
                # if it actually follows the label. Carahsoft prints "Bill
                # To:" on the far left and "Date  Page" on the far right of
                # the same row; without the gap test the Bill To name
                # becomes "Date Page".
                tail_words = sorted(
                    [w for w in row.words if w.x0 >= lx1 - 1 and w.xmid <= band_x1],
                    key=lambda w: w.x0)
                if tail_words and tail_words[0].x0 - lx1 <= MAX_TAIL_GAP:
                    tail = re.sub(r"\(cid:\d+\)", "", " ".join(w.text for w in tail_words)).strip(" :*,\t")
                    if tail:
                        lines.append(tail)

                prev_bottom = row.bottom
                for nxt in rows[i + 1: i + 1 + max_lines * 2]:
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
                                r"qty\b|quantity\b|unit\s*price|extended\s*price|"
                                r"comments\b|total\s*(?:purchase|order)|payment\s*in|"
                                r"please\s*send\s*(?:all|electronic))",
                                text, re.I):
                        break
                    if re.search(r"\b(qty|quantity)\b.*\b(unit\s*price|extended\s*price|price)\b", text, re.I):
                        break
                    if re.search(r"\b\d+\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2}\b", text):
                        break
                    clean_text = re.sub(r"\(cid:\d+\)", "", text).strip(" :*,\t")
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
                if key in seen_blocks:
                    continue
                seen_blocks.add(key)
                parties.append(party)

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
