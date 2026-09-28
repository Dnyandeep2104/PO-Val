"""Where PDFs come from.

Local folder today, Azure Blob once the storage account access ticket
clears. Both yield the same Document, so the pipeline does not know or
care which one is wired up. Flip it with one config value.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..models import sha256_bytes

log = logging.getLogger(__name__)


@dataclass
class Document:
    source_id: str
    data: bytes
    modified: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return sha256_bytes(self.data)


class DocumentSource(ABC):
    name = "base"

    @abstractmethod
    def list_documents(self) -> Iterator[Document]:
        ...

    def list_new(self, ledger=None) -> Iterator[Document]:
        """Idempotency lives here.

        Prady wants this on a 15-minute schedule. Without a ledger, every
        run reprocesses the entire container and every clean PO creates a
        duplicate booking form. Hash-based rather than name-based, so a
        reseller resending under a new filename is still caught.
        """
        for doc in self.list_documents():
            if ledger is not None and ledger.seen(doc.content_hash):
                log.debug("skipping already-processed %s", doc.source_id)
                continue
            yield doc


class LocalFolderSource(DocumentSource):
    """For development and for the Databricks Workspace path the prototype
    used. Prady's suggestion: run against an uploaded PDF while access is
    pending."""

    name = "local"

    def __init__(self, folder: str | Path, pattern: str = "*.pdf", recursive: bool = False):
        self.folder = Path(folder)
        self.pattern = pattern
        self.recursive = recursive

    def list_documents(self) -> Iterator[Document]:
        if not self.folder.exists():
            log.warning("source folder does not exist: %s", self.folder)
            return
        globber = self.folder.rglob if self.recursive else self.folder.glob
        for path in sorted(globber(self.pattern)):
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except Exception as exc:
                log.error("could not read %s: %s", path, exc)
                continue
            yield Document(
                source_id=str(path),
                data=data,
                modified=datetime.fromtimestamp(path.stat().st_mtime),
                metadata={"size": path.stat().st_size},
            )
