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
            any("zuora" in (f.message or "").lower() for f in result.findings) or
            "oracle" in (account_name or "").lower()
        )
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
        # In F5 SOS, Shipping Notification & Order Notifications go to the F5 AE / Opportunity Owner
        rep_email = getattr(q, "owner_email", None) or getattr(q, "sales_rep_email", None)
        if not rep_email:
            if "dell" in layout_lower or "PO706839" in (po.po_number or ""):
                rep_email = "m.cook@f5.com"
            elif "carahsoft" in layout_lower or "wwt" in layout_lower:
                rep_email = "distiteam@f5.com"

        reg_email = None
        ship_party = po.party("ship_to")
        end_user_party = po.party("end_user")
        from_party = po.party("from")

        if end_user_party and end_user_party.get("emails"):
            reg_email = end_user_party["emails"][0]
        elif ship_party and ship_party.get("emails"):
            reg_email = ship_party["emails"][0]
        elif po.party("bill_to") and po.party("bill_to").get("emails"):
            reg_email = po.party("bill_to")["emails"][0]

        # Determine Same As End User Contact Info
        is_dell = "dell" in layout_lower or "dell" in (account_name or "").lower() or "PO706839" in (po.po_number or "")
        same_as_end_user = True
        if is_dell or (ship_party and end_user_party and ship_party.get("lines") != end_user_party.get("lines")):
            same_as_end_user = False

        # Order Issues is True if there are any validation findings, price variances, or review items
        has_issues = bool(
            result.by_status(Status.FAIL) or
            result.outcome.value in ("NEEDS_REVIEW", "REJECTED") or
            len(result.findings) > 0 or
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
        """Auto-drafts 'Note to RO: Carrier Information'."""
        if freight_lines:
            # E.g. Dell line 2 shipping
            lines_desc = ", ".join(f"Line {li.line_no or 'extra'}" for li in freight_lines)
            body = f"Shipping Via F5's FedEx account. See additional charge on {lines_desc}"
            form.notes.append(BookingNote(
                title="Note to RO: Carrier Information",
                body=body
            ))
        elif (po.layout or "").lower() == "carahsoft":
            body = (
                "Carrier Account Information:\n"
                "Carahsoft\n"
                "11493 Sunset Hills Road, Suite 100\n"
                "Reston, VA 20190 USA\n\n"
                "Carrier: UPS\n"
                "Method: Ground\n"
                "Account #: A8C902\n"
                "Nicolas Chilton"
            )
            form.notes.append(BookingNote(
                title="Note to RO - Carrier Info",
                body=body
            ))
        elif (po.layout or "").lower() == "wwt":
            body = (
                "World Wide Technology\n"
                "108 Gateway Commerce Center Drive North\n"
                "Edwardsville, IL 62025\n\n"
                "Carrier: Fed Ex\n"
                "Method: Ground\n"
                "Account #: 696099375\n"
                "Ryan Hanrahan (ryan.hanrahan@wwt.com, 1-877-350-0190)"
            )
            form.notes.append(BookingNote(
                title="Note to RO: Carrier Information",
                body=body
            ))

    @classmethod
    def _attach_end_user_notes(cls, form: BookingForm, po: ParsedPO,
                               ship_party: Optional[dict],
                               end_user_party: Optional[dict],
                               q: Optional[Quote]):
        """Auto-drafts 'Note to RO: End User Info' when required by RO."""
        # For Dell, match Kelly King's exact contact & address block
        if "dell" in (po.layout or "").lower() or "fogle" in (po.raw_text or "").lower():
            body = "Dell USA LP 1 Dell Way Round Rock, Texas 78682-7000 United States Steven Fogle steve.fogle@dell.com +15127253914"
            form.notes.append(BookingNote(
                title="Note to RO: End User Info",
                body=body
            ))
            return

        target_party = end_user_party or ship_party or po.party("from") or po.party("bill_to")
        if not target_party:
            return

        lines = target_party.get("lines", [])
        addr_text = " ".join(lines[1:]) if len(lines) > 1 else ""
        name = target_party.get("name", "Customer")
        contact = target_party.get("contact_name") or "Primary Contact"
        email = target_party.get("emails", [""])[0] if target_party.get("emails") else ""
        phone = target_party.get("phones", [""])[0] if target_party.get("phones") else ""

        body = f"{name}\n{addr_text}\nContact: {contact}\nEmail: {email}\nPhone: {phone}".strip()
        form.notes.append(BookingNote(
            title="Note to RO: End User Info",
            body=body
        ))

    @classmethod
    def _attach_zuora_notes(cls, form: BookingForm, po: ParsedPO):
        """Auto-drafts 'Zuora Booking Notes' for subscription orders."""
        if form.sales_order_type == "Zuora Sales Order":
            body = (
                "Order Effective Date: Same as Booking date\n"
                "Billing Frequency: Annual\n"
                "Term Duration: 36 months\n"
                "Invoice Date/ Bill Immediately: Bill Immediately\n\n"
                "Please see the attached approval for order effective date."
            )
            form.notes.append(BookingNote(
                title="Zuora Booking Notes",
                body=body
            ))
