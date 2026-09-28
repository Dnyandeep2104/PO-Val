"""The processing ledger.

Two jobs:
  1. Idempotency. A 15-minute schedule over a landing container will see
     the same blob forever. Content-hash keyed, so a resend under a new
     filename is still recognised.
  2. Duplicate-order detection. Same PO number, different document. That
     is a business exception, not a technical one, and the not_duplicate
     rule surfaces it.

JsonLedger for local development. DeltaLedger for Databricks, where the
table is also the audit trail and the thing you build a dashboard on.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# Outcomes that end a document's life. Anything else means we expect to
# look at it again.
TERMINAL = {"VALIDATED", "NEEDS_REVIEW", "REJECTED", "DUPLICATE"}


class Ledger(ABC):
    @abstractmethod
    def seen(self, content_hash: str) -> bool:
        """True only when the document reached a terminal outcome.

        A DEFERRED document must not be skipped on the next run — that is
        the whole point of deferring it."""

    @abstractmethod
    def record(self, entry: dict) -> None: ...

    @abstractmethod
    def find_po_number(self, po_number: str,
                       exclude_hash: str = "") -> list[dict]: ...

    def attempts(self, content_hash: str) -> int:
        """How many times we have already looked at this document."""
        return 0

    def first_seen(self, content_hash: str) -> Optional[datetime]:
        return None

    def record_result(self, result) -> None:
        d = result.to_dict()
        self.record({
            "content_hash": d["content_hash"],
            "source_id": d["source_id"],
            "po_number": d["po_number"],
            "quote_number": d["quote_number"],
            "outcome": d["outcome"],
            "checklist": d["checklist"],
            "run_id": d["run_id"],
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "n_failed": d["n_failed"],
            "opportunity_id": d["opportunity_id"],
            "booking_form_id": (d.get("action") or {}).get("booking_form_id"),
        })


class JsonLedger(Ledger):
    """Append-only JSONL. Fine to a few hundred thousand rows."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._entries: Optional[list[dict]] = None

    def _load(self) -> list[dict]:
        if self._entries is None:
            self._entries = []
            if self.path.exists():
                with open(self.path) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            log.warning("skipping malformed ledger line")
        return self._entries

    def _records(self, content_hash: str) -> list[dict]:
        return [e for e in self._load() if e.get("content_hash") == content_hash]

    def seen(self, content_hash: str) -> bool:
        if not content_hash:
            return False
        recs = self._records(content_hash)
        return bool(recs) and recs[-1].get("outcome") in TERMINAL

    def attempts(self, content_hash: str) -> int:
        return len(self._records(content_hash)) if content_hash else 0

    def first_seen(self, content_hash: str) -> Optional[datetime]:
        recs = self._records(content_hash)
        if not recs:
            return None
        try:
            return datetime.fromisoformat(recs[0]["processed_at"])
        except (KeyError, ValueError):
            return None

    def record(self, entry: dict) -> None:
        with self._lock:
            self._load().append(entry)
            with open(self.path, "a") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")

    def find_po_number(self, po_number: str, exclude_hash: str = "") -> list[dict]:
        if not po_number:
            return []
        return [e for e in self._load()
                if e.get("po_number") == po_number
                and e.get("content_hash") != exclude_hash]

    def reset(self) -> None:
        """Development convenience. Never call this on the real ledger."""
        if self.path.exists():
            self.path.unlink()
        self._entries = None


class DeltaLedger(Ledger):
    """Databricks. The table doubles as the audit trail."""

    SCHEMA = """
        content_hash STRING, source_id STRING, po_number STRING,
        quote_number STRING, outcome STRING, checklist STRING,
        run_id STRING, processed_at TIMESTAMP, n_failed INT,
        opportunity_id STRING, booking_form_id STRING
    """

    def __init__(self, spark, table: str = "sales_ops.po_validation.processed"):
        self.spark = spark
        self.table = table
        self._ensure()

    def _ensure(self) -> None:
        try:
            self.spark.sql(
                f"CREATE TABLE IF NOT EXISTS {self.table} ({self.SCHEMA}) USING DELTA")
        except Exception as exc:
            log.error("could not create ledger table %s: %s", self.table, exc)
            raise

    def seen(self, content_hash: str) -> bool:
        if not content_hash:
            return False
        safe = content_hash.replace("'", "")
        rows = self.spark.sql(
            f"SELECT outcome FROM {self.table} WHERE content_hash = '{safe}' "
            f"ORDER BY processed_at DESC LIMIT 1").collect()
        return bool(rows) and rows[0]["outcome"] in TERMINAL

    def attempts(self, content_hash: str) -> int:
        if not content_hash:
            return 0
        safe = content_hash.replace("'", "")
        return self.spark.sql(
            f"SELECT 1 FROM {self.table} WHERE content_hash = '{safe}'").count()

    def first_seen(self, content_hash: str):
        if not content_hash:
            return None
        safe = content_hash.replace("'", "")
        rows = self.spark.sql(
            f"SELECT MIN(processed_at) AS t FROM {self.table} "
            f"WHERE content_hash = '{safe}'").collect()
        return rows[0]["t"] if rows else None

    def record(self, entry: dict) -> None:
        try:
            import pandas as pd
        except ImportError:
            raise RuntimeError(
                "DeltaLedger requires pandas in a Databricks environment. "
                "Install with: pip install pandas")
        entry = dict(entry)
        entry["processed_at"] = pd.to_datetime(entry.get("processed_at"))
        entry.setdefault("n_failed", 0)
        df = self.spark.createDataFrame(pd.DataFrame([entry]))
        df.write.mode("append").saveAsTable(self.table)

    def find_po_number(self, po_number: str, exclude_hash: str = "") -> list[dict]:
        if not po_number:
            return []
        safe_po = po_number.replace("'", "")
        safe_hash = (exclude_hash or "").replace("'", "")
        rows = self.spark.sql(
            f"SELECT * FROM {self.table} "
            f"WHERE po_number = '{safe_po}' AND content_hash <> '{safe_hash}' "
            f"ORDER BY processed_at"
        ).collect()
        return [r.asDict() for r in rows]


class NullLedger(Ledger):
    """Reprocess everything, remember nothing. Only for one-off backfills."""

    def seen(self, content_hash: str) -> bool:
        return False

    def record(self, entry: dict) -> None:
        pass

    def find_po_number(self, po_number: str, exclude_hash: str = "") -> list[dict]:
        return []
