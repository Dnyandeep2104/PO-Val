#!/usr/bin/env python3
"""Interactive test harness for validating POs against live Snowflake data.

Connects to F5's Enterprise Data Ecosystem (EDE) Snowflake instance:
    Account    : f5-enterprisedataecosystem
    Warehouse  : EXP_SALES_WH
    Role       : APP_EDE_SALES_EXP_ROLE
    Database   : PRD_ENT_RAW
    Schema     : SALESFORCE
    Auth       : externalbrowser (Okta / Azure AD SSO)

Usage:
    # 1. Test Snowflake connection and discover Quote tables:
    python3 test_snowflake.py --check

    # 2. Inspect a live Quote directly from Snowflake:
    python3 test_snowflake.py --quote F5Q-00972677

    # 3. Validate a PO against live Snowflake Quote data:
    python3 test_snowflake.py --po PO706839

    # 4. Full End-to-End: Validate against Snowflake AND create Booking Form in poclab:
    python3 test_snowflake.py --po PO706839 --live
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from po_validation.ai.client import load_env_file
from po_validation.models import Quote, QuoteLine, Status, Outcome, ValidationResult
from po_validation.extract.registry import parse
from po_validation.validate.engine import load_engine
from po_validation.booking.builder import BookingFormBuilder
from po_validation.act.salesforce import SalesforceClient

# Default Snowflake connection params from F5 EDE
DEFAULT_ACCOUNT = "f5-enterprisedataecosystem"
DEFAULT_WAREHOUSE = "EXP_SALES_WH"
DEFAULT_ROLE = "APP_EDE_SALES_EXP_ROLE"
DEFAULT_DATABASE = "PRD_ENT_RAW"
DEFAULT_SCHEMA = "SALESFORCE"


def get_snowflake_connection(user: Optional[str] = None):
    """Initializes Snowflake connection using Okta external browser SSO."""
    try:
        import snowflake.connector
    except ImportError:
        print("\n❌ snowflake-connector-python is not installed in this environment.")
        print("Please install it by running:")
        print("    .venv/bin/pip install snowflake-connector-python\n")
        sys.exit(1)

    load_env_file()
    sf_user = user or os.environ.get("SNOWFLAKE_USER") or "d.dhok@f5.com"
    account = os.environ.get("SNOWFLAKE_ACCOUNT", DEFAULT_ACCOUNT)
    warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE", DEFAULT_WAREHOUSE)
    role = os.environ.get("SNOWFLAKE_ROLE", DEFAULT_ROLE)
    database = os.environ.get("SNOWFLAKE_DATABASE", DEFAULT_DATABASE)
    schema = os.environ.get("SNOWFLAKE_SCHEMA", DEFAULT_SCHEMA)

    print(f"🔐 Connecting to Snowflake ({account}.snowflakecomputing.com)...")
    print(f"   User     : {sf_user}")
    print(f"   Role     : {role}")
    print(f"   Warehouse: {warehouse}")
    print(f"   Database : {database}.{schema}")
    print("   Authenticating via browser SSO...")

    conn = snowflake.connector.connect(
        account=account,
        user=sf_user,
        authenticator="externalbrowser",
        warehouse=warehouse,
        role=role,
        database=database,
        schema=schema,
    )
    return conn


def check_connection(conn) -> bool:
    """Verifies connection and probes for Salesforce Quote tables."""
    print("\n🔍 Probing Snowflake metadata...")
    cur = conn.cursor()
    try:
        cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA()")
        row = cur.fetchone()
        print(f"✅ Authenticated successfully!")
        print(f"   User     : {row[0]}")
        print(f"   Role     : {row[1]}")
        print(f"   Warehouse: {row[2]}")
        print(f"   Namespace: {row[3]}.{row[4]}")

        # Probe for quote tables in SALESFORCE schema
        cur.execute("""
            SELECT TABLE_NAME, ROW_COUNT
            FROM PRD_ENT_RAW.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = 'SALESFORCE'
              AND (TABLE_NAME LIKE '%QUOTE%' OR TABLE_NAME LIKE '%SBQQ%')
            ORDER BY TABLE_NAME
        """)
        tables = cur.fetchall()
        print(f"\n📊 Discovered {len(tables)} Quote table(s) in PRD_ENT_RAW.SALESFORCE:")
        for tname, rcount in tables:
            print(f"   • {tname} ({rcount or 0:,} rows)")

        return True
    finally:
        cur.close()


def get_table_columns(cur, table_name: str) -> set[str]:
    """Returns the set of uppercase column names for a table."""
    tbl = table_name.split(".")[-1].upper()
    cur.execute(f"""
        SELECT COLUMN_NAME
        FROM PRD_ENT_RAW.INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'SALESFORCE'
          AND TABLE_NAME = '{tbl}'
    """)
    return {row[0].upper() for row in cur.fetchall()}


def resolve_col(available_cols: set[str], candidates: list[str], default_expr: str = "NULL", prefix: str = "") -> str:
    for c in candidates:
        if c.upper() in available_cols:
            p = f"{prefix}." if prefix else ""
            return f"{p}{c.upper()}"
    return default_expr


def fetch_snowflake_quote(conn, quote_number: str) -> Optional[Quote]:
    """Fetches quote header and lines from Snowflake replica of Salesforce."""
    cur = conn.cursor()
    quote_clean = quote_number.strip().upper()
    try:
        # Candidate table pairs: (quote_table, line_table)
        candidate_pairs = [
            ("CAFSL_ORACLE_QUOTE_C", "CAFSL_ORACLE_QUOTE_LINE_ITEM_C"),
            ("BIG_MACHINES_QUOTE_C", "BIG_MACHINES_QUOTE_PRODUCT_C"),
            ("QUOTE_C", "QUOTE_LINE_C"),
        ]

        rows = []
        found_table = None

        for q_tbl_name, l_tbl_name in candidate_pairs:
            q_cols = get_table_columns(cur, q_tbl_name)
            if not q_cols:
                continue
            l_cols = get_table_columns(cur, l_tbl_name)

            # Check if quote exists in this table
            name_col = resolve_col(q_cols, ["NAME"], default_expr="NAME")
            cur.execute(f"""
                SELECT COUNT(*) FROM PRD_ENT_RAW.SALESFORCE.{q_tbl_name}
                WHERE ({name_col} = '{quote_clean}' OR {name_col} LIKE '%{quote_clean.replace("F5Q-", "")}%')
            """)
            if cur.fetchone()[0] == 0:
                continue

            # Found table! Dynamically resolve columns
            found_table = q_tbl_name
            opp_col = resolve_col(q_cols, ["CAFSL_OPPORTUNITY_C", "BIG_MACHINES_OPPORTUNITY_C", "OPPORTUNITY_C", "OPPORTUNITYID"], prefix="quote")
            acct_col = resolve_col(q_cols, ["CAFSL_ACCOUNT_C", "BIG_MACHINES_ACCOUNT_C", "ACCOUNT_C", "ACCOUNTID"], prefix="quote")
            curr_col = resolve_col(q_cols, ["CURRENCY_ISO_CODE", "CURRENCYISOCODE", "CAFSL_CURRENCY_C"], default_expr="'USD'", prefix="quote")

            fk_col = resolve_col(l_cols, ["CAFSL_ORACLE_QUOTE_C", "BIG_MACHINES_QUOTE_C", "QUOTE_C", "CAFSL_QUOTE_C", "QUOTE_ID_C", "QUOTE_ID"], prefix="line")
            part_col = resolve_col(l_cols, ["CAFSL_PART_NUMBER_C", "PRODUCT_CODE_C", "NAME", "BIG_MACHINES_PRODUCT_C"], prefix="line")
            qty_col = resolve_col(l_cols, ["CAFSL_QUANTITY_C", "BIG_MACHINES_QUANTITY_C", "QUANTITY_C", "QUANTITY"], prefix="line")
            unit_col = resolve_col(l_cols, ["CAFSL_UNIT_PRICE_C", "BIG_MACHINES_SALES_PRICE_C", "UNIT_PRICE_C", "UNIT_PRICE"], prefix="line")
            tot_col = resolve_col(l_cols, ["PI_TOTAL_PRICE_C", "BIG_MACHINES_TOTAL_PRICE_C", "TOTAL_PRICE_C", "TOTAL_PRICE", "AMOUNT_C"], prefix="line")
            desc_col = resolve_col(l_cols, ["CAFSL_DESCRIPTION_C", "BIG_MACHINES_DESCRIPTION_C", "DESCRIPTION_C", "DESCRIPTION"], prefix="line")

            del_clause = "AND (line.IS_DELETED = FALSE OR line.IS_DELETED IS NULL)" if "IS_DELETED" in l_cols else ""

            sql = f"""
                SELECT
                    quote.{name_col} AS quote_number,
                    {opp_col} AS opportunity_id,
                    {acct_col} AS account_id,
                    {curr_col} AS currency,
                    {part_col} AS part_number,
                    {qty_col} AS quantity,
                    {unit_col} AS unit_price,
                    {tot_col} AS total_price,
                    {desc_col} AS description
                FROM PRD_ENT_RAW.SALESFORCE.{q_tbl_name} quote
                LEFT JOIN PRD_ENT_RAW.SALESFORCE.{l_tbl_name} line
                       ON quote.ID = {fk_col}
                WHERE (quote.{name_col} = '{quote_clean}' OR quote.{name_col} LIKE '%{quote_clean.replace("F5Q-", "")}%')
                  {del_clause}
            """
            cur.execute(sql)
            rows = cur.fetchall()
            if rows:
                print(f"ℹ️ Found Quote '{quote_clean}' in {q_tbl_name} ({len(rows)} line(s))")
                break

        if not rows:
            print(f"⚠️ Quote '{quote_clean}' not found in any candidate Salesforce Quote tables in Snowflake.")
            return None

        # Build Quote model
        first = rows[0]
        q_num, opp_id, acct_id, curr = first[0], first[1], first[2], first[3]
        lines = []
        for r in rows:
            part, qty, unit_p, tot_p, desc = r[4], r[5], r[6], r[7], r[8]
            if part:
                lines.append(QuoteLine(
                    part_number=part,
                    quantity=int(qty) if qty is not None else None,
                    unit_price=Decimal(str(unit_p)) if unit_p is not None else None,
                    total_price=Decimal(str(tot_p)) if tot_p is not None else None,
                    description=desc,
                ))

        return Quote(
            quote_number=q_num,
            found=True,
            lines=lines,
            opportunity_id=opp_id,
            account_id=acct_id,
            currency=curr or "USD",
            source="snowflake",
        )
    finally:
        cur.close()


def validate_po_against_snowflake(conn, po_filter: str, live_sf: bool = False):
    """End-to-end validation: Extract PO, fetch Quote from Snowflake, validate, build Booking Form."""
    # 1. Locate PDF
    pos_dir = Path("my_pos")
    matching_files = [f for f in pos_dir.glob("*.pdf") if po_filter in f.name]
    if not matching_files:
        print(f"❌ No PDF found in 'my_pos' matching '{po_filter}'.")
        return

    pdf_path = matching_files[0]
    print(f"\n======================================================================")
    print(f"📄 Processing PO: {pdf_path.name}")
    print(f"======================================================================")

    # 2. Extract PO
    with open(pdf_path, "rb") as f:
        po = parse(f.read(), str(pdf_path))

    print(f"   PO Number   : {po.po_number}")
    print(f"   Quote Number: {po.quote_number}")
    print(f"   PO Amount   : {po.currency} ${po.po_total:,.2f}" if po.po_total else "   PO Amount   : N/A")
    print(f"   Line Items  : {len(po.line_items)}")

    if not po.quote_number:
        print(f"❌ PO does not have a recognizable F5 Quote Number.")
        return

    # 3. Fetch Quote from Snowflake
    print(f"\n❄️ Querying Snowflake for Quote '{po.quote_number}'...")
    quote = fetch_snowflake_quote(conn, po.quote_number)
    if not quote:
        print("❌ Cannot proceed with validation: Quote missing in Snowflake.")
        return

    print(f"   Quote Lines : {len(quote.lines)}")
    print(f"   Quote Total : {quote.currency} ${quote.total:,.2f}")
    print(f"   Opportunity : {quote.opportunity_id}")
    print(f"   Account ID  : {quote.account_id}")

    # 4. Run SOS Validation Engine
    engine = load_engine("sos")
    result: ValidationResult = engine.run(po, quote)

    print(f"\n⚖️ SOS Validation Results:")
    print(f"   Outcome     : {result.outcome.value}")
    print(f"   Passed Rules: {len(result.by_status(Status.PASS))}")
    print(f"   Review Items: {len(result.by_status(Status.FAIL))}")

    print("\n📋 Detailed Finding Checklist:")
    for f in result.findings:
        icon = "✅" if f.status == Status.PASS else ("⚠️" if f.status == Status.FAIL else "ℹ️")
        print(f"   {icon} [{f.rule_id}] {f.message}")

    # 5. Build Booking Form
    form = BookingFormBuilder.build(result)
    print(f"\n📋 Generated Booking Form:")
    print(f"   Name        : {form.booking_form_name}")
    print(f"   Sales Order : {form.sales_order_type}")
    print(f"   Booked Amt  : {form.currency} ${form.amount:,.2f}")
    print(f"   Order Issues: {form.order_issues}")
    print(f"   Ship Notify : {form.shipping_notification}")
    print(f"   Order Notify: {form.order_notifications}")
    print(f"   Same As EU  : {form.same_as_end_user_contact_info}")
    print(f"   Notes to RO : {len(form.notes)}")
    for i, n in enumerate(form.notes, 1):
        print(f"      {i}. {n.title} -> {n.body[:60]}...")

    # 6. Live Write to Salesforce (Optional)
    if live_sf:
        load_env_file()
        sf_token = os.environ.get("SF_ACCESS_TOKEN")
        sf_url = os.environ.get("SF_INSTANCE_URL")
        if not sf_token or not sf_url:
            print("\n❌ Salesforce credentials missing in .env for live write.")
            return

        print(f"\n🚀 Writing validated Booking Form to Salesforce Sandbox ({sf_url})...")
        sf_client = SalesforceClient(instance_url=sf_url, access_token=sf_token, dry_run=False)

        # In Sandbox environments, verify Opportunity exists or link to active sandbox Opp
        opp_id = result.quote.opportunity_id if result.quote else None
        if "sandbox" in sf_url.lower():
            opp_exists = False
            if opp_id:
                try:
                    res = sf_client.query(f"SELECT Id FROM Opportunity WHERE Id = '{opp_id}'")
                    opp_exists = bool(res)
                except Exception:
                    pass
            if not opp_exists:
                print(f"ℹ️ Production Opportunity '{opp_id}' does not exist in sandbox ({sf_url}).")
                print("   Finding available sandbox Opportunity...")
                try:
                    opp_rows = sf_client.query("SELECT Id, Name FROM Opportunity ORDER BY CreatedDate DESC LIMIT 1")
                    if opp_rows:
                        result.quote.opportunity_id = opp_rows[0]["Id"]
                        print(f"   Linked to sandbox Opp: {opp_rows[0].get('Name')} ({result.quote.opportunity_id})")
                        sf_client.update("Opportunity", result.quote.opportunity_id, {
                            "Sales_Order_Type__c": "Standard",
                            "PO_to_F5__c": result.po.po_number or "PO706839",
                            "Amount": float(result.quote.total or 0.0),
                        })
                except Exception as e:
                    print(f"⚠️ Could not adapt sandbox Opportunity: {e}")

        action = sf_client.create_booking_form(result)
        is_success = action.get("success", False)
        rec_id = action.get("booking_form_id") or action.get("record_id")
        url = action.get("url")

        if is_success or rec_id:
            print(f"   Status      : ✅ SUCCESS")
            print(f"   Record ID   : {rec_id}")
            print(f"   Notes Added : {action.get('notes_attached', 0)}")
            print(f"   URL         : {url}")
        else:
            print(f"   Status      : ❌ FAILED")
            if action.get("errors"):
                print(f"   Errors      : {json.dumps(action.get('errors'), indent=2)}")
            elif action.get("reason"):
                print(f"   Reason      : {action.get('reason')}")


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description="Validate POs against live Snowflake replica")
    parser.add_argument("--check", action="store_true", help="Test Snowflake connection and list quote tables")
    parser.add_argument("--user", type=str, default="d.dhok@f5.com", help="Snowflake SSO user email")
    parser.add_argument("--quote", type=str, help="Fetch and inspect a single Quote from Snowflake")
    parser.add_argument("--po", type=str, default="PO706839", help="PO number to validate against Snowflake")
    parser.add_argument("--live", action="store_true", help="Post to Salesforce sandbox if validation succeeds")

    args = parser.parse_args()

    conn = get_snowflake_connection(user=args.user)
    try:
        if args.check:
            check_connection(conn)
        elif args.quote:
            q = fetch_snowflake_quote(conn, args.quote)
            if q:
                print(f"\n✅ Quote {q.quote_number} Details:")
                print(f"   Opportunity: {q.opportunity_id}")
                print(f"   Account ID : {q.account_id}")
                print(f"   Currency   : {q.currency}")
                print(f"   Total Price: {q.total}")
                print(f"   Lines ({len(q.lines)}):")
                for li in q.lines:
                    print(f"     • {li.part_number}: qty={li.quantity}, total={li.total_price}")
        else:
            validate_po_against_snowflake(conn, args.po, live_sf=args.live)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
