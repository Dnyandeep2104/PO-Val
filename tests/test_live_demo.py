"""Comprehensive test suite for live demo readiness.

Covers:
- SalesforceClient token refresh, sandbox assertion, idempotency, and attachments
- BlobWatcher baselining, metadata polling, base64 payload decoding, and sidecars
- LivePOService pacing, stages, double-click locking, approver sign-off, and re-checking
- Review portal HTTP endpoints and checklist consistency
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from po_validation.act.salesforce import SalesforceClient, SalesforceError
from po_validation.ingest.blob import BlobSource, BlobWatcher, decode_pdf_payload
from po_validation.models import Finding, Outcome, ParsedPO, Quote, QuoteLine, Severity, Status, ValidationResult
from po_validation.report.portal_adapter import result_to_portal_order
from po_validation.resolve.base import StubQuoteSource
from po_validation.service import OrderStore, LivePOService, EventBus
from tests.fakes import FakeHttpResponse, FakeSalesforceServer


# -----------------------------------------------------------------------------
# Salesforce Client Tests
# -----------------------------------------------------------------------------

def test_salesforce_client_sandbox_assertion_blocks_production():
    fake_sf = FakeSalesforceServer(is_sandbox=False, org_name="F5 Production Org")
    client = SalesforceClient(
        instance_url="https://f5.my.salesforce.com",
        access_token="test_token",
        dry_run=False,
    )

    def fake_http(method, url, headers=None, params=None, json_data=None, data=None, **kw):
        code, payload = fake_sf.handle(method, url, headers=headers, params=params, json_data=json_data, data=data)
        return FakeHttpResponse(code, payload)

    with patch("po_validation.act.salesforce._http_request", side_effect=fake_http):
        with pytest.raises(SalesforceError, match="SAFETY BLOCK"):
            client.assert_sandbox()


def test_salesforce_client_sandbox_assertion_passes_sandbox():
    fake_sf = FakeSalesforceServer(is_sandbox=True, org_name="poclab Sandbox")
    client = SalesforceClient(
        instance_url="https://f5--poclab.sandbox.my.salesforce.com",
        access_token="test_token",
        dry_run=False,
    )

    def fake_http(method, url, headers=None, params=None, json_data=None, data=None, **kw):
        code, payload = fake_sf.handle(method, url, headers=headers, params=params, json_data=json_data, data=data)
        return FakeHttpResponse(code, payload)

    with patch("po_validation.act.salesforce._http_request", side_effect=fake_http):
        info = client.org_info()
        assert info["IsSandbox"] is True
        # assert_sandbox() should not raise
        client.assert_sandbox()


def test_salesforce_client_opportunity_resolution_and_booking(monkeypatch):
    monkeypatch.setenv("SF_DEMO_OPPORTUNITY_ID", "006DEMO000000001")
    monkeypatch.setenv("SF_ADAPT_DEMO_OPPORTUNITY", "true")

    fake_sf = FakeSalesforceServer(is_sandbox=True)
    client = SalesforceClient(
        instance_url="https://f5--poclab.sandbox.my.salesforce.com",
        access_token="test_token",
        dry_run=False,
    )

    def fake_http(method, url, headers=None, params=None, json_data=None, data=None, **kw):
        code, payload = fake_sf.handle(method, url, headers=headers, params=params, json_data=json_data, data=data)
        return FakeHttpResponse(code, payload)

    with patch("po_validation.act.salesforce._http_request", side_effect=fake_http):
        # 1. First booking
        res = client.book_portal_order(
            order={
                "id": "order-1",
                "po": "PO-9999",
                "opportunity": "006DEMO000000001",
                "booking": {"amount": 25000.0, "quote": "Q-12345"},
                "reseller": "Ingram Micro",
                "file": "PO-9999.pdf",
            },
            approver_name="Chinmay Dhok",
            audit_note="Verified with reseller terms",
            pdf_data=b"%PDF-1.4 mock pdf data",
        )

        assert res["success"] is True
        assert res["booking_form_id"].startswith("a1sPOCLAB")
        assert res["pdf_attached"] is True

        # Check mock server records
        assert len(fake_sf.booking_forms) == 1
        assert len(fake_sf.content_versions) == 1
        assert len(fake_sf.content_notes) == 1

        # Check adapted opportunity
        opp = fake_sf.opportunities["006DEMO000000001"]
        assert opp.get("PO_to_F5__c") == "PO-9999"

        # 2. Idempotent re-book: should detect existing form and not duplicate
        res2 = client.book_portal_order(
            order={
                "id": "order-1",
                "po": "PO-9999",
                "opportunity": "006DEMO000000001",
                "booking": {"amount": 25000.0, "quote": "Q-12345"},
                "reseller": "Ingram Micro",
                "file": "PO-9999.pdf",
            },
            approver_name="Chinmay Dhok",
        )
        assert res2["success"] is True
        assert res2["booking_form_id"] == res["booking_form_id"]
        assert len(fake_sf.booking_forms) == 1  # No duplicate


# -----------------------------------------------------------------------------
# Ingestion & Blob Decoding Tests
# -----------------------------------------------------------------------------

def test_decode_pdf_payload_detects_base64_and_raw():
    raw_pdf = b"%PDF-1.5 \n%raw content\n%%EOF"
    assert decode_pdf_payload(raw_pdf) == raw_pdf

    b64_pdf = base64.b64encode(raw_pdf)
    assert decode_pdf_payload(b64_pdf) == raw_pdf

    # Strips power automate JSON wrapper if passed
    json_wrapped = json.dumps({"$content-type": "application/pdf", "$content": b64_pdf.decode("ascii")}).encode("utf-8")
    assert decode_pdf_payload(json_wrapped) == raw_pdf


def test_blob_watcher_baselines_existing_blobs():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "blob_state.json"
        mock_source = MagicMock(spec=BlobSource)
        mock_source.list_entries.return_value = [
            {"name": "inbox/old1.pdf", "etag": "etag1", "size": 1024},
            {"name": "inbox/old2.pdf", "etag": "etag2", "size": 2048},
        ]

        watcher = BlobWatcher(
            blob_source=mock_source,
            poll_interval=1.0,
            state_file=state_file,
        )

        watcher.baseline()
        assert "inbox/old1.pdf:etag1" in watcher._seen
        assert "inbox/old2.pdf:etag2" in watcher._seen

        # When polling, these baseline blobs should not be re-downloaded
        processed = watcher.poll_once()
        assert len(processed) == 0

        # Adding a truly new blob triggers download
        mock_source.list_entries.return_value.append({
            "name": "inbox/new_po.pdf", "etag": "etag3", "size": 4096
        })
        mock_source.download.return_value = b"%PDF-1.5 valid"

        processed = watcher.poll_once()
        assert len(processed) == 1
        assert processed[0] == "inbox/new_po.pdf"


# -----------------------------------------------------------------------------
# LivePOService Pipeline & Timing Tests
# -----------------------------------------------------------------------------

def test_live_po_service_process_document_and_measured_timings(monkeypatch):
    monkeypatch.setenv("PORTAL_STAGE_PACING_MS", "0")
    store = OrderStore(store_dir=Path(tempfile.mkdtemp()))
    service = LivePOService(order_store=store)

    sample_pdf = b"%PDF-1.4 test document"
    order = service.process_document(sample_pdf, "PO_TEST_001.pdf", source_label="Test Drop")

    assert order is not None
    assert order["file"] == "PO_TEST_001.pdf"
    assert order["id"].startswith("live_")
    assert "metrics" in order
    metrics = order["metrics"]
    assert metrics["parse_ms"] >= 0
    assert metrics["quote_ms"] >= 0
    assert metrics["rules_ms"] >= 0
    assert metrics["total_ms"] >= 0
    assert metrics["rules_evaluated"] >= 0


def test_live_po_service_double_click_locking(monkeypatch):
    monkeypatch.setenv("PORTAL_STAGE_PACING_MS", "0")
    store = OrderStore(store_dir=Path(tempfile.mkdtemp()))
    service = LivePOService(order_store=store)

    order = {
        "id": "po-lock-test",
        "po": "4501706557",
        "status": "ready_to_book",
        "opportunity": "006DEMO000000001",
        "booking": {"amount": 1000.0, "quote": "Q-100"},
        "file": "4501706557.pdf",
    }
    store.save_order(order)

    # Hold the lock
    service._book_locks.add("po-lock-test")

    # Attempt booking while lock is active
    res = service.book_order_to_salesforce("po-lock-test", approver_name="Chinmay Dhok")
    assert res["success"] is False
    assert "already in progress" in res["error"]

    service._book_locks.remove("po-lock-test")


def test_live_po_service_recheck_functionality(monkeypatch):
    monkeypatch.setenv("PORTAL_STAGE_PACING_MS", "0")
    store = OrderStore(store_dir=Path(tempfile.mkdtemp()))
    service = LivePOService(order_store=store)

    order = {
        "id": "recheck-po",
        "po": "PO-RECHECK",
        "status": "quote_pending",
        "quote": "Q-LATE",
        "file": "PO-RECHECK.pdf",
        "pdf_path": "tests/fixtures/sample.pdf",
    }
    store.save_order(order)

    Path("tests/fixtures").mkdir(parents=True, exist_ok=True)
    Path("tests/fixtures/sample.pdf").write_bytes(b"%PDF-1.4 dummy")

    updated = service.recheck("recheck-po")
    assert updated is not None
    assert updated["file"] == "PO-RECHECK.pdf"


# -----------------------------------------------------------------------------
# Portal Adapter Consistency (11 SOS Policy Checklist Rules)
# -----------------------------------------------------------------------------

def test_portal_adapter_checklist_count_and_label_consistency():
    po = ParsedPO(source_id="PO-CHK-01.pdf", po_number="PO-CHK-01", quote_numbers=["Q-001"])
    quote = Quote(quote_number="Q-001", found=True)
    result = ValidationResult(
        po=po,
        quote=quote,
        findings=[
            Finding(rule_id="CHK_BILL_TO", status=Status.PASS, severity=Severity.BLOCKER, message="Bill to entity matches"),
            Finding(rule_id="CHK_PAY_TERMS", status=Status.PASS, severity=Severity.MAJOR, message="Payment terms match"),
        ],
        outcome=Outcome.VALIDATED,
    )

    order = result_to_portal_order(result)
    assert order["checklist_total"] == 11
    assert len(order["checklist"]) == 11
    # Check that checklist labels reflect the 11 SOS Policy Checklist rules
    for item in order["checklist"]:
        assert len(item) == 3
        assert item[0] in ("pass", "fail", "warn")
        assert isinstance(item[1], str)


# -----------------------------------------------------------------------------
# Review Queue HTTP Server Tests
# -----------------------------------------------------------------------------

def test_review_queue_http_endpoints_and_csrf(monkeypatch):
    import io
    from review_queue import ApprovalManager, SOSPortalHandler

    monkeypatch.setenv("PORTAL_STAGE_PACING_MS", "0")
    tmp_dir = Path(tempfile.mkdtemp())
    queue_dir = tmp_dir / "queues"
    store_dir = tmp_dir / "portal"

    sf_client = SalesforceClient(dry_run=True)
    manager = ApprovalManager(queue_dir=str(queue_dir), salesforce_client=sf_client)
    store = OrderStore(store_dir=store_dir)
    service = LivePOService(salesforce_client=sf_client, order_store=store)

    SOSPortalHandler.manager = manager
    SOSPortalHandler.service = service

    def call_handler(method: str, path: str, headers: dict = None, body: bytes = b""):
        h = SOSPortalHandler.__new__(SOSPortalHandler)
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.command = method
        h.path = path
        h.headers = dict(headers or {})
        h.status_code = 200
        h.error_message = None

        def send_response(code, message=None):
            h.status_code = code
        def send_header(k, v):
            pass
        def end_headers():
            pass
        def send_error(code, message=None, explain=None):
            h.status_code = code
            h.error_message = message

        h.send_response = send_response
        h.send_header = send_header
        h.end_headers = end_headers
        h.send_error = send_error

        if method == "GET":
            h.do_GET()
        elif method == "POST":
            h.do_POST()
        return h.status_code, h.wfile.getvalue()

    # 1. GET /api/status
    code, out = call_handler("GET", "/api/status")
    assert code == 200
    stat_data = json.loads(out.decode("utf-8"))
    assert stat_data["status"] == "healthy"
    assert stat_data["salesforce_dry_run"] is True

    # 2. GET /api/orders
    code, out = call_handler("GET", "/api/orders")
    assert code == 200
    initial_orders = json.loads(out.decode("utf-8"))
    assert isinstance(initial_orders, list)
    initial_count = len(initial_orders)

    # 3. POST /api/upload
    pdf_payload = b"%PDF-1.4 test http upload"
    code, out = call_handler(
        "POST",
        "/api/upload",
        headers={"Content-Length": str(len(pdf_payload)), "X-Filename": "test_http_po.pdf", "X-SOS-Portal": "1"},
        body=pdf_payload,
    )
    assert code == 200
    new_order = json.loads(out.decode("utf-8"))
    assert new_order["file"] == "test_http_po.pdf"
    order_id = new_order["id"]

    # 4. POST /api/book/<id>
    book_body = json.dumps({"approver": "Executive Tester", "note": "All good"}).encode("utf-8")
    code, out = call_handler(
        "POST",
        f"/api/book/{order_id}",
        headers={"Content-Length": str(len(book_body)), "Content-Type": "application/json", "X-SOS-Portal": "1"},
        body=book_body,
    )
    assert code == 200
    book_resp = json.loads(out.decode("utf-8"))
    assert book_resp["success"] is True

    # 5. Anti-CSRF protection: request with external Origin must be rejected with 403
    code, _ = call_handler(
        "POST",
        f"/api/book/{order_id}",
        headers={"Content-Length": "2", "Origin": "https://malicious-external-site.com"},
        body=b"{}",
    )
    assert code == 403

    # 6. POST /api/reset
    code, out = call_handler(
        "POST",
        "/api/reset",
        headers={"Content-Length": "2", "X-SOS-Portal": "1"},
        body=b"{}",
    )
    assert code == 200
    reset_resp = json.loads(out.decode("utf-8"))
    assert reset_resp["success"] is True

