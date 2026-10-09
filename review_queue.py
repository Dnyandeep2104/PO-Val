#!/usr/bin/env python3
"""SOS Purchase Order Review & 1-Click Approval Tool.

Allows Sales Operations (SOS) specialists to inspect validated orders,
review line items and auto-drafted RO notes, and execute 1-click booking
approvals to Salesforce (poclab sandbox or dry-run).

Supports:
  1. CLI List & Approve:
     python3 review_queue.py --list
     python3 review_queue.py --approve <PO_NUMBER>
     python3 review_queue.py --interactive

  2. Local Web Dashboard:
     python3 review_queue.py --serve --port 8080
"""

from __future__ import annotations

import argparse
from datetime import datetime
import html
import http.server
import json
import logging
import os
from pathlib import Path
import queue
import socketserver
import sys
from typing import Any, Optional
import urllib.parse

from po_validation.act.salesforce import SalesforceClient
from po_validation.service import LivePOService

log = logging.getLogger("sos_review")


class ApprovalManager:
    """Manages the lifecycle of orders in the SOS approval and review queues."""

    def __init__(self, queue_dir: str | Path = "out/queues",
                 salesforce_client: Optional[SalesforceClient] = None):
        self.queue_dir = Path(queue_dir)
        self.approval_dir = self.queue_dir / "sos_approval_queue"
        self.review_dir = self.queue_dir / "sos_review_queue"
        self.reseller_dir = self.queue_dir / "reseller_response_queue"
        self.approved_dir = self.queue_dir / "approved"

        self.approval_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self.approved_dir.mkdir(parents=True, exist_ok=True)

        self.sf = salesforce_client or SalesforceClient(dry_run=True)

    def list_pending_approvals(self) -> list[dict[str, Any]]:
        """Return list of PO items waiting in the approval queue."""
        items = []
        for item_path in sorted(self.approval_dir.glob("*_item.json")):
            try:
                data = json.loads(item_path.read_text())
                data["_file"] = str(item_path)
                data["_po_stem"] = item_path.stem.replace("_item", "")
                items.append(data)
            except Exception as e:
                log.warning("Could not read %s: %s", item_path, e)
        return items

    def list_review_exceptions(self) -> list[dict[str, Any]]:
        """Return list of PO items waiting in the review queue."""
        items = []
        for item_path in sorted(self.review_dir.glob("*_item.json")):
            try:
                data = json.loads(item_path.read_text())
                data["_file"] = str(item_path)
                data["_po_stem"] = item_path.stem.replace("_item", "")
                items.append(data)
            except Exception as e:
                log.warning("Could not read %s: %s", item_path, e)
        return items

    def _find_item_file(self, target: str, directory: Path) -> Optional[Path]:
        """Find item file matching exact stem, item_key, or po_number."""
        stem = target.replace(" ", "_")
        direct = directory / f"{stem}_item.json"
        if direct.exists():
            return direct
        # Look for composite key matches: {target}_*_item.json
        for f in sorted(directory.glob(f"{stem}_*_item.json")):
            return f
        # Scan json contents for po_number == target or item_key == target
        for f in sorted(directory.glob("*_item.json")):
            try:
                d = json.loads(f.read_text())
                if d.get("item_key") == target or d.get("po_number") == target:
                    return f
            except Exception:
                pass
        return None

    def get_po_details(self, target: str) -> dict[str, Any]:
        """Load all artifacts for a specific PO or key across queues."""
        for q_dir in (self.approval_dir, self.review_dir, self.approved_dir):
            item_file = self._find_item_file(target, q_dir)
            if item_file and item_file.exists():
                data = json.loads(item_file.read_text())
                stem = item_file.stem.replace("_item", "")
                brief_file = q_dir / f"{stem}_exception_brief.txt"
                if brief_file.exists():
                    data["brief"] = brief_file.read_text()
                html_file = q_dir / f"{stem}_review.html"
                if html_file.exists():
                    data["html_preview"] = html_file.read_text()
                draft_file = self.reseller_dir / f"{stem}_draft_reseller_reply.txt"
                if not draft_file.exists():
                    base_po = stem.rsplit("_", 1)[0]
                    draft_file = self.reseller_dir / f"{base_po}_draft_reseller_reply.txt"
                if draft_file.exists():
                    data["reseller_draft"] = draft_file.read_text()
                # Check for booking form payload
                bf_file = Path("out/booking_forms") / f"{stem}_salesforce_booking_form.json"
                if not bf_file.exists():
                    base_po = stem.rsplit("_", 1)[0]
                    bf_file = Path("out/booking_forms") / f"{base_po}_salesforce_booking_form.json"
                if bf_file.exists():
                    data["booking_form"] = json.loads(bf_file.read_text())
                return data
        return {}

    def approve_order(self, target: str, approver_name: str = "SOS Specialist",
                      notes: str = "Approved via local review queue") -> dict[str, Any]:
        """Approve a validated PO, submit to Salesforce, and move to approved/."""
        item_file = self._find_item_file(target, self.approval_dir)
        if not item_file or not item_file.exists():
            return {
                "success": False,
                "error": f"PO/Key '{target}' not found in sos_approval_queue."
            }

        stem = item_file.stem.replace("_item", "")
        item_data = json.loads(item_file.read_text())
        po_num = item_data.get("po_number") or stem.rsplit("_", 1)[0]

        # Load booking form payload
        bf_file = Path("out/booking_forms") / f"{stem}_salesforce_booking_form.json"
        if not bf_file.exists():
            base_po = stem.rsplit("_", 1)[0]
            alt_file = Path("out/booking_forms") / f"{base_po}_salesforce_booking_form.json"
            if alt_file.exists():
                bf_file = alt_file

        booking_payload = {}
        if bf_file.exists():
            try:
                booking_payload = json.loads(bf_file.read_text())
            except Exception:
                booking_payload = {}

        if not booking_payload:
            if "booking_payload" in item_data:
                booking_payload = item_data["booking_payload"]
            else:
                return {
                    "success": False,
                    "error": f"Booking form payload not found for '{target}'. Refusing approval without valid Booking Form."
                }

        # Commit to Salesforce
        approval_time = datetime.now().isoformat()
        sf_result = {
            "attempted": True,
            "dry_run": self.sf.dry_run,
            "simulated_id": f"a1BPo000000{abs(hash(po_num)) % 1000000:06d}XYZ",
            "opportunity_id": booking_payload.get("Opportunity__c"),
            "amount": booking_payload.get("Total_Amount__c"),
            "account": booking_payload.get("End_User_Account_Name__c"),
        }

        # If live credentials available, execute write
        if not self.sf.dry_run:
            try:
                # In live mode, POST directly to Salesforce instance
                sf_resp = self.sf.create_booking_form_from_payload(booking_payload)
                sf_result["salesforce_id"] = sf_resp.get("booking_form_id")
                sf_result["live"] = True
            except Exception as exc:
                log.error("Salesforce commit failed: %s", exc)
                return {"success": False, "error": f"Salesforce commit failed: {exc}"}
        else:
            sf_result["salesforce_id"] = sf_result["simulated_id"]

        # Record approval manifest
        record = {
            "item_key": stem,
            "po_number": po_num,
            "status": "APPROVED",
            "approved_by": approver_name,
            "approved_at": approval_time,
            "comments": notes,
            "salesforce_booking_id": sf_result.get("salesforce_id"),
            "dry_run": self.sf.dry_run,
            "booking_payload": booking_payload,
        }
        record_file = self.approved_dir / f"{stem}_approval_record.json"
        record_file.write_text(json.dumps(record, indent=2))

        # Move all related item files from approval_dir to approved_dir
        for f in self.approval_dir.glob(f"{stem}_*"):
            dest = self.approved_dir / f.name
            f.rename(dest)

        return {
            "success": True,
            "po_number": po_num,
            "item_key": stem,
            "salesforce_id": sf_result.get("salesforce_id"),
            "dry_run": self.sf.dry_run,
            "approved_at": approval_time,
            "record_file": str(record_file),
        }


# ----------------------------------------------------------- Local Web Server

def generate_dashboard_html(manager: ApprovalManager) -> str:
    """Generate modern, responsive HTML dashboard for SOS specialists."""
    approvals = manager.list_pending_approvals()
    reviews = manager.list_review_exceptions()

    approvals_rows = ""
    for a in approvals:
        po = html.escape(str(a.get("po_number") or "-"))
        key = html.escape(str(a.get("item_key") or a.get("_po_stem") or po))
        quote = html.escape(str(a.get("quote_number") or "-"))
        source = html.escape(str(a.get("source_id", "").split("/")[-1]))
        approvals_rows += f"""
        <tr>
            <td><strong style="color:#0f52ba;">{po}</strong></td>
            <td>{quote}</td>
            <td><code>{source}</code></td>
            <td><span class="badge badge-success">VALIDATED</span></td>
            <td style="text-align:right;">
                <button class="btn btn-primary" data-key="{key}" onclick="approvePO(this.dataset.key)">⚡ 1-Click Approve</button>
                <button class="btn btn-secondary" data-key="{key}" onclick="viewDetails(this.dataset.key)">View Details</button>
            </td>
        </tr>
        """
    if not approvals:
        approvals_rows = '<tr><td colspan="5" class="empty">No orders currently waiting in the approval queue. Clean orders will appear here automatically.</td></tr>'

    reviews_rows = ""
    for r in reviews:
        po = html.escape(str(r.get("po_number") or "-"))
        key = html.escape(str(r.get("item_key") or r.get("_po_stem") or po))
        quote = html.escape(str(r.get("quote_number") or "-"))
        outcome = html.escape(str(r.get("outcome") or "NEEDS_REVIEW"))
        failures = r.get("failures", [])
        failure_summary = html.escape(failures[0]["msg"]) if failures else "Requires manual review"
        badge_cls = "badge-danger" if outcome == "REJECTED" else "badge-warning"
        reviews_rows += f"""
        <tr>
            <td><strong>{po}</strong></td>
            <td>{quote}</td>
            <td><span class="badge {badge_cls}">{outcome}</span></td>
            <td style="max-width:380px;font-size:12px;">{failure_summary}</td>
            <td style="text-align:right;">
                <button class="btn btn-secondary" data-key="{key}" onclick="viewDetails(this.dataset.key)">Review Issues</button>
            </td>
        </tr>
        """
    if not reviews:
        reviews_rows = '<tr><td colspan="5" class="empty">No exceptions currently waiting for review.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>F5 SOS Purchase Order Validation & Approval Portal</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: #f4f6f9;
            margin: 0;
            padding: 24px;
            color: #212529;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #002f6c;
            color: #fff;
            padding: 16px 24px;
            border-radius: 8px;
            margin-bottom: 24px;
        }}
        .header h1 {{ margin: 0; font-size: 20px; }}
        .header .env-badge {{ background: #28a745; padding: 4px 10px; border-radius: 4px; font-size: 12px; font-weight: bold; }}
        .container {{ display: grid; grid-template-columns: 1fr; gap: 24px; }}
        .card {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); padding: 20px; }}
        .card h2 {{ margin-top: 0; font-size: 16px; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 14px; }}
        th {{ background: #fafafa; font-weight: 600; color: #555; }}
        .badge {{ padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; text-transform: uppercase; }}
        .badge-success {{ background: #d4edda; color: #155724; }}
        .badge-warning {{ background: #fff3cd; color: #856404; }}
        .badge-danger {{ background: #f8d7da; color: #721c24; }}
        .btn {{ border: none; padding: 6px 14px; border-radius: 4px; font-size: 13px; font-weight: 600; cursor: pointer; transition: 0.2s; }}
        .btn-primary {{ background: #0070d2; color: #fff; }}
        .btn-primary:hover {{ background: #005fb2; }}
        .btn-secondary {{ background: #e0e5ee; color: #333; margin-left: 6px; }}
        .btn-secondary:hover {{ background: #d0d7e5; }}
        .empty {{ text-align: center; color: #777; padding: 24px; font-style: italic; }}
        #modal {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); align-items: center; justify-content: center; }}
        #modalContent {{ background: #fff; width: 80%; max-width: 850px; max-height: 85vh; border-radius: 8px; overflow-y: auto; padding: 24px; }}
        pre {{ background: #f8f9fa; padding: 12px; border-radius: 4px; font-size: 12px; overflow-x: auto; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>F5 Sales Operations (SOS) — Order Review & Booking Portal</h1>
            <div style="font-size: 12px; opacity: 0.8; margin-top: 4px;">Automated Validation, Exception Routing, and 1-Click Salesforce Booking</div>
        </div>
        <div style="display: flex; align-items: center; gap: 10px;">
            <a href="/prototype" style="background: #ff3a50; color: #fff; text-decoration: none; padding: 6px 12px; border-radius: 4px; font-size: 12px; font-weight: bold;">✨ Interactive SOS Prototype</a>
            <span class="env-badge">Local Sandbox Mode (poclab)</span>
        </div>
    </div>

    <div class="container">
        <!-- Ready for Approval -->
        <div class="card">
            <h2>⚡ Orders Ready for 1-Click Approval ({len(approvals)})</h2>
            <table>
                <thead>
                    <tr>
                        <th>PO Number</th>
                        <th>Quote Number</th>
                        <th>Source File</th>
                        <th>Status</th>
                        <th style="text-align:right;">Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {approvals_rows}
                </tbody>
            </table>
        </div>

        <!-- Exception Queue -->
        <div class="card">
            <h2>⚠️ Exceptions & Discrepancies Requiring Review ({len(reviews)})</h2>
            <table>
                <thead>
                    <tr>
                        <th>PO Number</th>
                        <th>Quote Number</th>
                        <th>Outcome</th>
                        <th>Primary Discrepancy</th>
                        <th style="text-align:right;">Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {reviews_rows}
                </tbody>
            </table>
        </div>
    </div>

    <!-- Modal for details -->
    <div id="modal">
        <div id="modalContent">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 id="modalTitle" style="margin:0;">Order Details</h3>
                <button onclick="closeModal()" class="btn btn-secondary">✕ Close</button>
            </div>
            <hr style="border:none; border-top:1px solid #eee; margin:16px 0;">
            <div id="modalBody"></div>
        </div>
    </div>

    <script>
        function approvePO(targetKey) {{
            if (!confirm("Approve purchase order " + targetKey + " and commit Booking Form to Salesforce?")) return;
            fetch('/api/approve/' + encodeURIComponent(targetKey), {{
                method: 'POST',
                headers: {{ 'X-Requested-With': 'XMLHttpRequest' }}
            }})
                .then(r => r.json())
                .then(data => {{
                    if (data.success) {{
                        alert("✅ PO " + (data.po_number || targetKey) + " successfully approved!\nSalesforce Booking Form ID: " + data.salesforce_id);
                        window.location.reload();
                    }} else {{
                        alert("❌ Approval failed: " + data.error);
                    }}
                }})
                .catch(err => alert("Error: " + err));
        }}

        function viewDetails(targetKey) {{
            fetch('/api/details/' + encodeURIComponent(targetKey))
                .then(r => r.json())
                .then(data => {{
                    document.getElementById('modalTitle').innerText = "Details for " + (data.po_number || targetKey);
                    let html = "";
                    if (data.brief) {{
                        html += "<h4>Validation Brief</h4><pre>" + escapeHtml(data.brief) + "</pre>";
                    }}
                    if (data.reseller_draft) {{
                        html += "<h4>Auto-Drafted Reseller Discrepancy Email</h4><pre>" + escapeHtml(data.reseller_draft) + "</pre>";
                    }}
                    if (data.booking_form) {{
                        html += "<h4>Salesforce Booking Form Payload</h4><pre>" + escapeHtml(JSON.stringify(data.booking_form, null, 2)) + "</pre>";
                    }}
                    document.getElementById('modalBody').innerHTML = html;
                    document.getElementById('modal').style.display = 'flex';
                }});
        }}

        function closeModal() {{
            document.getElementById('modal').style.display = 'none';
        }}

        function escapeHtml(str) {{
            return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        }}
    </script>
</body>
</html>"""


class SOSPortalHandler(http.server.SimpleHTTPRequestHandler):
    """Handles HTTP requests for the SOS web approval portal."""

    manager: ApprovalManager = None
    service: Optional[LivePOService] = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. Primary Modern Portal UI
        if path in ("/", "/index.html", "/prototype", "/prototype.html", "/demo"):
            proto_file = Path(__file__).parent / "sos_review_prototype.html"
            if proto_file.exists():
                content = proto_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        # 2. Legacy Table View (fallback)
        if path in ("/legacy", "/table"):
            content = generate_dashboard_html(self.manager).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        # 3. List Ingested Orders
        if path == "/api/orders":
            orders = self.service.store.list_orders() if self.service else []
            resp = json.dumps(orders).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        # 4. Server-Sent Events (SSE) Live Stream
        if path == "/api/stream":
            if not self.service:
                self.send_error(503, "Live service unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = self.service.bus.subscribe()
            try:
                init_msg = json.dumps({
                    "stage": "connected",
                    "message": "Connected to SOS Live Pipeline Stream",
                    "timestamp": datetime.now().isoformat(),
                })
                self.wfile.write(f"data: {init_msg}\n\n".encode("utf-8"))
                self.wfile.flush()

                while True:
                    try:
                        evt = q.get(timeout=10.0)
                        data_str = json.dumps(evt)
                        self.wfile.write(f"data: {data_str}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.service.bus.unsubscribe(q)
            return

        # 5. Fallback polling for events
        if path == "/api/stream/events":
            events = self.service.bus.recent_events() if self.service else []
            resp = json.dumps(events).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        # 6. Stream/Download PO PDF
        if path.startswith("/api/pdf/"):
            target = urllib.parse.unquote(path.replace("/api/pdf/", ""))
            order = self.service.store.get_order(target) if self.service else None
            pdf_bytes = None
            if order and order.get("pdf_path") and os.path.exists(order["pdf_path"]):
                pdf_bytes = Path(order["pdf_path"]).read_bytes()
            elif order and order.get("pdf_b64"):
                pdf_bytes = base64.b64decode(order["pdf_b64"])

            if pdf_bytes:
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'inline; filename="{order.get("file", "order.pdf")}"')
                self.send_header("Content-Length", str(len(pdf_bytes)))
                self.end_headers()
                self.wfile.write(pdf_bytes)
                return
            self.send_error(404, "PDF Not Found")
            return

        # 7. System Health & Status Checks
        if path in ("/api/health", "/api/status"):
            stat = self.service.system_status() if self.service else {}
            health = {
                "status": "healthy",
                "azure_blob": os.environ.get("AZURE_STORAGE_CONNECTION_STRING") is not None or os.environ.get("AZURE_STORAGE_CONTAINER") is not None,
                "salesforce_dry_run": self.manager.sf.dry_run,
                "salesforce_instance": self.manager.sf.instance_url,
                "orders_count": len(self.service.store.list_orders()) if self.service else 0,
                **stat,
            }
            resp = json.dumps(health).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        # 8. Queue details
        if path.startswith("/api/details/"):
            target = urllib.parse.unquote(path.replace("/api/details/", ""))
            details = self.manager.get_po_details(target)
            resp = json.dumps(details).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        # Anti-CSRF Check: Reject cross-origin requests
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if origin:
            parsed_origin = urllib.parse.urlparse(origin)
            if parsed_origin.hostname not in ("localhost", "127.0.0.1"):
                self.send_error(403, "Forbidden: Cross-Origin Request Blocked")
                return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. 1-Click Salesforce Booking (with PDF Attachment)
        if path.startswith("/api/book/"):
            target = urllib.parse.unquote(path.replace("/api/book/", ""))
            content_length = int(self.headers.get("Content-Length", 0))
            body_json = {}
            if content_length > 0:
                try:
                    body_json = json.loads(self.rfile.read(content_length).decode("utf-8"))
                except Exception:
                    pass

            approver = body_json.get("approver") or os.environ.get("PORTAL_APPROVER_NAME", "Chinmay Dhok (SOS Specialist)")
            note = body_json.get("note") or body_json.get("notes_to_ro", "")
            edits = body_json.get("reviewer_edits")
            flags = body_json.get("acknowledged_flags")

            if self.service and self.service.store.get_order(target):
                result = self.service.book_order_to_salesforce(
                    target,
                    approver_name=approver,
                    audit_note=note,
                    reviewer_edits=edits,
                    acknowledged_flags=flags,
                )
            else:
                result = self.manager.approve_order(target, approver_name=approver, notes=note)

            resp = json.dumps(result).encode("utf-8")
            self.send_response(200 if result.get("success") else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        # 2. Re-check Quote Route
        if path.startswith("/api/recheck/"):
            target = urllib.parse.unquote(path.replace("/api/recheck/", ""))
            if self.service:
                updated = self.service.recheck(target)
                if updated:
                    resp = json.dumps(updated).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    return
            self.send_error(404, "Order not found or recheck unavailable")
            return

        # 3. Reset Demo State
        if path == "/api/reset":
            if self.service:
                self.service.reset_portal_state()
            resp = json.dumps({"success": True, "message": "Portal state reset."}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        # 2. Direct Inbound PO Upload
        if path == "/api/upload":
            content_length = int(self.headers.get("Content-Length", 0))
            filename = self.headers.get("X-Filename") or f"PO_Inbound_{datetime.now().strftime('%H%M%S')}.pdf"
            raw_bytes = self.rfile.read(content_length) if content_length > 0 else b""

            # Seamless demo simulation fallback if small stub payload passed
            if len(raw_bytes) < 500 and "4501706557" in filename:
                sample_path = Path("my_pos/4501706557 1 1 2.pdf")
                if sample_path.exists():
                    raw_bytes = sample_path.read_bytes()

            if not raw_bytes:
                self.send_error(400, "Empty upload payload")
                return

            # Extract PDF bytes if multipart
            if b"%PDF-" in raw_bytes:
                start = raw_bytes.find(b"%PDF-")
                end = raw_bytes.rfind(b"%%EOF")
                if end != -1:
                    pdf_bytes = raw_bytes[start:end + 5]
                else:
                    pdf_bytes = raw_bytes[start:]
            else:
                pdf_bytes = raw_bytes

            if not self.service:
                self.send_error(503, "Live service unavailable")
                return

            order = self.service.process_document(pdf_bytes, filename, source_label="Direct Inbound Upload")
            resp = json.dumps(order).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        # Legacy approve route
        if path.startswith("/api/approve/"):
            target = urllib.parse.unquote(path.replace("/api/approve/", ""))
            result = self.manager.approve_order(target)
            resp = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        self.send_error(404, "Not Found")


def run_preflight(live: bool = False) -> int:
    """Validate connectivity and readiness of all external dependencies."""
    from po_validation.ai.client import load_env_file
    load_env_file()
    is_live = live or os.environ.get("SF_LIVE_MODE", "false").lower() in ("true", "1", "yes")
    all_ok = True
    print("\n🔍 Running RevOps SOS Live Pre-Flight Checks...")

    # 1. Salesforce
    sf = SalesforceClient.from_env(dry_run=(not is_live))
    sf_stat = sf.preflight()
    if sf_stat.get("ok"):
        print(f"  \033[32mOK\033[0m  Salesforce   {sf_stat.get('detail')}")
    else:
        print(f"  \033[31m!!\033[0m  Salesforce   {sf_stat.get('detail')}")
        all_ok = False

    # 2. Quotes
    sf_user = os.environ.get("SNOWFLAKE_USER", "").strip()
    if sf_user:
        try:
            from po_validation.resolve.snowflake_live import SnowflakeConnectorQuoteSource
            src = SnowflakeConnectorQuoteSource.from_env()
            q_stat = src.preflight()
            if q_stat.get("ok"):
                print(f"  \033[32mOK\033[0m  Quotes       {q_stat.get('detail')}")
            else:
                print(f"  \033[33m!!\033[0m  Quotes       Snowflake live failed ({q_stat.get('detail')}); fallback to my_quotes.json ready.")
        except Exception as e:
            print(f"  \033[33m!!\033[0m  Quotes       Snowflake live check error ({e}); fallback to my_quotes.json.")
    else:
        qf = Path("my_quotes.json")
        if qf.is_file():
            print(f"  \033[32mOK\033[0m  Quotes       Offline export 'my_quotes.json' ready.")
        else:
            print(f"  \033[31m!!\033[0m  Quotes       Neither SNOWFLAKE_USER nor my_quotes.json found.")
            all_ok = False

    # 3. Azure Blob
    sas = os.environ.get("AZURE_STORAGE_SAS_TOKEN", "").strip()
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    has_valid_sas = sas and "..." not in sas
    has_valid_conn = conn and "AccountName=..." not in conn and "..." not in conn
    if has_valid_sas or has_valid_conn:
        try:
            from po_validation.ingest.blob import BlobSource
            auth = "sas_token" if has_valid_sas else "connection_string"
            bs = BlobSource(auth=auth, prefix=os.environ.get("AZURE_STORAGE_PREFIX", "inbox/"))
            b_stat = bs.check_access()
            if b_stat.get("ok"):
                print(f"  \033[32mOK\033[0m  Azure Blob   {b_stat.get('detail')}")
            else:
                print(f"  \033[31m!!\033[0m  Azure Blob   {b_stat.get('detail')}")
                all_ok = False
        except Exception as e:
            print(f"  \033[31m!!\033[0m  Azure Blob   Check failed: {e}")
            all_ok = False
    else:
        print("  \033[33m--\033[0m  Azure Blob   Unconfigured / placeholder in .env (Set AZURE_STORAGE_SAS_TOKEN for live email intake).")

    # 4. Folder
    inbox = Path("inbound_pos")
    inbox.mkdir(parents=True, exist_ok=True)
    print(f"  \033[32mOK\033[0m  Folder       Drop PDFs into {inbox}/")

    print()
    return 0 if all_ok else 1


def run_server(
    manager: ApprovalManager,
    port: int = 8080,
    live_service: Optional[LivePOService] = None,
    preload_folder: Optional[str] = None,
    backfill: bool = False,
):
    if live_service is None:
        live_service = LivePOService(salesforce_client=manager.sf, backfill_blob=backfill)
    if preload_folder:
        loaded = live_service.preload(preload_folder)
        log.info("Preloaded %d sample order(s) from %s", loaded, preload_folder)
    live_service.start_background_watchers()

    SOSPortalHandler.manager = manager
    SOSPortalHandler.service = live_service

    # Bind strictly to localhost (127.0.0.1) using ThreadingHTTPServer for SSE concurrency
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), SOSPortalHandler) as httpd:
        sf_mode = "LIVE (Sandbox poclab)" if not manager.sf.dry_run else "SIMULATION (Dry-Run / Demo-Safe)"
        print(f"\n🚀 RevOps SOS Executive Portal running at http://127.0.0.1:{port}")
        print(f"   • Dashboard UI:     http://127.0.0.1:{port}/")
        print(f"   • Real-Time Stream: http://127.0.0.1:{port}/api/stream")
        print(f"   • Salesforce Mode:  {sf_mode}")
        print(f"   • Inbound Watcher:  Polling inbound_pos/ and Azure Blob Storage")
        print("   Press Ctrl+C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            live_service.stop_background_watchers()


# ---------------------------------------------------------------- CLI Main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SOS Review & Approval Tool")
    ap.add_argument("--queue-dir", default="out/queues", help="Queue base directory (default: out/queues)")
    ap.add_argument("--list", action="store_true", help="List all pending approvals and review exceptions")
    ap.add_argument("--approve", metavar="PO_NUMBER", help="Approve specified PO number and commit to Salesforce")
    ap.add_argument("--live", action="store_true", help="Execute write against live Salesforce sandbox (requires auth)")
    ap.add_argument("--interactive", action="store_true", help="Step through pending approvals interactively")
    ap.add_argument("--serve", action="store_true", help="Launch local web dashboard")
    ap.add_argument("--port", type=int, default=8080, help="Web dashboard port (default: 8080)")
    ap.add_argument("--preflight", action="store_true", help="Run preflight checks against Salesforce, Snowflake, and Azure Blob")
    ap.add_argument("--reset", action="store_true", help="Clear portal orders and Azure blob state")
    ap.add_argument("--preload", metavar="DIR", help="Preload sample PO PDFs from directory before starting server")
    ap.add_argument("--backfill", action="store_true", help="Process existing blobs in container")
    args = ap.parse_args(argv)

    if args.preflight:
        return run_preflight(live=args.live)

    if args.reset:
        srv = LivePOService()
        srv.reset_portal_state()
        print("✅ Portal orders and Azure blob state cleared.")
        return 0

    is_live = args.live or os.environ.get("SF_LIVE_MODE", "false").lower() in ("true", "1", "yes")
    sf_client = SalesforceClient.from_env(dry_run=(not is_live))
    manager = ApprovalManager(queue_dir=args.queue_dir, salesforce_client=sf_client)

    if args.serve:
        run_server(manager, port=args.port, preload_folder=args.preload, backfill=args.backfill)
        return 0

    if args.approve:
        res = manager.approve_order(args.approve)
        if res["success"]:
            print(f"✅ PO #{args.approve} approved successfully!")
            print(f"   Salesforce Booking Form ID: {res['salesforce_id']}")
            print(f"   Audit Record saved: {res['record_file']}")
            return 0
        else:
            print(f"❌ Approval failed: {res.get('error')}")
            return 1

    if args.interactive:
        pending = manager.list_pending_approvals()
        if not pending:
            print("No pending orders waiting for approval in sos_approval_queue.")
            return 0
        print(f"Found {len(pending)} order(s) waiting for approval:\n")
        for item in pending:
            po = item.get("po_number")
            quote = item.get("quote_number")
            print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            print(f"  PO Number: {po}   Quote: {quote}")
            print(f"  File: {item.get('source_id')}")
            choice = input("  Action: [A]pprove & Book | [S]kip | [V]iew Details | [Q]uit > ").strip().lower()
            if choice == "a":
                res = manager.approve_order(po)
                if res["success"]:
                    print(f"  ✅ Approved! Salesforce ID: {res['salesforce_id']}\n")
                else:
                    print(f"  ❌ Approval failed: {res.get('error')}\n")
            elif choice == "v":
                details = manager.get_po_details(po)
                print("\n--- Validation Brief ---")
                print(details.get("brief", "(No brief)"))
            elif choice == "q":
                break
        return 0

    # Default or --list: print queue summary
    approvals = manager.list_pending_approvals()
    reviews = manager.list_review_exceptions()

    print(f"\n📋 SOS Approval Queue ({len(approvals)} orders ready for 1-click booking):")
    if approvals:
        for a in approvals:
            print(f"   [VALIDATED] PO #{a.get('po_number')} (Quote: {a.get('quote_number')}) - {a.get('source_id')}")
    else:
        print("   (Empty)")

    print(f"\n⚠️  SOS Review Queue ({len(reviews)} orders requiring specialist review):")
    if reviews:
        for r in reviews:
            fails = len(r.get("failures", []))
            print(f"   [{r.get('outcome')}] PO #{r.get('po_number')} (Quote: {r.get('quote_number')}) - {fails} issue(s)")
    else:
        print("   (Empty)")

    print("\nNext Steps:")
    print("  • To approve an order:   python3 review_queue.py --approve <PO_NUMBER>")
    print("  • To review in browser:  python3 review_queue.py --serve --port 8080")
    print("  • Interactive CLI mode:  python3 review_queue.py --interactive\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
