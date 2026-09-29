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

    # 4. Incomplete/truncated PDF file (missing %%EOF)
    truncated_pdf = tmp_path / "truncated.pdf"
    truncated_pdf.write_bytes(b"%PDF-1.5 fake pdf content data that has not finished writing")
    assert not is_file_ready(truncated_pdf, wait_sec=0.01, retries=1)

    # 5. Valid PDF file (complete with %%EOF)
    valid_pdf = tmp_path / "valid.pdf"
    valid_pdf.write_bytes(b"%PDF-1.5 fake pdf content data\n%%EOF\n")
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
    pdf_file.write_bytes(b"%PDF-1.4 test purchase order contents\n%%EOF\n")

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
    pdf_file.write_bytes(b"%PDF-1.4 unique purchase order contents\n%%EOF\n")

    # First scan processes the file
    results = watcher.scan_once()
    assert len(results) == 1

    # Second scan leaves file in place but skips processing via ledger
    results_second = watcher.scan_once()
    assert len(results_second) == 0
    assert pdf_file.exists()


def test_folder_watcher_deferral_skips_during_window(tmp_path):
    """Verify watcher delays re-processing DEFERRED documents until window expires."""
    watch_dir = tmp_path / "watch_defer"
    watch_dir.mkdir()

    class DeferringEngine:
        checklist = DummyChecklist()
        calls = 0
        def run(self, po, quote=None):
            self.calls += 1
            return ValidationResult(
                po=po,
                quote=None,
                outcome=Outcome.DEFERRED,
                checklist="dummy"
            )

    engine = DeferringEngine()
    pipeline = Pipeline(source=None, engine=engine, ledger=JsonLedger(tmp_path / "ledger.jsonl"))
    watcher = FolderWatcher(
        watch_dir=watch_dir,
        pipeline=pipeline,
        deferral_delay=300.0,  # 5 minutes
    )

    pdf = watch_dir / "order_defer.pdf"
    pdf.write_bytes(b"%PDF-1.4 deferral test contents\n%%EOF\n")

    # First scan: processes once and sets deferral timestamp
    res1 = watcher.scan_once()
    assert len(res1) == 1
    assert engine.calls == 1

    # Second scan immediate: file is skipped because deferral window is active!
    res2 = watcher.scan_once()
    assert len(res2) == 0
    assert engine.calls == 1  # Not called again!


def test_folder_watcher_collision_avoidance(tmp_path):
    """Verify watcher appends content hash to prevent overwriting existing files."""
    watch_dir = tmp_path / "watch_col"
    watch_dir.mkdir()
    proc_dir = tmp_path / "proc_col"
    proc_dir.mkdir()

    # Create an existing file in processed_dir with the same name
    existing = proc_dir / "order_same.pdf"
    existing.write_bytes(b"existing file in destination")

    engine = DummyEngine()
    pipeline = Pipeline(source=None, engine=engine, ledger=JsonLedger(tmp_path / "ledger.jsonl"))
    watcher = FolderWatcher(
        watch_dir=watch_dir,
        pipeline=pipeline,
        processed_dir=proc_dir,
    )

    incoming = watch_dir / "order_same.pdf"
    incoming.write_bytes(b"%PDF-1.4 new different order contents\n%%EOF\n")

    results = watcher.scan_once()
    assert len(results) == 1
    assert not incoming.exists()
    assert existing.read_bytes() == b"existing file in destination"
    # Disambiguated file was created
    matches = list(proc_dir.glob("order_same_*.pdf"))
    assert len(matches) == 1


def test_folder_watcher_unhandled_error_moves_to_failed(tmp_path):
    """Verify unexpected processing error is caught and moves file to failed_dir."""
    watch_dir = tmp_path / "watch_err"
    watch_dir.mkdir()
    failed_dir = tmp_path / "failed_err"
    failed_dir.mkdir()

    class CrashingPipeline:
        ledger = JsonLedger(tmp_path / "ledger.jsonl")
        def process_document(self, doc):
            raise RuntimeError("Unexpected memory corruption in OCR")

    watcher = FolderWatcher(
        watch_dir=watch_dir,
        pipeline=CrashingPipeline(),
        failed_dir=failed_dir,
    )

    incoming = watch_dir / "corrupted.pdf"
    incoming.write_bytes(b"%PDF-1.4 will trigger crash\n%%EOF\n")

    results = watcher.scan_once()
    assert len(results) == 0
    assert not incoming.exists()
    assert (failed_dir / "corrupted.pdf").exists()

