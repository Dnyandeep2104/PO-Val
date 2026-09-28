"""Runs a checklist against a parsed PO.

The checklist is YAML, not Python. SOS and the renewals team get one file
each. Adding a check to SOS after a conversation with the business is a
config edit and a code review, not a refactor.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

import yaml

from ..models import (Finding, ParsedPO, Quote, Severity, Status,
                      ValidationResult, Outcome)
from .rules import REGISTRY, Ctx

log = logging.getLogger(__name__)

# Checklists are looked for next to the repo first, then inside the
# installed package. The second location is what lets a wheel run on a
# cluster with no repo checked out.
RULES_DIR = Path(__file__).resolve().parents[2] / "rules"
PACKAGED_RULES_DIR = Path(__file__).resolve().parents[1] / "rules"   # optional

# Checklists compiled into the source itself. Empty in the normal package,
# where the yaml files on disk are the source of truth. The single-file
# build fills this in so the bundle has no external dependencies at all.
EMBEDDED_CHECKLISTS: dict[str, str] = {}


class Checklist:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.rules = spec.get("rules", [])
        self.validate_spec()

    @classmethod
    def load(cls, name: str, rules_dir: Optional[Path] = None) -> "Checklist":
        searched = [Path(rules_dir)] if rules_dir else [RULES_DIR, PACKAGED_RULES_DIR]
        path = next((d / f"{name}.yaml" for d in searched
                     if (d / f"{name}.yaml").exists()), None)
        if path is not None:
            with open(path) as fh:
                return cls(name, yaml.safe_load(fh))
        if name in EMBEDDED_CHECKLISTS:
            return cls(name, yaml.safe_load(EMBEDDED_CHECKLISTS[name]))
        raise FileNotFoundError(
            f"No checklist '{name}.yaml' in any of: "
            + ", ".join(str(d) for d in searched)
            + (f" (embedded: {sorted(EMBEDDED_CHECKLISTS)})"
               if EMBEDDED_CHECKLISTS else ""))

    def validate_spec(self) -> None:
        """Fail loudly at load time, not halfway through a batch."""
        seen = set()
        for r in self.rules:
            for key in ("id", "check", "severity"):
                if key not in r:
                    raise ValueError(f"[{self.name}] rule missing '{key}': {r}")
            if r["id"] in seen:
                raise ValueError(f"[{self.name}] duplicate rule id: {r['id']}")
            seen.add(r["id"])
            if r["check"] not in REGISTRY:
                raise ValueError(
                    f"[{self.name}] rule '{r['id']}' references unknown check "
                    f"'{r['check']}'. Known: {sorted(REGISTRY)}")
            try:
                Severity(r["severity"])
            except ValueError:
                raise ValueError(
                    f"[{self.name}] rule '{r['id']}' has bad severity "
                    f"'{r['severity']}'. Use one of {[s.value for s in Severity]}.")

    def __repr__(self):
        return f"<Checklist {self.name}: {len(self.rules)} rules>"


class Engine:
    def __init__(self, checklist: Checklist, ledger=None):
        self.checklist = checklist
        self.ledger = ledger

    def run(self, po: ParsedPO, quote: Optional[Quote] = None) -> ValidationResult:
        ctx = Ctx(po=po, quote=quote, ledger=self.ledger, config=self.checklist.spec)
        result = ValidationResult(
            po=po, quote=quote,
            checklist=self.checklist.name,
            run_id=uuid.uuid4().hex[:12],
        )

        for spec in self.checklist.rules:
            if not spec.get("enabled", True):
                continue
            fn = REGISTRY[spec["check"]]
            try:
                out = fn(ctx, spec)
            except Exception as exc:
                log.exception("rule %s raised", spec["id"])
                out = Finding(
                    rule_id=spec["id"], status=Status.SKIP,
                    severity=Severity(spec["severity"]),
                    message=f"Rule errored and was skipped: {type(exc).__name__}: {exc}",
                )
            result.findings.extend(out if isinstance(out, list) else [out])

        # An unparseable document is its own outcome. Reporting a pile of
        # failed business rules against a PDF we never read would send
        # someone chasing a pricing discrepancy that does not exist.
        if po.layout == "failed" or (not po.line_items and not po.quote_number):
            result.outcome = Outcome.EXTRACTION_FAILED
        else:
            result.decide()
        return result


def load_engine(checklist_name: str = "sos", ledger=None,
                rules_dir: Optional[Path] = None) -> Engine:
    return Engine(Checklist.load(checklist_name, rules_dir), ledger=ledger)
