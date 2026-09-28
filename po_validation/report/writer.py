"""Reporting and exception routing.

Every PO produces a record whatever happens to it. VALIDATED ones need an
audit trail; everything else needs a human, and that human needs to see
the reason in one screen without opening the PDF.

Sinks are pluggable. Console and JSONL work today with no access at all.
Delta and Teams are the production shapes.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models import Outcome, Status, ValidationResult

log = logging.getLogger(__name__)

ICON = {Status.PASS: "PASS", Status.FAIL: "FAIL",
        Status.WARN: "WARN", Status.SKIP: "SKIP"}


# ---------------------------------------------------------------- rendering

def render_text(result: ValidationResult, verbose: bool = False) -> str:
    """One PO, one screen. This is what lands in the exception queue."""
    po, q = result.po, result.quote
    L: list[str] = []
    L.append("=" * 74)
    L.append(f"{result.outcome.value}   {po.source_id}")
    L.append("=" * 74)
    L.append(f"  PO number     : {po.po_number or '-'}")
    L.append(f"  Quote         : {po.quote_number or '-'}"
             + (f"  ({'found' if q and q.found else 'NOT FOUND'})" if q else ""))
    L.append(f"  Opportunity   : {(q.opportunity_id if q else None) or '-'}")
    L.append(f"  PO date       : {po.po_date or '-'}")
    L.append(f"  Layout        : {po.layout}   confidence {po.confidence:.2f}")
    L.append(f"  Lines         : {len(po.line_items)}"
             f"   PO total {po.po_total or po.computed_total or '-'}"
             f"   quote total {(q.total if q else None) or '-'}")
    L.append(f"  Checklist     : {result.checklist}   run {result.run_id}")

    failed = result.by_status(Status.FAIL)
    if failed:
        L.append("")
        L.append(f"  {len(failed)} FAILED CHECK(S)")
        for f in failed:
            L.append(f"    [{f.severity.value:<7}] {f.rule_id}: {f.message}")

    skipped = result.by_status(Status.SKIP)
    if skipped:
        L.append("")
        L.append(f"  {len(skipped)} check(s) could not run:")
        for f in skipped:
            L.append(f"    - {f.rule_id}: {f.message}")

    if verbose:
        passed = result.by_status(Status.PASS)
        if passed:
            L.append("")
            L.append(f"  {len(passed)} passed:")
            for f in passed:
                L.append(f"    - {f.rule_id}: {f.message}")
        if po.line_items:
            L.append("")
            L.append("  Extracted lines:")
            for li in po.line_items:
                # part_number is legitimately None when the PO prints
                # "Not Available" in the SKU column (Dell does). Formatting
                # None with a width raises, which killed the whole report
                # for that document even though the result itself was fine.
                part = li.part_number or "(no SKU on PO)"
                L.append(f"    {part:<28} qty {str(li.quantity):>5}"
                         f"  unit {str(li.unit_price):>12}"
                         f"  total {str(li.total_price):>12}")

    if result.action:
        L.append("")
        L.append(f"  Action: {json.dumps(result.action, default=str)[:400]}")

    return "\n".join(L)


def render_summary(results: list[ValidationResult]) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
    width = max((len(k) for k in counts), default=10)
    lines = ["", f"{len(results)} document(s) processed", "-" * 34]
    for outcome in Outcome:
        if outcome.value in counts:
            lines.append(f"  {outcome.value:<{width}}  {counts[outcome.value]:>4}")
    return "\n".join(lines)


# ------------------------------------------------------------------- sinks

class Sink(ABC):
    @abstractmethod
    def emit(self, result: ValidationResult) -> None: ...

    def close(self) -> None:
        pass


class ConsoleSink(Sink):
    def __init__(self, verbose: bool = False, only_exceptions: bool = False):
        self.verbose = verbose
        self.only_exceptions = only_exceptions

    def emit(self, result: ValidationResult) -> None:
        if self.only_exceptions and result.outcome is Outcome.VALIDATED:
            return
        print(render_text(result, self.verbose))


class JsonlSink(Sink):
    """Machine-readable, one JSON object per PO. Feed a dashboard from it."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, result: ValidationResult) -> None:
        with open(self.path, "a") as fh:
            fh.write(json.dumps(result.to_dict(), default=str) + "\n")


class DeltaSink(Sink):
    """Databricks. One row per finding, so you can group failures by rule
    and see which checks actually fire in the wild. That tells you which
    parts of the checklist are earning their keep."""

    SCHEMA = """
        run_id STRING, evaluated_at TIMESTAMP, checklist STRING,
        outcome STRING, source_id STRING, po_number STRING,
        quote_number STRING, layout STRING, confidence DOUBLE,
        rule_id STRING, status STRING, severity STRING, message STRING,
        expected STRING, actual STRING, delta STRING, line_ref STRING
    """

    def __init__(self, spark, table: str = "sales_ops.po_validation.findings"):
        self.spark = spark
        self.table = table
        spark.sql(f"CREATE TABLE IF NOT EXISTS {table} ({self.SCHEMA}) USING DELTA")

    def emit(self, result: ValidationResult) -> None:
        try:
            import pandas as pd
        except ImportError:
            raise RuntimeError(
                "DeltaSink requires pandas in a Databricks environment. "
                "Install with: pip install pandas")
        d = result.to_dict()
        rows = [{
            "run_id": d["run_id"],
            "evaluated_at": pd.to_datetime(d["evaluated_at"]),
            "checklist": d["checklist"], "outcome": d["outcome"],
            "source_id": d["source_id"], "po_number": d["po_number"],
            "quote_number": d["quote_number"], "layout": d["layout"],
            "confidence": float(d["confidence"]),
            "rule_id": f["rule_id"], "status": f["status"],
            "severity": f["severity"], "message": f["message"],
            "expected": _s(f.get("expected")), "actual": _s(f.get("actual")),
            "delta": _s(f.get("delta")), "line_ref": f.get("line_ref"),
        } for f in d["findings"]]
        if rows:
            (self.spark.createDataFrame(pd.DataFrame(rows))
             .write.mode("append").saveAsTable(self.table))


class TeamsWebhookSink(Sink):
    """Post exceptions to a Teams channel. Only exceptions: a bot that
    announces every success gets muted, and then it announces nothing."""

    def __init__(self, webhook_url: str,
                 outcomes: Optional[set[Outcome]] = None):
        self.webhook_url = webhook_url
        self.outcomes = outcomes or {Outcome.REJECTED, Outcome.NEEDS_REVIEW,
                                     Outcome.EXTRACTION_FAILED, Outcome.DUPLICATE}

    def emit(self, result: ValidationResult) -> None:
        if result.outcome not in self.outcomes:
            return
        failed = result.by_status(Status.FAIL)
        text = (f"**{result.outcome.value}** — PO {result.po.po_number or '?'} "
                f"(quote {result.po.quote_number or '?'})\n\n"
                + "\n".join(f"- `{f.severity.value}` {f.rule_id}: {f.message}"
                            for f in failed[:10]))
        try:
            import urllib.request
            body = json.dumps({"text": text}).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15):
                pass
        except Exception as exc:
            log.error("Teams webhook failed: %s", exc)


# --------------------------------------------------------------- dispatcher

class ExceptionRouter:
    """Sends each result to the sinks its outcome maps to.

    Queue names come from the checklist's `routing:` block, so who gets
    told about what is business config, not code.
    """

    def __init__(self, sinks: list[Sink], routing: Optional[dict] = None,
                 queues: Optional[dict[str, list[Sink]]] = None):
        self.sinks = sinks
        self.routing = routing or {}
        self.queues = queues or {}

    def route(self, result: ValidationResult) -> list[str]:
        for sink in self.sinks:
            try:
                sink.emit(result)
            except Exception as exc:
                log.error("sink %s failed: %s", type(sink).__name__, exc)

        notified: list[str] = []
        for queue in self.routing.get(result.outcome.value, {}).get("notify", []):
            notified.append(queue)
            for sink in self.queues.get(queue, []):
                try:
                    sink.emit(result)
                except Exception as exc:
                    log.error("queue %s sink failed: %s", queue, exc)
        return notified

    def should_book(self, result: ValidationResult) -> bool:
        return bool(self.routing.get(result.outcome.value, {})
                    .get("create_booking_form", False))

    def close(self) -> None:
        for sink in self.sinks:
            sink.close()


def _s(v):
    return None if v is None else str(v)
