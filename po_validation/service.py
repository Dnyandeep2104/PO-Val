"""Live backend service for RevOps SOS PO Review Portal.

Coordinates:
- Order storage & persistence (out/portal/)
- Background Azure Blob Storage & Inbound folder watchers
- Server-Sent Events (SSE) broadcasting real-time ingestion & validation status
- 1-Click Salesforce Booking Form creation with PO PDF ContentVersion attachment
- Dedicated reviewer controls, exception sign-offs, and idempotency locks
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import time
from typing import Any, Callable, Optional

from .ai.client import load_env_file
from .act.salesforce import SalesforceClient
from .ingest.blob import BlobSource, BlobWatcher
from .ingest.sources import Document
from .models import Outcome, ValidationResult
from .pipeline import Pipeline
from .report.portal_adapter import result_to_portal_order
from .resolve.base import StubQuoteSource
from .validate.engine import load_engine

log = logging.getLogger(__name__)


def safe_filename(name: str) -> str:
    """Strip directories and non-safe characters from filenames."""
    bname = os.path.basename(name)
    cleaned = re.sub(r"[^\w\.\-\ ]", "_", bname)
    return cleaned.strip() or "order.pdf"


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
            "timestamp": datetime.now(timezone.utc).isoformat(),
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

    def __init__(self, store_dir: Path = Path("out/portal")):
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._orders: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load_from_disk()

    def _load_from_disk(self):
        # Also migrate from out/portal_orders if present
        legacy_dir = Path("out/portal_orders")
        candidate_dirs = [self.store_dir]
        if legacy_dir.is_dir() and legacy_dir != self.store_dir:
            candidate_dirs.append(legacy_dir)

        for sdir in candidate_dirs:
            for f in sorted(sdir.glob("*.json")):
                if f.name.startswith("blob_state"):
                    continue
                try:
                    order = json.loads(f.read_text(encoding="utf-8"))
                    oid = order.get("id")
                    if oid and oid not in self._orders:
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
            # Match by clean PO number or ID
            for o in self._orders.values():
                if o.get("po") == target or o.get("id") == target:
                    return o
            return None

    def list_orders(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._orders.values(), key=lambda x: x.get("received", ""), reverse=True)

    def clear(self):
        with self._lock:
            self._orders.clear()
            for f in self.store_dir.glob("*.json"):
                if f.name.startswith("blob_state"):
                    continue
                try:
                    f.unlink()
                except Exception:
                    pass


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
        backfill_blob: bool = False,
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

        self.pacing_ms = int(os.environ.get("PORTAL_STAGE_PACING_MS", "450"))
        self._running = False
        self._threads: list[threading.Thread] = []
        self._book_locks: set[str] = set()
        self._book_lock_mutex = threading.Lock()
        self.backfill_blob = backfill_blob

    def _build_default_pipeline(self) -> Pipeline:
        engine = load_engine("sos")

        # Live Snowflake quote source or stub fallback
        fallback_source = StubQuoteSource.from_json_file("my_quotes.json")
        quote_source = fallback_source
        sf_user = os.environ.get("SNOWFLAKE_USER", "").strip()
        if sf_user:
            try:
                from .resolve.snowflake_live import SnowflakeConnectorQuoteSource, FallbackQuoteSource
                live_source = SnowflakeConnectorQuoteSource.from_env()
                quote_source = FallbackQuoteSource(live_source, fallback_source)
                log.info("Configured live Snowflake quotes for user %s with fallback to my_quotes.json", sf_user)
            except Exception as e:
                log.warning("Could not init live Snowflake Quote Source: %s. Using stub quotes.", e)

        return Pipeline(source=None, engine=engine, quote_source=quote_source, salesforce=self.sf)

    # -------------------------------------------------------- Ingestion & Run
    def process_document(
        self,
        doc_data: bytes,
        filename: str,
        source_label: str = "Live Inbound",
        email_meta: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Ingests a PO document through the multi-stage validation engine."""
        fname = safe_filename(filename)
        pdf_path = self.ingested_pdf_dir / fname
        try:
            pdf_path.write_bytes(doc_data)
        except Exception as e:
            log.warning("Could not write ingested PDF to disk: %s", e)

        start_time = time.perf_counter()

        # Stage 1: Received
        from_hint = (email_meta or {}).get("from") or "Live PO Ingestion"
        self.bus.publish("inbound_received", {
            "stage": "received",
            "filename": fname,
            "source": source_label,
            "size": len(doc_data),
            "email_from": from_hint,
            "message": f"📥 Inbound PO received: {fname} ({len(doc_data):,} bytes)",
        })
        if self.pacing_ms > 0:
            time.sleep(self.pacing_ms / 1000.0)

        # Stage 2: Read PDF / Parse
        t_parse_start = time.perf_counter()
        meta = dict(email_meta or {})
        meta["source_type"] = "portal_upload"
        doc = Document(
            source_id=str(pdf_path),
            data=doc_data,
            modified=datetime.now(timezone.utc),
            metadata=meta,
        )
        parsed_po = self.pipeline.parse_document(doc)
        parse_ms = int((time.perf_counter() - t_parse_start) * 1000)

        po_num = parsed_po.po_number or "PO-DETECTING"
        self.bus.publish("stage_read_pdf", {
            "stage": "read_pdf",
            "po": po_num,
            "vendor": parsed_po.reseller_name or "Detected Vendor",
            "lines_count": len(parsed_po.line_items),
            "total": float(parsed_po.po_total or 0.0),
            "layout": parsed_po.layout,
            "latency_ms": parse_ms,
            "message": f"📄 Parsed PDF: layout '{parsed_po.layout}' ({len(parsed_po.line_items)} lines, ${parsed_po.po_total or 0:,.2f})",
        })
        if self.pacing_ms > 0:
            time.sleep(self.pacing_ms / 1000.0)

        # Stage 3: Quote Lookup
        t_quote_start = time.perf_counter()
        quote = self.pipeline.resolve_quote(parsed_po)
        quote_ms = int((time.perf_counter() - t_quote_start) * 1000)

        self.bus.publish("stage_quote_lookup", {
            "stage": "quote_lookup",
            "po": po_num,
            "quote": quote.quote_number,
            "quote_found": quote.found,
            "quote_source": getattr(quote, "source", "lookup"),
            "opportunity": quote.opportunity_id or "Not Linked",
            "latency_ms": quote_ms,
            "message": f"🔍 Quote {quote.quote_number}: {'Found in ' + getattr(quote, 'source', 'Snowflake') if quote.found else 'Not Found'}",
        })
        if self.pacing_ms > 0:
            time.sleep(self.pacing_ms / 1000.0)

        # Stage 4: Run Checklist & Policy Engine
        t_rules_start = time.perf_counter()
        validation_result = self.pipeline.run_checks(parsed_po, quote)
        rules_ms = int((time.perf_counter() - t_rules_start) * 1000)
        total_ms = int((time.perf_counter() - start_time) * 1000)

        # Convert to Portal Order structure
        order = result_to_portal_order(
            validation_result,
            doc_data=doc_data,
            doc_filename=fname,
            source_tag=source_label,
        )
        order["pdf_path"] = str(pdf_path)
        order["latency_ms"] = total_ms
        order["timings"] = {
            "parse_ms": parse_ms,
            "quote_ms": quote_ms,
            "rules_ms": rules_ms,
            "total_ms": total_ms,
            "rules_evaluated": len(validation_result.findings),
        }
        order["metrics"] = order["timings"]
        if email_meta:
            order["email_metadata"] = email_meta

        # Save to Store
        self.store.save_order(order)

        # Stage 5: Ready for Review
        flagged_count = order.get("flagged_count", 0)
        if flagged_count == 0:
            msg = f"✨ PO #{order['po']} passed all rules! Ready for 1-Click Salesforce Booking."
        else:
            msg = f"⚠️ PO #{order['po']} received ({flagged_count} item(s) need SOS review)."

        self.bus.publish("order_ready", {
            "stage": "ready",
            "message": msg,
            "order": order,
            "total_ms": total_ms,
        })

        return order

    def recheck(self, order_id: str) -> Optional[dict[str, Any]]:
        """Re-runs validation for an existing order (useful if CPQ replication completed)."""
        order = self.store.get_order(order_id)
        if not order:
            return None
        pdf_path = order.get("pdf_path")
        if not pdf_path or not os.path.exists(pdf_path):
            return None
        data = Path(pdf_path).read_bytes()
        filename = order.get("file") or Path(pdf_path).name
        meta = order.get("email_metadata")
        return self.process_document(data, filename, source_label="Re-Checked", email_meta=meta)

    # ------------------------------------------------------- Salesforce Booking
    def book_order_to_salesforce(
        self,
        order_id: str,
        approver_name: str = "SOS Specialist",
        audit_note: str = "",
        reviewer_edits: Optional[dict] = None,
        acknowledged_flags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Creates the Booking_Form__c in Salesforce sandbox AND attaches the PO PDF via ContentVersion."""
        with self._book_lock_mutex:
            if order_id in self._book_locks:
                return {"success": False, "error": f"Booking already in progress for order {order_id}."}
            self._book_locks.add(order_id)

        try:
            order = self.store.get_order(order_id)
            if not order:
                return {"success": False, "error": f"Order '{order_id}' not found in store."}

            po_num = order.get("po", "UNKNOWN")
            log.info("Booking order %s (PO #%s) to Salesforce (approver: %s)...", order_id, po_num, approver_name)

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

            # Execute Booking Form Creation + PDF Attachment in Salesforce
            sf_res = self.sf.book_portal_order(
                order=order,
                approver_name=approver_name,
                reviewer_edits=reviewer_edits,
                notes_to_ro=audit_note,
                pdf_data=pdf_data,
                acknowledged_flags=acknowledged_flags,
            )

            if sf_res.get("success"):
                order["status"] = "booked"
                order["booking_result"] = sf_res
                order["salesforce_booking"] = sf_res
                order["booked_at"] = datetime.now(timezone.utc).isoformat()
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
                    "approver": approver_name,
                })

                return {
                    "success": True,
                    "booking_form_id": sf_res.get("booking_form_id"),
                    "url": sf_res.get("url"),
                    "pdf_attached": sf_res.get("pdf_attached"),
                    "dry_run": sf_res.get("dry_run", self.sf.dry_run),
                    "order": order,
                    "details": sf_res,
                }
            else:
                return {
                    "success": False,
                    "error": sf_res.get("error") or "Salesforce booking failed.",
                    "details": sf_res,
                }
        except Exception as exc:
            log.error("Salesforce booking exception for %s: %s", order_id, exc, exc_info=True)
            return {
                "success": False,
                "error": f"Salesforce booking error: {str(exc)}",
            }
        finally:
            with self._book_lock_mutex:
                self._book_locks.discard(order_id)

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
        """Monitors Azure Blob Storage container for incoming POs."""
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
        sas_token = os.environ.get("AZURE_STORAGE_SAS_TOKEN", "")

        # Skip if placeholder or unconfigured
        if not conn_str and not sas_token:
            log.info("ℹ️ Azure Blob Storage unconfigured. To enable live email intake, set AZURE_STORAGE_SAS_TOKEN or AZURE_STORAGE_CONNECTION_STRING in .env.")
            return
        if "AccountName=..." in conn_str or "..." in sas_token:
            log.info("ℹ️ Azure Blob Storage contains template placeholders. Running local folder intake only.")
            return

        auth_method = "sas_token" if sas_token else "connection_string"
        prefix = os.environ.get("AZURE_STORAGE_PREFIX", "inbox/")
        poll_sec = float(os.environ.get("AZURE_POLL_SECONDS", "4.0"))

        try:
            blob_source = BlobSource(auth=auth_method, prefix=prefix)
            chk = blob_source.check_access()
            if not chk.get("ok"):
                log.warning("Azure Blob preflight failed: %s", chk.get("detail"))
                return

            def on_blob_pdf(name, data, meta):
                self.process_document(
                    doc_data=data,
                    filename=os.path.basename(name),
                    source_label="Azure Blob (Power Automate)",
                    email_meta=meta,
                )

            watcher = BlobWatcher(
                blob_source=blob_source,
                on_pdf=on_blob_pdf,
                poll_interval=poll_sec,
                backfill=self.backfill_blob,
            )
            log.info("☁️ Azure Blob Watcher active on '%s/%s' (polling every %.1fs)", blob_source.container, prefix, poll_sec)
            watcher.baseline()

            while self._running:
                watcher.poll_once()
                time.sleep(poll_sec)
        except Exception as exc:
            log.warning("Azure Blob watcher error: %s", exc)

    def start_background_watchers(self):
        """Starts background intake threads for local folder and Azure Blob."""
        self._running = True
        t_folder = threading.Thread(target=self._inbox_folder_worker, daemon=True, name="InboxFolderWatcher")
        t_blob = threading.Thread(target=self._azure_blob_worker, daemon=True, name="AzureBlobWatcher")
        self._threads = [t_folder, t_blob]
        for t in self._threads:
            t.start()

    def stop_background_watchers(self):
        self._running = False

    def preload(self, folder: Path | str) -> int:
        """Preloads sample POs from a folder so the queue isn't empty."""
        p = Path(folder)
        if not p.is_dir():
            return 0
        loaded = 0
        for pdf in sorted(p.glob("*.pdf")):
            try:
                data = pdf.read_bytes()
                self.process_document(data, pdf.name, source_label="Preloaded Sample")
                loaded += 1
            except Exception as exc:
                log.warning("Could not preload %s: %s", pdf, exc)
        return loaded

    def reset_portal_state(self):
        """Clears all orders from store and disk for a fresh demo."""
        self.store.clear()
        state_file = Path("out/portal/blob_state.json")
        if state_file.is_file():
            try:
                state_file.unlink()
            except Exception:
                pass

    def system_status(self) -> dict[str, Any]:
        """Provides status summary for preflight and portal dashboard."""
        sf_pre = self.sf.preflight()
        q_source = self.pipeline.quote_source
        q_mode = getattr(q_source, "name", "unknown")

        return {
            "salesforce": sf_pre,
            "quotes": {
                "ok": True,
                "mode": q_mode,
                "detail": f"Quote engine active ({q_mode})",
            },
            "inbound_folder": {
                "ok": True,
                "path": str(self.inbox_dir),
            },
            "orders_count": len(self.store.list_orders()),
        }
