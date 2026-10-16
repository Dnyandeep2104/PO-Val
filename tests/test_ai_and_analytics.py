import io
from decimal import Decimal
from unittest.mock import MagicMock

from po_validation.ai.client import (
    F5AIClient,
    append_checkpoint,
    load_checkpoint,
    make_key,
)
from po_validation.models import (
    Finding,
    LineItem,
    Outcome,
    ParsedPO,
    Quote,
    QuoteLine,
    Severity,
    Status,
    ValidationResult,
)
from po_validation.report.portal_adapter import result_to_portal_order
from review_queue import SOSPortalHandler


def test_f5ai_sos_expert_strips_ingram_pseudo_lines_and_unwraps_columns(tmp_path):
    """Verify F5AI SOS Expert layer removes Ingram Micro address/comment pseudo-lines (001-010)
    and unwraps mid-word split company names/emails before validation."""
    po = ParsedPO(
        source_id="test_ingram.pdf",
        po_number="51-190701",
        quote_number="Q-495232",
        currency="USD",
        po_total=Decimal("23805.29"),
        f5_entity="CORP",
        f5_entity_name="F5 NETWORKS INC",
        reseller_name="CDW LOGISTICS",
        parties=[
            {
                "role": "bill_to",
                "name": "INGRAM MICRO INC.",
                "lines": ["3351 MICHELSON DR STE 100", "IRVINE CA 92612"],
                "country": "US",
            }
        ],
        line_items=[
            LineItem(line_no="001", part_number="QUOTE-COMMENT", description="QUOTE NUMBER: Q-495232", quantity=1, unit_price=Decimal("0"), total_price=Decimal("0")),
            LineItem(line_no="002", part_number="RESELLER-ROW", description="CDW LOGISTICS INC / VERNON HILLS IL", quantity=1, unit_price=Decimal("0"), total_price=Decimal("0")),
            LineItem(line_no="009", part_number="ADDR-ROW", description="2980 FAIRVIEW PARK DR FALLS CHURCH VA 22042", quantity=1, unit_price=Decimal("0"), total_price=Decimal("0")),
            LineItem(
                line_no="011",
                part_number="F5-BIG-LTM-I4600",
                description="BIG-IP I4600 LTM APPLIANCE",
                quantity=1,
                unit_price=Decimal("18500.00"),
                total_price=Decimal("18500.00"),
            ),
            LineItem(
                line_no="012",
                part_number="F5-SVC-BIG-PREM-L1-3",
                description="PREMIUM SUPPORT FOR BIG-IP",
                quantity=1,
                unit_price=Decimal("5305.29"),
                total_price=Decimal("5305.29"),
            ),
        ],
        raw_text=(
            "+-----------------------------------+-----------------------------------+\n"
            "| Reseller Information              | End User Information              |\n"
            "+-----------------------------------+-----------------------------------+\n"
            "| Company Na  CDW LOGISTICS INC     | Company Na  NORTHROP GRUMMAN SYST |\n"
            "| me          200 N MILWAUKEE AVE   | me          EMS CORPORATION       |\n"
            "| Contact Ema JASON.ADAMS@CDW.C     | Address 1   2980 FAIRVIEW PARK DR |\n"
            "| il          OM                    | City        FALLS CHURCH VA 22042 |\n"
            "+-----------------------------------+-----------------------------------+"
        ),
    )

    quote = Quote(
        quote_number="Q-495232",
        found=True,
        currency="USD",
        account_name="INGRAM MICRO INC.",
        end_user_name="NORTHROP GRUMMAN SYSTEMS CORPORATION",
        reseller_name="CDW LOGISTICS INC",
        lines=[
            QuoteLine(part_number="F5-BIG-LTM-I4600", quantity=1, unit_price=Decimal("18500.00"), total_price=Decimal("18500.00")),
            QuoteLine(part_number="F5-SVC-BIG-PREM-L1-3", quantity=1, unit_price=Decimal("5305.29"), total_price=Decimal("5305.29")),
        ],
    )

    # Exercise deterministic cognitive SOS expert path (api_key="YOUR_API_KEY_HERE" skips live HTTP call)
    client = F5AIClient(api_key="YOUR_API_KEY_HERE", checkpoint_path=str(tmp_path / "ai_ckpt.jsonl"))
    insights = client.refine_parsed_po(po, quote)

    # 3 pseudo lines removed, only 2 real F5 SKU lines remain
    assert len(po.line_items) == 2
    assert [li.part_number for li in po.line_items] == ["F5-BIG-LTM-I4600", "F5-SVC-BIG-PREM-L1-3"]
    assert insights.get("removed_fake_sku_rows") == 3
    assert len(insights.get("corrections", [])) >= 1


def test_checkpoint_utilities(tmp_path):
    ckpt_file = str(tmp_path / "ai_checkpoint.jsonl")
    k1 = make_key("PO-1001", "sos_expert")
    append_checkpoint(ckpt_file, k1, {"po_number": "PO-1001", "confidence": 0.99})

    loaded = load_checkpoint(ckpt_file)
    assert k1 in loaded
    assert loaded[k1]["po_number"] == "PO-1001"


def test_infer_theater_and_enrich_portal_order():
    po = ParsedPO(
        source_id="fed_po.pdf",
        po_number="PO-FED-777",
        quote_number="Q-777",
        currency="USD",
        po_total=Decimal("125000.00"),
        f5_entity="GSLLC",
        f5_entity_name="F5 Networks Government Solutions LLC",
        reseller_name="World Wide Technology",
        parties=[
            {
                "role": "bill_to",
                "name": "Carahsoft Technology Corp",
                "lines": ["11493 Sunset Hills Rd", "Reston, VA 20190"],
                "country": "US",
            },
            {
                "role": "ship_to",
                "name": "DoD Logistics Center",
                "lines": ["1400 Defense Pentagon", "Arlington, VA 22202"],
                "country": "US",
                "emails": ["r.dane@mail.mil"],
            },
        ],
    )
    res = ValidationResult(
        po=po,
        quote=None,
        outcome=Outcome.NEEDS_REVIEW,
        findings=[
            Finding(
                rule_id="payment_terms",
                status=Status.FAIL,
                severity=Severity.MAJOR,
                message="PO states Net 60 vs Account Master Net 30.",
            )
        ],
    )

    order = result_to_portal_order(res, doc_filename="fed_po.pdf")
    assert order["theater"] == "AMER"
    assert order["sub_region"] == "US Federal (GSLLC)"
    assert order["blocker_category"] == "Payment Terms"
    assert "Arlington" in order["shipping_address"]
    assert "gpt-6-astra" in order["ai_insights"]["model"]


def test_pdf_endpoint_serves_file_from_disk_and_download_mode(tmp_path):
    pdf_file = tmp_path / "ingram_test.pdf"
    raw_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF"
    pdf_file.write_bytes(raw_bytes)

    mock_service = MagicMock()
    mock_service.store.get_order.return_value = {
        "id": "live_51-190701",
        "file": "ingram_test.pdf",
        "pdf_path": str(pdf_file),
    }

    handler = SOSPortalHandler.__new__(SOSPortalHandler)
    handler.service = mock_service
    handler.path = "/api/pdf/live_51-190701?download=1"
    handler.wfile = io.BytesIO()

    responses = []
    headers = {}
    handler.send_response = lambda code: responses.append(code)
    handler.send_header = lambda k, v: headers.setdefault(k, v)
    handler.end_headers = lambda: None

    handler.do_GET()

    assert responses == [200]
    assert headers.get("Content-Type") == "application/pdf"
    assert "attachment;" in headers.get("Content-Disposition", "")
    assert handler.wfile.getvalue() == raw_bytes


def test_f5ai_sos_expert_repairs_multi_qty_base_price_and_splits(tmp_path):
    """Verify F5AI SOS Expert detects when multi-quantity lines (Qty=2) had base unit price
    extracted as total_price (e.g., PO 70-AP476-D), repairs extended prices to Qty × Base Price,
    and reconciles repeated SKUs (`F5-SVC-BIG-VE+PREL13`) against their exact CPQ quote lines."""
    po = ParsedPO(
        source_id="70-AP476-D.pdf",
        po_number="70-AP476-D",
        quote_number="F5Q-01304095",
        currency="USD",
        po_total=Decimal("36063.89"),
        f5_entity="CORP",
        f5_entity_name="F5, Inc.",
        line_items=[
            LineItem(line_no="006", part_number="F5-BIG-LTM-VE-1G-V23", description="BIGIP VE 1G", quantity=Decimal("2"), unit_price=Decimal("6778.25"), total_price=Decimal("13556.50")),
            LineItem(line_no="007", part_number="F5-SVC-BIG-VE+PREL13", description="PREMIUM SVC", quantity=Decimal("2"), unit_price=Decimal("4111.36"), total_price=Decimal("8222.73")),
            LineItem(line_no="008", part_number="F5-BIG-LTM-VE-25MV23", description="BIGIP VE 25M", quantity=Decimal("2"), unit_price=Decimal("1269.20"), total_price=Decimal("2538.41")),
            LineItem(line_no="009", part_number="F5-SVC-BIG-VE+PREL13", description="PREMIUM SVC", quantity=Decimal("2"), unit_price=Decimal("769.84"), total_price=Decimal("1539.68")),
            LineItem(line_no="010", part_number="F5-ADD-BIG-DNS-VE-1K", description="BIG DNS VE 1K", quantity=Decimal("2"), unit_price=Decimal("3176.54"), total_price=Decimal("6353.09")),
            LineItem(line_no="011", part_number="F5-SVC-BIG-VE+PREL13", description="PREMIUM SVC", quantity=Decimal("2"), unit_price=Decimal("1926.74"), total_price=Decimal("3853.48")),
        ],
    )
    quote = Quote(
        quote_number="F5Q-01304095",
        found=True,
        currency="USD",
        lines=[
            QuoteLine(part_number="F5-BIG-LTM-VE-1G-V23", quantity=Decimal("2"), unit_price=Decimal("13556.50"), total_price=Decimal("27113.00")),
            QuoteLine(part_number="F5-SVC-BIG-VE+PREL13", quantity=Decimal("2"), unit_price=Decimal("8222.73"), total_price=Decimal("16445.46")),
            QuoteLine(part_number="F5-BIG-LTM-VE-25MV23", quantity=Decimal("2"), unit_price=Decimal("2538.41"), total_price=Decimal("5076.82")),
            QuoteLine(part_number="F5-SVC-BIG-VE+PREL13", quantity=Decimal("2"), unit_price=Decimal("1539.68"), total_price=Decimal("3079.36")),
            QuoteLine(part_number="F5-ADD-BIG-DNS-VE-1K", quantity=Decimal("2"), unit_price=Decimal("6353.09"), total_price=Decimal("12706.18")),
            QuoteLine(part_number="F5-SVC-BIG-VE+PREL13", quantity=Decimal("2"), unit_price=Decimal("3853.48"), total_price=Decimal("7706.96")),
        ],
    )

    client = F5AIClient(api_key="YOUR_API_KEY_HERE", checkpoint_path=str(tmp_path / "ai_ckpt2.jsonl"))
    client.refine_parsed_po(po, quote)

    assert po.po_total == Decimal("72127.78")
    assert [li.total_price for li in po.line_items] == [
        Decimal("27113.00"),
        Decimal("16445.46"),
        Decimal("5076.82"),
        Decimal("3079.36"),
        Decimal("12706.18"),
        Decimal("7706.96"),
    ]

    res = ValidationResult(po=po, quote=quote, outcome=Outcome.VALIDATED, findings=[])
    order = result_to_portal_order(res, doc_filename="70-AP476-D.pdf")
    assert order["status"] == "ready"
    assert all(li["match"] == "yes" for li in order["lines"])

