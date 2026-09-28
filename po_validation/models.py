"""Canonical data contracts for the PO validation pipeline.

Every extractor returns a ParsedPO. Every quote source returns a Quote.
The validation engine only ever sees these two shapes, which is what lets
us add reseller layouts and swap the quote backend without touching rules.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------

def money(value: Any) -> Optional[Decimal]:
    """Coerce anything money-ish to a 2dp Decimal. None-safe.

    Uses Decimal throughout rather than float. Float arithmetic on a
    50-line PO accumulates error that a $1 tolerance check will happily
    hide, and 'off by a cent' is exactly the class of bug this system
    exists to catch.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        if cleaned in ("", "-"):
            return None
        neg = cleaned.startswith("(") and cleaned.endswith(")")
        if neg:
            cleaned = cleaned[1:-1]
        d = Decimal(cleaned)
        if neg:
            d = -d
    else:
        d = Decimal(str(value))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_part(part: Optional[str]) -> Optional[str]:
    """Part numbers arrive with stray whitespace and inconsistent case.

    Normalizing at the boundary means the matching logic never has to
    care. We deliberately do NOT strip hyphens or plus signs: F5 SKUs
    like F5-SVC-BIG-VE+PREL13 carry meaning in those characters.
    """
    if part is None:
        return None
    return " ".join(part.split()).upper().strip()


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------

class Severity(str, Enum):
    BLOCKER = "BLOCKER"   # cannot proceed, do not create a booking form
    MAJOR = "MAJOR"       # needs a human before booking
    MINOR = "MINOR"       # log it, do not hold up the booking
    INFO = "INFO"


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"         # rule could not run (missing prerequisite)


class Outcome(str, Enum):
    VALIDATED = "VALIDATED"           # clean, safe to auto-book
    NEEDS_REVIEW = "NEEDS_REVIEW"     # MAJOR findings, route to a human
    REJECTED = "REJECTED"             # BLOCKER findings
    EXTRACTION_FAILED = "EXTRACTION_FAILED"  # we never got usable data out of the PDF
    DUPLICATE = "DUPLICATE"           # already processed this document
    DEFERRED = "DEFERRED"             # can't decide yet, retry next run


# --------------------------------------------------------------------------
# parsed PO
# --------------------------------------------------------------------------

@dataclass
class LineItem:
    part_number: Optional[str] = None
    quantity: Optional[int] = None
    unit_price: Optional[Decimal] = None
    total_price: Optional[Decimal] = None
    description: Optional[str] = None
    line_no: Optional[str] = None
    raw: Optional[str] = None

    def __post_init__(self):
        self.part_number = normalize_part(self.part_number)
        self.unit_price = money(self.unit_price)
        self.total_price = money(self.total_price)

    @property
    def key(self) -> tuple:
        """Composite key for matching.

        Part number alone is NOT unique on a PO. The same SKU legitimately
        appears on multiple lines with different service terms or start
        dates. Pairing on part number alone is the bug in the prototype.
        """
        return (self.part_number, self.quantity)


@dataclass
class ParsedPO:
    """Everything we managed to pull off the PDF, plus how sure we are."""

    source_id: str                       # blob name or local path
    content_hash: str = ""               # sha256 of the file bytes, for dedupe
    po_number: Optional[str] = None
    quote_number: Optional[str] = None
    po_date: Optional[date] = None
    currency: Optional[str] = None
    reseller_name: Optional[str] = None
    bill_to: Optional[str] = None
    ship_to: Optional[str] = None
    po_total: Optional[Decimal] = None
    line_items: list[LineItem] = field(default_factory=list)

    # checklist fields
    payment_terms: Optional[str] = None
    f5_entity: Optional[str] = None            # CORP | GSLLC | EMEA | SG
    f5_entity_name: Optional[str] = None
    parties: list[dict] = field(default_factory=list)
    inco_terms: Optional[str] = None
    inco_terms_acceptable: Optional[bool] = None
    carriers: list[str] = field(default_factory=list)
    carrier_account: Optional[str] = None
    eori: Optional[str] = None
    importer_of_record: Optional[str] = None
    quote_numbers: list[str] = field(default_factory=list)
    total_candidates: list = field(default_factory=list)

    # provenance
    layout: str = "unknown"              # which extractor handled it
    confidence: float = 0.0              # 0..1, see extract/base.py
    is_ocr: bool = False
    arithmetic_failures: list[str] = field(default_factory=list)
    extraction_diagnostics: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    raw_text: str = ""

    def party(self, role: str) -> Optional[dict]:
        for p in self.parties:
            if p.get("role") == role:
                return p
        return None

    def parties_of(self, role: str) -> list[dict]:
        return [p for p in self.parties if p.get("role") == role]

    # Fields that must always be Decimal, whenever they are set. Extractors
    # assign these after construction, which would otherwise bypass the
    # __post_init__ coercion and leave a str in a field the rules subtract.
    _MONEY_FIELDS = ("po_total",)

    def __setattr__(self, name, value):
        if name in self._MONEY_FIELDS:
            value = money(value)
        object.__setattr__(self, name, value)

    def __post_init__(self):
        self.po_total = money(self.po_total)

    @property
    def computed_total(self) -> Optional[Decimal]:
        """Sum of line totals. Compared against po_total as a self-check:
        if the lines do not add up to the PO's own stated total, we
        mis-parsed and should not trust anything else on the page."""
        totals = [li.total_price for li in self.line_items if li.total_price is not None]
        if not totals:
            return None
        return money(sum(totals))

    def to_dict(self) -> dict:
        return _jsonify(asdict(self))


# --------------------------------------------------------------------------
# quote (from Snowflake / SFDC / whatever L2Q becomes)
# --------------------------------------------------------------------------

@dataclass
class QuoteLine:
    part_number: Optional[str] = None
    quantity: Optional[int] = None
    unit_price: Optional[Decimal] = None
    total_price: Optional[Decimal] = None
    description: Optional[str] = None

    def __post_init__(self):
        self.part_number = normalize_part(self.part_number)
        self.unit_price = money(self.unit_price)
        self.total_price = money(self.total_price)

    @property
    def key(self) -> tuple:
        return (self.part_number, self.quantity)


@dataclass
class Quote:
    quote_number: str
    found: bool = False
    lines: list[QuoteLine] = field(default_factory=list)
    opportunity_id: Optional[str] = None
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    currency: Optional[str] = None
    expiry_date: Optional[date] = None
    status: Optional[str] = None
    source: str = "unknown"              # "snowflake", "sfdc", "stub"

    # Reference data the PO is checked against. The checklist is mostly
    # about parties, so "is this PO correct" cannot be answered from line
    # items alone — the Bill To on the paper has to be the account the
    # quote belongs to, and the payment terms have to be the ones that
    # account is set up with.
    account_name: Optional[str] = None       # Bill To account on the quote
    payment_terms: Optional[str] = None      # the account's default terms
    end_user_name: Optional[str] = None
    reseller_name: Optional[str] = None

    @property
    def total(self) -> Optional[Decimal]:
        totals = [ln.total_price for ln in self.lines if ln.total_price is not None]
        if not totals:
            return None
        return money(sum(totals))


# --------------------------------------------------------------------------
# validation output
# --------------------------------------------------------------------------

@dataclass
class Finding:
    rule_id: str
    status: Status
    severity: Severity
    message: str
    expected: Any = None
    actual: Any = None
    delta: Any = None
    line_ref: Optional[str] = None       # which PO line, when applicable

    def to_dict(self) -> dict:
        return _jsonify(asdict(self))


@dataclass
class ValidationResult:
    po: ParsedPO
    quote: Optional[Quote]
    findings: list[Finding] = field(default_factory=list)
    outcome: Outcome = Outcome.NEEDS_REVIEW
    checklist: str = "sos"
    run_id: str = ""
    evaluated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    action: dict = field(default_factory=dict)   # what act/ did, if anything

    def by_status(self, status: Status) -> list[Finding]:
        return [f for f in self.findings if f.status is status]

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings
                if f.status is Status.FAIL and f.severity is Severity.BLOCKER]

    @property
    def majors(self) -> list[Finding]:
        return [f for f in self.findings
                if f.status is Status.FAIL and f.severity is Severity.MAJOR]

    def decide(self) -> Outcome:
        """Severity, not count, drives the outcome. One BLOCKER rejects;
        any MAJOR routes to a human; everything else auto-books."""
        if self.blockers:
            self.outcome = Outcome.REJECTED
        elif self.majors:
            self.outcome = Outcome.NEEDS_REVIEW
        else:
            self.outcome = Outcome.VALIDATED
        return self.outcome

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "checklist": self.checklist,
            "evaluated_at": self.evaluated_at.isoformat(),
            "outcome": self.outcome.value,
            "source_id": self.po.source_id,
            "content_hash": self.po.content_hash,
            "po_number": self.po.po_number,
            "quote_number": self.po.quote_number,
            "layout": self.po.layout,
            "confidence": self.po.confidence,
            "opportunity_id": self.quote.opportunity_id if self.quote else None,
            "account_id": self.quote.account_id if self.quote else None,
            "po_total": str(self.po.po_total) if self.po.po_total else None,
            "quote_total": str(self.quote.total) if self.quote and self.quote.total else None,
            "n_findings": len(self.findings),
            "n_failed": len(self.by_status(Status.FAIL)),
            "findings": [f.to_dict() for f in self.findings],
            "action": self.action,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _jsonify(obj: Any) -> Any:
    """Make dataclass dicts JSON-safe (Decimal, date, Enum)."""
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    return obj
