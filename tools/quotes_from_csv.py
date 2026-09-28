#!/usr/bin/env python3
"""Turn a CSV export of the quote query into a quotes file.

    python tools/quotes_from_csv.py quote_export.csv
    python run_local.py --quotes my_quotes.json --verbose

Why: the live Snowflake reader needs a Spark session, so it only runs on
Databricks. This lets you test the seven quote-comparison checks on your
laptop with real quote data, by exporting it once instead of wiring up a
connection.

Column names are matched loosely and case-insensitively, so it copes with
whatever casing Databricks or the Snowflake UI produces.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

# logical name -> accepted header spellings, first match wins
ALIASES = {
    "quote_number":   ["quote_number", "name", "quote_name", "quote"],
    "part_number":    ["part_number", "cafsl_part_number_c", "part", "sku"],
    "total_price":    ["total_price", "pi_total_price_c", "line_total", "amount"],
    "unit_price":     ["unit_price", "cafsl_unit_price_c", "price"],
    "quantity":       ["quantity", "cafsl_quantity_c", "qty"],
    "opportunity_id": ["opportunity_id", "cafsl_opportunity_c", "opportunity"],
    "account_id":     ["account_id", "cafsl_account_c", "account"],
    "currency":       ["currency", "currencyisocode", "currency_code"],
    "expiry_date":    ["expiry_date", "cafsl_expiration_date_c", "expiration_date"],
    "status":         ["status", "cafsl_status_c", "quote_status"],
}


def build_map(headers: list[str]) -> dict[str, str]:
    lowered = {h.strip().lower(): h for h in headers}
    found = {}
    for logical, names in ALIASES.items():
        for n in names:
            if n in lowered:
                found[logical] = lowered[n]
                break
    return found


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return None if v in ("", "null", "NULL", "None", "NaN") else v


def number(value: str | None) -> str | None:
    v = clean(value)
    if v is None:
        return None
    v = re.sub(r"[^\d.\-]", "", v)
    return v or None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file")
    ap.add_argument("--out", default="my_quotes.json")
    a = ap.parse_args(argv)

    path = Path(a.csv_file)
    if not path.exists():
        print(f"No such file: {path}")
        return 1

    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("The CSV has no rows.")
        return 1

    cmap = build_map(list(rows[0].keys()))
    for required in ("quote_number", "part_number"):
        if required not in cmap:
            print(f"Could not find a '{required}' column.")
            print(f"Columns in the file: {', '.join(rows[0].keys())}")
            return 1
    missing = [k for k in ("quantity", "unit_price", "total_price")
               if k not in cmap]
    if missing:
        print(f"Note: no column found for {', '.join(missing)}. "
              f"Checks needing them will report as unavailable rather than fail.\n")

    quotes: dict[str, dict] = {}
    for row in rows:
        qn = clean(row.get(cmap["quote_number"]))
        if not qn:
            continue
        q = quotes.setdefault(qn.upper(), {"quote_number": qn.upper(), "lines": []})
        for field in ("opportunity_id", "account_id", "currency",
                      "expiry_date", "status"):
            if field in cmap and not q.get(field):
                val = clean(row.get(cmap[field]))
                if val:
                    q[field] = val
        part = clean(row.get(cmap["part_number"]))
        if not part:
            continue                       # quote header row with no line
        line = {"part_number": part}
        if "quantity" in cmap:
            n = number(row.get(cmap["quantity"]))
            if n:
                line["quantity"] = int(float(n))
        for f in ("unit_price", "total_price"):
            if f in cmap:
                n = number(row.get(cmap[f]))
                if n:
                    line[f] = n
        q["lines"].append(line)

    out = Path(a.out)
    out.write_text(json.dumps(list(quotes.values()), indent=2))

    print(f"Wrote {out} — {len(quotes)} quote(s), "
          f"{sum(len(q['lines']) for q in quotes.values())} line(s):")
    for q in quotes.values():
        print(f"   {q['quote_number']}  {len(q['lines'])} line(s)"
              f"   opportunity={q.get('opportunity_id', '-')}")
    print(f"\nNow run:\n   python run_local.py --quotes {out} --verbose")
    return 0


if __name__ == "__main__":
    sys.exit(main())
