"""Quote lookup against the EDE Snowflake replica of Salesforce.

Built from the query that already works in the prototype notebook. Two
things changed:

  1. The quote number is format-validated before it touches the SQL
     string (see QuoteSource.validate_quote_number).
  2. We pull quantity, unit price, currency, status and expiry alongside
     part number and total. The prototype pulled only part number and
     total, which is why its quantity check could not exist.

>>> COLUMNS BELOW NEED VERIFICATION AGAINST THE REAL SCHEMA. <<<
Only the five columns in the prototype's query are confirmed. The rest
are best-guess names following the same cafsl_*_c convention. Run
probe_columns() once you are on the cluster; it tells you which of these
actually exist and the loader then silently drops the ones that do not,
so a wrong guess degrades a rule to SKIP rather than crashing the run.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ..models import Quote, QuoteLine
from .base import QuoteSource

log = logging.getLogger(__name__)

QUOTE_TABLE = os.environ.get("SNOWFLAKE_QUOTE_TABLE", "PRD_ENT_RAW.SALESFORCE.CAFSL_ORACLE_QUOTE_C")
LINE_TABLE = os.environ.get("SNOWFLAKE_LINE_TABLE", "PRD_ENT_RAW.SALESFORCE.CAFSL_ORACLE_QUOTE_LINE_ITEM_C")

# logical name -> (alias, column). CONFIRMED entries came from the
# prototype's working query. UNVERIFIED entries are conventional guesses.
COLUMNS: dict[str, tuple[str, str, bool]] = {
    # logical            alias    column                       confirmed
    # The prototype selected these four WITHOUT a table prefix and let
    # Snowflake resolve them. Guessing a prefix broke it: opportunity and
    # account are not on the line table. alias None = unqualified, exactly
    # as the proven query did.
    "part_number":      (None,    "cafsl_part_number_c",       True),
    "total_price":      (None,    "pi_total_price_c",          True),
    "opportunity_id":   (None,    "cafsl_opportunity_c",       True),
    "account_id":       (None,    "cafsl_account_c",           True),
    "quantity":         ("line",  "cafsl_quantity_c",          False),
    "unit_price":       ("line",  "cafsl_unit_price_c",        False),
    "description":      ("line",  "cafsl_description_c",       False),
    "currency":         ("quote", "currencyisocode",           False),
    "expiry_date":      ("quote", "cafsl_expiration_date_c",   False),
    "quote_status":     ("quote", "cafsl_status_c",            False),
}


class SnowflakeQuoteSource(QuoteSource):
    name = "snowflake"

    def __init__(self, spark, options: dict, columns: Optional[dict] = None):
        """
        spark   : the Databricks SparkSession
        options : the snowflake connector options dict already built in
                  the notebook's first cell (key-pair auth from Key Vault)
        columns : override COLUMNS after you have probed the real schema
        """
        self.spark = spark
        self.options = options
        self.columns = columns or dict(COLUMNS)
        self._available: Optional[set[str]] = None

    # ------------------------------------------------------------------ api
    def fetch(self, quote_number: str) -> Quote:
        q = self.validate_quote_number(quote_number)
        cols = self._usable_columns()

        select = ", ".join(
            (f"{alias}.{col}" if alias else col) + f" AS {logical}"
            for logical, (alias, col, _) in cols.items())
        sql = f"""
            SELECT {select}
            FROM {QUOTE_TABLE} quote
            LEFT JOIN {LINE_TABLE} line
                   ON quote.id = line.cafsl_oracle_quote_c
            WHERE quote.name = '{q}'
              AND line.is_deleted = false
            ORDER BY cafsl_part_number_c
        """
        pdf = self._read(sql)

        if pdf is None or len(pdf) == 0:
            return Quote(quote_number=q, found=False, source=self.name)

        return self._to_quote(q, pdf, cols)

    # -------------------------------------------------------------- helpers
    def _read(self, sql: str):
        try:
            return (self.spark.read
                    .format("snowflake")
                    .options(**self.options)
                    .option("query", sql)
                    .load()
                    .toPandas())
        except Exception as exc:
            log.exception("Snowflake query failed")
            raise RuntimeError(f"Snowflake query failed: {exc}") from exc

    def _usable_columns(self) -> dict:
        """Drop unverified columns that do not exist in the target schema."""
        if self._available is None:
            self._available = self.probe_columns()
        return {k: v for k, v in self.columns.items()
                if v[2] or k in self._available}

    def probe_columns(self) -> set[str]:
        """Ask Snowflake which of the unverified columns actually exist.

        Run this once on the cluster and paste the result into COLUMNS as
        confirmed=True. Until then it runs automatically and costs one
        cheap metadata query per session.
        """
        available: set[str] = set()
        for table, alias in ((QUOTE_TABLE, "quote"), (LINE_TABLE, "line")):
            db, schema, tbl = table.split(".")
            sql = (f"SELECT column_name FROM {db}.INFORMATION_SCHEMA.COLUMNS "
                   f"WHERE table_schema = '{schema}' AND table_name = '{tbl}'")
            try:
                pdf = self._read(sql)
                names = {str(c).lower() for c in pdf.iloc[:, 0].tolist()}
            except Exception as exc:
                log.warning("Column probe failed for %s: %s. "
                            "Falling back to confirmed columns only.", table, exc)
                continue
            for logical, (al, col, confirmed) in self.columns.items():
                if al == alias and col.lower() in names:
                    available.add(logical)
        log.info("Quote schema probe found: %s", sorted(available))
        return available

    def _to_quote(self, quote_number: str, pdf, cols: dict) -> Quote:
        def get(row, logical) -> Any:
            if logical not in cols:
                return None
            # Snowflake returns upper-cased aliases through the connector
            for candidate in (logical.upper(), logical, logical.lower()):
                if candidate in row.index:
                    val = row[candidate]
                    return None if _is_null(val) else val
            return None

        first = pdf.iloc[0]
        lines: list[QuoteLine] = []
        for _, row in pdf.iterrows():
            part = get(row, "part_number")
            if part is None:
                continue          # LEFT JOIN produced a quote with no lines
            qty = get(row, "quantity")
            lines.append(QuoteLine(
                part_number=part,
                quantity=int(qty) if qty is not None else None,
                unit_price=get(row, "unit_price"),
                total_price=get(row, "total_price"),
                description=get(row, "description"),
            ))

        expiry = get(first, "expiry_date")
        return Quote(
            quote_number=quote_number,
            found=True,
            lines=lines,
            opportunity_id=_first_non_null(pdf, cols, "opportunity_id"),
            account_id=_first_non_null(pdf, cols, "account_id"),
            currency=get(first, "currency"),
            expiry_date=_as_date(expiry),
            status=get(first, "quote_status"),
            account_name=get(first, "account_name"),
            payment_terms=get(first, "payment_terms"),
            end_user_name=get(first, "end_user_name"),
            reseller_name=get(first, "reseller_name"),
            source=self.name,
        )


def _is_null(val) -> bool:
    if val is None:
        return True
    try:
        import math
        if isinstance(val, float) and math.isnan(val):
            return True
    except Exception:
        pass
    try:
        import pandas as pd
        return bool(pd.isna(val))
    except Exception:
        return False


def _first_non_null(pdf, cols: dict, logical: str):
    if logical not in cols:
        return None
    for candidate in (logical.upper(), logical, logical.lower()):
        if candidate in pdf.columns:
            s = pdf[candidate].dropna()
            return s.iloc[0] if len(s) else None
    return None


def _as_date(val):
    if val is None:
        return None
    if hasattr(val, "date"):
        try:
            return val.date()
        except Exception:
            pass
    from ..extract.base import parse_date
    return parse_date(str(val))
