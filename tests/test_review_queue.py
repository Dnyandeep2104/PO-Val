import json
from pathlib import Path
import pytest

from review_queue import ApprovalManager
from po_validation.act.salesforce import SalesforceClient


def test_approval_manager_lifecycle(tmp_path):
    queue_base = tmp_path / "queues"
    approval_dir = queue_base / "sos_approval_queue"
    approval_dir.mkdir(parents=True)

    # 1. Create a simulated item in sos_approval_queue
    item_data = {
        "po_number": "PO-999888",
        "quote_number": "F5Q-001122",
        "source_id": "incoming/PO-999888.pdf",
        "outcome": "VALIDATED",
        "failed_count": 0,
        "failures": [],
        "routed_at": "2026-09-28T12:00:00",
        "ready_for_approval": True,
    }
    item_file = approval_dir / "PO-999888_item.json"
    item_file.write_text(json.dumps(item_data))

    brief_file = approval_dir / "PO-999888_exception_brief.txt"
    brief_file.write_text("PO-999888 is clean and validated.")

    manager = ApprovalManager(queue_dir=queue_base, salesforce_client=SalesforceClient(dry_run=True))

    # 2. Test list_pending_approvals
    pending = manager.list_pending_approvals()
    assert len(pending) == 1
    assert pending[0]["po_number"] == "PO-999888"

    # 3. Test get_po_details
    details = manager.get_po_details("PO-999888")
    assert details["po_number"] == "PO-999888"
    assert "clean and validated" in details["brief"]

    # 4. Test approve_order
    res = manager.approve_order("PO-999888", approver_name="Jane Doe", notes="Verified line items")
    assert res["success"] is True
    assert "salesforce_id" in res
    assert res["dry_run"] is True

    # 5. Verify moved to approved directory
    assert not (approval_dir / "PO-999888_item.json").exists()
    approved_dir = queue_base / "approved"
    assert (approved_dir / "PO-999888_item.json").exists()
    assert (approved_dir / "PO-999888_approval_record.json").exists()

    record = json.loads((approved_dir / "PO-999888_approval_record.json").read_text())
    assert record["status"] == "APPROVED"
    assert record["approved_by"] == "Jane Doe"
    assert record["comments"] == "Verified line items"

    # 6. Approvals list should now be empty
    assert len(manager.list_pending_approvals()) == 0


def test_generate_dashboard_html(tmp_path):
    from review_queue import generate_dashboard_html
    manager = ApprovalManager(queue_dir=tmp_path / "queues")
    html_page = generate_dashboard_html(manager)
    assert "F5 Sales Operations (SOS)" in html_page
    assert "1-Click Approval" in html_page

