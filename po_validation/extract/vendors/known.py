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


class WwtExtractor(BaseExtractor):
    name = "wwt"
    fingerprints = ["world wide technology", "wwt"]


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
