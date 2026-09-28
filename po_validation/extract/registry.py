"""Layout detection and dispatch.

Detection picks the highest-scoring extractor. Falling through to the
generic one is not a failure: the geometric reader handles unseen layouts,
and the checklist reports the fallback so engineering knows when a new
reseller has become regular traffic.
"""

from __future__ import annotations

import logging

from ..models import ParsedPO, sha256_bytes
from .base import BaseExtractor
from .layout import PdfView
from .vendors.known import (AribaExtractor, CarahsoftExtractor, GenericExtractor,
                            IngramExtractor, SapExtractor, SynnexExtractor,
                            WwtExtractor)

log = logging.getLogger(__name__)

VENDOR_THRESHOLD = 0.50

EXTRACTORS: list[BaseExtractor] = [
    IngramExtractor(), SynnexExtractor(), WwtExtractor(),
    CarahsoftExtractor(), AribaExtractor(), SapExtractor(),
    GenericExtractor(),
]


def detect(view: PdfView) -> tuple[BaseExtractor, float]:
    scored = []
    for ex in EXTRACTORS:
        try:
            scored.append((ex, ex.matches(view)))
        except Exception as exc:
            log.warning("matcher %s raised: %s", ex.name, exc)
            scored.append((ex, 0.0))
    scored.sort(key=lambda t: t[1], reverse=True)
    best, score = scored[0]
    if score < VENDOR_THRESHOLD or best.name == "generic":
        return next(e for e in EXTRACTORS if e.name == "generic"), score
    return best, score


def parse(data: bytes, source_id: str) -> ParsedPO:
    """Bytes in, ParsedPO out. Never raises: an unreadable PDF becomes a
    zero-confidence result carrying the error, so the pipeline can route
    it to exceptions like any other bad document."""
    try:
        view = PdfView(data, name=source_id)
        extractor, score = detect(view)
        po = extractor.extract(view, source_id)
        if score < VENDOR_THRESHOLD:
            po.warnings.append(
                f"No reseller layout matched (best score {score:.2f}); "
                f"used the general table reader.")
        return po
    except Exception as exc:
        log.exception("extraction failed for %s", source_id)
        po = ParsedPO(source_id=source_id,
                      content_hash=sha256_bytes(data) if data else "",
                      layout="failed", confidence=0.0)
        po.warnings.append(f"Extraction raised {type(exc).__name__}: {exc}")
        return po


def register(extractor: BaseExtractor) -> None:
    EXTRACTORS.insert(0, extractor)
