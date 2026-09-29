from pathlib import Path
import pytest

from po_validation.models import (
    Finding,
    Outcome,
    ParsedPO,
    Quote,
    Severity,
    Status,
    ValidationResult,
)
from po_validation.report.email import render_reseller_response_email
from po_validation.report.writer import ExceptionRouter, LocalQueueSink


def test_render_reseller_response_email():
    po = ParsedPO(
        source_id="carahsoft_order.pdf",
        po_number="PO-778899",
        quote_number="F5Q-123456",
        layout="carahsoft",
    )
    quote = Quote(quote_number="F5Q-123456", found=True)
    findings = [
        Finding(
            rule_id="line_items_match",
            status=Status.FAIL,
            severity=Severity.BLOCKER,
            message="Part SKU-ABC: PO unit price $500 does not match quote $400",
        ),
        Finding(
            rule_id="parser_confidence",
            status=Status.FAIL,
            severity=Severity.BLOCKER,
            message="Parser confidence 0.4 below threshold 0.85",
        )
    ]
    result = ValidationResult(
        po=po,
        quote=quote,
        findings=findings,
        outcome=Outcome.REJECTED,
        checklist="sos",
    )

    draft = render_reseller_response_email(result)
    assert "PO-778899" in draft
    assert "F5Q-123456" in draft
    assert "SKU-ABC" in draft
    assert "Carahsoft" in draft
    assert "Required Next Steps:" in draft
    assert "[BLOCKER]" not in draft
    assert "Parser confidence" not in draft


def test_local_queue_sink_and_exception_router(tmp_path):
    queue_base = tmp_path / "queues"
    routing = {
        "REJECTED": {
            "create_booking_form": False,
            "notify": ["sos_review_queue", "reseller_response_queue"],
        },
        "VALIDATED": {
            "create_booking_form": False,
            "notify": ["sos_approval_queue"],
        },
    }

    queues = {
        "sos_review_queue": [LocalQueueSink("sos_review_queue", base_dir=queue_base)],
        "reseller_response_queue": [LocalQueueSink("reseller_response_queue", base_dir=queue_base)],
        "sos_approval_queue": [LocalQueueSink("sos_approval_queue", base_dir=queue_base)],
    }

    router = ExceptionRouter(sinks=[], routing=routing, queues=queues)

    # 1. Test REJECTED outcome routing
    po_rejected = ParsedPO(
        source_id="test_po_1.pdf",
        po_number="PO-REJECT-01",
        quote_number="Q-01",
        layout="synnex",
    )
    result_rejected = ValidationResult(
        po=po_rejected,
        quote=None,
        findings=[
            Finding(
                rule_id="quote_exists",
                status=Status.FAIL,
                severity=Severity.BLOCKER,
                message="Quote Q-01 not found",
            )
        ],
        outcome=Outcome.REJECTED,
        checklist="sos",
    )

    notified = router.route(result_rejected)
    assert "sos_review_queue" in notified
    assert "reseller_response_queue" in notified

    # Verify files created in sos_review_queue
    review_dir = queue_base / "sos_review_queue"
    assert (review_dir / "PO-REJECT-01_exception_brief.txt").exists()
    assert (review_dir / "PO-REJECT-01_review.html").exists()
    assert (review_dir / "PO-REJECT-01_item.json").exists()

    # Verify files created in reseller_response_queue
    reseller_dir = queue_base / "reseller_response_queue"
    assert (reseller_dir / "PO-REJECT-01_draft_reseller_reply.txt").exists()
    reply_content = (reseller_dir / "PO-REJECT-01_draft_reseller_reply.txt").read_text()
    assert "Quote Q-01 not found" in reply_content

    # 2. Test VALIDATED outcome routing
    po_valid = ParsedPO(
        source_id="test_po_2.pdf",
        po_number="PO-VALID-02",
        quote_number="Q-02",
        layout="carahsoft",
    )
    result_valid = ValidationResult(
        po=po_valid,
        quote=Quote(quote_number="Q-02", found=True),
        findings=[],
        outcome=Outcome.VALIDATED,
        checklist="sos",
    )

    notified_valid = router.route(result_valid)
    assert "sos_approval_queue" in notified_valid

    approval_dir = queue_base / "sos_approval_queue"
    assert (approval_dir / "PO-VALID-02_item.json").exists()
    assert (approval_dir / "PO-VALID-02_exception_brief.txt").exists()
