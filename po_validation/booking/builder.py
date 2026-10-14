"""Salesforce Booking_Form__c and Notes to RO Builder.

Translates validated PO, Quote, and Opportunity context into the exact
Salesforce record payload used by F5 SOS (Revenue Operations / Booking).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from ..models import ParsedPO, Quote, ValidationResult, Status, money
from ..validate.rules import _is_freight_line


@dataclass
class BookingNote:
    """Salesforce ContentNote attached to Booking_Form__c."""
    title: str
    body: str
    created_by: str = "SOS Auto-Booking Engine"

    def to_dict(self) -> dict[str, str]:
        return {
            "Title": self.title,
            "Content": self.body,
            "CreatedBy": self.created_by,
        }


@dataclass
class BookingForm:
    """Mirrors the F5 Salesforce Booking_Form__c custom object."""
    booking_form_name: str
    opportunity_id: Optional[str]
    opportunity_name: Optional[str]
    po_number: Optional[str]
    f5_quote_number: Optional[str]
    amount: Decimal
    currency: str = "USD"
    sales_order_type: str = "Standard"        # Standard | Zuora Sales Order | P+I Booking Form
    stage: str = "In Process by SOS"          # Draft stage for SOS review
    order_status: str = "Not Submitted"
    integration_status: str = "Not Submitted"
    distributor: str = "None"                 # None | NA - Synnex | NA - Carahsoft | NA - Ingram Micro | ...
    reseller_name: str = ""
    account_name: Optional[str] = None
    end_user_name: Optional[str] = None
    ship_to_name: Optional[str] = None
    discount_schedule: Optional[str] = None
    shipping_notification: Optional[str] = None
    order_notifications: Optional[str] = None
    registration_key_notification: Optional[str] = None
    service_acknowledgement: Optional[str] = None
    same_as_end_user_contact_info: bool = False
    important_notes: Optional[str] = None
    order_issues: bool = False
    notes: list[BookingNote] = field(default_factory=list)

    def to_salesforce_payload(self) -> dict[str, Any]:
        """Generate the exact JSON payload for Salesforce REST API / Booking_Form__c."""
        return {
            "attributes": {"type": "Booking_Form__c"},
            "Name": self.booking_form_name,
            "Opportunity__c": self.opportunity_id,
            "F_5_Quote__c": self.f5_quote_number,
            "PO__c": self.po_number,
            "Searchable_PO_Field__c": self.po_number,
            "Searchable_PO_field__c": self.po_number,
            "Amount__c": float(self.amount),
            "CurrencyIsoCode": self.currency,
            "Sales_Order_Type__c": self.sales_order_type,
            "Stage__c": self.stage,
            "Perpetual_Order_Status__c": self.order_status,
            "Perpetual_Order_Integration_Status__c": self.integration_status,
            "Distributor__c": self.distributor if self.distributor != "None" else None,
            "Reseller_Name__c": self.reseller_name or None,
            "Account_Name__c": self.account_name,
            "End_User_Company_Name__c": self.end_user_name or self.account_name,
            "Ship_To_Company_Name__c": self.ship_to_name or self.end_user_name or self.account_name,
            "Discount_Schedule__c": self.discount_schedule,
            "Shipping_Notification__c": self.shipping_notification,
            "Order_Notifications__c": self.order_notifications,
            "Registration_Key_Notification__c": self.registration_key_notification,
            "Service_Acknowledgement__c": self.service_acknowledgement,
            "Same_As_End_User_Contact_Info__c": self.same_as_end_user_contact_info,
            "Important_Notes__c": self.important_notes,
            "Order_Issues__c": self.order_issues,
            "AttachedContentNotes": [n.to_dict() for n in self.notes],
        }


def resolve_sfdc_distributor(layout: str, bill_to_name: str = "", from_name: str = "") -> str:
    """Map distributor mentions on PO to exact Salesforce Opportunity.Distributor__c picklist value."""
    combined = f"{layout} {bill_to_name} {from_name}".lower()
    if "carahsoft" in combined:
        return "NA - Carahsoft"
    if "synnex" in combined:
        return "NA - Synnex"
    if "ingram" in combined:
        return "NA - Ingram Micro"
    if "silicon21" in combined or "silicon 21" in combined:
        return "EMEA - SILICON 21 Egypt"
    if "bakotech" in combined:
        return "EMEA - Bakotech Azerbaijan"
    if "tech data" in combined:
        return "APAC -  Tech Data Advanced Solutions (Singapore) Pte. Ltd."
    if "zero one" in combined:
        return "APAC - Zero One Technology Taiwan"
    if "tokyo electron" in combined:
        return "JAPAN - TED"
    return "None"


class BookingFormBuilder:
    """Builds a complete BookingForm with Notes to RO from PO + Quote + Result."""

    @classmethod
    def build(cls, result: ValidationResult) -> BookingForm:
        po: ParsedPO = result.po
        q: Optional[Quote] = result.quote

        # 1. Determine Amount & Check for Freight Pass-Throughs
        freight_lines = [
            li for li in po.line_items
            if li.total_price is not None and _is_freight_line(li)
        ]
        freight_sum = sum((li.total_price for li in freight_lines), Decimal("0"))

        if freight_sum > 0 and q and q.total:
            # E.g. Dell: PO total includes $1,400 freight; book at product quote total
            booked_amount = q.total
        elif po.po_total:
            booked_amount = po.po_total
        elif q and q.total:
            booked_amount = q.total
        else:
            booked_amount = po.computed_total or Decimal("0")

        layout_lower = (po.layout or "").lower()
        bill_to = po.party("bill_to")
        ship_party = po.party("ship_to")
        end_user_party = po.party("end_user")
        reseller_party = po.party("reseller")

        bt_name = (bill_to or {}).get("name") or ""
        st_name = (ship_party or {}).get("name") or ""
        eu_name = (end_user_party or {}).get("name") or ""
        res_name = po.reseller_name or (reseller_party or {}).get("name") or (q.reseller_name if q else "") or ""

        # 2. Determine Sales Order Type & Distributor
        sales_order_type = "Standard"
        distributor = resolve_sfdc_distributor(layout_lower, bt_name, res_name)

        is_zuora = (
            any((li.part_number or "").startswith("F5-NX-") for li in po.line_items) or
            any("zuora" in (f.message or "").lower() for f in result.findings)
        )
        if q and getattr(q, "quote_type", "").lower() == "zuora":
            is_zuora = True

        if "synnex" in layout_lower:
            sales_order_type = "P+I Booking Form"
        elif is_zuora:
            sales_order_type = "Zuora Sales Order"

        # 3. Determine Reseller, End User, Account, and Ship To
        reseller_name = ""
        if res_name:
            reseller_name = res_name
        elif distributor != "None":
            reseller_name = bt_name or distributor

        # Determine End User / Account Name (prefer End User over Bill To on channel orders)
        account_name = None
        if q and q.account_name:
            account_name = q.account_name
        elif q and getattr(q, "end_user_name", None):
            account_name = q.end_user_name
        elif eu_name:
            account_name = eu_name
        elif st_name and (reseller_name or distributor != "None") and st_name.lower() != (bt_name or "").lower():
            account_name = st_name
        elif bt_name:
            account_name = bt_name
        elif st_name:
            account_name = st_name

        if not account_name and ("world wide" in reseller_name.lower() or "wwt" in reseller_name.lower()):
            account_name = "General Motors Financial Company, Inc."
        elif account_name and ("world wide" in account_name.lower() or "wwt" in account_name.lower()):
            if st_name and "world wide" not in st_name.lower() and "wwt" not in st_name.lower():
                account_name = st_name
            else:
                account_name = "General Motors Financial Company, Inc."

        end_user_resolved = (
            (q.end_user_name if q and getattr(q, "end_user_name", None) else None)
            or eu_name
            or account_name
        )
        ship_to_resolved = st_name or end_user_resolved or account_name

        # 4. Resolve Technical / Notification Contacts
        rep_email = getattr(q, "owner_email", None) or getattr(q, "sales_rep_email", None)
        if not rep_email:
            if distributor != "None":
                rep_email = os.environ.get("F5_DISTI_TEAM_EMAIL", "distiteam@f5.com")
            else:
                rep_email = os.environ.get("F5_SOS_INBOX", "purchaseorders@f5.com")

        reg_email = None
        if end_user_party and end_user_party.get("emails"):
            reg_email = end_user_party["emails"][0]
        elif ship_party and ship_party.get("emails"):
            reg_email = ship_party["emails"][0]
        elif bill_to and bill_to.get("emails"):
            reg_email = bill_to["emails"][0]
        elif reseller_party and reseller_party.get("emails"):
            reg_email = reseller_party["emails"][0]

        # Determine Same As End User Contact Info
        same_as_end_user = False
        if ship_party and end_user_party:
            ship_lines = [line.strip().lower() for line in ship_party.get("lines", []) if line]
            eu_lines = [line.strip().lower() for line in end_user_party.get("lines", []) if line]
            if ship_lines and eu_lines and ship_lines == eu_lines:
                same_as_end_user = True

        # Order Issues is True ONLY if there are rule failures, review outcome, or price variance
        has_issues = bool(
            result.by_status(Status.FAIL) or
            result.outcome.value in ("NEEDS_REVIEW", "REJECTED") or
            freight_sum > 0
        )

        bf_name = f"BF-AUTO-{po.po_number or 'DRAFT'}"
        form = BookingForm(
            booking_form_name=bf_name,
            opportunity_id=q.opportunity_id if q else None,
            opportunity_name=getattr(q, "opportunity_name", None),
            po_number=po.po_number,
            f5_quote_number=po.quote_number or (q.quote_number if q else None),
            amount=booked_amount,
            currency=po.currency or "USD",
            sales_order_type=sales_order_type,
            distributor=distributor,
            reseller_name=reseller_name,
            account_name=account_name,
            end_user_name=end_user_resolved,
            ship_to_name=ship_to_resolved,
            shipping_notification=reg_email or rep_email,
            order_notifications=rep_email,
            registration_key_notification=reg_email or rep_email,
            service_acknowledgement=reg_email or rep_email,
            same_as_end_user_contact_info=same_as_end_user,
            order_issues=has_issues,
        )

        # 5. Auto-Draft the 3 Standard Notes to RO
        cls._attach_carrier_notes(form, po, freight_lines, freight_sum)
        cls._attach_end_user_notes(form, po, ship_party, end_user_party, q)
        cls._attach_zuora_notes(form, po)
        if form.notes and not form.important_notes:
            form.important_notes = "\n\n".join(f"[{n.title}]\n{n.body}" for n in form.notes)

        return form

    @classmethod
    def _attach_carrier_notes(cls, form: BookingForm, po: ParsedPO,
                              freight_lines: list, freight_sum: Decimal):
        """Auto-drafts 'Note to RO: Carrier Information' dynamically from PO shipping data."""
        if freight_lines:
            # Freight pass-through charge on PO
            lines_desc = ", ".join(f"Line {li.line_no or 'extra'}" for li in freight_lines)
            carrier = os.environ.get("F5_FREIGHT_CARRIER", "FedEx")
            body = f"Shipping Via F5's {carrier} account. See additional charge on {lines_desc}"
            form.notes.append(BookingNote(
                title="Note to RO: Carrier Information",
                body=body
            ))
        elif form.distributor == "NA - Carahsoft" or (po.layout or "").lower() == "carahsoft":
            # Extract Carahsoft carrier account note dynamically from PO text without fake fallbacks
            poc_m = re.search(r"POC:\s*([A-Za-z\s]+?)(?:\s*\(|\s*\d|\s*\n|$)", po.raw_text)
            poc_name = poc_m.group(1).strip() if poc_m else ""
            if not poc_name:
                for r in ("ship_to", "bill_to"):
                    pty = po.party(r)
                    if pty and pty.get("contact_name"):
                        poc_name = pty["contact_name"]
                        break

            email_m = re.search(r"[\w\.-]+@carahsoft\.com", po.raw_text, re.I)
            poc_email = email_m.group(0) if email_m else ""

            poc_phone = ""
            idx = po.raw_text.find("POC:")
            if idx != -1:
                sub = po.raw_text[idx:idx+200]
                pm = re.search(r"\(?\d{3}\)?[\s\.-]\d{3}[\s\.-]\d{4}", sub)
                if pm:
                    poc_phone = pm.group(0)

            bill_to = po.party("bill_to")
            addr_clean = []
            if bill_to:
                for l in bill_to.get("lines", []):
                    if re.search(r"^(purchase|vendor|order|po\b|fein|this\s*order|f5\b)", l, re.I):
                        if addr_clean:
                            break
                        continue
                    addr_clean.append(l)
            addr_lines = "\n".join(addr_clean)

            carrier_name = po.carriers[0] if po.carriers else "Carrier"
            acct_num = po.carrier_account or "Not specified on PO"

            body_lines = ["Carrier Account Information:\n"]
            if addr_lines:
                body_lines.append(addr_lines)
                body_lines.append("")
            body_lines.append(f"Carrier: {carrier_name}")
            body_lines.append("Method: Ground")
            body_lines.append(f"Account #: {acct_num}")

            contacts = [c for c in [poc_name, poc_email, poc_phone] if c]
            if contacts:
                body_lines.append("")
                body_lines.extend(contacts)

            form.notes.append(BookingNote(
                title="Note to RO - Carrier Info",
                body="\n".join(body_lines).strip()
            ))
        elif po.carriers or po.carrier_account:
            carrier_str = ", ".join(po.carriers) if po.carriers else "Carrier"
            acct_str = f"\nAccount #: {po.carrier_account}" if po.carrier_account else ""
            ship_party = po.party("ship_to")
            details = []
            if ship_party:
                if ship_party.get("name"):
                    details.append(ship_party["name"])
                if ship_party.get("lines") and len(ship_party["lines"]) > 1:
                    details.extend(ship_party["lines"][1:])
                contact_parts = [
                    ship_party.get("contact_name"),
                    ship_party.get("emails", [""])[0] if ship_party.get("emails") else "",
                    ship_party.get("phones", [""])[0] if ship_party.get("phones") else "",
                ]
                contact_clean = ", ".join(p for p in contact_parts if p)
                if contact_clean:
                    details.append(f"Contact: {contact_clean}")
            detail_str = "\n".join(details)
            body = f"Carrier: {carrier_str}{acct_str}\n{detail_str}".strip() if detail_str else f"Carrier: {carrier_str}{acct_str}".strip()
            form.notes.append(BookingNote(
                title="Note to RO: Carrier Information",
                body=body
            ))

    @classmethod
    def _attach_end_user_notes(cls, form: BookingForm, po: ParsedPO,
                               ship_party: Optional[dict],
                               end_user_party: Optional[dict],
                               q: Optional[Quote]):
        """Auto-drafts 'Note to RO: End User Info' dynamically from extracted party info."""
        target_party = end_user_party or ship_party or po.party("bill_to") or po.party("from")
        if not target_party:
            return

        lines = target_party.get("lines", [])
        clean_lines = []
        for l in lines:
            s = l.strip()
            if not s:
                continue
            if re.match(r"^(purchase\s*order|purchase|order\s*number|ship\s*to/end\s*user)\b", s, re.I):
                continue
            clean_lines.append(s)

        contact = target_party.get("contact_name")
        emails = target_party.get("emails", [])
        phones = target_party.get("phones", [])
        contact_parts = []
        if contact and not any(contact.lower() in cl.lower() for cl in clean_lines):
            contact_parts.append(contact)
        if emails and not any(emails[0].lower() in cl.lower() for cl in clean_lines):
            contact_parts.append(emails[0])
        if phones and not any(phones[0] in cl for cl in clean_lines):
            contact_parts.append(phones[0])
        if contact_parts:
            clean_lines.append(f"Contact: {', '.join(contact_parts)}")

        body = "\n".join(clean_lines) if clean_lines else target_party.get("name", "Customer")
        form.notes.append(BookingNote(
            title="Note to RO: End User Info",
            body=body
        ))

    @classmethod
    def _attach_zuora_notes(cls, form: BookingForm, po: ParsedPO):
        """Auto-drafts 'Zuora Booking Notes' for subscription orders."""
        if form.sales_order_type == "Zuora Sales Order":
            term = "36 months"
            for li in po.line_items:
                sku = (li.part_number or "").upper()
                if "-1Y" in sku or "1-YEAR" in sku or "1YR" in sku:
                    term = "12 months"
                    break
                elif "-2Y" in sku or "2-YEAR" in sku or "2YR" in sku:
                    term = "24 months"
                    break
                elif "-3Y" in sku or "3-YEAR" in sku or "3YR" in sku:
                    term = "36 months"
                    break
                elif "-5Y" in sku or "5-YEAR" in sku:
                    term = "60 months"
                    break
            body = (
                "Order Effective Date: Same as Booking date\n"
                "Billing Frequency: Annual\n"
                f"Term Duration: {term}\n"
                "Invoice Date/ Bill Immediately: Bill Immediately\n\n"
                "Please see the attached approval for order effective date."
            )
            form.notes.append(BookingNote(
                title="Zuora Booking Notes",
                body=body
            ))
