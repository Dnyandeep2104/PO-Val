#!/usr/bin/env python3
"""Run the full PO validation pipeline locally, with no Azure, no
Snowflake and no Salesforce.

    python run_local.py --folder fixtures --verbose
    python run_local.py --folder fixtures --checklist renewals
    python run_local.py --folder fixtures --reprocess     # ignore the ledger

Quote data comes from a small stub file (see --quotes). Point it at the
real Snowflake source by swapping one object; the notebook driver shows
how. This is the loop you can iterate business rules in today.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from po_validation.ingest.ledger import JsonLedger, NullLedger
from po_validation.ingest.sources import LocalFolderSource
from po_validation.ingest.watcher import FolderWatcher
from po_validation.models import Quote, QuoteLine

from po_validation.pipeline import Pipeline
from po_validation.report.writer import (ConsoleSink, ExceptionRouter,
                                         JsonlSink, render_summary)
from po_validation.resolve.base import StubQuoteSource
from po_validation.validate.engine import load_engine
from po_validation.booking.builder import BookingFormBuilder
from po_validation.booking.card import render_adaptive_card, render_html_email_preview
from po_validation.ai.client import F5AIClient

HERE = Path(__file__).resolve().parent


def load_stub_quotes(path: Path) -> StubQuoteSource:
    """Quotes from JSON, standing in for the Snowflake lookup."""
    src = StubQuoteSource()
    if not path.exists():
        return src
    for q in json.loads(path.read_text()):
        src.add(Quote(
            quote_number=q["quote_number"],
            found=True,
            opportunity_id=q.get("opportunity_id"),
            account_id=q.get("account_id"),
            currency=q.get("currency"),
            status=q.get("status"),
            account_name=q.get("account_name"),
            payment_terms=q.get("payment_terms"),
            end_user_name=q.get("end_user_name"),
            reseller_name=q.get("reseller_name"),
            source="stub",
            lines=[QuoteLine(part_number=l["part_number"],
                             quantity=l.get("quantity"),
                             unit_price=l.get("unit_price"),
                             total_price=l.get("total_price"))
                   for l in q.get("lines", [])],
        ))
    return src


def write_booking_artifacts(res, bf_dir: Path, ai_client: F5AIClient) -> None:
    """Generate Salesforce JSON payload, Outlook Adaptive Card, and HTML preview."""
    po_num = res.po.po_number or Path(res.source_id).stem.replace(" ", "_")
    form = BookingFormBuilder.build(res)
    blocker_msgs = [f.message for f in res.findings if f.severity.value == "BLOCKER" and f.status.value == "FAIL"]
    major_msgs = [f.message for f in res.findings if f.severity.value == "MAJOR" and f.status.value == "FAIL"]
    note_titles = [n.title for n in form.notes]

    summary = ai_client.generate_sos_summary(
        po_number=res.po.po_number or "N/A",
        account=form.account_name or "Unknown",
        amount=f"{form.currency} ${form.amount:,.2f}",
        blockers=blocker_msgs,
        majors=major_msgs,
        notes=note_titles,
    )

    # 1. Salesforce Booking_Form__c REST API Payload
    sf_payload = form.to_salesforce_payload()
    (bf_dir / f"{po_num}_salesforce_booking_form.json").write_text(
        json.dumps(sf_payload, indent=2)
    )

    # 2. Outlook Actionable Adaptive Card (JSON)
    card = render_adaptive_card(form, summary)
    (bf_dir / f"{po_num}_outlook_card.json").write_text(
        json.dumps(card, indent=2)
    )

    # 3. Outlook HTML Email Preview
    html = render_html_email_preview(form, summary)
    (bf_dir / f"{po_num}_email_preview.html").write_text(html)



def choose_folder(folder: Optional[str]) -> Optional[str]:
    """Work out which folder to read, and say so plainly when it is empty.

    Reporting "0 document(s) processed" for a folder that contains no PDFs
    is technically true and completely unhelpful: it reads like the tool
    failed. The usual cause is running with no arguments, which used to
    silently default to `fixtures` while the real POs sat in `my_pos`.
    """
    candidates = [folder] if folder else ["my_pos", "fixtures"]

    for name in candidates:
        path = Path(name)
        if path.is_dir() and any(path.glob("*.pdf")):
            n = len(list(path.glob("*.pdf")))
            if not folder:
                print(f"Reading {n} PDF(s) from '{name}/'. "
                      f"Use --folder to choose a different one.\n")
            return name

    # Nothing usable. Explain what we looked at and what to do about it.
    print("No PDF files found.\n")
    for name in candidates:
        path = Path(name)
        if not path.is_dir():
            print(f"  '{name}/' does not exist")
        else:
            others = [f.name for f in path.iterdir() if f.is_file()][:4]
            extra = f" (contains: {', '.join(others)})" if others else " (empty)"
            print(f"  '{name}/' has no .pdf files{extra}")

    here = Path(".")
    elsewhere = sorted(
        {f.parent.name or "." for f in here.glob("*/*.pdf")}
        | ({"."} if any(here.glob("*.pdf")) else set()))
    print()
    if elsewhere:
        print("PDFs were found in: " + ", ".join(f"{d}/" for d in elsewhere))
        print(f"Try:  python3 run_local.py --folder {elsewhere[0]} --verbose")
    else:
        print("Put your purchase order PDFs in the 'my_pos' folder, then run:")
        print("      python3 run_local.py --folder my_pos --verbose")
        print("\nOr create four practice PDFs first:")
        print("      python3 tests/make_fixtures.py")
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PO validation, local mode")
    ap.add_argument("--folder", default=None,
                    help="folder of PO PDFs (default: my_pos if it has PDFs, "
                         "otherwise fixtures)")
    ap.add_argument("--checklist", default="sos", help="sos | renewals")
    ap.add_argument("--quotes", default=None,
                    help="JSON file of quotes to validate against. Omit it and "
                         "the quote checks are skipped rather than failed.")
    ap.add_argument("--ledger", default="out/ledger.jsonl")
    ap.add_argument("--results", default="out/results.jsonl")
    ap.add_argument("--rules-dir", default=str(HERE / "rules"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--reprocess", action="store_true",
                    help="ignore the ledger and reprocess everything")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--exceptions-only", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--no-booking-forms", action="store_true",
                    help="do not generate Salesforce booking form payloads and cards")
    ap.add_argument("--watch", action="store_true",
                    help="continuously watch --folder for incoming PO PDFs and process automatically")
    ap.add_argument("--poll-interval", type=float, default=2.0,
                    help="seconds between folder scans in watch mode (default: 2.0)")
    ap.add_argument("--processed-dir", default=None,
                    help="optional directory to move processed PDFs into")
    ap.add_argument("--failed-dir", default=None,
                    help="optional directory to move failed PDFs into")
    args = ap.parse_args(argv)

    args.folder = choose_folder(args.folder)
    if args.folder is None:
        return 2

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")

    ledger = NullLedger() if args.no_ledger else JsonLedger(args.ledger)
    engine = load_engine(args.checklist, ledger=ledger,
                         rules_dir=Path(args.rules_dir))

    router = ExceptionRouter(
        sinks=[ConsoleSink(verbose=args.verbose,
                           only_exceptions=args.exceptions_only),
               JsonlSink(args.results)],
        routing=engine.checklist.spec.get("routing", {}),
    )

    # No quote file means no quote source, which makes the seven
    # quote-dependent checks SKIP. That is the honest picture while
    # Snowflake is not connected. Pointing them at a stub containing one
    # fake quote instead made every real PO fail `quote_exists` and come
    # out REJECTED, which looks like a verdict on the PO and is not.
    quote_source = load_stub_quotes(Path(args.quotes)) if args.quotes else None
    if quote_source is None:
        quote_checks = {"quote_found", "opportunity_resolved", "quote_not_expired",
                        "party_matches_reference", "quote_status_bookable",
                        "currency_matches", "line_items_match",
                        "po_total_matches_quote"}
        n = sum(1 for r in engine.checklist.rules if r["check"] in quote_checks)
        print(f"No quote data supplied, so the {n} checks that compare the PO "
              f"against its quote are skipped.\nPass --quotes my_quotes.json "
              f"to use exported quote data.\n")

    pipeline = Pipeline(
        source=LocalFolderSource(args.folder),
        engine=engine,
        quote_source=quote_source,
        router=router,
        ledger=ledger,
        salesforce=None,          # no writes in local mode
    )

    bf_dir = Path("out/booking_forms")
    bf_dir.mkdir(parents=True, exist_ok=True)
    ai_client = F5AIClient()

    if args.watch:
        def on_new_result(res):
            print(f"\n⚡ New PO Processed: {res.po.source_id} -> {res.outcome.value}")
            if not args.no_booking_forms:
                write_booking_artifacts(res, bf_dir, ai_client)
                print(f"   📦 Generated Booking Form & Card in {bf_dir}/")

        print(f"🚀 Automatic Ingestion Watcher active on '{args.folder}' (poll_interval={args.poll_interval}s)")
        print("   Drop PDF files into this folder to process. Press Ctrl+C to exit.\n")
        watcher = FolderWatcher(
            watch_dir=args.folder,
            pipeline=pipeline,
            poll_interval=args.poll_interval,
            processed_dir=args.processed_dir,
            failed_dir=args.failed_dir,
            on_result=on_new_result,
        )
        watcher.start()
        return 0

    results = pipeline.run(limit=args.limit, reprocess=args.reprocess)
    print(render_summary(results))

    if not args.no_booking_forms and results:
        count = 0
        for res in results:
            write_booking_artifacts(res, bf_dir, ai_client)
            count += 1
        print(f"\n📦 Generated {count} Salesforce Booking Form payload(s) & Outlook Cards in {bf_dir}/")

    print(f"\nchecklist : {engine.checklist}")
    print(f"results   : {args.results}")
    print(f"ledger    : {args.ledger}")

    # Non-zero if anything needs a human, so this can gate a scheduled job.
    return 1 if any(r.outcome.value != "VALIDATED" for r in results) else 0



if __name__ == "__main__":
    sys.exit(main())
