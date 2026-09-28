import tempfile
import time
from pathlib import Path
import pytest

from po_validation.ingest.watcher import FolderWatcher, is_file_ready
from po_validation.ingest.ledger import JsonLedger
from po_validation.ingest.sources import Document
from po_validation.models import Outcome, ParsedPO, ValidationResult
from po_validation.pipeline import Pipeline
from po_validation.validate.engine import Engine, Checklist


class DummyChecklist:
    name = "dummy"
    spec = {"routing": {}}
    rules = []


class DummyEngine:
    checklist = DummyChecklist()

    def run(self, po, quote=None):
        return ValidationResult(
            po=po,
            quote=quote,
            outcome=Outcome.VALIDATED,
            checklist="dummy"
        )


def test_is_file_ready(tmp_path):
    # 1. Non-existent file
    assert not is_file_ready(tmp_path / "nonexistent.pdf")

    # 2. Empty file
    empty_file = tmp_path / "empty.pdf"
    empty_file.write_bytes(b"")
    assert not is_file_ready(empty_file)

    # 3. Non-PDF file
    text_file = tmp_path / "text.txt"
    text_file.write_bytes(b"Hello world this is not a pdf")
    assert not is_file_ready(text_file)

    # 4. Valid PDF file
    valid_pdf = tmp_path / "valid.pdf"
    valid_pdf.write_bytes(b"%PDF-1.5 fake pdf content data")
    assert is_file_ready(valid_pdf, wait_sec=0.01, retries=1)


def test_folder_watcher_scan(tmp_path):
    watch_dir = tmp_path / "incoming"
    watch_dir.mkdir()
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    ledger_path = tmp_path / "ledger.jsonl"

    ledger = JsonLedger(ledger_path)
    engine = DummyEngine()
    pipeline = Pipeline(source=None, engine=engine, ledger=ledger)

    results_received = []

    def on_result(res):
        results_received.append(res)

    watcher = FolderWatcher(
        watch_dir=watch_dir,
        pipeline=pipeline,
        poll_interval=0.1,
        processed_dir=processed_dir,
        on_result=on_result,
    )

    # Write a test PDF
    pdf_file = watch_dir / "order_123.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 test purchase order contents")

    # Scan once
    results = watcher.scan_once()
    assert len(results) == 1
    assert results[0].outcome == Outcome.VALIDATED
    assert len(results_received) == 1

    # File should have moved to processed_dir
    assert not pdf_file.exists()
    assert (processed_dir / "order_123.pdf").exists()

    # Scanning again should find nothing
    results_second = watcher.scan_once()
    assert len(results_second) == 0


def test_folder_watcher_deduplication(tmp_path):
    watch_dir = tmp_path / "watch_no_move"
    watch_dir.mkdir()
    ledger_path = tmp_path / "ledger.jsonl"

    ledger = JsonLedger(ledger_path)
    engine = DummyEngine()
    pipeline = Pipeline(source=None, engine=engine, ledger=ledger)

    watcher = FolderWatcher(
        watch_dir=watch_dir,
        pipeline=pipeline,
        poll_interval=0.1,
    )

    pdf_file = watch_dir / "order_abc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 unique purchase order contents")

    # First scan processes the file
    results = watcher.scan_once()
    assert len(results) == 1

    # Second scan leaves file in place but skips processing via ledger
    results_second = watcher.scan_once()
    assert len(results_second) == 0
    assert pdf_file.exists()
