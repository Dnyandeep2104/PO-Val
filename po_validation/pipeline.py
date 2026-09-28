"""The orchestrator.

    source -> extract -> resolve -> validate -> act -> report -> ledger

Every stage is injected, so the same Pipeline object runs against a local
folder with a stub quote source on a laptop and against Blob, Snowflake
and Salesforce on the cluster. That is not architectural neatness for its
own sake: it is the only way to test business logic while access tickets
are pending.

One document failing never stops the batch.
"""

from __future__ import annotations

import logging
from typing import Optional

from datetime import datetime, timedelta, timezone

from .extract import registry
from .ingest.ledger import Ledger, NullLedger
from .ingest.sources import Document, DocumentSource
from .models import Outcome, ParsedPO, Quote, ValidationResult
from .report.writer import ExceptionRouter, render_summary
from .resolve.base import QuoteSource
from .validate.engine import Engine

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        source: DocumentSource,
        engine: Engine,
        quote_source: Optional[QuoteSource] = None,
        router: Optional[ExceptionRouter] = None,
        ledger: Optional[Ledger] = None,
        salesforce=None,
        record_all: bool = True,
        defer_max_attempts: int = 8,
        defer_window_hours: int = 24,
    ):
        self.source = source
        self.engine = engine
        self.quote_source = quote_source
        self.router = router
        self.ledger = ledger or NullLedger()
        self.salesforce = salesforce
        self.record_all = record_all
        self.defer_max_attempts = defer_max_attempts
        self.defer_window_hours = defer_window_hours

    # ------------------------------------------------------------ one doc
    def process_document(self, doc: Document) -> ValidationResult:
        po = registry.parse(doc.data, doc.source_id)
        quote = self._resolve(po)
        result = self.engine.run(po, quote)

        # A document we have already finished with is a duplicate. This is
        # belt-and-braces: list_new already filters on hash, but a manual
        # re-run or a backfill can route around that.
        if self.ledger.seen(po.content_hash):
            result.outcome = Outcome.DUPLICATE
        elif self._should_defer(result):
            result.outcome = Outcome.DEFERRED

        self._act(result)

        if self.router:
            result.action.setdefault("notified", self.router.route(result))

        if self.record_all or result.outcome is not Outcome.EXTRACTION_FAILED:
            try:
                self.ledger.record_result(result)
            except Exception as exc:
                log.error("ledger write failed for %s: %s", doc.source_id, exc)

        return result

    # Failures that mean "ask again later", not "this PO is wrong".
    #
    # The quote tables are a replica of Salesforce, so they lag. A quote
    # approved shortly before its PO arrives will not be there yet. On a
    # 15-minute schedule that is not hypothetical, and rejecting the PO
    # would have us tell a reseller their quote does not exist when it
    # does. These get held and retried instead.
    DEFERRABLE = {"quote_exists", "opportunity_linked"}

    def _should_defer(self, result: ValidationResult) -> bool:
        blockers = result.blockers
        if not blockers:
            return False
        if any(f.rule_id not in self.DEFERRABLE for f in blockers):
            return False          # something else is genuinely wrong too

        attempts = self.ledger.attempts(result.po.content_hash)
        if attempts >= self.defer_max_attempts:
            result.po.warnings.append(
                f"Quote still not found after {attempts} attempt(s) over "
                f"{self.defer_window_hours}h; treating as a real failure.")
            return False

        first = self.ledger.first_seen(result.po.content_hash)
        if first is not None:
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - first
            if age > timedelta(hours=self.defer_window_hours):
                result.po.warnings.append(
                    f"Quote still not found {age.days}d after first seen; "
                    f"treating as a real failure.")
                return False

        result.action["deferred_attempt"] = attempts + 1
        result.action["deferred_reason"] = blockers[0].message
        return True

    def _resolve(self, po: ParsedPO) -> Optional[Quote]:
        if self.quote_source is None or not po.quote_number:
            return None
        try:
            return self.quote_source.fetch(po.quote_number)
        except ValueError as exc:
            # Malformed quote number. Not an outage; the rules will catch it.
            log.info("quote lookup skipped for %s: %s", po.source_id, exc)
            return Quote(quote_number=po.quote_number or "", found=False,
                         source=self.quote_source.name)
        except Exception as exc:
            log.error("quote lookup failed for %s: %s", po.source_id, exc)
            po.warnings.append(f"Quote lookup error: {exc}")
            return None

    def _act(self, result: ValidationResult) -> None:
        if self.salesforce is None:
            return
        if self.router and not self.router.should_book(result):
            result.action["booked"] = False
            result.action["reason"] = (
                f"Outcome {result.outcome.value} is not configured to auto-book.")
            return
        if result.outcome is not Outcome.VALIDATED:
            result.action["booked"] = False
            result.action["reason"] = f"Outcome is {result.outcome.value}."
            return
        try:
            result.action.update(self.salesforce.create_booking_form(result))
        except Exception as exc:
            log.exception("booking form creation failed")
            result.action.update({"attempted": True, "success": False,
                                  "errors": [{"message": str(exc)}]})

    # -------------------------------------------------------------- batch
    def run(self, limit: Optional[int] = None,
            reprocess: bool = False) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        docs = (self.source.list_documents() if reprocess
                else self.source.list_new(self.ledger))

        for n, doc in enumerate(docs):
            if limit is not None and n >= limit:
                break
            try:
                results.append(self.process_document(doc))
            except Exception as exc:
                # Nothing gets to kill the batch. The next PO is somebody's
                # order too.
                log.exception("unhandled error processing %s", doc.source_id)
                po = ParsedPO(source_id=doc.source_id,
                              content_hash=doc.content_hash, layout="failed")
                po.warnings.append(f"Pipeline error: {type(exc).__name__}: {exc}")
                r = ValidationResult(po=po, quote=None,
                                     outcome=Outcome.EXTRACTION_FAILED,
                                     checklist=self.engine.checklist.name)
                if self.router:
                    self.router.route(r)
                results.append(r)

        log.info("%s", render_summary(results))
        return results
