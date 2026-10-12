"""Quote reconciliation against real CPQ structure.

The shapes here come from the live CPQ replica (CAFSL_ORACLE_QUOTE_LINE_ITEM_C)
for the SYNNEX, Carahsoft and NTT sample quotes: priced bundles with a $0
parent, trade-in credits that resellers net into the hardware price, $0
included lines, and a near-miss SKU. Each tolerance has a negative test so
loosening the matcher cannot hide a real discrepancy.
"""

from decimal import Decimal

from po_validation.models import LineItem, ParsedPO, Quote, QuoteLine, Status
from po_validation.validate.rules import line_items_match, quote_found

P = {"id": "line_items_match", "severity": "MAJOR", "price_tolerance": "1.00",
     "check_quantity": True, "extra_severity": "BLOCKER", "missing_severity": "MAJOR"}
BUNDLE = "BIGIP NEXT CNE BASED LAB SUBSCRIPTION BUNDLE"


class Ctx:
    def __init__(self, po, quote):
        self.po, self.quote, self.ledger, self.config = po, quote, None, {}


def po(*lines):
    return ParsedPO(source_id="t", line_items=[
        LineItem(part_number=p, quantity=q, total_price=t) for p, q, t in lines])


def quote(*lines):
    return Quote(quote_number="F5Q-00000001", found=True, lines=list(lines), source="test")


def ql(part, total, qty=None, **kw):
    return QuoteLine(part_number=part, quantity=qty, total_price=total, **kw)


def fails(findings):
    return [f for f in findings if f.status is Status.FAIL]


# -- bundles -----------------------------------------------------------------

def ntt_quote(child_total="34761.64"):
    return quote(ql("F5-NX-LAB-CNE-BNDSUB", "0.00", bundle_total="81075.10", bundle_key=BUNDLE),
                 ql("F5-NX-LAB-CNF-CGN", child_total, bundle_key=BUNDLE),
                 ql("F5-NX-LAB-CNF-DNS", "23156.73", bundle_key=BUNDLE),
                 ql("F5-NX-LAB-CNF-EFW", "5784.76", bundle_key=BUNDLE),
                 ql("F5-NX-LAB-CNF-PE", "11587.21", bundle_key=BUNDLE),
                 ql("F5-NX-LAB-CNF-SPK", "5784.76", bundle_key=BUNDLE),
                 ql("F5-NX-SPT-STD", "0.00", bundle_key=BUNDLE))


def test_bundle_priced_on_parent_matches_as_a_whole():
    p_ = po(("F5-NX-LAB-CNE-BNDSUB", 1, "81075.60"), ("F5-NX-LAB-CNF-CGN", 1, "0"),
            ("F5-NX-SPT-STD", 1, "0"))
    assert not fails(line_items_match(Ctx(p_, ntt_quote()), P))


def test_bundle_priced_on_children_matches_too():
    p_ = po(("F5-NX-LAB-CNF-CGN", 1, "34761.64"), ("F5-NX-LAB-CNF-DNS", 1, "23156.73"),
            ("F5-NX-LAB-CNF-EFW", 1, "5784.76"), ("F5-NX-LAB-CNF-PE", 1, "11587.21"),
            ("F5-NX-LAB-CNF-SPK", 1, "5784.76"))
    assert not fails(line_items_match(Ctx(p_, ntt_quote()), P))


def test_bundle_with_wrong_price_still_fails():
    p_ = po(("F5-NX-LAB-CNE-BNDSUB", 1, "90000.00"))
    out = fails(line_items_match(Ctx(p_, ntt_quote()), P))
    assert len(out) == 1 and "Bundle" in out[0].message


def test_plain_configuration_sharing_a_description_is_not_a_bundle():
    q = quote(ql("F5-BIG-LTM-VE-25MV18", "3716.51", bundle_key="VE LTM"),
              ql("F5-SVC-BIG-VE+STDL13", "1825.52", bundle_key="VE LTM"))
    p_ = po(("F5-BIG-LTM-VE-25MV18", 2, "3716.51"), ("F5-SVC-BIG-VE+STDL13", 2, "2000.00"))
    out = fails(line_items_match(Ctx(p_, q), P))
    assert len(out) == 1 and "F5-SVC-BIG-VE+STDL13" in out[0].message


# -- trade-in credits --------------------------------------------------------

def carahsoft_quote():
    return quote(ql("F5-BIG-BT-R10600", "547777.51"), ql("F5-UPG-RACK-D", "2518.99"),
                 *[ql("F5-CREDIT-TRADE-IN", "-4000.00") for _ in range(4)])


def test_credit_netted_into_hardware_price_reconciles():
    p_ = po(("F5-BIG-BT-R10600", 4, "531777.52"), ("F5-UPG-RACK-D", 4, "2519.00"))
    out = line_items_match(Ctx(p_, carahsoft_quote()), P)
    assert not fails(out)
    assert "netted" in out[-1].message


def test_credit_listed_on_po_matches_directly():
    p_ = po(("F5-BIG-BT-R10600", 4, "547777.51"), ("F5-UPG-RACK-D", 4, "2518.99"),
            ("F5-CREDIT-TRADE-IN", 4, "-16000.00"))
    assert not fails(line_items_match(Ctx(p_, carahsoft_quote()), P))


def test_discount_bigger_than_the_credit_still_fails():
    p_ = po(("F5-BIG-BT-R10600", 4, "520000.00"), ("F5-UPG-RACK-D", 4, "2519.00"))
    out = fails(line_items_match(Ctx(p_, carahsoft_quote()), P))
    msgs = " ".join(f.message for f in out)
    assert "F5-BIG-BT-R10600" in msgs and "Credit F5-CREDIT-TRADE-IN" in msgs


# -- $0 lines, SKU twins, extras ---------------------------------------------

def test_zero_dollar_quote_line_missing_from_po_is_not_a_hold():
    q = quote(ql("F5-BIG-LTM-VE-1G-V18", "63148.43"), ql("F5-BIG-VE-LAB-V18", "0.00"))
    p_ = po(("F5-BIG-LTM-VE-1G-V18", 1, "63148.43"))
    assert not fails(line_items_match(Ctx(p_, q), P))


def test_priced_quote_line_missing_from_po_still_flags():
    q = quote(ql("F5-BIG-LTM-VE-1G-V18", "63148.43"), ql("F5-SVC-BIG-VE+PREL13", "3933.17"))
    p_ = po(("F5-BIG-LTM-VE-1G-V18", 1, "63148.43"))
    out = fails(line_items_match(Ctx(p_, q), P))
    assert len(out) == 1 and "Partial order" in out[0].message


def test_near_miss_sku_at_same_price_is_one_major_not_a_blocker():
    q = quote(ql("F5-ADD-BIG-R56R59", "210504.03", qty=4), ql("F5-SVC-BIG-PRE-L1-3", "52878.44", qty=4))
    p_ = po(("F5-ADD-BIG-R56R59B", 4, "210504.04"), ("F5-SVC-BIG-PRE-L1-3", 4, "52878.44"))
    out = fails(line_items_match(Ctx(p_, q), P))
    assert len(out) == 1
    assert out[0].severity.value == "MAJOR" and "F5-ADD-BIG-R56R59" in out[0].message


def test_unknown_sku_at_a_different_price_is_still_a_blocker():
    q = quote(ql("F5-ADD-BIG-R56R59", "210504.03", qty=4))
    p_ = po(("F5-ADD-BIG-R56R59", 4, "210504.03"), ("F5-BIG-LTM-R2600", 2, "25000.00"))
    out = fails(line_items_match(Ctx(p_, q), P))
    assert len(out) == 1 and out[0].severity.value == "BLOCKER"


# -- replication lag -----------------------------------------------------------

def test_quote_header_without_lines_is_reported_as_not_replicated():
    p_ = {"id": "quote_exists", "severity": "BLOCKER"}
    out = quote_found(Ctx(po(), Quote(quote_number="F5Q-00000001", found=True, source="snowflake")), p_)
    assert out.status is Status.FAIL and "replicated" in out.message
