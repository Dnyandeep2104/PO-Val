#!/usr/bin/env python3
"""Measure extraction accuracy against hand-verified ground truth.

    python tests/accuracy.py --pdfs real

Prints a field-by-field scorecard. This is the number to quote when
someone asks how good the parser is, and the thing that tells you
immediately whether a change to the table reader helped or hurt. Add a
new reseller's PDF plus its ground-truth entry and the score updates.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from po_validation.extract import registry            # noqa: E402
from po_validation.models import money, normalize_part  # noqa: E402

GT_PATH = Path(__file__).parent / "ground_truth.json"
SCALARS = ["quote_number", "po_number", "po_total", "currency",
           "payment_terms", "f5_entity"]


def eq_money(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return abs(money(a) - money(b)) <= Decimal("0.01")


def compare(po, truth) -> dict:
    res = {"fields": {}, "lines": {}}

    for f in SCALARS:
        expected = truth.get(f)
        actual = getattr(po, f, None)
        if f == "po_total":
            ok = eq_money(expected, actual)
        else:
            ok = (str(expected).upper() if expected else None) == \
                 (str(actual).upper() if actual else None)
        res["fields"][f] = {"ok": ok, "expected": expected,
                            "actual": str(actual) if actual is not None else None}

    exp_lines = truth.get("line_items", [])
    got = po.line_items
    matched, detail = 0, []
    for i, (part, qty, total) in enumerate(exp_lines):
        if i >= len(got):
            detail.append(f"line {i + 1} missing")
            continue
        g = got[i]
        ok_part = normalize_part(part) == g.part_number
        ok_qty = qty == g.quantity
        ok_total = eq_money(total, g.total_price)
        if ok_part and ok_qty and ok_total:
            matched += 1
        else:
            bad = [n for n, o in (("part", ok_part), ("qty", ok_qty),
                                  ("total", ok_total)) if not o]
            detail.append(
                f"line {i + 1} {'/'.join(bad)}: expected "
                f"({part}, {qty}, {total}) got "
                f"({g.part_number}, {g.quantity}, {g.total_price})")
    if len(got) > len(exp_lines):
        detail.append(f"{len(got) - len(exp_lines)} extra line(s) extracted")

    res["lines"] = {"expected": len(exp_lines), "got": len(got),
                    "matched": matched, "detail": detail}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs", default="real")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    truth = {k: v for k, v in json.loads(GT_PATH.read_text()).items()
             if not k.startswith("_")}
    folder = Path(args.pdfs)

    field_hits = {f: [0, 0] for f in SCALARS}
    line_total = line_ok = 0
    rows = []

    for name, t in truth.items():
        path = folder / name
        if not path.exists():
            print(f"  missing sample: {path}")
            continue
        po = registry.parse(path.read_bytes(), name)
        r = compare(po, t)
        for f, v in r["fields"].items():
            field_hits[f][1] += 1
            field_hits[f][0] += int(bool(v["ok"]))
        line_total += r["lines"]["expected"]
        line_ok += r["lines"]["matched"]
        rows.append((name, t.get("vendor", "?"), po, r))

    print("\n" + "=" * 82)
    print("EXTRACTION ACCURACY")
    print("=" * 82)
    print(f"{'document':<32}{'layout':<11}{'conf':>5}  {'fields':>7}  {'lines':>9}")
    print("-" * 82)
    for name, vendor, po, r in rows:
        f_ok = sum(1 for v in r["fields"].values() if v["ok"])
        print(f"{name[:31]:<32}{po.layout:<11}{po.confidence:>5.2f}  "
              f"{f_ok}/{len(SCALARS):<5}  "
              f"{r['lines']['matched']}/{r['lines']['expected']:<7}")
        if args.verbose:
            for f, v in r["fields"].items():
                if not v["ok"]:
                    print(f"      {f}: expected {v['expected']!r}, "
                          f"got {v['actual']!r}")
            for d in r["lines"]["detail"]:
                print(f"      {d}")

    print("-" * 82)
    print("field accuracy:")
    for f, (ok, n) in field_hits.items():
        bar = "#" * int(20 * ok / max(n, 1))
        print(f"  {f:<16}{ok}/{n}  {bar}")
    total_f = sum(v[0] for v in field_hits.values())
    total_n = sum(v[1] for v in field_hits.values())
    print(f"\n  overall fields : {total_f}/{total_n} "
          f"({100 * total_f / max(total_n, 1):.1f}%)")
    print(f"  line items     : {line_ok}/{line_total} "
          f"({100 * line_ok / max(line_total, 1):.1f}%)")
    print("=" * 82)
    return 0 if total_f == total_n and line_ok == line_total else 1


if __name__ == "__main__":
    sys.exit(main())
