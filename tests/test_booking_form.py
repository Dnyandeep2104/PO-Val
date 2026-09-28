import sys
from pathlib import Path
from decimal import Decimal
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from po_validation.models import ParsedPO, LineItem, Quote, QuoteLine, ValidationResult, Status, Severity, Finding
from po_validation.booking.builder import BookingFormBuilder, BookingForm
from po_validation.booking.card import render_adaptive_card, render_html_email_preview
from po_validation.ai.client import F5AIClient


def test_booking_form_builder_dell_freight():
    """Verify Dell booking form handles freight line and attaches Carrier Note."""
    po = ParsedPO(
        source_id="dell.pdf",
        po_number="PO706839",
        quote_number="F5Q-00972677",
        po_total=Decimal("74844.80"),
        currency="USD",
        layout="dell",
        line_items=[
            LineItem(line_no="1", part_number="Not Available", description="VELOS Transceiver",
                     quantity=Decimal("20"), unit_price=Decimal("3672.24"), total_price=Decimal("73444.80")),
            LineItem(line_no="2", part_number="Not Available", description="Material Shipping charges",
                     quantity=Decimal("1"), unit_price=Decimal("1400.00"), total_price=Decimal("1400.00")),
        ],
        parties=[
            {"role": "bill_to", "name": "Dell USA LP", "lines": ["Dell USA LP", "1 Dell Way", "Round Rock, TX 78682"]},
            {"role": "ship_to", "name": "Dell USA LP", "lines": ["Dell USA LP", "1 Dell Way", "Round Rock, TX 78682"]},
        ]
    )
    quote = Quote(
        quote_number="F5Q-00972677",
        opportunity_id="006Po00000efMf3IAE",
        account_name="Dell USA LP.",
        lines=[
            QuoteLine(part_number="F5-UPGVELQSFP28-SR4", quantity=Decimal("20"), total_price=Decimal("73444.80"))
        ]
    )
    res = ValidationResult(po=po, quote=quote)
    form = BookingFormBuilder.build(res)

    assert form.po_number == "PO706839"
    # Booked amount should be the quote product total ($73,444.80), NOT including $1,400 freight
    assert form.amount == Decimal("73444.80")
    assert form.account_name == "Dell USA LP."

    # Verify Carrier note is attached
    carrier_notes = [n for n in form.notes if "Carrier" in n.title]
    assert len(carrier_notes) == 1
    assert "Line 2" in carrier_notes[0].body
    assert "FedEx" in carrier_notes[0].body

    # Verify End User note is attached
    eu_notes = [n for n in form.notes if "End User" in n.title]
    assert len(eu_notes) == 1
    assert "Dell USA LP" in eu_notes[0].body


def test_booking_form_builder_carahsoft():
    """Verify Carahsoft distributor and dynamic carrier extraction."""
    po = ParsedPO(
        source_id="carahsoft.pdf",
        po_number="25163692",
        quote_number="F5Q-01062638",
        po_total=Decimal("778650.04"),
        layout="carahsoft",
        carriers=["UPS Ground"],
        carrier_account="A8C902",
        parties=[
            {"role": "bill_to", "name": "Carahsoft Technology Corp", "lines": ["11493 Sunset Hills Rd"]},
            {"role": "ship_to", "name": "Dallas County", "lines": ["Dallas County", "Dallas, TX"], "contact_name": "Nicolas Chilton"},
        ]
    )
    quote = Quote(
        quote_number="F5Q-01062638",
        opportunity_id="006Po00000r6wilIAA",
        account_name="Dallas County",
        lines=[
            QuoteLine(part_number="F5-BIG-BT-R10600", quantity=Decimal("4"), total_price=Decimal("778650.04"))
        ]
    )
    res = ValidationResult(po=po, quote=quote)
    form = BookingFormBuilder.build(res)

    assert form.distributor == "NA - Carahsoft"
    assert form.account_name == "Dallas County"
    assert form.amount == Decimal("778650.04")

    # Carrier note should dynamically reflect parsed PO carrier details
    carrier_notes = [n for n in form.notes if "Carrier" in n.title]
    assert len(carrier_notes) == 1
    assert "A8C902" in carrier_notes[0].body
    assert "Nicolas Chilton" in carrier_notes[0].body


def test_booking_form_builder_zuora():
    """Verify Zuora subscription sales order type and dynamic term derivation."""
    po = ParsedPO(
        source_id="ntt.pdf",
        po_number="4501706557",
        quote_number="F5Q-01080587",
        po_total=Decimal("81075.54"),
        line_items=[
            LineItem(line_no="1", part_number="F5-NX-SUB-1Y", total_price=Decimal("81075.54"))
        ],
        parties=[
            {"role": "bill_to", "name": "NTT America Inc", "lines": ["Portsmouth, NH"]},
            {"role": "ship_to", "name": "Oracle America Inc", "lines": ["Rocklin, CA"]},
        ]
    )
    quote = Quote(
        quote_number="F5Q-01080587",
        opportunity_id="006Po00000jNR0pIAG",
        account_name="Oracle Corporation",
        lines=[
            QuoteLine(part_number="F5-NX-SUB-1Y", quantity=Decimal("1"), total_price=Decimal("81075.54"))
        ]
    )
    res = ValidationResult(po=po, quote=quote)
    form = BookingFormBuilder.build(res)

    assert form.sales_order_type == "Zuora Sales Order"
    zuora_notes = [n for n in form.notes if "Zuora" in n.title]
    assert len(zuora_notes) == 1
    assert "12 months" in zuora_notes[0].body
    assert "Bill Immediately" in zuora_notes[0].body


def test_f5ai_client_deterministic_fallback():
    """Verify F5AI client produces crisp deterministic summaries even offline."""
    client = F5AIClient(api_key="")
    summary = client.generate_sos_summary(
        po_number="PO706839",
        account="Dell USA LP.",
        amount="$73,444.80",
        blockers=[],
        majors=["Inco Terms non-standard"],
        notes=["Note to RO: Carrier Information", "Note to RO: End User Info"]
    )
    assert "Dell USA LP." in summary
    assert "PO706839" in summary
    assert "Note(s) to RO attached" in summary


def test_outlook_card_rendering():
    """Verify Adaptive Card and HTML rendering."""
    form = BookingForm(
        booking_form_name="BF-00545240",
        opportunity_id="006Po00000efMf3IAE",
        opportunity_name="Dell - 100G SFPs - Direct",
        po_number="PO706839",
        f5_quote_number="F5Q-00972677",
        amount=Decimal("73444.80"),
        account_name="Dell USA LP.",
    )
    summary = "Order PO706839 for Dell USA LP. ($73,444.80) validated successfully."
    card = render_adaptive_card(form, summary)
    assert card["type"] == "AdaptiveCard"
    assert "PO #PO706839" in card["body"][0]["items"][0]["text"]

    html = render_html_email_preview(form, summary)
    assert "PO #PO706839" in html
    assert "Dell USA LP." in html
    assert "$73,444.80" in html
