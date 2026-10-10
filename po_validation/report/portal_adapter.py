"""Portal Adapter: transforms Pipeline ValidationResult into UI Portal Order format."""

from __future__ import annotations

import base64
from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path
from typing import Any, Optional

from ..models import Outcome, Severity, Status, ValidationResult
from ..booking.builder import BookingFormBuilder


def format_money(val: Any) -> str:
    if val is None:
        return "$0.00"
    try:
        f = float(val)
        return f"${f:,.2f}"
    except Exception:
        return str(val)


def result_to_portal_order(
    result: ValidationResult,
    doc_data: Optional[bytes] = None,
    doc_filename: Optional[str] = None,
    source_tag: str = "Live Inbound",
) -> dict[str, Any]:
    po = result.po
    q = result.quote
    po_num = po.po_number or f"PO-{result.run_id[:8]}"
    clean_id = f"live_{po_num}".replace(" ", "_")

    # Determine vendor / from
    reseller_party = po.party("reseller")
    bill_to_party = po.party("bill_to")
    ship_to_party = po.party("ship_to")
    end_user_party = po.party("end_user")

    from_name = po.reseller_name or (reseller_party or {}).get("name") or (bill_to_party or {}).get("name") or (ship_to_party or {}).get("name") or "Unknown Customer"

    # Channel classification
    channel = "Reseller Partner"
    if reseller_party:
        channel = "Distributor" if any(w in reseller_party.get("name", "").lower() for w in ("ingram", "synnex", "arrow", "tech data")) else "Reseller Partner"
    elif bill_to_party and any(w in bill_to_party.get("name", "").lower() for w in ("ingram", "synnex", "arrow", "tech data")):
        channel = "Distributor"
    elif not reseller_party:
        channel = "Direct Customer"

    # Filename & PDF data
    filename = doc_filename or (Path(po.source_id).name if po.source_id else f"{po_num}.pdf")
    pdf_b64 = ""
    if doc_data:
        pdf_b64 = base64.b64encode(doc_data).decode("utf-8")
    elif po.source_id and os.path.exists(po.source_id):
        try:
            pdf_b64 = base64.b64encode(Path(po.source_id).read_bytes()).decode("utf-8")
        except Exception:
            pass

    # Status mapping
    if result.outcome == Outcome.VALIDATED:
        status_key = "ready"
    elif result.outcome in (Outcome.NEEDS_REVIEW, Outcome.DEFERRED):
        status_key = "review"
    else:
        status_key = "return"

    # Amy's 11 SOS Checklist Items
    # Rule evaluation mapping
    def rule_stat(rule_id: str) -> tuple[str, str]:
        for f in result.findings:
            if f.rule_id == rule_id:
                if f.status == Status.PASS:
                    return "pass", f.message or "Passed"
                elif f.status == Status.FAIL:
                    st = "fail" if f.severity in (Severity.BLOCKER, Severity.MAJOR) else "warn"
                    return st, f.message
                elif f.status == Status.WARN:
                    return "warn", f.message
                elif f.status == Status.SKIP:
                    return "pass", f.message or "N/A"
        return "pass", "Verified"

    f5_st, f5_msg = rule_stat("f5_entity")
    po_st, po_msg = rule_stat("po_number_present")
    pay_st, pay_msg = rule_stat("payment_terms_present")
    bt_st, bt_msg = rule_stat("bill_to_present")
    st_st, st_msg = rule_stat("ship_to_present")
    eu_st, eu_msg = rule_stat("end_user_present")
    res_st, res_msg = rule_stat("reseller_present")
    inco_st, inco_msg = rule_stat("inco_terms")
    prod_st, prod_msg = rule_stat("line_arithmetic")
    tot_st, tot_msg = rule_stat("parser_self_check")
    ship_st, ship_msg = rule_stat("shipping_details_present")

    checklist = [
        [f5_st, "1. F5 Entity", f5_msg],
        [po_st, "2. PO Number", f"PO# {po_num}" if po_st == "pass" else po_msg],
        [pay_st, "3. Payment Terms", f"{po.payment_terms or 'Terms present'}" if pay_st == "pass" else pay_msg],
        [bt_st, "4. Bill To", f"{(bill_to_party or {}).get('name') or 'Bill To entity verified'}" if bt_st == "pass" else bt_msg],
        [st_st, "5. Ship To", f"{(ship_to_party or {}).get('name') or 'Physical address verified'}" if st_st == "pass" else st_msg],
        [eu_st, "6. End User", f"{(end_user_party or {}).get('name') or 'End User entity verified'}" if eu_st == "pass" else eu_msg],
        [res_st, "7. Reseller", f"{(reseller_party or {}).get('name') or 'Direct / Reseller verified'}" if res_st == "pass" else res_msg],
        [inco_st, "8. Inco Terms", f"{po.inco_terms or 'Standard terms'}" if inco_st == "pass" else inco_msg],
        [prod_st, "9. Product Information", f"{len(po.line_items)} lines with SKU & pricing" if prod_st == "pass" else prod_msg],
        [tot_st, "10. PO Total", f"{format_money(po.po_total or po.computed_total)} (reconciled)" if tot_st == "pass" else tot_msg],
        [ship_st, "11. Shipping Details", "Carrier and logistics verified" if ship_st == "pass" else ship_msg],
    ]

    # F5 CPQ / Snowflake Quote Checks
    quote_checks = [
        ["pass" if (q and q.found) else "fail", "CPQ Quote Exists", f"{q.quote_number} active in CPQ" if (q and q.found) else f"Quote {po.quote_number or 'NOT CITED'} not found"],
        ["pass" if (q and q.opportunity_id) else "fail", "Opportunity Resolved", f"Opp {q.opportunity_id}" if (q and q.opportunity_id) else "No opportunity linked"],
        [rule_stat("quote_not_expired")[0], "Quote Not Expired", rule_stat("quote_not_expired")[1]],
        [rule_stat("bill_to_matches_account")[0], "Bill-To Matches Account", rule_stat("bill_to_matches_account")[1]],
        [rule_stat("end_user_matches_quote")[0], "End User Matches Quote", rule_stat("end_user_matches_quote")[1]],
        [rule_stat("currency_match")[0], "Currency ISO Match", rule_stat("currency_match")[1]],
        [rule_stat("line_items_match")[0], "Line Items & Pricing Match", rule_stat("line_items_match")[1]],
        [rule_stat("order_total_match")[0], "Order Total Reconciliation", rule_stat("order_total_match")[1]],
        [rule_stat("duplicate_po")[0], "Duplicate PO Check", rule_stat("duplicate_po")[1]],
    ]

    # Flags / Review exceptions
    flags = []
    for idx, f in enumerate(result.findings):
        if f.status in (Status.FAIL, Status.WARN):
            flags.append({
                "id": f"flag_{f.rule_id}_{idx}",
                "severity": f.severity.value,
                "rule": f.rule_id,
                "title": f.rule_id.replace("_", " ").title(),
                "detail": f.message,
                "suggested": "Review discrepancy with partner or submit exception note.",
            })

    # Header comparison table
    po_tot_flt = float(po.po_total or po.computed_total or Decimal("0.0"))
    q_tot_flt = float(q.total) if (q and q.total is not None) else None
    tot_match = "yes" if (q_tot_flt is not None and abs(po_tot_flt - q_tot_flt) <= 1.0) else "no"

    compare = [
        ["CPQ Quote Reference", po.quote_number or "(none)", (q.quote_number if q else "(none)"), "yes" if (q and q.quote_number == po.quote_number) else "no"],
        ["Opportunity Target", (q.opportunity_id if q else "-"), (q.opportunity_id if q else "-"), "yes" if (q and q.opportunity_id) else "no"],
        ["Currency ISO", po.currency or "USD", (q.currency if q else "USD") or "USD", "yes" if (not q or not q.currency or q.currency == (po.currency or "USD")) else "no"],
        ["Net Bookable Total", po_tot_flt, q_tot_flt or 0.0, tot_match],
    ]

    # Lines table
    lines = []
    q_lines = {ql.part_number: ql for ql in (q.lines if q else []) if ql.part_number}
    for li in po.line_items:
        p_num = li.part_number or "(no SKU)"
        p_tot = float(li.total_price) if li.total_price is not None else 0.0
        q_match = q_lines.get(p_num)
        sf_tot = float(q_match.total_price) if q_match and q_match.total_price is not None else None
        m_status = "yes" if (sf_tot is not None and abs(p_tot - sf_tot) <= 0.05) else ("diff" if sf_tot is not None else "no")
        lines.append({
            "poSku": p_num,
            "desc": li.description or p_num,
            "qty": int(li.quantity or 1),
            "total": p_tot,
            "sfSku": q_match.part_number if q_match else "-",
            "sfTotal": sf_tot or 0.0,
            "match": m_status,
        })

    # Booking Form Draft & Notes to RO
    try:
        bf = BookingFormBuilder.build(result)
        booking = {
            "opportunity": bf.opportunity_id or (q.opportunity_id if q else ""),
            "amount": float(bf.amount or po_tot_flt),
            "orderType": bf.sales_order_type or "Standard",
            "distributor": (reseller_party or {}).get("name") or from_name,
            "reseller": from_name,
            "issues": len(result.blockers) > 0,
        }
        notes = [{"title": n.title, "body": n.body} for n in bf.notes]
        sfdc_payload = bf.to_salesforce_payload()
    except Exception as exc:
        booking = {
            "opportunity": q.opportunity_id if q else "",
            "amount": po_tot_flt,
            "orderType": "Standard",
            "distributor": from_name,
            "reseller": from_name,
            "issues": len(result.blockers) > 0,
        }
        notes = []
        sfdc_payload = {
            "Opportunity__c": booking["opportunity"],
            "PO__c": po_num,
            "Total_Amount__c": po_tot_flt,
            "Sales_Order_Type__c": "Standard",
        }

    # Summary string
    pass_cnt = sum(1 for c in checklist if c[0] == "pass")
    if pass_cnt == 11 and result.outcome == Outcome.VALIDATED:
        summary_str = f"All 11 SOS checklist items passed cleanly. Quote {po.quote_number or (q.quote_number if q else '')} matched 100%."
    else:
        summary_str = f"{pass_cnt} of 11 SOS checklist items passed ({11 - pass_cnt} flagged). Pipeline outcome: {result.outcome.value}."

    return {
        "id": clean_id,
        "po": po_num,
        "from": from_name,
        "channel": channel,
        "file": filename,
        "received": datetime.now().strftime("%I:%M %p"),
        "poDate": str(po.po_date or datetime.now().strftime("%b %d, %Y")),
        "quote": po.quote_number or (q.quote_number if q else ""),
        "opportunity": booking["opportunity"],
        "status": status_key,
        "summary": summary_str,
        "checklist": checklist,
        "quoteChecks": quote_checks,
        "flags": flags,
        "compare": compare,
        "lines": lines,
        "booking": booking,
        "notes": notes,
        "salesforce_payload": sfdc_payload,
        "pdf_b64": pdf_b64,
        "live": True,
        "source_tag": source_tag,
        "checklist_total": 11,
    }
