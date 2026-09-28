"""Tests.

The comparison tests are written against the specific defects found in
the prototype notebook, so a regression here means we have reintroduced a
bug that was already shipped once.

    python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from po_validation.extract import registry                       # noqa: E402
from po_validation.ingest.ledger import JsonLedger, NullLedger    # noqa: E402
from po_validation.ingest.sources import LocalFolderSource        # noqa: E402
from po_validation.models import (LineItem, Outcome, ParsedPO, Quote,  # noqa: E402
                                  QuoteLine, Status, money)
from po_validation.pipeline import Pipeline                       # noqa: E402
from po_validation.report.writer import ExceptionRouter           # noqa: E402
from po_validation.resolve.base import StubQuoteSource            # noqa: E402
from po_validation.validate.engine import Checklist, Engine, load_engine  # noqa: E402
from po_validation.validate.rules import Ctx, REGISTRY            # noqa: E402

from make_fixtures import build_all                               # noqa: E402

RULES = ROOT / "rules"


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory):
    return build_all(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture
def quote():
    return Quote(
        quote_number="F5Q-00972766", found=True,
        opportunity_id="006do00000K8sFsAAJ", account_id="0015000000Mbzb7AAB",
        currency="USD",
        lines=[QuoteLine("F5-BIG-VE-BTA-25MV18", 2, "10082.52", "20165.04"),
               QuoteLine("F5-SVC-BIG-VE+PREL13", 2, "2038.53", "4077.06")],
    )


# ---------------------------------------------------------------- money

def test_money_is_exact():
    """Float would give 0.30000000000000004 here."""
    assert money("0.10") + money("0.20") == Decimal("0.30")


def test_money_parses_messy_strings():
    assert money("$1,234.56") == Decimal("1234.56")
    assert money("(500.00)") == Decimal("-500.00")
    assert money(None) is None


def test_po_total_coerced_on_assignment():
    """Regression: extractors assign po_total after __post_init__."""
    po = ParsedPO(source_id="x")
    po.po_total = "1,000.00"
    assert po.po_total == Decimal("1000.00")


# ------------------------------------------------------------ extraction

def test_ingram_layout_detected(fixtures):
    po = registry.parse(fixtures["ingram_ok"].read_bytes(), "ingram")
    assert po.layout == "ingram"
    assert po.quote_number == "F5Q-00972766"
    assert len(po.line_items) == 2
    assert po.line_items[0].total_price == Decimal("20165.04")
    assert po.f5_entity == "CORP"


def test_unknown_layout_still_extracts(fixtures):
    """The whole point of the registry: an unseen layout still parses."""
    po = registry.parse(fixtures["techdata_ok"].read_bytes(), "td")
    assert len(po.line_items) == 2
    parts = {li.part_number for li in po.line_items}
    assert parts == {"F5-BIG-VE-BTA-25MV18", "F5-SVC-BIG-VE+PREL13"}
    assert po.line_items[0].quantity == 2


def test_quantity_is_captured(fixtures):
    """The prototype parsed quantity and then discarded it."""
    po = registry.parse(fixtures["ingram_ok"].read_bytes(), "x")
    assert all(li.quantity is not None for li in po.line_items)


def test_extraction_never_raises():
    po = registry.parse(b"this is not a pdf at all", "junk.pdf")
    assert po.confidence == 0.0
    assert not po.line_items
    assert po.warnings


def test_lines_summing_wrong_is_flagged():
    po = ParsedPO(source_id="x", po_total="100.00",
                  line_items=[LineItem("PART-A", 1, "10.00", "10.00")])
    from po_validation.extract.base import BaseExtractor
    warnings = BaseExtractor().self_check(po)
    assert any("do not sum" in w for w in warnings)


def test_severity_drives_outcome_minimal():
    """A PO with only MINOR problems must not be held."""
    from po_validation.validate.engine import Checklist, Engine
    cl = Checklist("t", {"rules": [
        {"id": "lines", "check": "has_line_items", "severity": "BLOCKER"},
        {"id": "layout", "check": "known_layout", "severity": "MINOR"}]})
    po = ParsedPO(source_id="x", layout="generic",
                  line_items=[LineItem("F5-A-1", 1, "1.00", "1.00")])
    r = Engine(cl).run(po, None)
    assert r.outcome is Outcome.VALIDATED


# ------------------------------------------------------------- comparison

def _ctx(po, quote=None, ledger=None):
    return Ctx(po=po, quote=quote, ledger=ledger)


def test_repeated_sku_aggregates_correctly(quote):
    """Prototype bug: sorting by part number and zipping mispairs when the
    same SKU appears on two PO lines. Two lines of qty 1 must satisfy one
    quote line of qty 2."""
    po = ParsedPO(source_id="x", line_items=[
        LineItem("F5-BIG-VE-BTA-25MV18", 1, "10082.52", "10082.52"),
        LineItem("F5-BIG-VE-BTA-25MV18", 1, "10082.52", "10082.52"),
        LineItem("F5-SVC-BIG-VE+PREL13", 2, "2038.53", "4077.06"),
    ])
    findings = REGISTRY["line_items_match"](
        _ctx(po, quote),
        {"id": "r", "severity": "MAJOR", "price_tolerance": "1.00"})
    assert all(f.status is Status.PASS for f in findings), \
        [f.message for f in findings]


def test_quantity_mismatch_is_caught(quote):
    po = ParsedPO(source_id="x", line_items=[
        LineItem("F5-BIG-VE-BTA-25MV18", 2, "10082.52", "20165.04"),
        LineItem("F5-SVC-BIG-VE+PREL13", 5, "2038.53", "4077.06"),
    ])
    findings = REGISTRY["line_items_match"](
        _ctx(po, quote), {"id": "r", "severity": "MAJOR"})
    assert any("quantity" in f.message and f.status is Status.FAIL
               for f in findings)


def test_extra_part_on_po_blocks(quote):
    po = ParsedPO(source_id="x", line_items=[
        LineItem("F5-BIG-VE-BTA-25MV18", 2, "10082.52", "20165.04"),
        LineItem("F5-SVC-BIG-VE+PREL13", 2, "2038.53", "4077.06"),
        LineItem("F5-SOMETHING-ELSE", 1, "1.00", "1.00"),
    ])
    findings = REGISTRY["line_items_match"](
        _ctx(po, quote),
        {"id": "r", "severity": "MAJOR", "extra_severity": "BLOCKER"})
    extra = [f for f in findings if "not on quote" in f.message]
    assert extra and extra[0].severity.value == "BLOCKER"


def test_aggregate_tolerance_catches_accumulated_drift():
    """Per-line $1 tolerance permits N dollars over N lines. The order
    total rule exists to cap that independently."""
    q = Quote(quote_number="F5Q-00000001", found=True,
              lines=[QuoteLine(f"F5-PART-{i}", 1, "100.00", "100.00")
                     for i in range(40)])
    po = ParsedPO(source_id="x", line_items=[
        LineItem(f"F5-PART-{i}", 1, "100.90", "100.90") for i in range(40)])

    per_line = REGISTRY["line_items_match"](
        _ctx(po, q), {"id": "r", "severity": "MAJOR", "price_tolerance": "1.00"})
    assert all(f.status is Status.PASS for f in per_line)   # each line passes

    overall = REGISTRY["po_total_matches_quote"](
        _ctx(po, q), {"id": "t", "severity": "MAJOR", "tolerance": "1.00"})
    assert overall.status is Status.FAIL                     # aggregate does not
    assert "36.00" in overall.message


def test_missing_quote_number_is_not_a_price_mismatch():
    """Prototype collapsed every failure into one red X."""
    po = ParsedPO(source_id="x", line_items=[LineItem("PART-A", 1, "1.00", "1.00")])
    f = REGISTRY["field_present"](
        _ctx(po), {"id": "qn", "severity": "BLOCKER", "field": "quote_number"})
    assert f.status is Status.FAIL and f.severity.value == "BLOCKER"


# ---------------------------------------------------------------- safety

def test_quote_number_format_is_whitelisted():
    src = StubQuoteSource()
    with pytest.raises(ValueError):
        src.fetch("F5Q-1' OR '1'='1")
    with pytest.raises(ValueError):
        src.fetch("not-a-quote")
    assert src.fetch("F5Q-12345678").found is False   # valid shape, absent


# --------------------------------------------------------------- ledger

def test_ledger_prevents_reprocessing(tmp_path, fixtures, quote):
    ledger = JsonLedger(tmp_path / "ledger.jsonl")
    src = LocalFolderSource(fixtures["ingram_ok"].parent)
    engine = load_engine("sos", ledger=ledger, rules_dir=RULES)
    qs = StubQuoteSource({quote.quote_number: quote})
    pipe = Pipeline(source=src, engine=engine, quote_source=qs, ledger=ledger)

    first = pipe.run()
    assert len(first) > 0
    second = pipe.run()
    # Everything that reached a terminal outcome is skipped. Anything
    # DEFERRED is deliberately picked up again — that is what deferral is.
    terminal_first = [r for r in first if r.outcome is not Outcome.DEFERRED]
    assert terminal_first
    assert all(r.outcome is Outcome.DEFERRED for r in second)


def test_duplicate_po_number_flagged(tmp_path, fixtures, quote):
    ledger = JsonLedger(tmp_path / "l.jsonl")
    ledger.record({"content_hash": "otherhash", "po_number": "4501234567",
                   "source_id": "earlier.pdf"})
    po = registry.parse(fixtures["ingram_ok"].read_bytes(), "again.pdf")
    f = REGISTRY["not_duplicate"](
        _ctx(po, quote, ledger), {"id": "d", "severity": "MAJOR"})
    assert f.status is Status.FAIL


# --------------------------------------------------------------- engine

def test_both_checklists_load_and_validate():
    for name in ("sos", "renewals"):
        cl = Checklist.load(name, RULES)
        assert cl.rules

def test_unknown_check_name_fails_at_load():
    with pytest.raises(ValueError, match="unknown check"):
        Checklist("bad", {"rules": [
            {"id": "x", "check": "does_not_exist", "severity": "MAJOR"}]})


def test_bad_severity_fails_at_load():
    with pytest.raises(ValueError, match="severity"):
        Checklist("bad", {"rules": [
            {"id": "x", "check": "has_line_items", "severity": "CATASTROPHE"}]})


def test_duplicate_rule_id_fails_at_load():
    with pytest.raises(ValueError, match="duplicate"):
        Checklist("bad", {"rules": [
            {"id": "x", "check": "has_line_items", "severity": "MAJOR"},
            {"id": "x", "check": "has_line_items", "severity": "MAJOR"}]})


def test_a_raising_rule_does_not_kill_the_run(quote):
    def boom(ctx, p):
        raise RuntimeError("nope")
    REGISTRY["_boom"] = boom
    try:
        cl = Checklist("t", {"rules": [
            {"id": "explodes", "check": "_boom", "severity": "MAJOR"},
            {"id": "lines", "check": "has_line_items", "severity": "BLOCKER"}]})
        po = ParsedPO(source_id="x", quote_number="F5Q-00972766",
                      line_items=[LineItem("PART-A", 1, "1.00", "1.00")])
        result = Engine(cl).run(po, quote)
        assert len(result.findings) == 2
        assert result.findings[0].status is Status.SKIP
        assert result.findings[1].status is Status.PASS
    finally:
        REGISTRY.pop("_boom")


def test_missing_po_number_blocks(quote):
    """Checklist item 2 is a hard requirement."""
    po = ParsedPO(source_id="x", quote_number="F5Q-00972766",
                  po_total="24242.10", confidence=1.0, layout="ingram",
                  f5_entity="CORP",
                  line_items=[LineItem("F5-BIG-VE-BTA-25MV18", 2, "10082.52", "20165.04"),
                              LineItem("F5-SVC-BIG-VE+PREL13", 2, "2038.53", "4077.06")])
    engine = load_engine("sos", ledger=NullLedger(), rules_dir=RULES)
    result = engine.run(po, quote)
    assert result.outcome is Outcome.REJECTED
    assert any(f.rule_id == "po_number_present" for f in result.blockers)


def test_unreadable_pdf_is_extraction_failed(tmp_path, quote):
    (tmp_path / "broken.pdf").write_bytes(b"nonsense")
    engine = load_engine("sos", ledger=NullLedger(), rules_dir=RULES)
    pipe = Pipeline(source=LocalFolderSource(tmp_path), engine=engine,
                    quote_source=StubQuoteSource(), ledger=NullLedger())
    results = pipe.run()
    assert results[0].outcome is Outcome.EXTRACTION_FAILED


# ------------------------------------------------------------ end to end

def test_end_to_end_outcomes(tmp_path, fixtures, quote):
    engine = load_engine("sos", ledger=NullLedger(), rules_dir=RULES)
    qs = StubQuoteSource({quote.quote_number: quote})
    router = ExceptionRouter(sinks=[], routing=engine.checklist.spec["routing"])
    pipe = Pipeline(source=LocalFolderSource(fixtures["ingram_ok"].parent),
                    engine=engine, quote_source=qs, router=router,
                    ledger=NullLedger())
    by_name = {Path(r.po.source_id).stem: r for r in pipe.run()}

    # unknown_quote cites a quote that is not in the source. On a first
    # sighting that is more likely replica lag than a bad PO, so it is
    # held rather than rejected.
    assert by_name["unknown_quote"].outcome is Outcome.DEFERRED
    # the mismatched PO must never come out clean
    assert by_name["ingram_mismatch"].outcome is not Outcome.VALIDATED
    assert any(f.rule_id == "line_items_match"
               for f in by_name["ingram_mismatch"].by_status(Status.FAIL))


def test_only_validated_books(fixtures, quote):
    """No booking form is created for anything that failed."""
    class FakeSF:
        def __init__(self): self.calls = []
        def create_booking_form(self, result):
            self.calls.append(result.po.source_id)
            return {"attempted": True, "success": True, "booking_form_id": "a1s000"}

    sf = FakeSF()
    engine = load_engine("sos", ledger=NullLedger(), rules_dir=RULES)
    router = ExceptionRouter(sinks=[], routing=engine.checklist.spec["routing"])
    pipe = Pipeline(source=LocalFolderSource(fixtures["ingram_ok"].parent),
                    engine=engine,
                    quote_source=StubQuoteSource({quote.quote_number: quote}),
                    router=router, ledger=NullLedger(), salesforce=sf)
    results = pipe.run()
    booked = {Path(p).stem for p in sf.calls}
    validated = {Path(r.po.source_id).stem for r in results
                 if r.outcome is Outcome.VALIDATED}
    assert booked == validated
    assert "ingram_mismatch" not in booked


# ------------------------------------------------------- deferral

def _deferrable_pipeline(tmp_path, fixtures, quote, **kw):
    ledger = JsonLedger(tmp_path / "l.jsonl")
    engine = load_engine("sos", ledger=ledger, rules_dir=RULES)
    return Pipeline(source=LocalFolderSource(fixtures["unknown_quote"].parent),
                    engine=engine,
                    quote_source=StubQuoteSource({quote.quote_number: quote}),
                    ledger=ledger, **kw), ledger


def test_missing_quote_defers_rather_than_rejecting(tmp_path, fixtures, quote):
    """A quote absent from the replica is probably lag, not a bad PO.

    Rejecting on the first look would have us tell a reseller their quote
    does not exist when it does and simply has not replicated yet.
    """
    pipe, _ = _deferrable_pipeline(tmp_path, fixtures, quote)
    results = {Path(r.po.source_id).stem: r for r in pipe.run()}
    assert results["unknown_quote"].outcome is Outcome.DEFERRED
    assert results["unknown_quote"].action["deferred_attempt"] == 1


def test_deferred_document_is_retried(tmp_path, fixtures, quote):
    pipe, _ = _deferrable_pipeline(tmp_path, fixtures, quote)
    pipe.run()
    again = {Path(r.po.source_id).stem: r for r in pipe.run()}
    assert "unknown_quote" in again
    assert again["unknown_quote"].action["deferred_attempt"] == 2


def test_deferral_gives_up_and_rejects(tmp_path, fixtures, quote):
    """Deferral must be bounded, or a genuinely bad quote number is
    retried forever and nobody is ever told."""
    pipe, _ = _deferrable_pipeline(tmp_path, fixtures, quote,
                                   defer_max_attempts=3)
    outcomes = []
    for _ in range(4):
        for r in pipe.run():
            if Path(r.po.source_id).stem == "unknown_quote":
                outcomes.append(r.outcome)
    assert outcomes[:3] == [Outcome.DEFERRED] * 3
    assert outcomes[3] is Outcome.REJECTED


def test_only_quote_failures_defer(tmp_path, fixtures, quote):
    """A PO with a real problem alongside the missing quote must not be
    held — the other problem is not going to resolve itself."""
    from po_validation.models import Status
    pipe, _ = _deferrable_pipeline(tmp_path, fixtures, quote)
    for r in pipe.run():
        blocker_ids = {f.rule_id for f in r.blockers}
        if r.outcome is Outcome.DEFERRED:
            assert blocker_ids <= Pipeline.DEFERRABLE


def test_deferred_never_books(tmp_path, fixtures, quote):
    class FakeSF:
        def __init__(self): self.calls = []
        def create_booking_form(self, result):
            self.calls.append(result.po.source_id)
            return {"attempted": True, "success": True}

    sf = FakeSF()
    ledger = JsonLedger(tmp_path / "l.jsonl")
    engine = load_engine("sos", ledger=ledger, rules_dir=RULES)
    router = ExceptionRouter(sinks=[], routing=engine.checklist.spec["routing"])
    pipe = Pipeline(source=LocalFolderSource(fixtures["unknown_quote"].parent),
                    engine=engine,
                    quote_source=StubQuoteSource({quote.quote_number: quote}),
                    router=router, ledger=ledger, salesforce=sf)
    results = pipe.run()
    booked = {Path(p).stem for p in sf.calls}
    deferred = {Path(r.po.source_id).stem for r in results
                if r.outcome is Outcome.DEFERRED}
    assert deferred and not (booked & deferred)


def test_po_without_f5_skus_is_not_rejected_on_part_matching():
    """Regression. PayPal prints internal item refs and Dell prints "Not
    Available" instead of F5 SKUs. The checklist accepts the cited quote in
    place of product information, so part-by-part matching must not reject
    them. The order total is still compared."""
    q = Quote(quote_number="F5Q-00986557", found=True,
              lines=[QuoteLine("F5-BIG-VE-BEST-1G", None, None, "63148.43"),
                     QuoteLine("F5-SVC-BIG-VE+PREM", None, None, "3933.17")])
    for items in ([LineItem("061925.1MG", 1, "63148.43", "63148.43"),
                   LineItem("061925.2MG", 1, "3933.17", "3933.17")],
                  [LineItem(None, 1, "63148.43", "63148.43"),
                   LineItem(None, 1, "3933.17", "3933.17")]):
        po = ParsedPO(source_id="x", po_total="67081.60", line_items=items)
        lines = REGISTRY["line_items_match"](
            _ctx(po, q), {"id": "r", "severity": "MAJOR", "extra_severity": "BLOCKER"})
        assert all(f.status is Status.PASS for f in lines)
        total = REGISTRY["po_total_matches_quote"](
            _ctx(po, q), {"id": "t", "severity": "MAJOR", "tolerance": "1.00"})
        assert total.status is Status.PASS


def test_california_is_not_read_as_canada():
    """Regression. A bare "CA" used to match Canada, so every California
    Ship To was treated as international and failed for a missing
    Importer of Record — NTT's PO, shipping to Redwood Shores, CA."""
    for lines, expect in (
        (["Oracle America Inc.", "500 Oracle Parkway", "Redwood Shores, CA 94065"], Status.PASS),
        (["Sun Life", "227 King St S", "Waterloo, ON N2J 4C5", "Canada"], Status.FAIL),
    ):
        po = ParsedPO(source_id="x",
                      parties=[{"role": "ship_to", "name": lines[0], "lines": lines}])
        f = REGISTRY["international_shipping"](_ctx(po), {"id": "i", "severity": "MAJOR"})
        assert f.status is expect, (lines[-1], f.message)
