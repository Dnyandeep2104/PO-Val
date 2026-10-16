"""Checklist rule implementations.

Each function is registered under a name that the YAML checklist refers
to via `check:`. A rule receives the evaluation context and its own
params, and returns one Finding or a list of them.

Rules must never raise. The engine catches anything that escapes, but a
rule that returns SKIP with a reason produces a far more useful audit
trail than one that blows up the run.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional

from ..models import Finding, Severity, Status, money

REGISTRY: dict[str, Callable] = {}


def rule(name: str):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


class Ctx:
    """What a rule can see."""

    def __init__(self, po, quote, ledger=None, config: Optional[dict] = None):
        self.po = po
        self.quote = quote
        self.ledger = ledger
        self.config = config or {}


def _f(rule_id, status, severity, message, **kw) -> Finding:
    return Finding(rule_id=rule_id, status=status, severity=Severity(severity),
                   message=message, **kw)


# ---------------------------------------------------------------------------
# presence and shape
# ---------------------------------------------------------------------------

@rule("field_present")
def field_present(ctx: Ctx, p: dict) -> Finding:
    """params: field, on ('po'|'quote'), severity"""
    rid, sev = p["id"], p["severity"]
    target = ctx.quote if p.get("on") == "quote" else ctx.po
    label = p.get("label", p["field"].replace("_", " "))
    if target is None:
        return _f(rid, Status.SKIP, sev, f"No {p.get('on', 'po')} to check {label} on.")
    val = getattr(target, p["field"], None)
    if p["field"] == "po_total" and val in (None, "", []):
        val = getattr(target, "computed_total", None)
    if val in (None, "", []):
        return _f(rid, Status.FAIL, sev, f"{label.capitalize()} is missing from the PO.")
    return _f(rid, Status.PASS, sev, f"{label.capitalize()} found: {val}", actual=val)


@rule("field_matches_pattern")
def field_matches_pattern(ctx: Ctx, p: dict) -> Finding:
    """params: field, pattern, severity"""
    rid, sev = p["id"], p["severity"]
    val = getattr(ctx.po, p["field"], None)
    if val in (None, ""):
        return _f(rid, Status.SKIP, sev, f"{p['field']} not present, cannot pattern-check.")
    if re.fullmatch(p["pattern"], str(val), re.I):
        return _f(rid, Status.PASS, sev, f"{p['field']} is well formed.", actual=val)
    return _f(rid, Status.FAIL, sev,
              f"{p['field']} {val!r} does not match expected format {p['pattern']}.",
              expected=p["pattern"], actual=val)


@rule("min_confidence")
def min_confidence(ctx: Ctx, p: dict) -> Finding:
    """params: threshold, severity. Routes shaky parses to a human."""
    rid, sev = p["id"], p["severity"]
    threshold = float(p.get("threshold", 0.7))
    if ctx.po.confidence >= threshold:
        return _f(rid, Status.PASS, sev,
                  f"Extraction confidence {ctx.po.confidence:.2f} is at or above {threshold:.2f}.",
                  actual=ctx.po.confidence)
    return _f(rid, Status.FAIL, sev,
              f"Extraction confidence {ctx.po.confidence:.2f} is below {threshold:.2f}. "
              f"Layout '{ctx.po.layout}'. Human should eyeball the PDF.",
              expected=threshold, actual=ctx.po.confidence)


@rule("known_layout")
def known_layout(ctx: Ctx, p: dict) -> Finding:
    rid, sev = p["id"], p["severity"]
    if ctx.po.layout in ("generic", "failed", "unknown"):
        return _f(rid, Status.FAIL, sev,
                  f"No reseller layout matched; parsed with '{ctx.po.layout}'. "
                  f"If this reseller is now regular traffic, add a vendor extractor.",
                  actual=ctx.po.layout)
    return _f(rid, Status.PASS, sev, f"Recognised layout: {ctx.po.layout}.",
              actual=ctx.po.layout)


@rule("no_extraction_warnings")
def no_extraction_warnings(ctx: Ctx, p: dict) -> Finding:
    rid, sev = p["id"], p["severity"]
    if not ctx.po.warnings:
        return _f(rid, Status.PASS, sev, "Extractor raised no warnings.")
    return _f(rid, Status.FAIL, sev,
              "Extractor warnings: " + " ".join(ctx.po.warnings),
              actual=ctx.po.warnings)


@rule("has_line_items")
def has_line_items(ctx: Ctx, p: dict) -> Finding:
    rid, sev = p["id"], p["severity"]
    n = len(ctx.po.line_items)
    minimum = int(p.get("min", 1))
    if n >= minimum:
        return _f(rid, Status.PASS, sev, f"{n} line item(s) extracted.", actual=n)
    return _f(rid, Status.FAIL, sev,
              f"Only {n} line item(s) extracted (need {minimum}). "
              f"This is an extraction failure, not necessarily a bad PO.",
              expected=minimum, actual=n)


# ---------------------------------------------------------------------------
# quote resolution
# ---------------------------------------------------------------------------

@rule("quote_found")
def quote_found(ctx: Ctx, p: dict) -> Finding:
    rid, sev = p["id"], p["severity"]
    if ctx.quote is None:
        return _f(rid, Status.SKIP, sev, "Quote lookup was not attempted.")
    if ctx.quote.found and not ctx.quote.lines:
        return _f(rid, Status.FAIL, sev,
                  f"Quote {ctx.quote.quote_number} exists but none of its line items have "
                  f"replicated to {ctx.quote.source} yet. Retry shortly.",
                  actual=ctx.quote.quote_number)
    if ctx.quote.found:
        return _f(rid, Status.PASS, sev,
                  f"Quote {ctx.quote.quote_number} found in {ctx.quote.source} "
                  f"with {len(ctx.quote.lines)} line(s).")
    return _f(rid, Status.FAIL, sev,
              f"Quote {ctx.quote.quote_number} referenced on the PO does not exist "
              f"in {ctx.quote.source}.", actual=ctx.quote.quote_number)


@rule("quote_not_expired")
def quote_not_expired(ctx: Ctx, p: dict) -> Finding:
    """params: grace_days, severity"""
    rid, sev = p["id"], p["severity"]
    if ctx.quote is None or not ctx.quote.found:
        return _f(rid, Status.SKIP, sev, "No quote to check expiry on.")
    if ctx.quote.expiry_date is None:
        return _f(rid, Status.SKIP, sev,
                  "Quote expiry date not available from the quote source.")
    grace = int(p.get("grace_days", 0))
    reference = ctx.po.po_date or date.today()
    cutoff = ctx.quote.expiry_date + timedelta(days=grace)
    if reference <= cutoff:
        return _f(rid, Status.PASS, sev,
                  f"Quote valid on {reference} (expires {ctx.quote.expiry_date}).")
    return _f(rid, Status.FAIL, sev,
              f"Quote expired {ctx.quote.expiry_date}; PO dated {reference} "
              f"is {(reference - ctx.quote.expiry_date).days} day(s) past expiry.",
              expected=str(cutoff), actual=str(reference))


@rule("opportunity_resolved")
def opportunity_resolved(ctx: Ctx, p: dict) -> Finding:
    rid, sev = p["id"], p["severity"]
    if ctx.quote is None or not ctx.quote.found:
        return _f(rid, Status.SKIP, sev, "No quote, so no opportunity to resolve.")
    if ctx.quote.opportunity_id:
        return _f(rid, Status.PASS, sev,
                  f"Opportunity {ctx.quote.opportunity_id} resolved from the quote.",
                  actual=ctx.quote.opportunity_id)
    return _f(rid, Status.FAIL, sev,
              "Quote has no linked opportunity; a booking form cannot be created.")


@rule("currency_matches")
def currency_matches(ctx: Ctx, p: dict) -> Finding:
    rid, sev = p["id"], p["severity"]
    if ctx.quote is None or not ctx.quote.currency:
        return _f(rid, Status.SKIP, sev, "Quote currency unavailable.")
    if not ctx.po.currency:
        return _f(rid, Status.SKIP, sev, "No currency found on the PO.")
    if ctx.po.currency.upper() == ctx.quote.currency.upper():
        return _f(rid, Status.PASS, sev, f"Currency matches ({ctx.po.currency}).")
    return _f(rid, Status.FAIL, sev,
              f"PO currency {ctx.po.currency} does not match quote currency "
              f"{ctx.quote.currency}.",
              expected=ctx.quote.currency, actual=ctx.po.currency)


# ---------------------------------------------------------------------------
# the money checks
# ---------------------------------------------------------------------------

# Specifically target actual freight/shipping charges and fees.
# Must NOT match software product terms like "Transport Layer Security" or "bot handling".
FREIGHT_RE = re.compile(
    r"\b(?:"
    r"freight(?:\s+(?:charges?|cost|fee|amount))?|"
    r"shipping(?:\s+(?:charges?|cost|fee|amount|\s*&\s*handling))?|"
    r"delivery\s+(?:charges?|fee|cost)|"
    r"transportation\s+(?:charges?|fee|cost)|"
    r"material\s+shipping(?:\s+charges?)?|"
    r"handling\s+(?:charges?|fee)"
    r")\b", re.I)

NON_FREIGHT_TERMS = [
    "transport layer security",
    "tls",
    "bot handling",
    "exception handling",
    "handling module",
    "software",
    "license",
    "subscription",
]


def _is_freight_line(li) -> bool:
    desc = (getattr(li, "description", "") or "").lower()
    part = (getattr(li, "part_number", "") or "").lower()
    combined = f"{desc} {part}".strip()
    if not combined:
        return False
    if any(term in combined for term in NON_FREIGHT_TERMS):
        return False
    return bool(FREIGHT_RE.search(combined))


def _match_lines_by_amount(ctx: Ctx, p: dict) -> list[Finding]:
    """Pair lines by unique amount when a PO carries no F5 SKUs.

    When a partner like PayPal prints internal item codes or Dell prints
    "Not Available", but cites the quote, we attempt to pair PO lines to
    quote lines by their total amounts. If amounts pair uniquely to the cent,
    they are considered matched without requiring a fuzzy API call.
    Freight/shipping lines are recognized as pass-through charges.
    """
    rid, sev = p["id"], p["severity"]
    tol = money(p.get("price_tolerance", "1.00"))
    po_lines = [li for li in ctx.po.line_items if li.total_price is not None]
    quote_candidates = [ql for ql in ctx.quote.lines
                        if ql.total_price is not None and ql.total_price != 0]

    if not po_lines or not quote_candidates:
        return [_f(rid, Status.SKIP, sev,
                   "The PO carries no F5 SKUs, so parts cannot be matched "
                   "line by line. The checklist accepts the cited quote in "
                   "their place; the order total is compared separately.")]

    unpaired_quote = list(quote_candidates)
    matched_pairs: list[tuple] = []
    findings: list[Finding] = []

    for po_li in po_lines:
        po_amt = po_li.total_price
        matches = [ql for ql in unpaired_quote if abs(ql.total_price - po_amt) <= tol]
        if len(matches) == 1:
            matched_q = matches[0]
            unpaired_quote.remove(matched_q)
            matched_pairs.append((po_li, matched_q))
        elif len(matches) > 1 and all(m.part_number == matches[0].part_number for m in matches):
            matched_q = matches[0]
            unpaired_quote.remove(matched_q)
            matched_pairs.append((po_li, matched_q))
        elif len(matches) == 0:
            if _is_freight_line(po_li):
                findings.append(_f(
                    rid, Status.PASS, sev,
                    f"PO line of {po_amt} ('{(po_li.description or '').strip()}') is a "
                    f"shipping/freight pass-through charge; Note to RO: Carrier Information required.",
                    actual=str(po_amt)))
            else:
                findings.append(_f(
                    rid, Status.FAIL, p.get("extra_severity", "MAJOR"),
                    f"PO line of {po_amt} has no matching amount on quote "
                    f"{ctx.quote.quote_number}, and carries no F5 SKU to identify it.",
                    actual=str(po_amt)))
        else:
            skus_str = ", ".join(ql.part_number for ql in matches)
            findings.append(_f(
                rid, Status.FAIL, sev,
                f"PO line of {po_amt} matches multiple quote lines ({skus_str}); "
                f"cannot pair uniquely by amount.",
                actual=str(po_amt)))

    for ql in unpaired_quote:
        findings.append(_f(
            rid, Status.FAIL, p.get("missing_severity", "MAJOR"),
            f"Part {ql.part_number} ({ql.total_price}) is on the quote but has no "
            f"matching amount on the PO. Partial order?",
            expected=str(ql.total_price), line_ref=ql.part_number))

    if not any(f.status == Status.FAIL for f in findings) and matched_pairs:
        pairs_str = ", ".join(f"{q.part_number}={po.total_price}" for po, q in matched_pairs)
        findings.append(_f(
            rid, Status.PASS, sev,
            f"All {len(matched_pairs)} line(s) paired uniquely to the quote by amount: {pairs_str}."))

    return findings


@rule("line_items_match")
def line_items_match(ctx: Ctx, p: dict) -> list[Finding]:
    """The core comparison. params: price_tolerance, check_quantity,
    allow_partial, severity, missing_severity, extra_severity

    Aggregates by part number on both sides before comparing. That is what
    makes repeated SKUs safe: a PO listing the same part on two lines with
    qty 1 each matches a quote line with qty 2, which is correct business
    behaviour and something the prototype's sort-and-zip got wrong.
    """
    rid, sev = p["id"], p["severity"]
    tol = money(p.get("price_tolerance", "1.00"))
    check_qty = bool(p.get("check_quantity", True))

    if ctx.quote is None or not ctx.quote.found:
        return [_f(rid, Status.SKIP, sev, "No quote to compare line items against.")]
    if not ctx.po.line_items:
        return [_f(rid, Status.SKIP, sev, "No PO line items to compare.")]

    # When a PO carries no F5 SKUs (e.g. PayPal internal item codes, Dell
    # "Not Available"), pair lines uniquely by amount to the cent.
    if not any(_is_f5_sku(li.part_number) for li in ctx.po.line_items):
        return _match_lines_by_amount(ctx, p)

    # Map reseller-prefixed or hyphenless SKUs on PO lines to matching quote SKUs
    # (e.g. F5_F5VOADNMSHPUBLB -> F5-V-O-ADN-MSH-PUBLB)
    def _norm_sku(s: Optional[str]) -> str:
        clean = re.sub(r"[^A-Za-z0-9]", "", s or "").upper()
        if clean.startswith("F5F5"):
            clean = clean[2:]
        return clean

    quote_skus = {ql.part_number for ql in ctx.quote.lines if ql.part_number}
    norm_to_quote = {_norm_sku(qs): qs for qs in quote_skus}

    mapped_po_lines = []
    for li in ctx.po.line_items:
        if li.part_number and _norm_sku(li.part_number) in norm_to_quote:
            import copy
            li_mapped = copy.copy(li)
            li_mapped.part_number = norm_to_quote[_norm_sku(li.part_number)]
            mapped_po_lines.append(li_mapped)
        else:
            mapped_po_lines.append(li)

    mapped_q_lines = []
    for ql in ctx.quote.lines:
        if ql.part_number and _norm_sku(ql.part_number) in norm_to_quote:
            import copy
            ql_mapped = copy.copy(ql)
            ql_mapped.part_number = norm_to_quote[_norm_sku(ql.part_number)]
            mapped_q_lines.append(ql_mapped)
        else:
            mapped_q_lines.append(ql)

    po_agg = _aggregate(mapped_po_lines)
    q_agg = _aggregate(mapped_q_lines)

    findings: list[Finding] = []
    notes: list[str] = []
    matched = 0

    # 1. Priced bundles. CPQ puts $0 on the parent SKU and the money on the
    #    children; a reseller may print the price on the parent instead
    #    (NTT: F5-NX-LAB-CNE-BNDSUB at 81,075.60, children at $0). Compare
    #    the bundle as a whole and take its SKUs out of the per-part pass.
    for key, members in _bundle_groups(ctx.quote.lines).items():
        skus = {m.part_number for m in members if m.part_number}
        on_po = skus & set(po_agg)
        if not on_po:
            continue                     # whole bundle missing: per-part pass reports it
        q_amt = sum((m.total_price or Decimal("0")) for m in members) \
            or next((m.bundle_total for m in members if m.bundle_total), Decimal("0"))
        po_amt = sum((po_agg[s_]["total"] or Decimal("0")) for s_ in on_po)
        extra = sorted(on_po - skus)
        for s_ in skus:
            po_agg.pop(s_, None)
            q_agg.pop(s_, None)
        delta = po_amt - q_amt
        if abs(delta) <= tol:
            matched += 1
            notes.append(f"bundle '{(key or '')[:50]}' ({len(skus)} SKUs) matches as a "
                         f"whole: PO {po_amt} vs quote {q_amt}")
        else:
            findings.append(_f(
                rid, Status.FAIL, sev,
                f"Bundle '{(key or '')[:50]}': PO total {po_amt} vs quote {q_amt}, off by "
                f"{delta:+}.", expected=str(q_amt), actual=str(po_amt), delta=str(delta)))

    # 2. Part by part.
    price_deltas: list[tuple[str, Decimal, Finding]] = []
    extras: dict[str, dict] = {}
    missing: dict[str, dict] = {}
    credits: dict[str, dict] = {}
    for part in sorted(set(po_agg) | set(q_agg)):
        on_po, on_quote = po_agg.get(part), q_agg.get(part)

        if on_quote is None:
            extras[part] = on_po
            continue

        if on_po is None:
            if not on_quote["total"]:
                notes.append(f"{part} is on the quote at $0 and not on the PO (included)")
            elif on_quote["total"] < 0:
                credits[part] = on_quote
            else:
                missing[part] = on_quote
            continue

        line_ok = True
        if check_qty and on_po["qty"] is not None and on_quote["qty"] is not None:
            if on_po["qty"] != on_quote["qty"]:
                money_match = on_po["total"] is not None and on_quote["total"] is not None and abs(on_po["total"] - on_quote["total"]) <= tol
                q_lines_for_part = [ql for ql in ctx.quote.lines if ql.part_number == part or _norm_sku(ql.part_number) == _norm_sku(part)]
                is_sub = any(
                    (getattr(ql, "line_type", "") or "").lower() in ("saas", "subscription")
                    or "subscription" in (getattr(ql, "description", "") or "").lower()
                    or "distributed cloud" in (getattr(ql, "description", "") or "").lower()
                    for ql in q_lines_for_part
                )
                if money_match and is_sub:
                    notes.append(f"Part {part}: Subscription/SaaS term quantity difference (PO qty {on_po['qty']} vs quote qty {on_quote['qty']}) reconciled by exact line total {on_po['total']}")
                else:
                    line_ok = False
                    findings.append(_f(
                        rid, Status.FAIL, sev,
                        f"Part {part}: PO quantity {on_po['qty']} does not match "
                        f"quote quantity {on_quote['qty']}.",
                        expected=on_quote["qty"], actual=on_po["qty"],
                        delta=on_po["qty"] - on_quote["qty"], line_ref=part))

        if on_po["total"] is not None and on_quote["total"] is not None:
            delta = on_po["total"] - on_quote["total"]
            if abs(delta) > tol:
                line_ok = False
                f_ = _f(rid, Status.FAIL, sev,
                        f"Part {part}: PO total {on_po['total']} vs quote "
                        f"{on_quote['total']}, off by {delta:+}.",
                        expected=str(on_quote["total"]), actual=str(on_po["total"]),
                        delta=str(delta), line_ref=part)
                findings.append(f_)
                price_deltas.append((part, delta, f_))
        elif on_po["total"] is None:
            line_ok = False
            findings.append(_f(
                rid, Status.FAIL, sev,
                f"Part {part}: no price extracted from the PO to compare.",
                line_ref=part))

        if line_ok:
            matched += 1

    # 3. Trade-in credits. Resellers usually net them into the hardware
    #    price instead of listing them (Carahsoft: R10600 printed 15,999.99
    #    under the quote; the quote carries 4 x -4,000.00 credit lines).
    if credits:
        credit_total = sum(c["total"] for c in credits.values())
        under = [(pt, d, f_) for pt, d, f_ in price_deltas if d < 0]
        if under and abs(sum(d for _, d, _ in under) - credit_total) <= tol:
            for _, _, f_ in under:
                findings.remove(f_)
            matched += len(under)
            notes.append(f"trade-in credit {credit_total} netted into "
                         f"{', '.join(pt for pt, _, _ in under)} on the PO")
        else:
            for part, c in credits.items():
                findings.append(_f(
                    rid, Status.FAIL, p.get("missing_severity", "MAJOR"),
                    f"Credit {part} ({c['total']}) is on the quote but neither listed on "
                    f"the PO nor netted into a line price.", expected=str(c["total"]),
                    line_ref=part))

    # 4. Pass-through freight/shipping lines on the PO.
    for part, on_po in list(extras.items()):
        po_match_lis = [li for li in ctx.po.line_items if li.part_number == part or li.description == part]
        if po_match_lis and any(_is_freight_line(li) for li in po_match_lis):
            extras.pop(part)
            notes.append(f"PO line '{part}' ({on_po['total']}) is a pass-through shipping/freight charge; Note to RO: Carrier Information required")

    # 5. Normalized SKU matching (reseller prefixes like F5_ or punctuation differences).
    def _norm_sku(s: Optional[str]) -> str:
        clean = re.sub(r"[^A-Za-z0-9]", "", s or "").upper()
        if clean.startswith("F5F5"):
            clean = clean[2:]
        return clean

    for part, on_po in list(extras.items()):
        norm_p = _norm_sku(part)
        twin = next((q for q in missing if _norm_sku(q) == norm_p), None)
        if twin:
            q_info = missing.pop(twin)
            extras.pop(part)
            delta = (on_po["total"] or Decimal("0")) - (q_info["total"] or Decimal("0"))
            q_lines_for_part = [ql for ql in ctx.quote.lines if ql.part_number == twin or _norm_sku(ql.part_number) == norm_p]
            is_sub = any(
                (getattr(ql, "line_type", "") or "").lower() in ("saas", "subscription")
                or "subscription" in (getattr(ql, "description", "") or "").lower()
                or "distributed cloud" in (getattr(ql, "description", "") or "").lower()
                for ql in q_lines_for_part
            )
            qty_match = (q_info["qty"] is None or on_po["qty"] is None or q_info["qty"] == on_po["qty"] or is_sub)
            if abs(delta) <= tol and qty_match:
                matched += 1
                notes.append(f"SKU {part} matches {twin} (reseller prefix format)")
            else:
                findings.append(_f(
                    rid, Status.FAIL, sev,
                    f"SKU {part} matches {twin} but differs in price or qty (delta: {delta:+}).",
                    expected=str(q_info["total"]), actual=str(on_po["total"]), delta=str(delta), line_ref=part))

    # 6. Nominal penny lines ($0.01) on PO matching included $0 lines on quote.
    for part, on_po in list(extras.items()):
        if on_po.get("total") is not None and abs(on_po["total"]) <= Decimal("0.05"):
            norm_p = _norm_sku(part)
            twin = next((q for q, info in missing.items()
                         if _norm_sku(q) == norm_p and (info.get("total") == Decimal("0") or info.get("total") is None)), None)
            if twin:
                missing.pop(twin)
                extras.pop(part)
                matched += 1
                notes.append(f"SKU {part} ({on_po['total']}) accepted as nominal token for included $0 quote line {twin}")

    # 7. A PO SKU that is not on the quote, where the quote has a different
    #    SKU at the same price and quantity, is almost always a typo or a
    #    SKU revision (4661555: F5-ADD-BIG-R56R59B vs F5-ADD-BIG-R56R59).
    #    That is a question for SOS, not grounds to return the PO.
    for part, on_po in list(extras.items()):
        twin = next((q for q, v in missing.items()
                     if v["total"] is not None and on_po["total"] is not None
                     and abs(v["total"] - on_po["total"]) <= tol
                     and (v["qty"] is None or on_po["qty"] is None or v["qty"] == on_po["qty"])),
                    None)
        if twin:
            missing.pop(twin)
            extras.pop(part)
            findings.append(_f(
                rid, Status.FAIL, sev,
                f"PO SKU {part} is not on quote {ctx.quote.quote_number}, but the quote has "
                f"{twin} at the same price and quantity ({on_po['total']}). Likely a SKU "
                f"typo or revision; confirm which SKU to book.",
                expected=twin, actual=part, line_ref=part))

    for part, on_po in extras.items():
        findings.append(_f(
            rid, Status.FAIL, p.get("extra_severity", "BLOCKER"),
            f"Part {part} appears on the PO but not on quote "
            f"{ctx.quote.quote_number}.",
            actual=part, line_ref=part))
    for part, on_quote in missing.items():
        findings.append(_f(
            rid, Status.FAIL, p.get("missing_severity", "MAJOR"),
            f"Part {part} is on the quote but absent from the PO "
            f"(qty {on_quote['qty']}, {on_quote['total']}). Partial order?",
            expected=part, line_ref=part))

    note = f" Notes: {'; '.join(notes)}." if notes else ""
    if not findings:
        findings.append(_f(
            rid, Status.PASS, sev,
            f"All {matched} part(s) match the quote on quantity and price "
            f"within {tol} tolerance.{note}"))
    elif notes:
        findings.append(_f(rid, Status.PASS, sev, f"Reconciled:{note[7:]}"))
    return findings


def _bundle_groups(lines) -> dict[str, list]:
    """Quote lines that make up a priced bundle, keyed by bundle.

    A parent carries BUNDLE_TOTAL_PRICE_C; its components share its
    BUNDLE_DESC_C. Plain configurations also share a description but have
    no bundle total, so they are never grouped.
    """
    groups: dict[str, list] = {}
    for parent in lines:
        if not parent.bundle_total:
            continue
        key = parent.bundle_key or f"parent:{parent.part_number}"
        groups[key] = ([l for l in lines if l.bundle_key == parent.bundle_key]
                       if parent.bundle_key else [parent])
    return groups


@rule("po_total_matches_quote")
def po_total_matches_quote(ctx: Ctx, p: dict) -> Finding:
    """Whole-order tolerance.

    Separate from the per-line check on purpose. A per-line $1 tolerance
    on a 40-line PO permits $40 of drift overall. This rule caps the
    aggregate independently.
    """
    rid, sev = p["id"], p["severity"]
    if ctx.quote is None or not ctx.quote.found:
        return _f(rid, Status.SKIP, sev, "No quote to total against.")
    q_total = ctx.quote.total
    po_total = ctx.po.po_total or ctx.po.computed_total
    if q_total is None or po_total is None:
        return _f(rid, Status.SKIP, sev, "Totals unavailable on one side.")
    tol = money(p.get("tolerance", "1.00"))
    delta = po_total - q_total
    if abs(delta) <= tol:
        return _f(rid, Status.PASS, sev,
                  f"Order total {po_total} matches quote total {q_total} "
                  f"within {tol}.", actual=str(po_total))

    freight_lines = [li for li in ctx.po.line_items
                     if li.total_price is not None and _is_freight_line(li)]
    freight_total = sum((li.total_price for li in freight_lines), Decimal("0"))
    if freight_total > 0 and abs((po_total - freight_total) - q_total) <= tol:
        return _f(rid, Status.PASS, sev,
                  f"PO product total {po_total - freight_total} matches quote total {q_total} "
                  f"(PO includes {freight_total} freight charges; Note to RO required).",
                  expected=str(q_total), actual=str(po_total), delta=str(freight_total))

    return _f(rid, Status.FAIL, sev,
              f"Order total {po_total} vs quote total {q_total}, off by {delta:+} "
              f"(tolerance {tol}).",
              expected=str(q_total), actual=str(po_total), delta=str(delta))


@rule("po_total_self_consistent")
def po_total_self_consistent(ctx: Ctx, p: dict) -> Finding:
    """Do the extracted lines add up to the total printed on the PO?

    Purely a parser health check, and a strong one: if these disagree we
    dropped or double-counted a row, and nothing else in this result is
    trustworthy.
    """
    rid, sev = p["id"], p["severity"]
    stated, computed = ctx.po.po_total, ctx.po.computed_total
    if stated is None or computed is None:
        return _f(rid, Status.SKIP, sev,
                  "PO does not state a grand total, or no line totals were parsed.")
    tol = money(p.get("tolerance", "1.00"))
    delta = computed - stated
    if abs(delta) <= tol:
        return _f(rid, Status.PASS, sev,
                  f"Extracted lines sum to {computed}, matching the PO's stated "
                  f"total {stated}.")
    return _f(rid, Status.FAIL, sev,
              f"Extracted lines sum to {computed} but the PO states {stated} "
              f"(off by {delta:+}). Line items were probably mis-parsed.",
              expected=str(stated), actual=str(computed), delta=str(delta))


@rule("value_threshold")
def value_threshold(ctx: Ctx, p: dict) -> Finding:
    """Hold high-value orders for a human regardless of how clean they look."""
    rid, sev = p["id"], p["severity"]
    total = ctx.po.po_total or ctx.po.computed_total
    if total is None:
        return _f(rid, Status.SKIP, sev, "No order total to threshold.")
    limit = money(p["limit"])
    if total <= limit:
        return _f(rid, Status.PASS, sev, f"Order total {total} is within the "
                                         f"auto-book limit of {limit}.")
    return _f(rid, Status.FAIL, sev,
              f"Order total {total} exceeds the auto-book limit of {limit}; "
              f"requires manual approval.",
              expected=str(limit), actual=str(total))


# ---------------------------------------------------------------------------
# duplicates
# ---------------------------------------------------------------------------

@rule("not_duplicate")
def not_duplicate(ctx: Ctx, p: dict) -> Finding:
    """Same PO number arriving twice is a real and expensive failure mode:
    resellers resend, and Power Automate will happily drop the same
    attachment in the container again."""
    rid, sev = p["id"], p["severity"]
    if ctx.ledger is None:
        return _f(rid, Status.SKIP, sev, "No ledger available for duplicate checking.")
    if not ctx.po.po_number:
        return _f(rid, Status.SKIP, sev, "No PO number extracted to dedupe on.")
    prior = ctx.ledger.find_po_number(ctx.po.po_number, exclude_hash=ctx.po.content_hash)
    if not prior:
        return _f(rid, Status.PASS, sev, f"PO number {ctx.po.po_number} not seen before.")
    return _f(rid, Status.FAIL, sev,
              f"PO number {ctx.po.po_number} was already processed "
              f"({len(prior)} prior submission(s), most recent {prior[-1].get('source_id')}). "
              f"Possible resend or duplicate order.",
              actual=ctx.po.po_number)


def _aggregate(lines) -> dict[str, dict[str, Any]]:
    """Collapse lines to one entry per part number, summing qty and total."""
    out: dict[str, dict[str, Any]] = {}
    for ln in lines:
        if not ln.part_number:
            continue
        slot = out.setdefault(ln.part_number, {"qty": None, "total": None, "n": 0})
        slot["n"] += 1
        if ln.quantity is not None:
            slot["qty"] = (slot["qty"] or 0) + ln.quantity
        if ln.total_price is not None:
            slot["total"] = (slot["total"] or Decimal("0.00")) + ln.total_price
    return out


# ---------------------------------------------------------------------------
# SOS checklist rules
#
# These implement the Purchase Order Checklist (Revenue Operations,
# 7-Feb-2025). Each rule below maps to a specific checkbox on that
# document; the yaml records which one.
# ---------------------------------------------------------------------------

F5_ENTITY_NAMES = {
    "CORP": "F5, Inc.",
    "GSLLC": "F5 Government Solutions, LLC",
    "EMEA": "F5 Networks Limited",
    "SG": "F5 Networks Singapore Pte Ltd",
}


@rule("f5_entity_valid")
def f5_entity_valid(ctx: Ctx, p: dict) -> Finding:
    """Checklist: 'PO is issued to an F5 entity'."""
    rid, sev = p["id"], p["severity"]
    allowed = p.get("allowed", list(F5_ENTITY_NAMES))
    entity = ctx.po.f5_entity
    if not entity:
        return _f(rid, Status.FAIL, sev,
                  "PO does not name a recognised F5 contracting entity "
                  f"({', '.join(F5_ENTITY_NAMES.values())}).")
    if entity not in allowed:
        return _f(rid, Status.FAIL, sev,
                  f"PO is issued to {ctx.po.f5_entity_name} ({entity}), which is "
                  f"not in the accepted list for this checklist.",
                  expected=allowed, actual=entity)
    return _f(rid, Status.PASS, sev,
              f"Issued to {ctx.po.f5_entity_name} ({entity}).", actual=entity)


@rule("party_present")
def party_present(ctx: Ctx, p: dict) -> Finding:
    """Checklist: Bill To / Ship To / End User / Reseller present and named."""
    rid, sev, role = p["id"], p["severity"], p["role"]
    label = p.get("label", role.replace("_", " ").title())
    found = ctx.po.parties_of(role)
    named = [x for x in found if x.get("name")]
    if not named:
        if role in ("end_user", "reseller"):
            if ctx.quote and getattr(ctx.quote, "sell_through_type", "").lower() == "direct":
                return _f(rid, Status.PASS, sev,
                          f"Direct sale: {label} not required as a separate block.")
            if role == "end_user" and ctx.quote and getattr(ctx.quote, "end_user_name", None):
                eu_name = ctx.quote.end_user_name
                bill_tos = [x for x in ctx.po.parties_of("bill_to") if x.get("name")]
                ship_tos = [x for x in ctx.po.parties_of("ship_to") if x.get("name")]
                if bill_tos and name_similarity(bill_tos[0]["name"], eu_name) >= 0.6:
                    return _f(rid, Status.PASS, sev,
                              f"End User: {bill_tos[0]['name']} (direct buyer on PO)")
                if ship_tos and name_similarity(ship_tos[0]["name"], eu_name) >= 0.6:
                    return _f(rid, Status.PASS, sev,
                              f"End User: {ship_tos[0]['name']} (Ship To on PO matches End User)")

        return _f(rid, Status.FAIL, sev,
                  f"No {label} block with a clearly labelled entity name.")
    return _f(rid, Status.PASS, sev,
              f"{label}: {named[0]['name']}", actual=named[0]["name"])


@rule("single_party")
def single_party(ctx: Ctx, p: dict) -> Finding:
    """Checklist: 'multiple legal entity names on a singular PO not accepted;
    multiple BT addresses on PO not accepted'."""
    rid, sev, role = p["id"], p["severity"], p["role"]
    label = p.get("label", role.replace("_", " ").title())
    named = [x for x in ctx.po.parties_of(role) if x.get("name")]
    if not named:
        return _f(rid, Status.PASS, sev, f"Single {label} on the PO.")

    # Cluster entity names to handle minor formatting variations across pages
    clusters: list[list[str]] = []
    for x in named:
        name = x["name"]
        matched_cluster = False
        for cl in clusters:
            rep = cl[0]
            if (_norm_name(name) == _norm_name(rep)
                    or name_similarity(name, rep) >= 0.65
                    or (_canon(name) and _canon(rep) and (_canon(name) in _canon(rep) or _canon(rep) in _canon(name))
                        and min(len(_canon(name)), len(_canon(rep))) >= 6)):
                cl.append(name)
                matched_cluster = True
                break
        if not matched_cluster:
            clusters.append([name])

    if len(clusters) <= 1:
        return _f(rid, Status.PASS, sev, f"Single {label} on the PO.")
    return _f(rid, Status.FAIL, sev,
              f"{len(clusters)} different {label} entities on one PO: "
              f"{', '.join(sorted(cl[0] for cl in clusters)[:4])}.",
              actual=[cl[0] for cl in clusters])


@rule("party_physical_address")
def party_physical_address(ctx: Ctx, p: dict) -> Finding:
    """Checklist: 'Physical address (PO Box rejected by F5)'."""
    rid, sev, role = p["id"], p["severity"], p["role"]
    label = p.get("label", role.replace("_", " ").title())
    found = [x for x in ctx.po.parties_of(role) if x.get("name")]
    if not found:
        return _f(rid, Status.SKIP, sev, f"No {label} block to check.")
    offenders = [x for x in found if x.get("po_box")]
    if offenders:
        return _f(rid, Status.FAIL, sev,
                  f"{label} uses a PO Box ({offenders[0]['name']}); F5 requires "
                  f"a physical address.", actual=offenders[0]["lines"])
    if not any(x.get("lines") for x in found):
        return _f(rid, Status.SKIP, sev, f"No {label} address lines found.")
    return _f(rid, Status.PASS, sev, f"{label} has a physical address.")


@rule("party_no_care_of")
def party_no_care_of(ctx: Ctx, p: dict) -> Finding:
    """Checklist: 'Clearly labeled entity name (c/o verbiage rejected by F5)'."""
    rid, sev, role = p["id"], p["severity"], p["role"]
    label = p.get("label", role.replace("_", " ").title())
    found = [x for x in ctx.po.parties_of(role) if x.get("name")]
    if not found:
        return _f(rid, Status.SKIP, sev, f"No {label} block to check.")
    offenders = [x for x in found if x.get("care_of")]
    if offenders:
        return _f(rid, Status.FAIL, sev,
                  f"{label} uses 'c/o' verbiage, which F5 rejects: "
                  f"{offenders[0]['name']}.", actual=offenders[0]["lines"])
    return _f(rid, Status.PASS, sev, f"{label} has no c/o verbiage.")


@rule("party_contact_complete")
def party_contact_complete(ctx: Ctx, p: dict) -> Finding:
    """Checklist: 'Contact: First and Last Name, Email, Phone #'."""
    rid, sev, role = p["id"], p["severity"], p["role"]
    label = p.get("label", role.replace("_", " ").title())
    require = set(p.get("require", ["contact_name", "email", "phone"]))
    found = [x for x in ctx.po.parties_of(role) if x.get("name")]
    if not found:
        return _f(rid, Status.SKIP, sev, f"No {label} block to check.")

    best, best_gaps = None, None
    for x in found:
        gaps = []
        if "contact_name" in require and not x.get("contact_name"):
            gaps.append("contact name")
        if "email" in require and not x.get("emails"):
            gaps.append("email")
        if "phone" in require and not x.get("phones"):
            # If a verified contact email is present, direct electronic contact is established;
            # missing phone alone does not flag an exception.
            if not ("email" in require and x.get("emails")):
                gaps.append("phone")
        if best_gaps is None or len(gaps) < len(best_gaps):
            best, best_gaps = x, gaps
    if not best_gaps:
        return _f(rid, Status.PASS, sev, f"{label} contact details complete.")
    stat = Status.WARN if sev == "MINOR" else Status.FAIL
    return _f(rid, stat, sev,
              f"{label} ({best['name']}) is missing: {', '.join(best_gaps)}.",
              expected=sorted(require), actual=best.get("contact_name"))


@rule("payment_terms_present")
def payment_terms_present(ctx: Ctx, p: dict) -> Finding:
    """Checklist: 'Payment Terms - if indicated on the PO, they must not
    exceed established terms defaulted in Bill To Account'.

    Presence is checkable here. Whether the terms *exceed* the Bill To
    Account default is not: that default lives on the Salesforce Account
    and is not in the quote payload today. See the account_payment_terms
    note in the checklist yaml.
    """
    rid, sev = p["id"], p["severity"]
    if not ctx.po.payment_terms:
        return _f(rid, Status.WARN if p.get("optional") else Status.FAIL, sev,
                  "No payment terms stated on the PO.")
    return _f(rid, Status.PASS, sev,
              f"Payment terms: {ctx.po.payment_terms}", actual=ctx.po.payment_terms)


@rule("payment_terms_within_limit")
def payment_terms_within_limit(ctx: Ctx, p: dict) -> Finding:
    """Terms must not exceed the Bill To Account default.

    Needs the account's default terms. Until the quote source returns
    them this SKIPs with an explicit reason rather than silently passing.
    """
    rid, sev = p["id"], p["severity"]
    if not ctx.po.payment_terms:
        return _f(rid, Status.SKIP, sev, "No payment terms on the PO to compare.")
    limit_days = p.get("max_days")
    account_default = getattr(ctx.quote, "payment_terms", None) if ctx.quote else None
    if account_default is None and limit_days is None:
        return _f(rid, Status.SKIP, sev,
                  f"PO states {ctx.po.payment_terms}, but the Bill To Account "
                  f"default terms are not available from the quote source, so "
                  f"this cannot be compared automatically.")
    days = _terms_days(ctx.po.payment_terms)
    cap = limit_days if limit_days is not None else _terms_days(account_default)
    if days is None or cap is None:
        return _f(rid, Status.SKIP, sev,
                  f"Could not read a day count from {ctx.po.payment_terms!r}.")
    if days <= cap:
        return _f(rid, Status.PASS, sev,
                  f"{ctx.po.payment_terms} is within the {cap}-day limit.")
    return _f(rid, Status.FAIL, sev,
              f"{ctx.po.payment_terms} exceeds the established {cap}-day terms.",
              expected=cap, actual=days)


@rule("inco_terms_acceptable")
def inco_terms_acceptable(ctx: Ctx, p: dict) -> Finding:
    """Checklist: 'Examples of acceptable Inco Terms: FCA Ship Point,
    Origin, EXW. Destination Inco Terms only accepted if/when approved.'
    Also: 'Not always present on PO'."""
    rid, sev = p["id"], p["severity"]
    if not ctx.po.inco_terms:
        return _f(rid, Status.SKIP, sev,
                  "No Inco Terms on the PO. The checklist notes these are not "
                  "always present.")
    if ctx.po.inco_terms_acceptable:
        return _f(rid, Status.PASS, sev,
                  f"Inco Terms {ctx.po.inco_terms} are acceptable.",
                  actual=ctx.po.inco_terms)
    return _f(rid, Status.FAIL, sev,
              f"Inco Terms {ctx.po.inco_terms!r} are not on the accepted list "
              f"(FCA Ship Point, Origin, EXW). Destination terms need prior "
              f"approval.", actual=ctx.po.inco_terms)


@rule("product_info_or_quote")
def product_info_or_quote(ctx: Ctx, p: dict) -> Finding:
    """Checklist: 'Product Information: SKU, Qty, Unit Price. If not present
    on the PO appropriately, Quote # (CPQ) PO is booking against is required.'

    This is the rule the prototype had backwards. A quote number is not
    universally required; it is the fallback when the PO does not carry
    complete product information. Two of the six sample POs (Dell and
    PayPal) have no F5 SKU at all and are valid only because they cite a
    quote.
    """
    rid, sev = p["id"], p["severity"]
    items = ctx.po.line_items
    if not items:
        return _f(rid, Status.FAIL, sev,
                  "No product lines could be read from the PO.")

    missing_sku = [li for li in items if not _is_f5_sku(li.part_number)]
    missing_qty = [li for li in items if li.quantity is None]
    missing_price = [li for li in items
                     if li.unit_price is None and li.total_price is None]
    complete = not (missing_sku or missing_qty or missing_price)

    if complete:
        return _f(rid, Status.PASS, sev,
                  f"All {len(items)} line(s) carry an F5 SKU, quantity and price.")

    gaps = []
    if missing_sku:
        gaps.append(f"{len(missing_sku)} line(s) without an F5 SKU")
    if missing_qty:
        gaps.append(f"{len(missing_qty)} without quantity")
    if missing_price:
        gaps.append(f"{len(missing_price)} without a price")

    if ctx.po.quote_number:
        return _f(rid, Status.PASS, sev,
                  f"Product information is incomplete ({'; '.join(gaps)}), but "
                  f"the PO cites quote {ctx.po.quote_number}, which the "
                  f"checklist accepts in place of it.",
                  actual=ctx.po.quote_number)
    return _f(rid, Status.FAIL, sev,
              f"Product information is incomplete ({'; '.join(gaps)}) and the PO "
              f"cites no F5 quote number to book against.")


@rule("shipping_details")
def shipping_details(ctx: Ctx, p: dict) -> Finding:
    """Checklist: 'Carrier | Method | Carrier Account #', or a freight
    forwarder with contact details."""
    rid, sev = p["id"], p["severity"]
    have_carrier = bool(ctx.po.carriers)
    have_acct = bool(ctx.po.carrier_account)
    if have_carrier and have_acct:
        return _f(rid, Status.PASS, sev,
                  f"Carrier {'/'.join(ctx.po.carriers)} with account "
                  f"{ctx.po.carrier_account}.")
    if have_carrier:
        return _f(rid, Status.FAIL, sev,
                  f"Carrier {'/'.join(ctx.po.carriers)} named but no carrier "
                  f"account number found. The checklist requires Carrier | "
                  f"Method | Carrier Account #.")
    return _f(rid, Status.WARN if p.get("optional", True) else Status.FAIL, sev,
              "No FedEx/UPS/DHL carrier and account found. If a freight "
              "forwarder is being used instead, confirm its name and contact "
              "details manually.")


US_STATE_ZIP_RE = re.compile(
    r"\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|"
    r"MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|"
    r"VA|WA|WV|WI|WY|DC)\b[\s,]*\d{5}(?:-\d{4})?\b")


@rule("international_shipping")
def international_shipping(ctx: Ctx, p: dict) -> Finding:
    """Checklist: Importer of Record required for ANY international shipment;
    EORI required when shipping into the EU or UK."""
    rid, sev = p["id"], p["severity"]
    ship = ctx.po.party("ship_to")
    if not ship:
        return _f(rid, Status.SKIP, sev, "No Ship To block to assess.")
    text = " ".join(ship.get("lines", []))

    # A US state code followed by a ZIP is the most reliable domestic signal
    # on a PO. The previous version matched a bare "CA" as Canada, which
    # made every California address international: NTT's Ship To, Oracle in
    # Redwood Shores, CA, was flagged as missing an Importer of Record.
    us_address = re.search(US_STATE_ZIP_RE, text)
    domestic = us_address or re.search(
        r"\b(?:USA|United States|U\.S\.A?\.)\b", text, re.I)
    eu_uk = re.search(
        r"\b(?:United Kingdom|UK|Ireland|France|Germany|Netherlands|Belgium|"
        r"Spain|Italy|Poland|Sweden|Denmark|Finland|Austria|Portugal|"
        r"Czech|Hungary|Romania|Greece|Northern Ireland)\b", text, re.I)
    non_us = re.search(r"\b(?:Canada|Mexico|Brazil|India|Japan|Singapore|"
                       r"Australia|China|Korea)\b", text, re.I) \
        or re.search(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b", text)   # Canadian postcode

    if not (eu_uk or non_us) and domestic:
        return _f(rid, Status.PASS, sev, "Domestic US shipment; no IOR or EORI needed.")
    if not (eu_uk or non_us):
        return _f(rid, Status.SKIP, sev,
                  "Could not determine the destination country from the Ship To "
                  "block.")

    # Virtual Edition, Subscription, and Services delivery is electronic (license keys via email).
    # Physical customs IOR / EORI only apply to tangible physical hardware crossing borders.
    def _is_software_or_service(li) -> bool:
        desc = (getattr(li, "description", "") or "").lower()
        part = (getattr(li, "part_number", "") or "").lower()
        return any(kw in desc for kw in [
            "virtual edition", "subscription", "service", "maintenance", "software", "license", "support"
        ]) or any(kw in part for kw in [
            "-ve", "ve-", "svc-", "sub-", "-sub", "add-", "cne-", "cnf-", "trg-"
        ])

    if ctx.po.line_items and all(_is_software_or_service(li) for li in ctx.po.line_items):
        return _f(rid, Status.PASS, sev,
                  f"International destination ({ship.get('name')}), but order is purely electronic "
                  f"software/services delivery (Virtual Edition / Subscriptions); physical customs IOR not required.")

    gaps = []
    if not ctx.po.importer_of_record:
        gaps.append("Importer of Record")
    if eu_uk and not ctx.po.eori:
        gaps.append("EORI number")
    if gaps:
        return _f(rid, Status.FAIL, sev,
                  f"International shipment to {ship.get('name')} is missing: "
                  f"{', '.join(gaps)}.")
    return _f(rid, Status.PASS, sev, "International shipping requirements present.")


@rule("line_arithmetic")
def line_arithmetic(ctx: Ctx, p: dict) -> Finding:
    """Unit price x quantity must equal the line total on every line.

    A parser health check with no business meaning of its own. When it
    fails, the columns were misread and nothing else about the line can
    be trusted.
    """
    rid, sev = p["id"], p["severity"]
    failures = ctx.po.arithmetic_failures
    if not failures:
        return _f(rid, Status.PASS, sev,
                  "Every extracted line reconciles (unit price x qty = total).")
    return _f(rid, Status.FAIL, sev,
              f"{len(failures)} line(s) do not reconcile arithmetically; the "
              f"columns were probably misread. First: {failures[0]}",
              actual=failures[:3])


@rule("not_scanned_or_low_quality")
def not_scanned_or_low_quality(ctx: Ctx, p: dict) -> Finding:
    rid, sev = p["id"], p["severity"]
    if ctx.po.is_ocr:
        return _f(rid, Status.FAIL, sev,
                  "This PO had no text layer and was read by OCR. Values should "
                  "be eyeballed against the PDF before booking.")
    return _f(rid, Status.PASS, sev, "PDF has a native text layer.")


def _norm_name(name: Optional[str]) -> str:
    if not name:
        return ""
    s = re.sub(r"[^\w\s]", " ", name.lower())
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|company|co|lp|ulc|plc|gmbh)\b",
               "", s)
    return " ".join(s.split())


def _is_f5_sku(part: Optional[str]) -> bool:
    return bool(part and re.match(r"^F5[N]?[-_]", part, re.I))


def _terms_days(terms: Optional[str]) -> Optional[int]:
    if not terms:
        return None
    m = re.search(r"(\d{1,3})", str(terms))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Checking the PO against Salesforce reference data
#
# The checklist is mostly about parties, so "is this PO correct" cannot be
# answered from line items alone. The Bill To printed on the paper has to
# be the account the quote belongs to; the payment terms have to be the
# ones that account is set up with. These rules do that comparison.
# ---------------------------------------------------------------------------

LEGAL_SUFFIXES = re.compile(
    r"\b(?:inc|llc|ltd|limited|corp|corporation|company|co|plc|gmbh|lp|llp|"
    r"ulc|pte|pty|bv|nv|ag|sa|sas|sarl|spa|aps|ab|oy|kk|srl|holdings|group)\b",
    re.I)


def _canon(name: Optional[str]) -> str:
    """Reduce a company name to something comparable.

    'NTT America Inc' and 'NTT AMERICA, INC.' are the same account. Legal
    suffixes and punctuation carry no matching signal and vary constantly
    between a PO and a CRM record.
    """
    if not name:
        return ""
    s = re.sub(r"[^\w\s]", " ", str(name).lower())
    s = LEGAL_SUFFIXES.sub(" ", s)
    return " ".join(s.split())


def name_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Token overlap and sequence similarity, 0..1. Deliberately robust to
    both whole-word variations ('NTT America' vs 'NTT America Solutions')
    and PDF-typesetting kerning splits ('LifeI nsurance' vs 'Life Insurance')."""
    import difflib
    ca, cb = _canon(a), _canon(b)
    if not ca or not cb:
        return 0.0
    ta, tb = set(ca.split()), set(cb.split())
    token_score = len(ta & tb) / min(len(ta), len(tb)) if (ta and tb) else 0.0
    seq_score = difflib.SequenceMatcher(None, ca, cb).ratio()
    nospace_score = difflib.SequenceMatcher(None, ca.replace(" ", ""), cb.replace(" ", "")).ratio()
    return max(token_score, seq_score, nospace_score)


@rule("party_matches_reference")
def party_matches_reference(ctx: Ctx, p: dict) -> Finding:
    """Does the party printed on the PO match the one on the quote?

    params: role, reference ('account_name'|'end_user_name'|'reseller_name'),
            threshold, severity

    This is the check that catches a PO raised against the wrong account —
    the kind of error that reconciles perfectly on price and is still
    wrong. Line-item matching cannot see it.
    """
    rid, sev, role = p["id"], p["severity"], p["role"]
    label = p.get("label", role.replace("_", " ").title())
    ref_field = p.get("reference", "account_name")

    if ctx.quote is None or not ctx.quote.found:
        return _f(rid, Status.SKIP, sev, "No quote to compare the party against.")
    expected = getattr(ctx.quote, ref_field, None)
    if not expected:
        return _f(rid, Status.SKIP, sev,
                  f"The quote source did not return {ref_field}, so the "
                  f"{label} cannot be verified against Salesforce.")

    parties = [x for x in ctx.po.parties_of(role) if x.get("name")]
    if not parties:
        return _f(rid, Status.SKIP, sev, f"No {label} found on the PO to compare.")

    threshold = float(p.get("threshold", 0.6))
    best = max(parties, key=lambda x: name_similarity(x["name"], expected))
    score = name_similarity(best["name"], expected)

    if score >= threshold:
        return _f(rid, Status.PASS, sev,
                  f"{label} '{best['name']}' matches the quote's {ref_field} "
                  f"'{expected}'.", expected=expected, actual=best["name"])
    return _f(rid, Status.FAIL, sev,
              f"{label} on the PO is '{best['name']}' but the quote is against "
              f"'{expected}'. The PO may have been raised on the wrong account.",
              expected=expected, actual=best["name"], delta=round(score, 2))


@rule("quote_status_bookable")
def quote_status_bookable(ctx: Ctx, p: dict) -> Finding:
    """Only book against a quote in an approved state."""
    rid, sev = p["id"], p["severity"]
    if ctx.quote is None or not ctx.quote.found:
        return _f(rid, Status.SKIP, sev, "No quote to check the status of.")
    if not ctx.quote.status:
        return _f(rid, Status.SKIP, sev,
                  "The quote source did not return a status.")
    allowed = [a.lower() for a in p.get("allowed", ["approved", "accepted", "active"])]
    if str(ctx.quote.status).lower() in allowed:
        return _f(rid, Status.PASS, sev, f"Quote status is {ctx.quote.status}.",
                  actual=ctx.quote.status)
    return _f(rid, Status.FAIL, sev,
              f"Quote status is '{ctx.quote.status}', which is not bookable "
              f"(expected one of: {', '.join(allowed)}).",
              expected=allowed, actual=ctx.quote.status)
