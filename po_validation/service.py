"""Live backend service for RevOps SOS PO Review Portal.

Coordinates:
- Order storage & persistence (out/portal_orders/)
- Background Azure Blob Storage & Inbound folder watchers
- Server-Sent Events (SSE) broadcasting real-time ingestion & validation status
- 1-Click Salesforce Booking Form creation with PO PDF ContentVersion attachment
"""

from __future__ import annotations

import base64
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable, Optional

from .ai.client import load_env_file
from .act.salesforce import SalesforceClient
from .ingest.sources import Document
from .models import Outcome, ValidationResult
from .pipeline import Pipeline
from .report.portal_adapter import result_to_portal_order
from .validate.engine import load_engine

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Event Bus for Server-Sent Events (SSE)
# --------------------------------------------------------------------------

class EventBus:
    """Thread-safe publish-subscribe bus for real-time frontend notifications."""

    def __init__(self):
        self._listeners: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._history: list[dict[str, Any]] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def publish(self, event_type: str, data: dict[str, Any]):
        msg = {
            "type": event_type,
            "timestamp": datetime.now().isoformat(),
            "data": data,
        }
        with self._lock:
            self._history.append(msg)
            if len(self._history) > 100:
                self._history.pop(0)
            dead = []
            for q in self._listeners:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for d in dead:
                if d in self._listeners:
                    self._listeners.remove(d)

    def recent_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)


# --------------------------------------------------------------------------
# Order Store
# --------------------------------------------------------------------------

class OrderStore:
    """Stores and persists portal orders across live sessions."""

    def __init__(self, store_dir: Path = Path("out/portal_orders")):
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._orders: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load_from_disk()

    def _load_from_disk(self):
        for f in sorted(self.store_dir.glob("*.json")):
            try:
                order = json.loads(f.read_text(encoding="utf-8"))
                oid = order.get("id")
                if oid:
                    self._orders[oid] = order
            except Exception as e:
                log.warning("Could not load stored order %s: %s", f, e)

    def save_order(self, order: dict[str, Any]):
        oid = order.get("id")
        if not oid:
            return
        with self._lock:
            self._orders[oid] = order
        # Persist to disk
        try:
            safe_id = oid.replace("/", "_").replace("\\", "_")
            out_file = self.store_dir / f"{safe_id}.json"
            out_file.write_text(json.dumps(order, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("Could not persist order %s to disk: %s", oid, e)

    def get_order(self, target: str) -> Optional[dict[str, Any]]:
        with self._lock:
            if target in self._orders:
                return self._orders[target]
            # Search by PO number
            for o in self._orders.values():
                if o.get("po") == target or o.get("id") == target:
                    return o
        return None

    def list_orders(self) -> list[dict[str, Any]]:
        with self._lock:
            # Return live orders sorted with most recent at top
            return sorted(self._orders.values(), key=lambda x: x.get("received", ""), reverse=True)


# --------------------------------------------------------------------------
# Service Orchestrator
# --------------------------------------------------------------------------

class LivePOService:
    """Coordinates end-to-end ingestion, validation, and Salesforce booking."""

    def __init__(
        self,
        pipeline: Optional[Pipeline] = None,
        salesforce_client: Optional[SalesforceClient] = None,
        inbox_dir: Path = Path("inbound_pos"),
        order_store: Optional[OrderStore] = None,
    ):
        load_env_file()
        self.inbox_dir = inbox_dir
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.ingested_pdf_dir = Path("out/ingested_pos")
        self.ingested_pdf_dir.mkdir(parents=True, exist_ok=True)

        self.store = order_store or OrderStore()
        self.bus = EventBus()
        self.sf = salesforce_client or SalesforceClient.from_env()

        # Build pipeline if not supplied
        if pipeline:
            self.pipeline = pipeline
        else:
            self.pipeline = self._build_default_pipeline()

        self._running = False
        self._threads: list[threading.Thread] = []

    def _build_default_pipeline(self) -> Pipeline:
        from .validate.engine import load_engine
        from .resolve.base import StubQuoteSource
        engine = load_engine("sos")

        # Check for Snowflake Quote Source or Stub
        quote_source = None
        sf_acc = os.environ.get("SNOWFLAKE_ACCOUNT")
        if sf_acc:
            try:
                from .resolve.snowflake import SnowflakeQuoteSource
                quote_source = SnowflakeQuoteSource.from_env()
                log.info("Connected to Snowflake CPQ Quote Source (%s)", sf_acc)
            except Exception as e:
                log.warning("Could not init Snowflake Quote Source: %s. Using stub quotes fallback.", e)

        if quote_source is None:
            # Load stub quotes from my_quotes.json if present
            q_file = Path("my_quotes.json")
            if q_file.exists():
                from run_local import load_stub_quotes
                quote_source = load_stub_quotes(q_file)
            else:
                quote_source = StubQuoteSource({})

        return Pipeline(source=None, engine=engine, quote_source=quote_source, salesforce=self.sf)

    # -------------------------------------------------------- Ingestion & Run
    def process_document(
        self,
        doc_data: bytes,
        filename: str,
        source_label: str = "Inbound",
    ) -> dict[str, Any]:
        """Ingests a PO document, broadcasts live stages, runs validation, and stores the order."""
        stem = Path(filename).stem
        log.info("Processing inbound document '%s' (%d bytes) from %s", filename, len(doc_data), source_label)

        # Stage 1: Inbound Received
        self.bus.publish("pipeline_stage", {
            "stage": "received",
            "message": f"📥 Inbound PO Received: {filename} via {source_label}",
            "filename": filename,
            "source": source_label,
            "bytes": len(doc_data),
        })
        time.sleep(0.3)

        # Save binary PDF locally for serving / SFDC attachment
        pdf_path = self.ingested_pdf_dir / filename
        try:
            pdf_path.write_bytes(doc_data)
        except Exception as e:
            log.warning("Could not cache PDF to %s: %s", pdf_path, e)

        # Stage 2: Extracting Fields
        self.bus.publish("pipeline_stage", {
            "stage": "extracting",
            "message": f"⚙️ AI & Document Parser extracting header, 11 parties, line items & Inco terms...",
            "filename": filename,
        })
        time.sleep(0.4)

        # Stage 3: Resolving Quote
        self.bus.publish("pipeline_stage", {
            "stage": "reconciling",
            "message": f"❄️ Snowflake CPQ reconciling quote pricing, billing account & opportunity...",
            "filename": filename,
        })

        # Run Document through Pipeline
        doc = Document(source_id=str(pdf_path), data=doc_data)
        validation_result = self.pipeline.process_document(doc)

        # Stage 4: Policy Validation (11-Item SOS Policy & Quote checks)
        self.bus.publish("pipeline_stage", {
            "stage": "validating",
            "message": f"📋 Evaluated SOS Checklist (11 items) + F5 CPQ Quote Reconciliation...",
            "filename": filename,
            "outcome": validation_result.outcome.value,
        })
        time.sleep(0.3)

        # Convert to Portal Order structure
        order = result_to_portal_order(
            validation_result,
            doc_data=doc_data,
            doc_filename=filename,
            source_tag=source_label,
        )
        order["pdf_path"] = str(pdf_path)

        # Save to Store
        self.store.save_order(order)

        # Stage 5: Ready for Review
        self.bus.publish("order_ready", {
            "stage": "ready",
            "message": f"✨ PO #{order['po']} from {order['from']} ready for 1-Click Salesforce Booking!",
            "order": order,
        })

        return order

    # ------------------------------------------------------- Salesforce Booking
    def book_order_to_salesforce(
        self,
        order_id: str,
        approver_name: str = "SOS Specialist",
        audit_note: str = "",
    ) -> dict[str, Any]:
        """Creates the Booking_Form__c in Salesforce sandbox AND attaches the PO PDF via ContentVersion."""
        order = self.store.get_order(order_id)
        if not order:
            return {"success": False, "error": f"Order '{order_id}' not found in store."}

        po_num = order.get("po", "UNKNOWN")
        log.info("Booking order %s (PO #%s) to Salesforce...", order_id, po_num)

        # Prepare payload
        payload = dict(order.get("salesforce_payload") or {})
        if not payload:
            payload = {
                "Opportunity__c": order.get("opportunity"),
                "PO__c": po_num,
                "Total_Amount__c": (order.get("booking") or {}).get("amount", 0.0),
                "Sales_Order_Type__c": (order.get("booking") or {}).get("orderType", "Standard"),
            }

        # Notes
        notes = list(order.get("notes") or [])
        if audit_note:
            notes.append({"title": f"Note to RO: SOS Approver Sign-off ({approver_name})", "body": audit_note})

        # Load PDF bytes
        pdf_data = None
        pdf_path_str = order.get("pdf_path")
        if pdf_path_str and os.path.exists(pdf_path_str):
            try:
                pdf_data = Path(pdf_path_str).read_bytes()
            except Exception as pe:
                log.warning("Could not read cached PDF from %s: %s", pdf_path_str, pe)
        elif order.get("pdf_b64"):
            try:
                pdf_data = base64.b64decode(order["pdf_b64"])
            except Exception as pe:
                log.warning("Could not decode base64 PDF: %s", pe)

        pdf_filename = order.get("file") or f"PO_{po_num}.pdf"

        # Execute Booking Form Creation + PDF Attachment in Salesforce
        sf_res = self.sf.create_booking_form_from_payload(
            payload=payload,
            pdf_data=pdf_data,
            pdf_filename=pdf_filename,
            notes=notes,
        )

        if sf_res.get("success"):
            order["status"] = "booked"
            order["booking_result"] = sf_res
            order["booked_at"] = datetime.now().isoformat()
            order["booked_by"] = approver_name
            order["sfdc_record_id"] = sf_res.get("booking_form_id")
            order["sfdc_record_url"] = sf_res.get("url")
            order["sfdc_pdf_attached"] = sf_res.get("pdf_attached")
            self.store.save_order(order)

            # Broadcast update
            self.bus.publish("order_booked", {
                "order_id": order_id,
                "po": po_num,
                "booking_form_id": sf_res.get("booking_form_id"),
                "url": sf_res.get("url"),
                "pdf_attached": sf_res.get("pdf_attached"),
            })

            return {
                "success": True,
                "booking_form_id": sf_res.get("booking_form_id"),
                "url": sf_res.get("url"),
                "pdf_attached": sf_res.get("pdf_attached"),
                "attachment": sf_res.get("pdf_attachment"),
                "dry_run": sf_res.get("dry_run", self.sf.dry_run),
                "order": order,
            }
        else:
            return {
                "success": False,
                "error": sf_res.get("raw_response") or str(sf_res.get("errors")),
                "details": sf_res,
            }

    # ------------------------------------------------------- Background Watchers
    def _inbox_folder_worker(self):
        """Monitors local inbound_pos/ folder for dropped PDFs."""
        seen_files: set[str] = set()
        log.info("📁 Local Inbound Watcher active on %s/", self.inbox_dir)
        while self._running:
            try:
                for pdf in self.inbox_dir.glob("*.pdf"):
                    fname = pdf.name
                    if fname in seen_files:
                        continue
                    # Wait briefly for full copy
                    time.sleep(0.5)
                    data = pdf.read_bytes()
                    seen_files.add(fname)
                    self.process_document(data, fname, source_label="Email Inbound (Folder)")
            except Exception as exc:
                log.debug("Inbox worker tick: %s", exc)
            time.sleep(2.0)

    def _azure_blob_worker(self):
        """Monitors Azure Blob Storage container if credentials are provided."""
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        sas_token = os.environ.get("AZURE_STORAGE_SAS_TOKEN")
        container = os.environ.get("AZURE_STORAGE_CONTAINER", "purchaseorders")

        if conn_str and ("AccountName=..." in conn_str or "..." in conn_str):
            conn_str = None

        if not (conn_str or sas_token):
            log.info("ℹ️ Azure Blob credentials not configured; standing by (local inbound_pos/ poller active).")
            return

        from .ingest.blob import BlobSource, BlobWatcher
        try:
            blob_source = BlobSource(container=container, auth="connection_string" if conn_str else "sas_token")
            access = blob_source.check_access()
            if not access.get("ok"):
                log.warning("Azure Blob preflight failed: %s", access.get("detail"))
                return

            log.info("☁️ Azure Blob Watcher active on container '%s'", container)

            def handle_blob_result(result: ValidationResult, doc: Document):
                order = result_to_portal_order(result, doc_data=doc.data, doc_filename=doc.source_id, source_tag="Azure Blob Storage")
                self.store.save_order(order)
                self.bus.publish("order_ready", {"order": order, "stage": "ready"})

            def handle_blob_event(evt: dict):
                self.bus.publish("pipeline_stage", evt)

            watcher = BlobWatcher(
                blob_source=blob_source,
                pipeline=self.pipeline,
                poll_interval=4.0,
                on_result=handle_blob_result,
                on_event=handle_blob_event,
            )
            while self._running:
                watcher.scan_once()
                time.sleep(4.0)

        except Exception as exc:
            log.warning("Azure Blob Watcher stopped: %s", exc)

    def start_background_watchers(self):
        self._running = True
        t1 = threading.Thread(target=self._inbox_folder_worker, daemon=True, name="InboxWatcher")
        t1.start()
        self._threads.append(t1)

        t2 = threading.Thread(target=self._azure_blob_worker, daemon=True, name="AzureBlobWatcher")
        t2.start()
        self._threads.append(t2)
        log.info("🚀 Background Ingestion Watchers launched.")

    def stop_background_watchers(self):
        self._running = False
