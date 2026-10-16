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
    """Verify F5AI SOS Expert repairs multi-quantity lines (Qty=2) ONLY when the PO's own
    raw_text contains the printed extended amounts and/or footer total (PO 70-AP476-D),
    without consulting the quote."""
    po = ParsedPO(
        source_id="70-AP476-D.pdf",
        po_number="70-AP476-D",
        quote_number="F5Q-01304095",
        currency="USD",
        po_total=Decimal("72127.78"),
        f5_entity="CORP",
        f5_entity_name="F5, Inc.",
        line_items=[
            LineItem(line_no="013", part_number="F5-BIG-LTM-VE-1G-V23", description="BIGIP VE 1G", quantity=Decimal("2"), unit_price=Decimal("6778.25"), total_price=Decimal("13556.50")),
            LineItem(line_no="014", part_number="F5-SVC-BIG-VE+PREL13", description="PREMIUM SVC", quantity=Decimal("2"), unit_price=Decimal("4111.36"), total_price=Decimal("8222.73")),
            LineItem(line_no="015", part_number="F5-BIG-LTM-VE-25MV23", description="BIGIP VE 25M", quantity=Decimal("2"), unit_price=Decimal("1269.20"), total_price=Decimal("2538.41")),
            LineItem(line_no="016", part_number="F5-SVC-BIG-VE+PREL13", description="PREMIUM SVC", quantity=Decimal("2"), unit_price=Decimal("769.84"), total_price=Decimal("1539.68")),
            LineItem(line_no="017", part_number="F5-ADD-BIG-DNS-VE-1K", description="BIG DNS VE 1K", quantity=Decimal("2"), unit_price=Decimal("3176.54"), total_price=Decimal("6353.09")),
            LineItem(line_no="018", part_number="F5-SVC-BIG-VE+PREL13", description="PREMIUM SVC", quantity=Decimal("2"), unit_price=Decimal("1926.74"), total_price=Decimal("3853.48")),
        ],
        raw_text=(
            "|013| 2 |2Q3542 F5-BIG-LTM-VE-1G-V23 | 13556.50 |EA | 27113.00\n"
            "|014| 2 |2S7559 F5-SVC-BIG-VE+PREL13 | 8222.73 |EA | 16445.46\n"
            "|015| 2 |9Q0108 F5-BIG-LTM-VE-25MV23 | 2538.41 |EA | 5076.82\n"
            "|016| 2 |2S7559 F5-SVC-BIG-VE+PREL13 | 1539.68 |EA | 3079.36\n"
            "|017| 2 |9V1788 F5-ADD-BIG-DNS-VE-1K | 6353.09 |EA | 12706.18\n"
            "|018| 2 |2S7559 F5-SVC-BIG-VE+PREL13 | 3853.48 |EA | 7706.96\n"
            "| | 12 | | 72127.78\n"
        ),
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
    client.refine_parsed_po(po)

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


def _make_clean_base_po_and_quote():
    po = ParsedPO(
        source_id="adv.pdf",
        layout="synnex",
        confidence=0.95,
        po_number="PO-ADV-01",
        quote_number="F5Q-01304095",
        currency="USD",
        payment_terms="NET 30",
        inco_terms="FCA Ship Point",
        inco_terms_acceptable=True,
        f5_entity="CORP",
        f5_entity_name="F5, Inc.",
        carriers=["FEDEX"],
        carrier_account="123456789",
        reseller_name="CDW LLC",
        po_total=Decimal("27113.00"),
        parties=[
            {"role": "bill_to", "name": "TD SYNNEX Corporation", "lines": ["44201 Nobel Dr", "Fremont, CA 94538"]},
            {"role": "ship_to", "name": "Acme Corp", "lines": ["100 Main St", "Austin, TX 78701"], "contact_name": "Jane Doe", "emails": ["jane@acme.com"], "phones": ["512-555-0199"]},
            {"role": "end_user", "name": "Acme Corp", "lines": ["100 Main St", "Austin, TX 78701"]},
            {"role": "reseller", "name": "CDW LLC", "lines": ["200 N Milwaukee Ave", "Vernon Hills, IL 60061"]},
        ],
        line_items=[
            LineItem(line_no="1", part_number="F5-BIG-LTM-VE-1G-V23", description="BIG-IP VE 1G", quantity=Decimal("2"), unit_price=Decimal("13556.50"), total_price=Decimal("27113.00")),
        ],
        raw_text="PO-ADV-01 F5Q-01304095 F5, Inc. TD SYNNEX Corporation Acme Corp CDW LLC F5-BIG-LTM-VE-1G-V23",
    )
    quote = Quote(
        quote_number="F5Q-01304095",
        found=True,
        status="Approved",
        currency="USD",
        opportunity_id="006000000000123",
        account_name="TD SYNNEX Corporation",
        end_user_name="Acme Corp",
        reseller_name="CDW LLC",
        payment_terms="NET 30",
        lines=[
            QuoteLine(part_number="F5-BIG-LTM-VE-1G-V23", description="BIG-IP VE 1G", quantity=Decimal("2"), unit_price=Decimal("13556.50"), total_price=Decimal("27113.00")),
        ],
    )
    return po, quote


def test_adversarial_reseller_underpays_by_half_is_not_rewritten(tmp_path):
    """Adversarial Test 1: Reseller under-pays by half (qty 2, PO line total $13,556.50,
    PO total $13,556.50, quote $27,113.00). Must NOT rewrite PO prices to $27,113.00
    and MUST route to Needs Review."""
    from po_validation.validate.engine import load_engine

    po, quote = _make_clean_base_po_and_quote()
    po.po_total = Decimal("13556.50")
    po.line_items = [
        LineItem(
            line_no="1",
            part_number="F5-BIG-LTM-VE-1G-V23",
            description="BIG-IP VE 1G",
            quantity=Decimal("2"),
            unit_price=Decimal("6778.25"),
            total_price=Decimal("13556.50"),
        )
    ]
    po.raw_text = "Line 1 Qty 2 F5-BIG-LTM-VE-1G-V23 Unit 6,778.25 Extended 13,556.50 Total 13,556.50"

    client = F5AIClient(api_key="", checkpoint_path=str(tmp_path / "adv1.jsonl"))
    client.refine_parsed_po(po, quote)

    assert po.line_items[0].total_price == Decimal("13556.50")
    assert po.po_total == Decimal("13556.50")

    res = load_engine().run(po, quote)
    assert res.outcome == Outcome.NEEDS_REVIEW
    failed_rules = {f.rule_id for f in res.by_status(Status.FAIL)}
    assert "line_items_match" in failed_rules
    assert "order_total_match" in failed_rules


def test_adversarial_wrong_sku_one_char_diff_is_not_renamed(tmp_path):
    """Adversarial Test 2: PO has a SKU 1 character different from the quote SKU
    (F5-ADD-BIG-R56R59X vs F5-ADD-BIG-R56R59). Must NOT rename the PO SKU to match
    the quote and MUST route to Needs Review."""
    from po_validation.validate.engine import load_engine

    po, quote = _make_clean_base_po_and_quote()
    po.line_items[0].part_number = "F5-ADD-BIG-R56R59X"
    quote.lines[0].part_number = "F5-ADD-BIG-R56R59"

    client = F5AIClient(api_key="", checkpoint_path=str(tmp_path / "adv2.jsonl"))
    client.refine_parsed_po(po, quote)

    assert po.line_items[0].part_number == "F5-ADD-BIG-R56R59X"

    res = load_engine().run(po, quote)
    assert res.outcome == Outcome.NEEDS_REVIEW
    failed_rules = {f.rule_id for f in res.by_status(Status.FAIL)}
    assert "line_items_match" in failed_rules


def test_adversarial_shaky_parse_confidence_is_preserved(tmp_path):
    """Adversarial Test 3: Shaky parse (confidence 0.40) must NOT be forced to 0.96
    and MUST fail extraction_confidence (Needs Review)."""
    from po_validation.validate.engine import load_engine

    po, quote = _make_clean_base_po_and_quote()
    po.confidence = 0.40

    client = F5AIClient(api_key="", checkpoint_path=str(tmp_path / "adv3.jsonl"))
    client.refine_parsed_po(po, quote)

    assert po.confidence == 0.40

    res = load_engine().run(po, quote)
    assert res.outcome == Outcome.NEEDS_REVIEW
    failed_rules = {f.rule_id for f in res.by_status(Status.FAIL)}
    assert "extraction_confidence" in failed_rules


def test_adversarial_india_freight_forwarder_is_not_ior(tmp_path):
    """Adversarial Test 4: Hardware shipment to India naming only a freight forwarder
    ('Expeditors International') without an Importer of Record must NOT set
    importer_of_record and MUST fail international_shipping (Needs Review)."""
    from po_validation.validate.engine import load_engine

    po, quote = _make_clean_base_po_and_quote()
    po.line_items[0].part_number = "F5-BIG-LTM-R5800"
    po.line_items[0].description = "BIG-IP R5800 HARDWARE APPLIANCE"
    quote.lines[0].part_number = "F5-BIG-LTM-R5800"
    quote.lines[0].description = "BIG-IP R5800 HARDWARE APPLIANCE"
    po.parties[1] = {
        "role": "ship_to",
        "name": "Acme India Pvt Ltd",
        "lines": ["Plot 42, Bandra Kurla Complex", "Mumbai, Maharashtra 400051, India"],
        "contact_name": "Rajesh Kumar",
        "emails": ["rajesh@acme.in"],
        "phones": ["+91-22-5555-0100"],
    }
    po.importer_of_record = None
    po.raw_text = "Ship To: Acme India Pvt Ltd, Mumbai, India. Forwarder: Expeditors International."

    client = F5AIClient(api_key="", checkpoint_path=str(tmp_path / "adv4.jsonl"))
    client.refine_parsed_po(po, quote)

    assert po.importer_of_record is None

    res = load_engine().run(po, quote)
    assert res.outcome == Outcome.NEEDS_REVIEW
    failed_rules = {f.rule_id for f in res.by_status(Status.FAIL)}
    assert "international_shipping" in failed_rules


def test_no_hardcoded_api_key_and_tls_verified_by_default(monkeypatch):
    import ssl
    import po_validation.ai.client as ai_mod

    assert not hasattr(ai_mod, "DEFAULT_F5AI_API_KEY")
    monkeypatch.delenv("F5AI_INSECURE_TLS", raising=False)
    ctx = F5AIClient._get_ssl_context("https://f5ai.pd.f5net.com/openai/")
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_ingram_70_ap476_d_extracts_extended_totals_and_summary_total():
    """Verify Ingram 70-AP476-D.pdf parser extracts extended totals (not unit costs)
    and footer summary total $72,127.78 directly from the PDF."""
    from pathlib import Path
    from po_validation.extract import registry

    pdf_path = Path(__file__).resolve().parent.parent / "70-AP476-D.pdf"
    if not pdf_path.exists():
        return
    po = registry.parse(pdf_path.read_bytes(), pdf_path.name)
    assert po.po_total == Decimal("72127.78")
    priced = [li for li in po.line_items if li.total_price is not None and li.total_price > 0]
    assert len(priced) == 6
    ext_totals = [li.total_price for li in priced]
    assert ext_totals == [
        Decimal("27113.00"),
        Decimal("16445.46"),
        Decimal("5076.82"),
        Decimal("3079.36"),
        Decimal("12706.18"),
        Decimal("7706.96"),
    ]





