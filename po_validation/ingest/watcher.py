"""Automatic Ingestion Watcher.

Monitors an incoming folder for purchase order PDFs, verifies file
stability (to avoid race conditions with in-progress file writes or copies),
and feeds new documents to the Pipeline automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Callable, Optional
from datetime import datetime

from .sources import Document
from ..pipeline import Pipeline
from ..models import ValidationResult

log = logging.getLogger(__name__)


def is_file_ready(path: Path, wait_sec: float = 0.2, retries: int = 2) -> bool:
    """Ensure file exists, has a valid PDF header, has %%EOF marker, and has stabilized in size."""
    if not path.is_file():
        return False
    try:
        initial_size = path.stat().st_size
        if initial_size < 10:
            return False
        # Verify valid PDF header
        with open(path, "rb") as f:
            header = f.read(5)
            if header != b"%PDF-":
                return False
            # Check for %%EOF in the last 1024 bytes if file has sufficient length
            if initial_size >= 20:
                f.seek(max(0, initial_size - 1024))
                tail = f.read()
                if b"%%EOF" not in tail:
                    return False
        # Verify write has completed (size is stable)
        for _ in range(retries):
            time.sleep(wait_sec)
            current_size = path.stat().st_size
            if current_size != initial_size:
                initial_size = current_size
            else:
                return True
    except Exception as e:
        log.debug("File %s not ready: %s", path, e)
        return False
    return True


class FolderWatcher:
    """Continuously or periodically watches a folder for new incoming PO documents."""

    def __init__(
        self,
        watch_dir: str | Path,
        pipeline: Pipeline,
        pattern: str = "*.pdf",
        poll_interval: float = 2.0,
        deferral_delay: float = 60.0,
        processed_dir: Optional[str | Path] = None,
        failed_dir: Optional[str | Path] = None,
        on_result: Optional[Callable[[ValidationResult], None]] = None,
    ):
        self.watch_dir = Path(watch_dir)
        self.pipeline = pipeline
        self.pattern = pattern
        self.poll_interval = poll_interval
        self.deferral_delay = deferral_delay
        self.processed_dir = Path(processed_dir) if processed_dir else None
        self.failed_dir = Path(failed_dir) if failed_dir else None
        self.on_result = on_result
        self._running = False

        # State tracking for deferrals and caching
        self._deferred_until: dict[str, float] = {}  # content_hash -> timestamp when retry is allowed
        self._stat_cache: dict[Path, tuple[float, int, str]] = {}  # path -> (mtime, size, content_hash)

        if self.processed_dir:
            self.processed_dir.mkdir(parents=True, exist_ok=True)
        if self.failed_dir:
            self.failed_dir.mkdir(parents=True, exist_ok=True)

    def _get_dest_path(self, target_dir: Path, pdf_path: Path, doc: Optional[Document] = None) -> Path:
        """Return collision-safe destination path, disambiguating with content hash if target exists."""
        dest = target_dir / pdf_path.name
        if not dest.exists():
            return dest
        hash_stem = doc.content_hash[:8] if (doc and doc.content_hash) else "dup"
        dest_hashed = target_dir / f"{pdf_path.stem}_{hash_stem}{pdf_path.suffix}"
        if not dest_hashed.exists():
            return dest_hashed
        return target_dir / f"{pdf_path.stem}_{hash_stem}_{int(time.time() * 1000)}{pdf_path.suffix}"

    def scan_once(self) -> list[ValidationResult]:
        """Scan directory once and process any new, stable files."""
        results = []
        if not self.watch_dir.exists():
            log.warning("Watch directory does not exist: %s", self.watch_dir)
            return results

        for pdf_path in sorted(self.watch_dir.glob(self.pattern)):
            if not pdf_path.is_file():
                continue

            # Skip files located inside destination directories
            if self.processed_dir and pdf_path.parent.resolve() == self.processed_dir.resolve():
                continue
            if self.failed_dir and pdf_path.parent.resolve() == self.failed_dir.resolve():
                continue

            try:
                st = pdf_path.stat()
            except Exception:
                continue

            # Check stat cache to avoid re-reading unchanged files repeatedly
            cached = self._stat_cache.get(pdf_path)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                cached_hash = cached[2]
                if self.pipeline.ledger.seen(cached_hash):
                    continue
                if self._deferred_until.get(cached_hash, 0) > time.time():
                    log.debug("Skipping deferred PO %s (deferral active)", pdf_path.name)
                    continue

            # Ensure file is completely written and has valid PDF %%EOF
            if not is_file_ready(pdf_path, wait_sec=0.1, retries=1):
                log.debug("Skipping unready/incomplete file: %s", pdf_path)
                continue

            try:
                data = pdf_path.read_bytes()
            except Exception as exc:
                log.error("Could not read %s: %s", pdf_path, exc)
                continue

            doc = Document(
                source_id=str(pdf_path),
                data=data,
                modified=datetime.fromtimestamp(st.st_mtime),
                metadata={"size": st.st_size},
            )
            content_hash = doc.content_hash
            self._stat_cache[pdf_path] = (st.st_mtime, st.st_size, content_hash)

            # Check deduplication ledger
            if self.pipeline.ledger.seen(content_hash):
                log.debug("Skipping already processed file: %s", pdf_path.name)
                continue

            # Check time-based deferral
            if self._deferred_until.get(content_hash, 0) > time.time():
                log.debug("Skipping deferred PO %s (waiting for retry window)", pdf_path.name)
                continue

            log.info("📥 Ingesting new purchase order: %s (%d bytes)", pdf_path.name, len(data))
            try:
                result = self.pipeline.process_document(doc)
                results.append(result)

                if self.on_result:
                    try:
                        self.on_result(result)
                    except Exception as exc:
                        log.error("on_result callback error: %s", exc)

                # Move processed / failed files if directory targets are configured
                if self.processed_dir and result.outcome.value in ("VALIDATED", "NEEDS_REVIEW", "REJECTED"):
                    dest = self._get_dest_path(self.processed_dir, pdf_path, doc)
                    log.info("Moving processed PO %s -> %s", pdf_path.name, dest)
                    pdf_path.rename(dest)
                    self._stat_cache.pop(pdf_path, None)
                elif self.failed_dir and result.outcome.value == "EXTRACTION_FAILED":
                    dest = self._get_dest_path(self.failed_dir, pdf_path, doc)
                    log.info("Moving failed PO %s -> %s", pdf_path.name, dest)
                    pdf_path.rename(dest)
                    self._stat_cache.pop(pdf_path, None)
                elif result.outcome.value == "DEFERRED":
                    self._deferred_until[content_hash] = time.time() + self.deferral_delay
                    log.info("PO %s deferred. Scheduled retry in %.0fs.", pdf_path.name, self.deferral_delay)

            except Exception as exc:
                log.error("Unhandled error processing PO %s: %s", pdf_path.name, exc)
                if self.failed_dir:
                    dest = self._get_dest_path(self.failed_dir, pdf_path, doc)
                    log.warning("Moving crashed PO %s -> %s", pdf_path.name, dest)
                    try:
                        pdf_path.rename(dest)
                        self._stat_cache.pop(pdf_path, None)
                    except Exception as m_exc:
                        log.error("Could not move crashed file %s: %s", pdf_path, m_exc)

        return results

    def start(self, max_polls: Optional[int] = None):
        """Start continuous watching loop until stopped or interrupted."""
        self._running = True
        log.info(
            "🚀 Folder Watcher started on '%s' (polling every %.1fs)...",
            self.watch_dir,
            self.poll_interval,
        )
        polls = 0
        try:
            while self._running:
                self.scan_once()
                polls += 1
                if max_polls is not None and polls >= max_polls:
                    break
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            log.info("Folder Watcher stopped by user.")
        finally:
            self._running = False

    def stop(self):
        """Signal watcher loop to stop."""
        self._running = False
