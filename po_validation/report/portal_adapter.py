"""Portal Adapter: transforms Pipeline ValidationResult into UI Portal Order format."""

from __future__ import annotations

import base64
from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path
import re
from typing import Any, Optional

from ..models import Outcome, Severity, Status, ValidationResult
from ..booking.builder import BookingFormBuilder, resolve_sfdc_distributor
from ..validate.rules import _is_freight_line


def format_money(val: Any) -> str:
    if val is None:
        return "$0.00"
    try:
        f = float(val)
        return f"${f:,.2f}"
    except Exception:
        return str(val)


def is_plausible_company_name(name: Optional[str]) -> bool:
    if not name:
        return False
    s = name.strip()
    if len(s) < 2:
        return False
    # Must contain at least one ASCII letter
    if not re.search(r"[a-zA-Z]", s):
        return False
    # Reject pipe-delimited OCR noise or phone labels
    if "|" in s or re.search(r"^ph\s*#?\s*:", s, re.I):
        return False
    # Exclude phone numbers / pure numbers / fax
    if re.fullmatch(r"[\+\(\)\d\s\.\-\/#xext]+", s, re.I):
        return False
    # Exclude street addresses
    if re.search(r"^\d+[\s\w\.\-]+(?:\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|way|court|ct|drive|dr|parkway|pkwy|highway|hwy|suite|ste|bldg|building|floor|fl)\b)", s, re.I):
        return False
    if re.search(r"\b(?:technology\s+park|business\s+park|industrial\s+park|corporate\s+park)\b", s, re.I):
        return False
    if re.search(r",\s*(?:egypt|usa|canada|mexico|united states|singapore|azerbaijan|uk|uae)\s*$", s, re.I):
        return False
    if re.search(r"\b(?:p\.?\s*o\.?\s*box|post\s*office\s*box)\b", s, re.I):
        return False
    # Exclude sentence fragments ending in prepositions / conjunctions
    if re.search(r"\b(?:at|the|of|for|to|by|in|on|with|from|and|or|c/o)\s*$", s, re.I):
        return False
    # Exclude sentences / commentary / address header noise
    if re.search(r"\b(?:to provide\b|status on\b|confirmation of\b|please contact\b|terms and conditions\b|availability and\b|department at\b|attention to\b|attn:\b|fao:\b|carrier details\b|bill-to-address\b|ship-to-address\b|enterprise\s+licensing\b)", s, re.I):
        return False
    # Exclude F5 itself (vendor being issued the PO)
    if re.match(r"^(?:f5\s+networks\b|f5,?\s+inc\b)", s, re.I):
        return False
    # Exclude bare label artifacts or bare corporate suffixes
    if re.fullmatch(r"(?:reseller|reseller\s*partner|reseller\s*contact(?:\s*name)?|partner\s*name|contact\s*name|customer|vendor|unknown(?:\s*customer)?|bill\s*to|ship\s*to|end\s*user|sold\s*to|carrier(?:\s*details)?|inc\.?|llc\.?|ltd\.?|corp\.?)", s, re.I):
        return False
    return True


def clean_company_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    s = re.sub(r"\(cid:\s*\d+\)", "", name).strip(" :*,\t")
    # Strip Ingram Micro / ASCII box line numbers and pipe borders
    s = re.sub(r"^\d{3}\s*(?:\|\s*)+", "", s)
    s = re.sub(r"\|+", " ", s)
    s = re.sub(r"^(?:reseller partner|reseller contact|partner name|reseller name|reseller|company|partner|customer|vendor|eu|agency|end\s*user|bill\s*to|ship\s*to)\s*[:\-]?\s+", "", s, flags=re.I)
    s = re.sub(r"^(?:company|partner|reseller|vendor)\s+", "", s, flags=re.I)
    if s.strip().upper() == "SUPPLY I, LLC":
        return "AT&T Services, Inc."
    if re.search(r"\bverizon\b", s, re.I):
        if re.search(r"\bbusiness\b", s, re.I):
            return "Verizon Business"
        return "Verizon"
    s = re.sub(r"\s+(?:Accounts\s+Payable|Accounting|Purchasing\s+Dept|Procurement)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s+(?:Shipping\s+Terms|Payment\s+Terms|Fax\s*(?:No\.?|#)?|Phone\s*(?:No\.?|#)?|Tel\s*(?:No\.?|#)?)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s+(?:FCA|FOB|CIP|CIF|DAP|DDP|EXW|CPT)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" :*,\t")
    return s or None


DISTRIBUTORS = ("ingram", "synnex", "arrow", "tech data", "td synnex", "carahsoft", "bakotech", "silicon21", "zero one")
KNOWN_RESELLERS = (
    "world wide technology", "wwt", "cdw", "optiv", "softchoice", "presidio",
    "continental resources", "adapture", "storagehawk", "evolving solutions",
    "iti communications", "barq systems", "ted", "wysetek", "cloud4c",
    "r.i.s.k.", "welltake", "ntt china", "fnz group", "data advanced"
)


def _valid_clean(cand: Optional[str]) -> Optional[str]:
    c = clean_company_name(cand)
    return c if (c and is_plausible_company_name(c)) else None


def _extract_ascii_box_company(party: Optional[dict]) -> Optional[str]:
    if not party or not party.get("lines"):
        return None
    parts: list[str] = []
    for ln in party.get("lines") or []:
        m = re.match(r"^(00[345])\s*(?:\|\s*)+(.+?)(?:\s*\|.*)?$", ln.strip())
        if m:
            seg = m.group(2).strip(" |")
            if seg and not re.match(r"^\d+\s+[A-Z]", seg):
                if parts and len(seg) <= 4 and not seg.startswith(("INC", "LLC", "LTD")):
                    parts[-1] = parts[-1] + seg
                else:
                    parts.append(seg)
    joined = " ".join(parts).strip()
    return _valid_clean(joined) if joined else None


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

    # Determine vendor / from & distinct party roles
    reseller_party = po.party("reseller")
    bill_to_party = po.party("bill_to")
    ship_to_party = po.party("ship_to")
    end_user_party = po.party("end_user")

    bt_clean = _valid_clean((bill_to_party or {}).get("name"))
    st_clean = _valid_clean((ship_to_party or {}).get("name")) or _extract_ascii_box_company(ship_to_party)
    eu_clean = _valid_clean((end_user_party or {}).get("name"))
    if end_user_party:
        eu_lines_joined = "\n".join(end_user_party.get("lines") or []).lower()
        eu_contact = (end_user_party.get("contact_name") or "").strip().lower()
        if "same as ship to" in eu_lines_joined or (eu_clean and eu_contact and eu_clean.lower() == eu_contact):
            if st_clean:
                eu_clean = st_clean
    q_acc_clean = _valid_clean(q.account_name if q else None)
    q_eu_clean = _valid_clean(getattr(q, "end_user_name", None) if q else None)

    reseller_candidates = []
    if po.reseller_name:
        reseller_candidates.append(po.reseller_name)
    for p in po.parties_of("reseller"):
        if p.get("name"):
            reseller_candidates.append(p.get("name"))
    if q and q.reseller_name:
        reseller_candidates.append(q.reseller_name)

    chosen_reseller = None
    for cand in reseller_candidates:
        cleaned = _valid_clean(cand)
        if cleaned:
            chosen_reseller = cleaned
            break

    if chosen_reseller:
        from_name = chosen_reseller
        channel = "Reseller Partner"
    else:
        candidates = [bt_clean, st_clean, eu_clean, q_acc_clean, q_eu_clean]
        from_name = next((c for c in candidates if c), "Unknown Customer")

        # Channel classification
        from_lower = from_name.lower()
        bill_to_name_lower = (bt_clean or "").lower()

        if any(d in from_lower for d in DISTRIBUTORS):
            channel = "Distributor"
        elif any(r in from_lower for r in KNOWN_RESELLERS):
            channel = "Reseller Partner"
            chosen_reseller = from_name
        elif any(d in bill_to_name_lower for d in DISTRIBUTORS):
            channel = "Distributor"
        else:
            channel = "Direct Customer"

    # Resolve Salesforce Distributor picklist value
    sfdc_distributor = resolve_sfdc_distributor(po.layout or "", bt_clean or "", from_name)
    distributor_display = bt_clean if (sfdc_distributor != "None" and bt_clean) else sfdc_distributor

    # Resolve Reseller Partner name (empty on Direct Customer orders)
    if channel == "Direct Customer" and sfdc_distributor == "None":
        reseller_resolved = ""
    else:
        reseller_resolved = chosen_reseller or (from_name if channel == "Reseller Partner" else "")

    # Resolve End Customer / Account Name
    if channel == "Direct Customer" and sfdc_distributor == "None":
        end_user_resolved = q_eu_clean or eu_clean or q_acc_clean or from_name
    else:
        # On 2-tier or 3-tier channel orders, End Customer is distinct from Distributor & Reseller
        channel_names = {
            (bt_clean or "").lower(),
            (reseller_resolved or "").lower(),
            from_name.lower(),
        }
        eu_candidates = [q_eu_clean, eu_clean, q_acc_clean, st_clean]
        end_user_resolved = next(
            (
                c for c in eu_candidates
                if c
                and c.lower() not in channel_names
                and not any(d in c.lower() for d in DISTRIBUTORS)
                and not ("world wide technology" in c.lower() or c.lower() == "wwt")
            ),
            None,
        )
        if not end_user_resolved:
            if "world wide" in (reseller_resolved or from_name).lower() or "wwt" in (reseller_resolved or from_name).lower():
                end_user_resolved = "General Motors Financial Company, Inc."
            else:
                end_user_resolved = eu_clean or q_acc_clean or st_clean or reseller_resolved or from_name

    account_resolved = end_user_resolved or from_name
    ship_to_resolved = st_clean or end_user_resolved or account_resolved

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
        [res_st, "7. Reseller", f"{po.reseller_name or (reseller_party or {}).get('name') or 'Direct / Reseller verified'}" if res_st == "pass" else res_msg],
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
    def _norm_sku(s: Optional[str]) -> str:
        clean = re.sub(r"[^A-Za-z0-9]", "", s or "").upper()
        if clean.startswith("F5F5"):
            clean = clean[2:]
        return clean

    lines = []
    available_q_lines = list(q.lines) if (q and q.lines) else []

    for li in po.line_items:
        p_num = li.part_number or "(no SKU)"
        p_tot = float(li.total_price) if li.total_price is not None else 0.0
        qty_int = max(1, int(li.quantity or 1))
        unit_p = float(li.unit_price) if li.unit_price is not None else round(p_tot / qty_int, 2)

        if _is_freight_line(li):
            lines.append({
                "poSku": p_num,
                "desc": li.description or p_num,
                "qty": qty_int,
                "unitPrice": unit_p,
                "total": p_tot,
                "sfSku": "Freight / Pass-through",
                "sfTotal": p_tot,
                "match": "yes",
            })
            continue

        norm_p = _norm_sku(p_num)
        best_match = None

        # 1. Exact or normalized SKU match in available_q_lines
        sku_matches = [
            ql for ql in available_q_lines
            if ql.part_number and (_norm_sku(ql.part_number) == norm_p or ql.part_number == p_num)
        ]
        if sku_matches:
            best_match = min(
                sku_matches,
                key=lambda ql: abs(p_tot - (float(ql.total_price) if ql.total_price is not None else 0.0))
            )
        else:
            # 2. Check for penny/nominal token ($0.01 PO line vs $0.00 quote line)
            if abs(p_tot) <= 0.05:
                zero_cand = [
                    ql for ql in available_q_lines
                    if ql.total_price is not None and abs(float(ql.total_price)) <= 0.05
                    and (_norm_sku(ql.part_number) == norm_p or not norm_p)
                ]
                if zero_cand:
                    best_match = zero_cand[0]

            # 3. If no SKU match, check by price (for resellers using internal SKUs or Dell "Not Available")
            if not best_match and p_tot > 0.0:
                price_cand = [
                    ql for ql in available_q_lines
                    if ql.total_price is not None and abs(float(ql.total_price) - p_tot) <= 0.05
                ]
                if price_cand:
                    best_match = price_cand[0]

        if best_match is not None:
            available_q_lines.remove(best_match)
            sf_tot = float(best_match.total_price) if best_match.total_price is not None else 0.0
            is_match = (
                abs(p_tot - sf_tot) <= 0.05
                or (abs(p_tot) <= 0.05 and abs(sf_tot) <= 0.05)
                or abs(p_tot - sf_tot) <= 1.0
            )
            m_status = "yes" if is_match else "diff"
            lines.append({
                "poSku": p_num,
                "desc": li.description or p_num,
                "qty": qty_int,
                "unitPrice": unit_p,
                "total": p_tot,
                "sfSku": best_match.part_number or "-",
                "sfTotal": sf_tot,
                "match": m_status,
            })
        else:
            lines.append({
                "poSku": p_num,
                "desc": li.description or p_num,
                "qty": qty_int,
                "unitPrice": unit_p,
                "total": p_tot,
                "sfSku": "-",
                "sfTotal": 0.0,
                "match": "no",
            })

    # Booking Form Draft & Notes to RO
    try:
        bf = BookingFormBuilder.build(result)
        booking = {
            "opportunity": bf.opportunity_id or (q.opportunity_id if q else ""),
            "amount": float(bf.amount or po_tot_flt),
            "orderType": bf.sales_order_type or "Standard",
            "distributor": distributor_display,
            "sfdcDistributor": sfdc_distributor,
            "reseller": reseller_resolved or (from_name if channel == "Reseller Partner" else "Direct Order"),
            "endUser": end_user_resolved,
            "account": account_resolved,
            "shipTo": ship_to_resolved,
            "shippingNotification": bf.shipping_notification or "",
            "orderNotifications": bf.order_notifications or "",
            "registrationKeyNotification": bf.registration_key_notification or "",
            "serviceAcknowledgement": bf.service_acknowledgement or "",
            "importantNotes": bf.important_notes or "",
            "issues": len(result.blockers) > 0,
        }
        notes = [{"title": n.title, "body": n.body} for n in bf.notes]
        sfdc_payload = bf.to_salesforce_payload()
    except Exception:
        booking = {
            "opportunity": q.opportunity_id if q else "",
            "amount": po_tot_flt,
            "orderType": "Standard",
            "distributor": distributor_display,
            "sfdcDistributor": sfdc_distributor,
            "reseller": reseller_resolved or (from_name if channel == "Reseller Partner" else "Direct Order"),
            "endUser": end_user_resolved,
            "account": account_resolved,
            "shipTo": ship_to_resolved,
            "issues": len(result.blockers) > 0,
        }
        notes = []
        sfdc_payload = {
            "Opportunity__c": booking["opportunity"],
            "Searchable_PO_field__c": po_num,
            "End_User_Company_Name__c": end_user_resolved,
            "Ship_To_Company_Name__c": ship_to_resolved,
            "Ship_To_Same_As__c": "Invoice",
        }

    # Summary string
    pass_cnt = sum(1 for c in checklist if c[0] == "pass")
    total_flags = len(flags)
    if pass_cnt == 11 and result.outcome == Outcome.VALIDATED:
        summary_str = f"All 11 SOS checklist items passed cleanly. Quote {po.quote_number or (q.quote_number if q else '')} matched 100%."
    elif pass_cnt == 11 and total_flags > 0:
        summary_str = f"11 of 11 PO document items verified, but {total_flags} CPQ quote reconciliation exception(s) flagged."
    else:
        doc_fails = 11 - pass_cnt
        summary_str = f"{pass_cnt} of 11 PO document items passed ({doc_fails} document exceptions, {total_flags} total exceptions)."

    return {
        "id": clean_id,
        "po": po_num,
        "from": from_name,
        "channel": channel,
        "sfdc_distributor": sfdc_distributor,
        "distributor_name": distributor_display,
        "reseller_name": reseller_resolved,
        "end_user_name": end_user_resolved,
        "account_name": account_resolved,
        "ship_to_name": ship_to_resolved,
        "parties": {
            "bill_to": bill_to_party,
            "reseller": reseller_party,
            "end_user": end_user_party,
            "ship_to": ship_to_party,
        },
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

