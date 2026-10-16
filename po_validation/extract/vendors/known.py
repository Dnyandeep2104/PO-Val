"""Reseller-specific extractors.

Almost nothing is needed here. The geometric table reader and the
checklist field scrapers in base.py handle all six sample layouts without
vendor code. A subclass exists only to (a) let the registry name the
layout in reports, which matters for spotting when one reseller's
template drifts, and (b) override a single field when a reseller renders
it in a way the general logic gets wrong.

Adding a reseller should normally mean adding fingerprints and nothing
else. If a subclass here grows a full parser, that is a signal the
general reader is missing something and should be improved instead.
"""

from __future__ import annotations

from typing import Optional

from ..base import BaseExtractor
from ..layout import PdfView


class IngramExtractor(BaseExtractor):
    name = "ingram"
    fingerprints = ["ingram micro"]

    def extract(self, view: PdfView, source_id: str):
        po = super().extract(view, source_id)
        from ..parties import Party
        import re

        # Extract partner reseller if cited on Ingram PO (e.g. CDW, ITSAVVY LLC)
        if re.search(r"\bCDW\s+LOGISTICS\s+LLC\b|\bCDW\s+ORDER#\b|@CDW\.COM\b|\bRSLR:\s*CDW\b", view.layout_text, re.I):
            po.reseller_name = "CDW"
            po.parties.append(Party(role="reseller", label="Reseller Partner", lines=["CDW"], page=0).to_dict())
        elif re.search(r"\bITSAVVY\s+LLC\b", view.layout_text, re.I):
            po.reseller_name = "ITSAVVY LLC"
            po.parties.append(Party(role="reseller", label="Reseller Partner", lines=["ITSAVVY LLC"], page=0).to_dict())

        has_bill_to = any(p.get("role") == "bill_to" and p.get("name") for p in po.parties)
        if not has_bill_to:
            bt = Party(
                role="bill_to",
                label="Letterhead",
                lines=["INGRAM MICRO", "3351 MICHELSON DR STE 100", "IRVINE, CA 92612-0697", "714-566-1000"],
                page=0
            )
            po.parties.append(bt.to_dict())

        has_ship_to = any(p.get("role") == "ship_to" and p.get("name") for p in po.parties)
        if not has_ship_to:
            m = re.search(r"\bS\s*\n\s*H\s*\n\s*I\s+([A-Za-z0-9 &.,'-]+)\s*\n\s*P\s+([^\n]+)\s*\n(?:\s*EUPO[^\n]+\n)?\s*T\s+([^\n]+)", view.layout_text)
            if m:
                name_line = m.group(1).strip()
                addr1 = m.group(2).strip()
                addr2 = m.group(3).strip()
                st = Party(role="ship_to", label="Ship To", lines=[name_line, addr1, addr2], page=0)
                po.parties.append(st.to_dict())
            else:
                eu_parties = [p for p in po.parties if p.get("role") == "end_user" and p.get("name")]
                if eu_parties:
                    st = Party(role="ship_to", label="Ship To (EU)", lines=list(eu_parties[0]["lines"]), page=0)
                    po.parties.append(st.to_dict())

        if not po.po_total:
            from ..layout import to_decimal
            from decimal import Decimal
            for m_tot in re.finditer(r"\|\s*\d+\s*\|\s*\|\s*(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\b", view.raw_text):
                cand = to_decimal(m_tot.group(1))
                if cand is not None and cand > Decimal("0.00"):
                    po.po_total = cand
                    po.total_candidates.append((9, str(cand), "ingram_footer_total"))
        return po


class SynnexExtractor(BaseExtractor):
    name = "synnex"
    fingerprints = ["td synnex", "synnex"]

    def po_number(self, view: PdfView) -> Optional[str]:
        # SYNNEX prints its own "Order Number" and the end customer's
        # "Cust PO #". The order F5 books against is SYNNEX's.
        val = view.label_value([r"order\s*number"])
        if val:
            token = val.split()[0].strip(" .:#-")
            if token and not token.isalpha():
                return token
        return super().po_number(view)

    def extract(self, view: PdfView, source_id: str):
        po = super().extract(view, source_id)
        resellers = [p for p in po.parties if p.get("role") == "reseller" and p.get("name")]
        if resellers and not po.reseller_name:
            po.reseller_name = resellers[0]["name"]
        return po


class WwtExtractor(BaseExtractor):
    name = "wwt"
    fingerprints = ["world wide technology", "wwt"]

    def extract(self, view: PdfView, source_id: str):
        po = super().extract(view, source_id)
        if not po.reseller_name:
            po.reseller_name = "WORLD WIDE TECHNOLOGY LLC"
        return po


class CarahsoftExtractor(BaseExtractor):
    name = "carahsoft"
    fingerprints = ["carahsoft"]


class AribaExtractor(BaseExtractor):
    """SAP Business Network / Ariba, used by Dell and PayPal among others.

    The quote number lives in a free-text comment rather than a field, and
    the part number column frequently reads "Not Available". Both are
    handled generically; this exists to label the layout.
    """
    name = "ariba"
    fingerprints = ["sap business network", "ariba"]


class SapExtractor(BaseExtractor):
    name = "sap"
    fingerprints = ["sap netweaver", "zdd_purch_order"]


class GenericExtractor(BaseExtractor):
    name = "generic"
    fingerprints = []
