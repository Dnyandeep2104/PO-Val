"""Where the truth about a quote lives.

Deliberately an interface. Today it is the Snowflake replica of the
Salesforce Oracle Quote objects. Prady flagged that L2Q may change how
purchase orders and quotes work; when that lands, we write one new
QuoteSource and change one line of config. The checklist, the extractors,
and the booking logic do not move.
"""

from __future__ import annotations

import json
from pathlib import Path
from decimal import Decimal
import re
from abc import ABC, abstractmethod
from typing import Optional, Union

from ..models import Quote, QuoteLine

QUOTE_NUMBER_RE = re.compile(r"^F5Q-\d{8}$")


class QuoteNotFound(Exception):
    pass


class QuoteSource(ABC):
    """Implementations must be safe to call with attacker-controlled input:
    the quote number is parsed off a PDF that arrived by email."""

    name = "base"

    @abstractmethod
    def fetch(self, quote_number: str) -> Quote:
        ...

    @staticmethod
    def validate_quote_number(quote_number: Optional[str]) -> str:
        """Whitelist the format before it ever reaches a query string.

        The prototype interpolated the parsed value straight into SQL.
        The blast radius is small given the warehouse role is read-only,
        but the value is attacker-influenced and this costs nothing.
        """
        if not quote_number:
            raise ValueError("No quote number supplied.")
        q = quote_number.strip().upper()
        if not QUOTE_NUMBER_RE.match(q):
            raise ValueError(
                f"Quote number {q!r} does not match the expected F5Q-######## format."
            )
        return q


class StubQuoteSource(QuoteSource):
    """In-memory quotes. Lets the whole pipeline run and be tested with no
    Snowflake, no Azure and no Salesforce, which is where you are today."""

    name = "stub"

    def __init__(self, quotes: Optional[dict[str, Quote]] = None):
        self.quotes = quotes or {}

    def add(self, quote: Quote) -> None:
        self.quotes[quote.quote_number.upper()] = quote

    def fetch(self, quote_number: str) -> Quote:
        q = self.validate_quote_number(quote_number)
        found = self.quotes.get(q)
        if found is None:
            return Quote(quote_number=q, found=False, source=self.name)
        found.found = True
        found.source = self.name
        return found

    @classmethod
    def from_lines(cls, quote_number: str, lines: list[tuple], **kw) -> "StubQuoteSource":
        """from_lines('F5Q-00972766', [(part, qty, unit, total), ...])"""
        q = Quote(
            quote_number=quote_number,
            found=True,
            lines=[QuoteLine(part_number=p, quantity=qt, unit_price=u, total_price=t)
                   for p, qt, u, t in lines],
            source="stub",
            **kw,
        )
        return cls({quote_number.upper(): q})

    @classmethod
    def from_json_file(cls, path: str | Path) -> "StubQuoteSource":
        p = Path(path)
        if not p.is_file():
            return cls({})
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return cls({})
        quotes = {}
        items = data if isinstance(data, list) else data.get("quotes", [])
        for item in items:
            qnum = item.get("quote_number") or item.get("name")
            if not qnum:
                continue
            lines = [
                QuoteLine(
                    part_number=l.get("part_number") or l.get("part") or "",
                    description=l.get("description") or "",
                    quantity=Decimal(str(l.get("quantity") or 0)),
                    unit_price=Decimal(str(l.get("unit_price") or 0)),
                    total_price=Decimal(str(l.get("total_price") or 0)),
                )
                for l in item.get("lines", [])
            ]
            q = Quote(
                quote_number=qnum,
                found=True,
                opportunity_id=item.get("opportunity_id"),
                account_name=item.get("account_name"),
                end_user_name=item.get("end_user_name"),
                reseller_name=item.get("reseller_name"),
                currency=item.get("currency", "USD"),
                status=item.get("status", "Bookable"),
                lines=lines,
            )
            quotes[qnum.upper()] = q
        return cls(quotes)
