#!/usr/bin/env python3
"""Which CPQ column holds the amount a reseller actually pays?

    .venv/bin/python3 tools/probe_quote_columns.py --folder my_pos

For every PO in the folder, pulls the cited quote's header and ALL line
columns from Snowflake, then scores every numeric line column against the
PO: does sum(column) reproduce the PO total, and per SKU, does it reproduce
each PO line? The column that wins across real POs is the one the matcher
should use. No guessing at column names.

Also diagnoses "no results": whether the quote header exists, how many
headers share the name (revisions), and how many lines are soft-deleted.

Writes out/quote_probe/{lines,headers,scores}.csv. Nothing leaves the laptop.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from po_validation.extract import registry                    # noqa: E402
from po_validation.models import normalize_part               # noqa: E402
from po_validation.resolve.snowflake import LINE_TABLE, QUOTE_TABLE  # noqa: E402

TOL = 1.0
PART = "CAFSL_PART_NUMBER_C"
CONTEXT_COLS = ["TYPE_C", "SUB_TYPE_C", "PARENT_LINE_ITEM_NUMBER_C", "PARENT_PRODUCT_SKU_C",
                "REQUIRED_BUNDLE_COMPONENT_C", "TRADE_IN_TYPE_C", "TERM_DURATION_MONTHS_C",
                "INITIAL_TERM_C", "TOTAL_QUANTITY_C", "IS_DELETED"]
NOT_MONEY = ("ID", "NUMBER", "MONTHS", "TERM", "PERCENT", "QUANTITY", "DURATION")


def connect():
    from po_validation.ai.client import load_env_file
    from po_validation.resolve.snowflake_live import SnowflakeConnectorQuoteSource
    load_env_file()
    return SnowflakeConnectorQuoteSource.from_env()


def numeric_columns(df):
    import pandas as pd
    out = []
    for c in df.columns:
        base = c.upper()[:-2] if c.upper().endswith("_C") else c.upper()
        if base.endswith(NOT_MONEY) or base in ("IS_DELETED", "QUOTE_NUMBER", "QUOTE_ID"):
            continue
        vals = pd.to_numeric(df[c], errors="coerce")
        if vals.notna().sum() and df[c].dtype != bool:
            out.append(c)
    return out


def po_view(po):
    by_sku = defaultdict(Decimal)
    for li in po.line_items:
        if li.part_number and li.total_price is not None:
            by_sku[li.part_number] += li.total_price
    total = po.po_total or po.computed_total
    return dict(by_sku), (float(total) if total is not None else None)


def main() -> int:
    import pandas as pd
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="my_pos")
    ap.add_argument("--quotes", nargs="*", help="extra quote numbers to dump (no PO needed)")
    ap.add_argument("--out", default="out/quote_probe")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    # 1. What the POs say
    pos = {}
    for path in sorted(Path(args.folder).glob("*.pdf")):
        po = registry.parse(path.read_bytes(), str(path))
        for qn in po.quote_numbers or ([po.quote_number] if po.quote_number else []):
            pos.setdefault(qn, []).append((path.name, po))
    wanted = sorted(set(pos) | set(args.quotes or []))
    if not wanted:
        print("No quote numbers found on any PO in", args.folder)
        return 1
    print(f"{len(wanted)} quote number(s) cited across {args.folder}")

    src = connect()
    ph = ", ".join(["%s"] * len(wanted))

    # 2. Headers: do they exist, and is a name shared by several records?
    hdr = src._read(f"SELECT * FROM {QUOTE_TABLE} WHERE name IN ({ph})", tuple(wanted))
    hdr.columns = [c.upper() for c in hdr.columns]
    # 3. Every line, deleted or not, so we can see what the filter removes.
    lines = src._read(
        f"SELECT q.name AS QUOTE_NUMBER, q.id AS QUOTE_ID, l.* FROM {QUOTE_TABLE} q "
        f"JOIN {LINE_TABLE} l ON l.cafsl_oracle_quote_c = q.id "
        f"WHERE q.name IN ({ph})", tuple(wanted))
    lines.columns = [c.upper() for c in lines.columns]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    hdr.to_csv(out / "headers.csv", index=False)
    lines.to_csv(out / "lines.csv", index=False)

    print("\nDIAGNOSIS (why a lookup can come back empty)")
    print(f"  {'quote':14} {'headers':>7} {'lines':>6} {'deleted':>8}  status-like columns")
    status_cols = [c for c in hdr.columns if "STATUS" in c or c == "IS_DELETED"]
    for qn in wanted:
        h = hdr[hdr["NAME"] == qn] if "NAME" in hdr.columns else hdr.iloc[0:0]
        l = lines[lines["QUOTE_NUMBER"] == qn]
        deleted = int(l["IS_DELETED"].astype(str).str.lower().isin(["true", "1"]).sum()) \
            if "IS_DELETED" in l.columns else 0
        st = "; ".join(f"{c}={h[c].iloc[0]}" for c in status_cols) if len(h) else "-"
        flag = "  <-- NO HEADER" if not len(h) else ("  <-- SEVERAL HEADERS" if len(h) > 1 else "")
        print(f"  {qn:14} {len(h):>7} {len(l):>6} {deleted:>8}  {st}{flag}")

    if lines.empty:
        print("\nNo quote lines came back for any cited quote; see headers.csv.")
        return 0

    live = lines[~lines["IS_DELETED"].astype(str).str.lower().isin(["true", "1"])] \
        if "IS_DELETED" in lines.columns else lines
    money_cols = numeric_columns(live)
    live = live.copy()
    for c in money_cols:
        live[c] = pd.to_numeric(live[c], errors="coerce").fillna(0.0)
    live["_SKU"] = live[PART].map(lambda s: normalize_part(str(s)) if pd.notna(s) else None)

    # 4. Score every numeric column against every PO.
    score = {c: {"order_hits": 0, "orders": 0, "sku_hits": 0, "skus": 0} for c in money_cols}
    detail = []
    for qn, refs in pos.items():
        ql = live[live["QUOTE_NUMBER"] == qn]
        if ql.empty:
            continue
        for fname, po in refs:
            po_sku, po_total = po_view(po)
            for c in money_cols:
                s = score[c]
                if po_total is not None:
                    s["orders"] += 1
                    s["order_hits"] += abs(ql[c].sum() - po_total) <= TOL
                agg = ql.groupby("_SKU")[c].sum()
                for sku, amt in po_sku.items():
                    s["skus"] += 1
                    s["sku_hits"] += sku in agg.index and abs(agg[sku] - float(amt)) <= TOL
            detail.append((fname, qn, po_sku, po_total, ql))

    ranked = sorted(score.items(), key=lambda kv: (-kv[1]["order_hits"], -kv[1]["sku_hits"]))
    pd.DataFrame([{"column": c, **s} for c, s in ranked]).to_csv(out / "scores.csv", index=False)

    print("\nCOLUMN SCOREBOARD  (order = sum of column matches PO total within $1;"
          " sku = per-SKU line totals match)")
    print(f"  {'column':38} {'orders':>9} {'skus':>9}")
    for c, s in ranked[:15]:
        print(f"  {c:38} {s['order_hits']:>4}/{s['orders']:<4} {s['sku_hits']:>4}/{s['skus']:<4}")

    top = [c for c, _ in ranked[:3]] + (["PI_TOTAL_PRICE_C"] if "PI_TOTAL_PRICE_C" in money_cols else [])
    top = list(dict.fromkeys(top))
    ctx = [c for c in CONTEXT_COLS if c in live.columns and c != "IS_DELETED"]
    print("\nPER-PO DETAIL  (top columns side by side; context columns explain bundles/terms/credits)")
    for fname, qn, po_sku, po_total, ql in detail:
        print(f"\n  {fname}  quote {qn}  PO total {po_total}")
        print("   " + f"{'SKU':26} {'PO':>11} " + " ".join(f"{c[:16]:>16}" for c in top)
              + "  " + " ".join(f"{c[:14]:>14}" for c in ctx))
        for sku in sorted(set(po_sku) | set(ql["_SKU"].dropna())):
            rows = ql[ql["_SKU"] == sku]
            vals = " ".join(f"{rows[c].sum():>16,.2f}" for c in top)
            cvals = " ".join(f"{str(rows[c].iloc[0])[:14]:>14}" if len(rows) else f"{'':>14}" for c in ctx)
            print(f"   {sku[:26]:26} {float(po_sku.get(sku, 0)):>11,.2f} {vals}  {cvals}")
        print("   " + f"{'SUM':26} {po_total or 0:>11,.2f} " + " ".join(f"{ql[c].sum():>16,.2f}" for c in top))

    print(f"\nRaw dumps: {out}/headers.csv, lines.csv, scores.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
