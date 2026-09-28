"""Salesforce Booking_Form__c and Notes to RO Builder.

Translates validated PO, Quote, and Opportunity context into the exact
Salesforce record payload used by F5 SOS (Revenue Operations / Booking).
"""

from __future__ import annotations

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
    stage: str = "Booked"
    distributor: str = "None"                 # None | NA - Synnex | NA - Carahsoft
    reseller_name: str = "F5 Direct Deal"
    account_name: Optional[str] = None
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
            "Amount__c": float(self.amount),
            "CurrencyIsoCode": self.currency,
            "Sales_Order_Type__c": self.sales_order_type,
            "Stage__c": self.stage,
            "Distributor__c": self.distributor,
            "Reseller_Name__c": self.reseller_name,
            "Account_Name__c": self.account_name,
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

        # 2. Determine Reseller & Account
        account_name = None
        if q and q.account_name:
            account_name = q.account_name
        else:
            bill_to = po.party("bill_to")
            if bill_to and bill_to.get("name"):
                account_name = bill_to["name"]

        # 3. Determine Sales Order Type & Distributor
        sales_order_type = "Standard"
        distributor = "None"

        is_zuora = (
            any((li.part_number or "").startswith("F5-NX-") for li in po.line_items) or
            any("zuora" in (f.message or "").lower() for f in result.findings)
        )
        if q and getattr(q, "quote_type", "").lower() == "zuora":
            is_zuora = True

        if "synnex" in layout_lower:
            distributor = "NA - Synnex"
            sales_order_type = "P+I Booking Form"
        elif "carahsoft" in layout_lower:
            distributor = "NA - Carahsoft"
        elif is_zuora:
            sales_order_type = "Zuora Sales Order"

        reseller_party = po.party("reseller")
        reseller_name = "F5 Direct Deal"
        if reseller_party and reseller_party.get("name"):
            reseller_name = reseller_party["name"]
        elif distributor != "None":
            reseller_name = distributor

        # 4. Resolve Technical / Notification Contacts
        # Pull from Quote Opportunity owner / Sales Rep if resolved
        rep_email = getattr(q, "owner_email", None) or getattr(q, "sales_rep_email", None)
        if not rep_email and distributor in ("NA - Synnex", "NA - Carahsoft"):
            rep_email = "distiteam@f5.com"

        reg_email = None
        ship_party = po.party("ship_to")
        end_user_party = po.party("end_user")

        if end_user_party and end_user_party.get("emails"):
            reg_email = end_user_party["emails"][0]
        elif ship_party and ship_party.get("emails"):
            reg_email = ship_party["emails"][0]
        elif po.party("bill_to") and po.party("bill_to").get("emails"):
            reg_email = po.party("bill_to")["emails"][0]

        # Determine Same As End User Contact Info
        # Must only be True if both exist and their addresses match. If no End User block exists, it is False.
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
            f5_quote_number=po.quote_number,
            amount=booked_amount,
            currency=po.currency or "USD",
            sales_order_type=sales_order_type,
            distributor=distributor,
            reseller_name=reseller_name,
            account_name=account_name,
            shipping_notification=rep_email,
            order_notifications=rep_email,
            registration_key_notification=reg_email if sales_order_type == "Zuora Sales Order" else None,
            service_acknowledgement=None,
            same_as_end_user_contact_info=same_as_end_user,
            order_issues=has_issues,
        )

        # 5. Auto-Draft the 3 Standard Notes to RO
        cls._attach_carrier_notes(form, po, freight_lines, freight_sum)
        cls._attach_end_user_notes(form, po, ship_party, end_user_party, q)
        cls._attach_zuora_notes(form, po)

        return form

    @classmethod
    def _attach_carrier_notes(cls, form: BookingForm, po: ParsedPO,
                              freight_lines: list, freight_sum: Decimal):
        """Auto-drafts 'Note to RO: Carrier Information' dynamically from PO shipping data."""
        if freight_lines:
            # Freight pass-through charge on PO
            lines_desc = ", ".join(f"Line {li.line_no or 'extra'}" for li in freight_lines)
            body = f"Shipping Via F5's FedEx account. See additional charge on {lines_desc}"
            form.notes.append(BookingNote(
                title="Note to RO: Carrier Information",
                body=body
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
        addr_text = " ".join(lines[1:]) if len(lines) > 1 else ""
        name = target_party.get("name", "Customer")
        contact = target_party.get("contact_name")
        email = target_party.get("emails", [""])[0] if target_party.get("emails") else ""
        phone = target_party.get("phones", [""])[0] if target_party.get("phones") else ""

        parts = [name, addr_text]
        if contact:
            parts.append(contact)
        if email:
            parts.append(email)
        if phone:
            parts.append(phone)
        body = " ".join(p.strip() for p in parts if p.strip())
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
