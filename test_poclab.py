#!/usr/bin/env python3
"""Interactive test harness for F5 Salesforce Sandbox (poclab).

Usage:
    # 1. Run simulated dry-run against poclab (Safe, no credentials required):
    python3 test_poclab.py --dry-run

    # 2. Test live connection check to poclab:
    python3 test_poclab.py --check --token <your_salesforce_sandbox_token>

    # 3. Create a real test record and notes inside poclab:
    python3 test_poclab.py --live --token <your_salesforce_sandbox_token> --po PO706839
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from po_validation.act.salesforce import SalesforceClient, DEFAULT_SANDBOX_URL
from po_validation.booking.builder import BookingFormBuilder
from po_validation.ingest.sources import LocalFolderSource
from po_validation.models import Quote, QuoteLine
from po_validation.resolve.base import StubQuoteSource
from po_validation.validate.engine import load_engine
from po_validation.pipeline import Pipeline
from po_validation.report.writer import ExceptionRouter, Sink
from run_local import load_stub_quotes
from po_validation.ai.client import load_env_file

class NullSink(Sink):
    def emit(self, result):
        pass


def main():
    load_env_file()
    ap = argparse.ArgumentParser(description="Test harness for F5 Salesforce Sandbox")
    default_url = os.environ.get("SF_INSTANCE_URL", "https://f5--poclab.sandbox.my.salesforce.com")
    default_token = os.environ.get("SF_ACCESS_TOKEN")
    default_username = os.environ.get("SF_USERNAME", "")
    default_security_token = os.environ.get("SF_SECURITY_TOKEN", "")

    ap.add_argument("--url", default=default_url, help="Sandbox instance URL")
    ap.add_argument("--token", default=default_token, help="Salesforce Sandbox OAuth Bearer Token (Session ID, starts with 00D...)")
    ap.add_argument("--username", default=default_username, help="Salesforce username")
    ap.add_argument("--password", default=os.environ.get("SF_PASSWORD"), help="Salesforce password (or prompt interactively)")
    ap.add_argument("--security-token", default=default_security_token, help="Salesforce Security Token")
    ap.add_argument("--check", action="store_true", help="Run pre-flight API access check on sandbox")
    ap.add_argument("--live", action="store_true", help="Perform real write to sandbox (creates test record)")
    ap.add_argument("--dry-run", action="store_true", help="Run in safe simulation mode (default)")
    ap.add_argument("--po", default=None, help="Specific PO to test (e.g. PO706839, 25163692, 4501706557)")
    args = ap.parse_args()

    print("=" * 70)
    print(" 🧪 F5 Salesforce Sandbox Test Harness")
    print(f" Target Instance: {args.url}")
    print("=" * 70)

    # Prompt for password if needed
    password = args.password
    if (args.check or args.live) and not args.token and not password:
        import getpass
        print(f"\n🔑 Salesforce Authentication for user: {args.username}")
        password = getpass.getpass("Enter Sandbox Password (hidden): ")

    # Authenticate client
    client = None
    if password:
        sec_tok = args.security_token or ""
        print(f"\n🔐 Authenticating via Partner Login for {args.username}...")
        try:
            client = SalesforceClient.from_password(
                username=args.username,
                password=password,
                security_token=sec_tok,
                login_url=args.url,
                dry_run=not args.live,
            )
            print("✅ Successfully authenticated with Salesforce!")
        except Exception as exc:
            print(f"❌ Login failed: {exc}")
            return 1
    tokens = [t.strip() for t in (args.token or "").split(",") if t.strip()]
    if not tokens:
        tokens = ["SIMULATED_TOKEN"]

    # 1. Preflight check
    if args.check:
        print(f"\n🔍 Testing {len(tokens)} candidate token(s) against sandbox...")
        for idx, tok in enumerate(tokens, 1):
            if len(tokens) > 1:
                print(f"\n--- Checking Token #{idx} (prefix: {tok[:25]}...) ---")
            cand_client = SalesforceClient(instance_url=args.url, access_token=tok, dry_run=False)
            res = cand_client.check()
            if res.get("ok"):
                print(f"\n🎉 Token #{idx} SUCCEEDED!")
                print(json.dumps(res, indent=2))
                # Persist the working token to .env
                try:
                    env_p = Path(".env")
                    if env_p.exists():
                        txt = env_p.read_text("utf-8")
                        lines = [f"SF_ACCESS_TOKEN={tok}" if l.startswith("SF_ACCESS_TOKEN=") else l
                                 for l in txt.splitlines()]
                        env_p.write_text("\n".join(lines) + "\n", "utf-8")
                        print("\n💾 Saved working token to .env!")
                except Exception:
                    pass
                return 0
            else:
                print(f"❌ Token #{idx} rejected: {res.get('detail')}")
        print("\n❌ All candidate tokens were rejected by Salesforce.")
        return 1

    active_token = tokens[0]
    client = SalesforceClient(instance_url=args.url, access_token=active_token, dry_run=not args.live)

    # 2. Pipeline processing
    dry_run = not args.live
    mode_label = "LIVE WRITE TO SANDBOX" if not dry_run else "DRY-RUN SIMULATION (Safe)"
    print(f"\nMode: {mode_label}")
    if dry_run:
        print("ℹ️  No changes will be written to Salesforce. Use --live to write.")

    engine = load_engine("sos")
    quotes = load_stub_quotes(Path("my_quotes.json"))
    router = ExceptionRouter(sinks=[NullSink()])
    pipeline = Pipeline(
        source=LocalFolderSource("my_pos"),
        engine=engine,
        quote_source=quotes,
        router=router,
        salesforce=client,
    )

    print("\n⏳ Validating POs and generating Booking Forms for poclab...")
    results = pipeline.run()

    target_results = results
    if args.po:
        target_results = [r for r in results if args.po in (r.po.po_number or "") or args.po in (r.po.source_id or "")]
        if not target_results:
            print(f"❌ PO '{args.po}' not found in results.")
            return 1

    # For live writes, verify Opportunity ID exists in sandbox and satisfies validation rules
    if not dry_run:
        # Hard Production Safety Guard: REFUSE to run live tests against production
        try:
            org_rows = client.query("SELECT Id, Name, IsSandbox FROM Organization LIMIT 1")
            if not org_rows or not org_rows[0].get("IsSandbox"):
                print("\n🛑 SAFETY CRITICAL: Connected Salesforce instance is NOT a Sandbox!")
                print("   test_poclab.py is a test harness and is strictly prohibited from running against Production.")
                print("   Aborting immediately with no changes made.\n")
                return 1
            print(f"🔒 Sandbox guard verified: Connected to '{org_rows[0].get('Name')}' (IsSandbox=True)")
        except Exception as exc:
            print(f"\n🛑 SAFETY CRITICAL: Could not verify Organization.IsSandbox: {exc}")
            print("   Aborting live execution for safety.\n")
            return 1

        from decimal import Decimal
        from po_validation.models import Quote

        for res in target_results:
            cand_name = None
            if res.quote and res.quote.account_name:
                cand_name = res.quote.account_name
            elif res.po.party("ship_to"):
                cand_name = res.po.party("ship_to").get("name")
            elif res.po.party("bill_to"):
                cand_name = res.po.party("bill_to").get("name")

            real_opp_id = None
            # 1. Prefer an Opportunity matching the customer account or PO number
            if cand_name:
                cand_clean = cand_name.split()[0].replace("'", "\\'")
                try:
                    opp_matches = client.query(f"SELECT Id, Name, Account.Name FROM Opportunity WHERE Name LIKE '%{cand_clean}%' OR Account.Name LIKE '%{cand_clean}%' LIMIT 1")
                    if opp_matches:
                        real_opp_id = opp_matches[0].get("Id")
                except Exception:
                    pass

            if not real_opp_id and res.po.po_number:
                try:
                    opp_matches = client.query(f"SELECT Id, Name FROM Opportunity WHERE PO_to_F5__c = '{res.po.po_number}' LIMIT 1")
                    if opp_matches:
                        real_opp_id = opp_matches[0].get("Id")
                except Exception:
                    pass

            # 2. Fallback: check existing Booking Forms in sandbox
            if not real_opp_id:
                try:
                    bf_rows = client.query("SELECT Opportunity__c FROM Booking_Form__c WHERE Opportunity__c != null ORDER BY CreatedDate DESC LIMIT 5")
                    for row in bf_rows:
                        cand = row.get("Opportunity__c")
                        if cand:
                            real_opp_id = cand
                            break
                except Exception:
                    pass

            # 3. Fallback: any active Opportunity
            if not real_opp_id:
                try:
                    opp_rows = client.query("SELECT Id, Name FROM Opportunity WHERE IsClosed = false LIMIT 1")
                    if not opp_rows:
                        opp_rows = client.query("SELECT Id, Name FROM Opportunity LIMIT 1")
                    if opp_rows:
                        real_opp_id = opp_rows[0].get("Id")
                except Exception:
                    pass

            # 4. Adapt Opportunity fields to match this specific PO
            if real_opp_id:
                try:
                    d_opp = client.describe("Opportunity")
                    update_payload = {}
                    po_num = res.po.po_number or "UNKNOWN"
                    po_amt = float(res.quote.total if (res.quote and res.quote.total) else (res.po.po_total or Decimal("0.0")))
                    for f in d_opp.get("fields", []):
                        if not f.get("updateable"):
                            continue
                        fname = f.get("name")
                        flabel = (f.get("label") or "").lower()
                        f_lower = fname.lower()
                        if "sales order type" in flabel or "sales_order_type" in f_lower or f_lower == "order_type__c":
                            p_vals = [pv["value"] for pv in f.get("picklistValues", []) if pv.get("active")]
                            update_payload[fname] = "Standard" if "Standard" in p_vals else (p_vals[0] if p_vals else "Standard")
                        elif "po#" in flabel or "po_to_f5" in f_lower or "po # to f5" in flabel or "customer_po" in f_lower:
                            update_payload[fname] = po_num
                        elif f_lower == "amount":
                            update_payload[fname] = po_amt

                    if update_payload:
                        client.update("Opportunity", real_opp_id, update_payload)
                except Exception:
                    pass

                if res.quote:
                    res.quote.opportunity_id = real_opp_id
                else:
                    res.quote = Quote(
                        quote_number=res.po.quote_number or "UNKNOWN_QUOTE",
                        total=res.po.po_total or Decimal("0"),
                        opportunity_id=real_opp_id,
                        is_final=True
                    )

    for res in target_results:
        po_num = res.po.po_number or "UNKNOWN"
        action = client.create_booking_form(res)
        print("-" * 70)
        print(f"📄 PO #{po_num} ({Path(res.po.source_id).name if res.po.source_id else 'PO'})")
        print(f"   Outcome          : {res.outcome.value}")
        print(f"   Opportunity      : {res.quote.opportunity_id if res.quote else 'N/A'}")
        
        if "payload" in action:
            p = action["payload"]
            print(f"   Booking Form Name: {p.get('Name')}")
            print(f"   Booked Amount    : {p.get('CurrencyIsoCode')} ${p.get('Amount__c', 0):,.2f}")
            print(f"   Order Type       : {p.get('Sales_Order_Type__c')}")
            print(f"   Distributor      : {p.get('Distributor__c')}")
            print(f"   Reseller         : {p.get('Reseller_Name__c')}")
            print(f"   Account          : {p.get('Account_Name__c')}")

        if dry_run:
            print(f"   Status           : 🛡️ SIMULATED (dry-run)")
            print(f"   Target URL       : {action.get('simulated_url')}")
            print(f"   Notes Prepared   : {action.get('notes_count', 0)} Note(s) to RO")
        else:
            if action.get("success"):
                if action.get("idempotent_updated"):
                    print(f"   Status           : 🔁 UPDATED EXISTING RECORD (Idempotency)")
                else:
                    print(f"   Status           : ✅ CREATED IN POCLAB!")
                print(f"   Record ID        : {action.get('booking_form_id')}")
                print(f"   Lightning View   : {action.get('url')}")
                print(f"   Notes Attached   : {action.get('notes_attached')} Note(s) to RO")
            else:
                print(f"   Status           : ❌ ERROR ({action.get('status_code')})")
                if action.get("reason"):
                    print(f"   Reason           : {action.get('reason')}")
                if action.get("errors"):
                    print(f"   Errors           : {json.dumps(action.get('errors'), indent=2)}")
                if action.get("raw_response"):
                    print(f"   Raw Response     : {action.get('raw_response')}")

    print("-" * 70)
    print(f"\n🎉 Completed test for {len(target_results)} purchase order(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
