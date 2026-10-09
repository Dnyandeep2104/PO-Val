"""Live quote lookup from a laptop, using the Snowflake Python connector.

SnowflakeQuoteSource (snowflake.py) reads through Spark, which only exists
on Databricks. The review portal runs on a laptop, so it needs the plain
connector. The query, the column map and the row-to-Quote logic are shared
with the Spark version; only the transport differs.

Auth is the same browser SSO that test_snowflake.py uses. The browser opens
once when the portal starts (do it before the demo, not during it). With
the connector's secure-local-storage extra installed the SSO token is cached
in the OS keychain, so restarts usually do not prompt again.

For an unattended deployment swap externalbrowser for key-pair auth
(SNOWFLAKE_PRIVATE_KEY_PATH): no browser, no human.

Fallback: if Snowflake cannot be reached at all, FallbackQuoteSource answers
from the last CSV/JSON export (my_quotes.json) and every answer is labelled
with where it came from, so the screen never claims "live" when it is not.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from ..models import Quote
from .base import QuoteSource
from .snowflake import COLUMNS, LINE_TABLE, QUOTE_TABLE, SnowflakeQuoteSource

log = logging.getLogger(__name__)


class SnowflakeConnectorQuoteSource(SnowflakeQuoteSource):
    name = "snowflake"

    def __init__(self, connect_kwargs: dict, columns: Optional[dict] = None,
                 connector=None):
        # spark/options unused; the parent's helpers are reused.
        super().__init__(spark=None, options={}, columns=columns or dict(COLUMNS))
        self.connect_kwargs = connect_kwargs
        self._connector = connector          # injectable for tests
        self._conn = None
        self._lock = threading.Lock()
        self.identity: dict = {}

    # ----------------------------------------------------------- creation
    @classmethod
    def from_env(cls) -> "SnowflakeConnectorQuoteSource":
        user = os.environ.get("SNOWFLAKE_USER", "").strip()
        if not user:
            raise RuntimeError("SNOWFLAKE_USER is not set.")
        kw: dict[str, Any] = {
            "account": os.environ.get("SNOWFLAKE_ACCOUNT", "f5-enterprisedataecosystem"),
            "user": user,
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "EXP_SALES_WH"),
            "role": os.environ.get("SNOWFLAKE_ROLE", "APP_EDE_SALES_EXP_ROLE"),
            "database": os.environ.get("SNOWFLAKE_DATABASE", "PRD_ENT_RAW"),
            "schema": os.environ.get("SNOWFLAKE_SCHEMA", "SALESFORCE"),
            "client_session_keep_alive": True,
            "login_timeout": 120,
            "network_timeout": 60,
        }
        key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", "").strip()
        if key_path:
            kw["private_key_file"] = key_path
            pw = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
            if pw:
                kw["private_key_file_pwd"] = pw
        else:
            kw["authenticator"] = os.environ.get("SNOWFLAKE_AUTHENTICATOR", "externalbrowser")
            kw["client_store_temporary_credential"] = True
        return cls(kw)

    # --------------------------------------------------------- connection
    def connect(self):
        with self._lock:
            if self._conn is None:
                connector = self._connector
                if connector is None:
                    import snowflake.connector as connector
                log.info("Connecting to Snowflake as %s (a browser window may open for SSO)...",
                         self.connect_kwargs.get("user"))
                self._conn = connector.connect(**self.connect_kwargs)
                try:
                    cur = self._conn.cursor()
                    cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE()")
                    row = cur.fetchone() or (None, None)
                    self.identity = {"user": row[0], "role": row[1]}
                    cur.close()
                except Exception:
                    self.identity = {"user": self.connect_kwargs.get("user")}
            return self._conn

    def _reset(self):
        with self._lock:
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _read(self, sql: str, params: Optional[tuple] = None):
        """Run a query and return a pandas DataFrame (what the parent's
        row mapping expects). Reconnects once if the session dropped."""
        import pandas as pd
        for attempt in (1, 2):
            try:
                cur = self.connect().cursor()
                try:
                    cur.execute(sql, params) if params else cur.execute(sql)
                    cols = [d[0] for d in (cur.description or [])]
                    return pd.DataFrame(cur.fetchall(), columns=cols)
                finally:
                    cur.close()
            except Exception as exc:
                if attempt == 1:
                    log.warning("Snowflake query failed (%s); reconnecting once.", exc)
                    self._reset()
                    continue
                raise RuntimeError(f"Snowflake query failed: {exc}") from exc

    # ---------------------------------------------------------------- api
    def fetch(self, quote_number: str) -> Quote:
        q = self.validate_quote_number(quote_number)
        cols = self._usable_columns()
        select = ", ".join(
            (f"{alias}.{col}" if alias else col) + f" AS {logical}"
            for logical, (alias, col, _) in cols.items())
        sql = (f"SELECT {select} FROM {QUOTE_TABLE} quote "
               f"LEFT JOIN {LINE_TABLE} line ON quote.id = line.cafsl_oracle_quote_c "
               f"WHERE quote.name = %s AND line.is_deleted = false "
               f"ORDER BY cafsl_part_number_c")
        pdf = self._read(sql, (q,))
        if pdf is None or len(pdf) == 0:
            return Quote(quote_number=q, found=False, source=self.name)
        return self._to_quote(q, pdf, cols)

    def preflight(self) -> dict:
        try:
            self.connect()
            return {"ok": True, "mode": "live",
                    "detail": f"Live: {self.connect_kwargs.get('account')} as "
                              f"{self.identity.get('user')} ({self.identity.get('role') or 'role ?'})"}
        except Exception as exc:
            return {"ok": False, "mode": "live", "detail": f"{type(exc).__name__}: {exc}"}


class FallbackQuoteSource(QuoteSource):
    """Primary source first; on an outage (not a 'not found'), the backup.
    The quote's .source records which one actually answered."""

    name = "fallback"

    def __init__(self, primary: QuoteSource, backup: Optional[QuoteSource],
                 backup_label: str = "quote export (offline copy)"):
        self.primary = primary
        self.backup = backup
        self.backup_label = backup_label
        self.last_error: Optional[str] = None

    def fetch(self, quote_number: str) -> Quote:
        try:
            quote = self.primary.fetch(quote_number)
            self.last_error = None
            return quote
        except ValueError:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.backup is None:
                raise
            log.warning("Live quote source unavailable (%s); answering from %s.",
                        exc, self.backup_label)
            quote = self.backup.fetch(quote_number)
            quote.source = self.backup_label
            return quote
