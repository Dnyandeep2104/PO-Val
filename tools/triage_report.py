#!/usr/bin/env python3
"""Why did each PO land where it landed? One row per PO, grouped by cause.

    .venv/bin/python3 tools/triage_report.py --folder my_pos            # quotes from Snowflake (or my_quotes.json)
    .venv/bin/python3 tools/triage_report.py --folder my_pos --perfect  # pretend the quote matches the PO exactly

--perfect builds each quote from the PO's own lines, so anything still
failing is the checklist or the parser, never the quote data. Running
both modes side by side separates "parser/checklist" from "CPQ data".

Writes out/triage_report.csv and prints a cause histogram.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from po_validation.extract import registry                      # noqa: E402
from po_validation.models import Quote, QuoteLine, Status         # noqa: E402
from po_validation.validate.engine import load_engine             # noqa: E402


def quote_source():
    import os
    from po_validation.ai.client import load_env_file
    from po_validation.resolve.base import StubQuoteSource
    load_env_file()
    stub = StubQuoteSource.from_json_file(ROOT / "my_quotes.json")
    if os.environ.get("SNOWFLAKE_USER", "").strip():
        from po_validation.resolve.snowflake_live import (FallbackQuoteSource,
                                                         SnowflakeConnectorQuoteSource)
        return FallbackQuoteSource(SnowflakeConnectorQuoteSource.from_env(), stub)
    return stub


def perfect_quote(po) -> Quote:
    return Quote(quote_number=po.quote_number or "F5Q-00000000", found=True,
                 lines=[QuoteLine(part_number=l.part_number, quantity=l.quantity,
                                  unit_price=l.unit_price, total_price=l.total_price)
                        for l in po.line_items],
                 opportunity_id="006PERFECTQUOTEAAA", currency=po.currency or "USD",
                 status="Approved", source="perfect")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="my_pos")
    ap.add_argument("--perfect", action="store_true")
    ap.add_argument("--out", default="out/triage_report.csv")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    engine = load_engine("sos")
    src = None if args.perfect else quote_source()
    rows, causes = [], Counter()

    for path in sorted(Path(args.folder).glob("*.pdf")):
        po = registry.parse(path.read_bytes(), str(path))
        if args.perfect:
            quote = perfect_quote(po)
        elif po.quote_number:
            try:
                quote = src.fetch(po.quote_number)
            except Exception as exc:          # malformed number or outage
                quote = Quote(quote_number=po.quote_number, found=False, source=f"error: {exc}")
        else:
            quote = Quote(quote_number="", found=False, source="not cited")
        r = engine.run(po, quote)
        holds = [f for f in r.findings if f.status is Status.FAIL
                 and f.severity.value in ("BLOCKER", "MAJOR")]
        for rid in sorted({f.rule_id for f in holds}):
            causes[rid] += 1
        rows.append({
            "file": path.name,
            "po_number": po.po_number,
            "layout": po.layout,
            "outcome": r.outcome.value,
            "quote": po.quote_number or "",
            "quote_found": bool(quote and quote.found),
            "quote_source": getattr(quote, "source", ""),
            "lines": len(po.line_items),
            "po_total": po.po_total,
            "lines_sum": po.computed_total,
            "quote_total": quote.total if quote and quote.found else None,
            "blockers": "; ".join(sorted({f.rule_id for f in holds if f.severity.value == "BLOCKER"})),
            "majors": "; ".join(sorted({f.rule_id for f in holds if f.severity.value == "MAJOR"})),
            "first_message": holds[0].message[:160] if holds else "",
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["file"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n{len(rows)} PO(s)  ->  {dict(Counter(r['outcome'] for r in rows))}")
    print("\nRules holding orders back (count of POs):")
    for rid, n in causes.most_common():
        print(f"  {n:3}  {rid}")
    print(f"\nPer-PO detail: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
