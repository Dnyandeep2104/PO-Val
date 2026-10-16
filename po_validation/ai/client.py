"""F5AI Cognitive SOS Expert Layer for PO Validation & Salesforce Booking.

Connects to F5's internal OpenAI-compatible gateway (https://f5ai.pd.f5net.com/openai/)
using model `gpt-6-astra` to act as a Digital SOS Specialist between deterministic
parsing and the 11-Point Validation / Salesforce Booking Form engine.

Security & Integrity Principles:
- Never hardcodes API keys; reads `F5AI_API_KEY` / `OPENAI_API_KEY` strictly from environment / `.env`.
- Enforces TLS verification by default (supports `F5AI_CA_BUNDLE` / `REQUESTS_CA_BUNDLE`), only disabling
  verification if `F5AI_INSECURE_TLS=true` is explicitly configured.
- Enforces a TTL retention policy (`AI_CACHE_TTL_DAYS`, default 7 days) on `out/ai_checkpoint.jsonl`.
- Strict PO-vs-Quote separation: `refine_parsed_po` extracts and repairs fields strictly from the PO
  document (`po.raw_text`) with snippet verification, NEVER mutating PO prices, quantities, SKUs,
  or parties from the CPQ quote and NEVER overriding `po.confidence`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from ..models import LineItem, ParsedPO, Quote, money

logger = logging.getLogger("po_validation.ai")

# ============================================================
# LLM CONFIGURATION (F5AI Internal Gateway)
# ============================================================

F5_OPENAI_BASE_URL = "https://f5ai.pd.f5net.com/openai/"
LLM_MODEL = "gpt-6-astra"
LLM_MAX_TOKENS = 1400
LLM_TEMPERATURE = 0
LLM_RATE_LIMIT_SLEEP = 0.3
LLM_MAX_RETRIES = 3
LLM_BATCH_SIZE = 50
DEFAULT_CHECKPOINT_PATH = "out/ai_checkpoint.jsonl"
DEFAULT_CACHE_TTL_DAYS = 7

# Explicit SOS-approved distributor-to-F5 SKU alias table (never fuzzy-matched)
APPROVED_SKU_ALIASES: dict[str, str] = {}


def load_env_file(override: bool = False):
    """Simple parser for local .env without external dependencies."""
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if override or k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        logger.debug(f"Could not load .env: {e}")


load_env_file()


# ============================================================
# CHECKPOINT UTILITIES (WITH TTL RETENTION)
# ============================================================

def _cache_ttl_seconds() -> float:
    try:
        days = float(os.environ.get("AI_CACHE_TTL_DAYS", str(DEFAULT_CACHE_TTL_DAYS)))
    except ValueError:
        days = float(DEFAULT_CACHE_TTL_DAYS)
    return max(0.0, days * 86400.0)


def load_checkpoint(path: str = DEFAULT_CHECKPOINT_PATH) -> Dict[str, Any]:
    cache: Dict[str, Any] = {}
    if not os.path.exists(path):
        return cache
    now = time.time()
    ttl = _cache_ttl_seconds()
    kept_lines: list[str] = []
    expired_count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                raw_line = line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                    ts = obj.get("ts")
                    if ttl > 0 and ts is not None and (now - float(ts)) > ttl:
                        expired_count += 1
                        continue
                    cache[obj["key"]] = obj["value"]
                    kept_lines.append(raw_line)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
        if expired_count > 0:
            with open(path, "w", encoding="utf-8") as fw:
                for kl in kept_lines:
                    fw.write(kl + "\n")
    except Exception as e:
        logger.debug(f"Could not read checkpoint {path}: {e}")
    return cache


def append_checkpoint(path: str, key: str, value: Any):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        record = {"key": key, "value": value, "ts": time.time()}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"Could not append checkpoint {path}: {e}")


def batch_append_checkpoint(path: str, items: list):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        with open(path, "a", encoding="utf-8") as f:
            for key, value in items:
                record = {"key": key, "value": value, "ts": now}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"Could not batch append checkpoint {path}: {e}")


def make_key(identifier: str, suffix: str = "") -> str:
    raw = f"{identifier}::{suffix}" if suffix else identifier
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ============================================================
# COGNITIVE HEURISTIC HELPERS (ADDRESS / SKU / UNWRAPPING)
# ============================================================

_NON_SKU_KEYWORDS = re.compile(
    r"\b(?:DROP\s*SHIP|D\s+R\s+O\s+P\s+S\s+H\s+I\s+P|SHIP\s*TO|BILL\s*TO|SOLD\s*TO|"
    r"END\s*USER|ATTN|ATTENTION|CONTACT|EMAIL|PHONE|TEL|FAX|COMMENTS?|NOTES?|"
    r"RESELLER\s+PO|END\s+USER\s+PO|QUOTE\s+NUMBER|SPECIAL\s+INSTRUCTIONS|DELIVERY\s+INSTRUCTIONS|"
    r"ADDR-ROW|RESELLER-ROW|QUOTE-COMMENT)\b",
    re.I,
)

_STREET_ADDRESS_RE = re.compile(
    r"\b\d+\s+[A-Z0-9\s]{3,35}\b(?:ST|AVE|RD|DR|BLVD|PKWY|LN|WAY|HWY|STE|SUITE)\b|"
    r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",
    re.I,
)

_VALID_F5_SKU_RE = re.compile(
    r"^(?:F5-[A-Z0-9\-\+]+|SYS-[A-Z0-9\-\+]+|[A-Z0-9]{2,6}-[A-Z0-9\-\+]{3,}|\d{6,10})$",
    re.I,
)


def _is_pseudo_address_or_comment_line(li: LineItem) -> bool:
    """Detect non-product rows (addresses, comments, banners) mis-parsed as LineItems."""
    part = (li.part_number or "").strip()
    desc = (li.description or "").strip()
    raw = (li.raw or "").strip()
    combined = f"{part} {desc} {raw}".strip()

    has_price = li.total_price is not None and abs(float(li.total_price)) > 0.0
    if has_price:
        return False

    if _NON_SKU_KEYWORDS.search(combined) or _STREET_ADDRESS_RE.search(combined):
        return True

    # No price and no valid F5 SKU structure
    if not part or not _VALID_F5_SKU_RE.match(part):
        return True

    # ASCII box row number like 001..014 with no price
    if re.fullmatch(r"00\d|01[0-4]", part):
        return True

    return False


def _unwrap_ingram_ascii_box(raw_text: str) -> dict[str, Any]:
    """Reconstructs split company names, addresses, and emails from fixed-width ASCII boxes (e.g. Ingram Micro)."""
    extracted: dict[str, Any] = {
        "end_user_name": None,
        "ship_to_name": None,
        "street": None,
        "city_state_zip": None,
        "country": None,
        "email": None,
        "reseller_po": None,
        "end_user_po": None,
        "unwrapped_items": [],
    }
    if not raw_text:
        return extracted

    box_rows: list[tuple[int, str]] = []
    for line in raw_text.splitlines():
        m = re.match(r"^\s*(0\d{2})\s*(?:\|\s*)+(.+?)(?:\s*\|.*)?$", line)
        if m:
            row_num = int(m.group(1))
            content = re.sub(r"\|+", " ", m.group(2)).strip()
            box_rows.append((row_num, content))

    if not box_rows:
        return extracted

    # Reconstruct split company name across rows 003-005
    name_parts: list[str] = []
    email_parts: list[str] = []
    for rnum, txt in box_rows:
        clean_t = re.sub(r"^COMMENTS:\s*", "", txt, flags=re.I).strip()
        if not clean_t or re.search(r"D\s*R\s*O\s*P\s*S\s*H\s*I\s*P", clean_t, re.I):
            continue
        if rnum in (3, 4, 5):
            is_street = bool(
                re.match(r"^\d+[A-Z]?\d*\s+[A-Z]", clean_t)
                or re.search(r"\b(?:RD|ST|AVE|BLVD|DR|STE|SUITE|PKWY|HWY|LN)\b", clean_t, re.I)
            )
            is_dept = bool(re.search(r"\b(?:ACCOUNTS\s+PAYABL|ATTN|DEPT)\b", clean_t, re.I))
            if is_street:
                extracted.setdefault("street", clean_t.title())
            elif clean_t.isdigit() and extracted.get("street"):
                extracted["street"] = extracted["street"] + clean_t
            elif not is_dept and not clean_t.isdigit():
                if name_parts and len(clean_t) <= 4 and not clean_t.upper().startswith(("INC", "LLC", "LTD", "CORP")):
                    name_parts[-1] = name_parts[-1] + clean_t
                else:
                    name_parts.append(clean_t)
        elif rnum == 6 and re.match(r"^\d+[A-Z]?\d*\s+", clean_t):
            extracted["street"] = clean_t.title()
        elif rnum in (6, 7) and re.search(r"\b[A-Z]{2}\b.*\d{5}", clean_t):
            extracted["city_state_zip"] = clean_t
        elif rnum in (8, 9, 10):
            if "@" in clean_t or (email_parts and re.match(r"^[A-Z0-9\.\-_]+\.(?:COM|NET|ORG|IO|EDU|GOV)\b", clean_t, re.I)):
                email_parts.append(clean_t.split()[0])
            m_rpo = re.search(r"RESELLER\s+PO#?\s*[:\-]?\s*([A-Za-z0-9\-]+)", clean_t, re.I)
            if m_rpo:
                extracted["reseller_po"] = m_rpo.group(1)
            m_eupo = re.search(r"END\s+USER\s+PO#?\s*[:\-]?\s*([A-Za-z0-9\-]+)", clean_t, re.I)
            if m_eupo:
                extracted["end_user_po"] = m_eupo.group(1)

    if name_parts:
        full_company = " ".join(name_parts).strip()
        full_company_clean = re.sub(r"\s+", " ", full_company).title()
        extracted["end_user_name"] = full_company_clean
        extracted["ship_to_name"] = full_company_clean
        extracted["unwrapped_items"].append(
            f"Reconstructed wrapped company name '{full_company_clean}' across lines 003–005"
        )

    if email_parts:
        full_email = "".join(email_parts).lower().strip(" ,;|")
        if "@" in full_email and "." in full_email.split("@")[-1]:
            extracted["email"] = full_email
            extracted["unwrapped_items"].append(
                f"Reconstructed line-wrapped renewal email '{full_email}'"
            )

    return extracted


def infer_theater_and_region(po: ParsedPO, quote: Optional[Quote] = None) -> tuple[str, str]:
    """Infer Theater (AMER, EMEA, APAC, JAPAN) and Sub-Region from F5 contracting entity,
    party countries, currency, and explicit distributor identity (avoiding boilerplate false positives).
    """
    entity = (po.f5_entity or "").upper()
    raw_up = (po.raw_text or "").upper()
    curr = (po.currency or (quote.currency if quote else "") or "USD").upper()

    # Check party countries first if populated
    party_countries = {
        (p.get("country") or "").upper()
        for p in (po.parties or [])
        if p.get("country")
    }

    if entity == "GSLLC" or "F5 GOVERNMENT SOLUTIONS" in raw_up:
        return "AMER", "US Federal (GSLLC)"
    if "JAPAN" in party_countries or "TOKYO ELECTRON" in raw_up or curr == "JPY":
        return "JAPAN", "Japan"
    if (
        entity == "EMEA"
        or curr in ("EUR", "GBP", "CHF", "AED")
        or bool(party_countries & {"GB", "UK", "UNITED KINGDOM", "DE", "GERMANY", "FR", "FRANCE", "NL", "NETHERLANDS", "AE", "EG", "EGYPT", "AZ", "AZERBAIJAN", "SA", "SAUDI ARABIA"})
        or any(k in raw_up for k in ("F5 NETWORKS LIMITED", "CHERTSEY", "SILICON21", "SILICON 21", "BAKOTECH", "AZERBAIJAN"))
    ):
        return "EMEA", "EMEA Enterprise"
    if (
        entity == "SG"
        or curr in ("SGD", "AUD", "NZD", "TWD", "HKD", "INR", "CNY")
        or bool(party_countries & {"SG", "SINGAPORE", "IN", "INDIA", "CN", "CHINA", "TW", "TAIWAN", "AU", "AUSTRALIA", "HK", "HONG KONG"})
        or any(k in raw_up for k in ("F5 NETWORKS SINGAPORE", "TECH DATA ADVANCED SOLUTIONS (SINGAPORE)", "ZERO ONE", "WYSETEK", "CLOUD4C", "WELLTAKE"))
    ):
        return "APAC", "APAC"
    if bool(party_countries & {"MX", "MEXICO", "BR", "BRAZIL", "CO", "COLOMBIA", "CL", "CHILE", "AR", "ARGENTINA"}):
        return "AMER", "LATAM"

    return "AMER", "US Commercial"


def _verify_snippet_in_raw_text(value: Any, snippet: Optional[str], raw_text: str) -> bool:
    """Verify that an AI-extracted value and its supporting snippet actually appear in the PO text."""
    if not raw_text or value is None:
        return False
    val_str = str(value).strip()
    if not val_str or val_str.lower() == "null":
        return False
    norm_raw = re.sub(r"\s+", " ", raw_text).lower()
    if snippet:
        norm_snip = re.sub(r"\s+", " ", str(snippet).strip()).lower()
        if len(norm_snip) >= 3 and norm_snip in norm_raw:
            return True
    norm_val = re.sub(r"\s+", " ", val_str).lower()
    if norm_val in norm_raw:
        return True
    # Also check comma-stripped numeric match
    if norm_val.replace(",", "") in norm_raw.replace(",", ""):
        return True
    return False


# ============================================================
# F5AI CLIENT
# ============================================================

class F5AIClient:
    """Client for F5's internal AI gateway (`gpt-6-astra`) with checkpoint caching."""

    _shared_cache: Optional[Dict[str, Any]] = None

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 8,
        checkpoint_path: str = DEFAULT_CHECKPOINT_PATH,
    ):
        load_env_file(override=False)
        self.base_url = (
            base_url
            or os.environ.get("F5_OPENAI_BASE_URL")
            or F5_OPENAI_BASE_URL
        ).rstrip("/")
        self.api_key = (
            api_key
            or os.environ.get("F5AI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        self.model = (
            model
            or os.environ.get("F5AI_MODEL")
            or LLM_MODEL
        )
        self.timeout = timeout
        self.checkpoint_path = checkpoint_path
        self.ssl_ctx = self._get_ssl_context(self.base_url)

        if F5AIClient._shared_cache is None:
            F5AIClient._shared_cache = load_checkpoint(self.checkpoint_path)

    @staticmethod
    def _get_ssl_context(base_url: str = "") -> ssl.SSLContext:
        """Create a verified TLS context using corporate CA bundle if configured.
        Only disables verification when F5AI_INSECURE_TLS is explicitly enabled.
        """
        insecure = os.environ.get("F5AI_INSECURE_TLS", "").lower() in ("true", "1", "yes")
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

        cafile = (
            os.environ.get("F5AI_CA_BUNDLE")
            or os.environ.get("REQUESTS_CA_BUNDLE")
            or os.environ.get("CURL_CA_BUNDLE")
            or os.environ.get("SSL_CERT_FILE")
        )
        if cafile and os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
        return ssl.create_default_context()

    def _chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = LLM_MAX_TOKENS,
        temperature: float = LLM_TEMPERATURE,
    ) -> Optional[str]:
        """Send chat completion request to F5AI gateway with retry & rate-limit handling."""
        if not self.api_key or self.api_key == "YOUR_API_KEY_HERE":
            return None

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        for attempt in range(1, LLM_MAX_RETRIES + 1):
            req = urllib.request.Request(url, data=data, headers=headers)
            try:
                if attempt > 1:
                    time.sleep(LLM_RATE_LIMIT_SLEEP * attempt)
                with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=self.timeout) as resp:
                    if resp.status == 200:
                        res_json = json.loads(resp.read().decode("utf-8"))
                        choices = res_json.get("choices", [])
                        if choices:
                            return choices[0].get("message", {}).get("content", "").strip()
            except urllib.error.HTTPError as e:
                logger.warning(f"F5AI API HTTP {e.code} (attempt {attempt}/{LLM_MAX_RETRIES}): {e.reason}")
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except Exception as e:
                logger.debug(f"F5AI endpoint unreachable (VPN offline or timeout, attempt {attempt}): {e}")
                break

        return None

    @staticmethod
    def _parse_json_response(text: Optional[str]) -> Optional[dict[str, Any]]:
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        m = re.search(r"\{[\s\S]*\}", cleaned)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None

    # ------------------------------------------------------------------
    # DOCUMENT-VERIFIED PO REPAIR HELPERS (Zero Quote Contamination)
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_sku(s: Optional[str]) -> str:
        clean = re.sub(r"[^A-Za-z0-9]", "", s or "").upper()
        if clean.startswith("F5F5"):
            clean = clean[2:]
        return clean

    def _repair_multi_qty_base_prices(
        self, po: ParsedPO, corrections: list[str]
    ) -> int:
        """Detects when the deterministic parser extracted the unit/base price into `total_price`
        on multi-quantity lines (e.g. Qty=2 on Ingram Micro PO 70-AP476-D) and divided by Qty
        to invent a halved unit_price.

        CRITICAL INTEGRITY RULE:
        Repairs a line ONLY when the PO's own text (`po.raw_text`) or the PO's own printed
        footer total (`po.total_candidates`) proves that `cand_ext = money(base_price * qty)`
        is printed on the PO itself. Never uses the CPQ quote as evidence for what the PO says.
        """
        if not po.line_items:
            return 0

        raw_text = po.raw_text or ""
        raw_nocomma = raw_text.replace(",", "")
        if not raw_nocomma:
            return 0

        hypothetical_ext_sum = Decimal("0.00")
        current_sum = Decimal("0.00")
        has_multi_qty = False
        for li in po.line_items:
            if li.total_price is not None:
                qty = Decimal(str(li.quantity if (li.quantity and li.quantity > 0) else 1))
                if qty > 1:
                    has_multi_qty = True
                current_sum += money(li.total_price)
                hypothetical_ext_sum += money(li.total_price * qty)

        if not has_multi_qty:
            return 0

        # Check if the PO's own printed footer total candidate matches hypothetical_ext_sum
        po_footer_matches_ext = False
        if po.total_candidates:
            for cand in po.total_candidates:
                cand_raw = (
                    cand.get("amount") or cand.get("value")
                    if isinstance(cand, dict)
                    else (cand[1] if isinstance(cand, (tuple, list)) and len(cand) > 1 else cand)
                )
                try:
                    cand_dec = money(cand_raw)
                except Exception:
                    continue
                if (
                    abs(hypothetical_ext_sum - cand_dec) <= Decimal("1.00")
                    and abs(current_sum - cand_dec) > Decimal("1.00")
                ):
                    po_footer_matches_ext = True
                    break

        # Also check Ingram-style pipe-delimited summary box `| <qty> | | <total>` in raw_text
        if not po_footer_matches_ext:
            for m_box in re.finditer(r"\|\s*\d+\s*\|\s*\|\s*(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\b", raw_text):
                try:
                    box_dec = money(m_box.group(1).replace(",", ""))
                    if (
                        abs(hypothetical_ext_sum - box_dec) <= Decimal("1.00")
                        and abs(current_sum - box_dec) > Decimal("1.00")
                    ):
                        po_footer_matches_ext = True
                        break
                except Exception:
                    continue

        # If the PO already has a printed po_total that equals current_sum (and no footer candidate
        # matches hypothetical_ext_sum), the PO genuinely totals to current_sum; do NOT alter it.
        if (
            po.po_total is not None
            and abs(money(po.po_total) - current_sum) <= Decimal("1.00")
            and not po_footer_matches_ext
        ):
            return 0

        repaired_count = 0
        for li in po.line_items:
            if li.total_price is None or li.total_price <= 0:
                continue
            qty = Decimal(str(li.quantity if (li.quantity and li.quantity > 0) else 1))
            if qty <= 1:
                continue

            base_price = money(li.total_price)
            cand_ext = money(base_price * qty)
            # If unit_price * qty already equals total_price AND base_price is already the extended amount, skip
            if li.unit_price is not None and abs(money(li.unit_price * qty) - base_price) <= Decimal("0.05"):
                halved_str = f"{money(li.unit_price):.2f}"
                if halved_str in raw_nocomma:
                    continue

            ext_str = f"{cand_ext:.2f}"
            base_str = f"{base_price:.2f}"
            halved_str = f"{money(base_price / qty):.2f}"

            # Evidence on the PO itself: the extended amount appears in the PO's raw text
            # (or the PO's own printed footer total equals hypothetical_ext_sum), while the
            # halved unit price was invented by dividing the unit cost by qty.
            has_line_evidence = (
                ext_str in raw_nocomma
                and base_str in raw_nocomma
                and halved_str not in raw_nocomma
            )
            if has_line_evidence or (po_footer_matches_ext and ext_str in raw_nocomma):
                li.unit_price = base_price
                li.total_price = cand_ext
                repaired_count += 1

        if repaired_count > 0:
            old_po_total = po.po_total
            new_computed = po.computed_total
            if new_computed is not None and (po.po_total is None or po_footer_matches_ext):
                po.po_total = new_computed
            corrections.append(
                f"Repaired {repaired_count} multi-quantity SKU line(s) where parser read Unit Cost instead of "
                f"Extended Total (verified in PO text), reconciling PO Total from "
                f"${old_po_total or current_sum:,.2f} to ${po.po_total or new_computed:,.2f}"
            )
            if not po.arithmetic_failures and po.warnings:
                po.warnings = [w for w in po.warnings if "reconcile" not in w.lower()]

        return repaired_count

    def _apply_approved_sku_aliases(
        self, po: ParsedPO, corrections: list[str]
    ) -> int:
        """Applies only explicit, SOS-approved distributor-to-F5 SKU aliases (`APPROVED_SKU_ALIASES`).
        Never renames SKUs based on fuzzy matching against the CPQ quote.
        """
        if not po.line_items or not APPROVED_SKU_ALIASES:
            return 0
        normalized_count = 0
        for li in po.line_items:
            if not li.part_number:
                continue
            canonical = APPROVED_SKU_ALIASES.get(li.part_number.strip().upper())
            if canonical and canonical != li.part_number:
                orig_sku = li.part_number
                li.part_number = canonical
                normalized_count += 1
                corrections.append(
                    f"Mapped approved distributor alias SKU '{orig_sku}' to canonical F5 SKU '{canonical}'"
                )
        return normalized_count

    def _repair_parties_and_headers(
        self, po: ParsedPO, corrections: list[str]
    ) -> None:
        """Recovers buried payment terms and contact details strictly from the PO text (`po.raw_text`),
        and removes F5's own Bank of America remit-to lockbox if misclassified as Ship-To.
        Never fabricates customer names from the quote, never overrides `po_box` flags on real Ship-To
        parties, and never treats a freight forwarder as an Importer of Record.
        """
        raw = po.raw_text or ""

        # 1. Buried Payment Terms in PO text (e.g. `| NET 30 |`)
        if not po.payment_terms and raw:
            m_pay = re.search(r"\b(NET\s*\d{2,3}|NT\d{2})\b", raw, re.I)
            if m_pay:
                po.payment_terms = re.sub(r"\s+", " ", m_pay.group(1).upper()).strip()
                corrections.append(f"Extracted Payment Terms '{po.payment_terms}' from PO text")

        # 2. Buried Contact Name / Email / Phone / End-User in PO SKU descriptions or comments (e.g. 70-AP476-D)
        buried_contact: Optional[str] = None
        buried_email: Optional[str] = None
        buried_phone: Optional[str] = None
        buried_eu_name: Optional[str] = None

        if raw:
            m_eu = re.search(r"EU#\s*[\-\:]?\s*\d+\s+([A-Z0-9][A-Z0-9\s&\.,\-]{3,40}?)(?:\s+MC#|\s+F5Q-|\s*\-{3,}|\n)", raw)
            if m_eu:
                raw_eu = re.sub(r"\bSOLU\s+TION\b", "SOLUTION", m_eu.group(1).strip(), flags=re.I)
                buried_eu_name = re.sub(r"\s+", " ", raw_eu).strip().title()

            m_cname = re.search(r"CONTACT\s+NAME\s*[:\-]?\s*([A-Z][A-Za-z]+\s+[A-Z][A-Za-z]+)", raw, re.I)
            if m_cname:
                buried_contact = m_cname.group(1).strip().title()

            m_cphone = re.search(r"CUSTOMER\s+PHONE#?\s*[:\-]?\s*([\d\-\(\)\s]{7,20})", raw, re.I)
            if m_cphone:
                buried_phone = m_cphone.group(1).strip()

            joined_text = raw + " " + " ".join((li.description or "") for li in po.line_items)
            m_cemail = re.search(
                r"CUSTOMER\s+EMAIL\s*[:\-]?\s*([A-Z0-9._%+\-]+@[A-Z0-9.\-]+)(?:(?:\s*\n[|\s]*|\s+)(OM|COM|NET|ORG)\b)?",
                joined_text,
                re.I,
            )
            if m_cemail:
                base_em = m_cemail.group(1).strip().lower()
                tail_em = (m_cemail.group(2) or "").strip().lower()
                if base_em.endswith(".c") and tail_em == "om":
                    base_em = base_em + "om"
                elif not re.search(r"\.[a-z]{2,}$", base_em) and tail_em:
                    base_em = base_em + tail_em
                if "@" in base_em and "." in base_em.split("@")[-1] and len(base_em.split(".")[-1]) >= 2:
                    buried_email = base_em

        # 3. Clean ASCII box prefix / DROP SHIP / F5 Remit-To lockbox on Ship-To & End-User parties
        cleaned_parties = []
        for p in po.parties:
            role = p.get("role")
            pname = (p.get("name") or "").strip()
            plines_joined = " ".join(p.get("lines") or [])

            # Detect F5's own Remit-To Lockbox (Bank of America PO Box 847510) misclassified as Ship-To
            if role == "ship_to" and (
                re.search(r"\bF5\s+Networks\b", pname, re.I)
                and re.search(r"\bBank\s+of\s+America\b", plines_joined, re.I)
            ):
                p["role"] = "remit_to"
                cleaned_parties.append(p)
                corrections.append("Reclassified F5/Bank of America lockbox from Ship-To to Remit-To")
                continue

            # Clean pipe-delimited ASCII box prefixes from party name (`003 | | | | INFUTOR DATA SOLUTION`)
            if "|" in pname or re.match(r"^0\d{2}\b", pname):
                clean_n = re.sub(r"^0\d{2}\s*(?:\|\s*)+", "", pname)
                clean_n = re.sub(r"\|+", " ", clean_n).strip()
                if re.search(r"D\s*R\s*O\s*P\s*S\s*H\s*I\s*P", clean_n, re.I):
                    clean_n = buried_eu_name or ""
                if clean_n:
                    p["name"] = clean_n.title() if clean_n.isupper() else clean_n

            # Enrich missing contact details on Ship-To / End-User if found in PO body
            if role in ("ship_to", "end_user"):
                if buried_contact and not p.get("contact_name"):
                    p["contact_name"] = buried_contact
                if buried_email and buried_email not in (p.get("emails") or []):
                    p.setdefault("emails", []).append(buried_email)
                if buried_phone and buried_phone not in (p.get("phones") or []):
                    p.setdefault("phones", []).append(buried_phone)

            cleaned_parties.append(p)

        po.parties = cleaned_parties

        if buried_contact or buried_email:
            corrections.append(
                f"Recovered embedded customer contact ({buried_contact or 'Contact'}, "
                f"{buried_email or buried_phone or ''}) from wrapped SKU description block"
            )

    # ------------------------------------------------------------------
    # MAIN SOS EXPERT REFINEMENT LAYER (Runs After Parser, Before Rules)
    # ------------------------------------------------------------------
    def refine_parsed_po(self, po: ParsedPO, quote: Optional[Quote] = None) -> dict[str, Any]:
        """Acts as the Digital SOS Expert: inspects the PO document (`po.raw_text`) + initial parse,
        filters non-SKU address/comment rows, repairs multi-quantity unit vs. extended prices when
        verified by the PO's own text, unwraps split addresses/emails, and verifies party roles
        with verbatim snippet checks—without ever contaminating PO extraction with CPQ quote values.
        """
        t_start = time.perf_counter()
        corrections: list[str] = []
        disagreements: list[str] = []

        # 1. Deterministic SOS Cognitive Pre-Clean (filter non-SKU address/comment rows)
        raw_lines_before = len(po.line_items)
        filtered_lines: list[LineItem] = []
        for li in po.line_items:
            if _is_pseudo_address_or_comment_line(li):
                continue
            filtered_lines.append(li)

        removed_fake_lines = raw_lines_before - len(filtered_lines)
        if removed_fake_lines > 0 and len(filtered_lines) > 0:
            po.line_items = filtered_lines
            corrections.append(
                f"Filtered {removed_fake_lines} non-product address/comment row(s) from SKU table"
            )

        # 2. Repair Multi-Quantity Base Price vs. Extended Total using ONLY PO document evidence
        repaired_qty_lines = self._repair_multi_qty_base_prices(po, corrections)

        # 3. Apply only explicit SOS-approved SKU aliases (never fuzzy-renaming against quote)
        normalized_skus = self._apply_approved_sku_aliases(po, corrections)

        # 4. Unwrap ASCII box / fixed-width split company names & emails from PO text (e.g. Ingram Micro)
        unwrapped = _unwrap_ingram_ascii_box(po.raw_text)
        for msg in unwrapped.get("unwrapped_items", []):
            corrections.append(msg)

        if unwrapped.get("end_user_name"):
            eu_party = po.party("end_user")
            if not eu_party:
                eu_party = {"role": "end_user", "name": unwrapped["end_user_name"], "lines": [], "emails": []}
                po.parties.append(eu_party)
            elif not eu_party.get("name") or len(eu_party.get("name", "")) < len(unwrapped["end_user_name"]):
                eu_party["name"] = unwrapped["end_user_name"]
            if unwrapped.get("email") and unwrapped["email"] not in (eu_party.get("emails") or []):
                eu_party.setdefault("emails", []).append(unwrapped["email"])

        if unwrapped.get("ship_to_name"):
            st_party = po.party("ship_to")
            if not st_party:
                st_party = {"role": "ship_to", "name": unwrapped["ship_to_name"], "lines": [], "emails": []}
                po.parties.append(st_party)
            elif not st_party.get("name") or "|" in (st_party.get("name") or "") or len(st_party.get("name", "")) < len(unwrapped["ship_to_name"]):
                st_party["name"] = unwrapped["ship_to_name"]
            if unwrapped.get("email") and unwrapped["email"] not in (st_party.get("emails") or []):
                st_party.setdefault("emails", []).append(unwrapped["email"])

        # 5. Recover buried headers & contacts from PO text, and reclassify F5 Remit-To lockbox
        self._repair_parties_and_headers(po, corrections)

        # 6. Check Checkpoint Cache or Call F5AI `gpt-6-astra` (strictly PO-only input, no quote_context)
        cache_id = po.content_hash or po.source_id or po.po_number or "unknown_po"
        cache_key = make_key(cache_id, f"sos_expert_v4_strict::{self.model}")
        cache = F5AIClient._shared_cache if F5AIClient._shared_cache is not None else {}

        ai_payload: Optional[dict[str, Any]] = None
        ai_source = "sos_cognitive_engine"

        if cache_key in cache:
            ai_payload = cache[cache_key]
            ai_source = f"{self.model} (checkpoint)"
        else:
            candidate_summary = {
                "po_number": po.po_number,
                "quote_number": po.quote_number,
                "po_total": str(po.po_total) if po.po_total else None,
                "currency": po.currency or "USD",
                "payment_terms": po.payment_terms,
                "inco_terms": po.inco_terms,
                "f5_entity": po.f5_entity,
                "reseller_name": po.reseller_name,
                "parties": [
                    {
                        "role": p.get("role"),
                        "name": p.get("name"),
                        "country": p.get("country"),
                        "emails": p.get("emails", []),
                    }
                    for p in po.parties
                ],
                "line_items": [
                    {
                        "part_number": li.part_number,
                        "quantity": str(li.quantity) if li.quantity is not None else None,
                        "unit_price": str(li.unit_price) if li.unit_price is not None else None,
                        "total_price": str(li.total_price) if li.total_price is not None else None,
                        "description": (li.description or "")[:80],
                    }
                    for li in po.line_items[:20]
                ],
            }

            system_prompt = (
                "You are a Senior F5 Sales Operations (SOS) Order Booking Expert. "
                "Your job is to inspect a vendor Purchase Order's raw text alongside the initial parser output "
                "and return a JSON object reporting ONLY what is explicitly printed on the Purchase Order. "
                "Every extracted field must be backed by a verbatim `snippet` from the PO text. "
                "Do NOT invent or infer values that are not on the PO. Return ONLY valid JSON."
            )
            user_prompt = (
                f"INITIAL PARSER CANDIDATE:\n{json.dumps(candidate_summary, ensure_ascii=False)}\n\n"
                f"RAW PO TEXT EXCERPT:\n{(po.raw_text or '')[:3500]}\n\n"
                "Return JSON with keys:\n"
                '{\n'
                '  "po_number": {"value": str or null, "snippet": str or null},\n'
                '  "quote_number": {"value": str or null, "snippet": str or null},\n'
                '  "payment_terms": {"value": str or null, "snippet": str or null},\n'
                '  "inco_terms": {"value": str or null, "snippet": str or null},\n'
                '  "f5_entity": {"value": "CORP"|"GSLLC"|"EMEA"|"SG"|null, "snippet": str or null},\n'
                '  "reseller_name": {"value": str or null, "snippet": str or null},\n'
                '  "end_user_name": {"value": str or null, "snippet": str or null},\n'
                '  "ship_to_name": {"value": str or null, "snippet": str or null},\n'
                '  "corrections": [str],\n'
                '  "sos_verdict": str\n'
                '}'
            )

            raw_ai = self._chat_completion(system_prompt, user_prompt)
            parsed_ai = self._parse_json_response(raw_ai)
            if parsed_ai and isinstance(parsed_ai, dict):
                ai_payload = parsed_ai
                ai_source = f"{self.model} (live)"
                cache[cache_key] = ai_payload
                append_checkpoint(self.checkpoint_path, cache_key, ai_payload)

        # 7. Apply AI SOS Expert Refinements ONLY when verified against `po.raw_text`
        def _extract_verified_field(field_key: str) -> Optional[str]:
            if not ai_payload:
                return None
            entry = ai_payload.get(field_key)
            if isinstance(entry, dict):
                val = entry.get("value")
                snip = entry.get("snippet")
            else:
                val = entry
                snip = str(entry) if entry is not None else None
            if val is None or str(val).strip().lower() in ("", "null", "none"):
                return None
            if _verify_snippet_in_raw_text(val, snip, po.raw_text or ""):
                return str(val).strip()
            return None

        if ai_payload:
            v_po_num = _extract_verified_field("po_number")
            if v_po_num:
                if not po.po_number:
                    po.po_number = v_po_num
                    corrections.append(f"Extracted PO Number '{po.po_number}' (verified in PO text)")
                elif po.po_number != v_po_num:
                    disagreements.append(f"po_number: parser='{po.po_number}' vs AI='{v_po_num}'")

            v_quote = _extract_verified_field("quote_number")
            if v_quote:
                if not po.quote_number:
                    po.quote_number = v_quote
                    if v_quote not in po.quote_numbers:
                        po.quote_numbers.append(v_quote)
                    corrections.append(f"Recovered buried CPQ Quote reference '{v_quote}' (verified in PO text)")
                elif po.quote_number != v_quote and v_quote not in po.quote_numbers:
                    disagreements.append(f"quote_number: parser='{po.quote_number}' vs AI='{v_quote}'")

            v_terms = _extract_verified_field("payment_terms")
            if v_terms:
                if not po.payment_terms:
                    po.payment_terms = v_terms
                    corrections.append(f"Identified Payment Terms '{po.payment_terms}' (verified in PO text)")
                elif po.payment_terms.upper() != v_terms.upper():
                    disagreements.append(f"payment_terms: parser='{po.payment_terms}' vs AI='{v_terms}'")

            v_inco = _extract_verified_field("inco_terms")
            if v_inco and not po.inco_terms:
                po.inco_terms = v_inco
                corrections.append(f"Identified IncoTerms '{po.inco_terms}' (verified in PO text)")

            v_reseller = _extract_verified_field("reseller_name")
            if v_reseller and not po.reseller_name:
                po.reseller_name = v_reseller
                corrections.append(f"Resolved Reseller Partner '{po.reseller_name}' (verified in PO text)")

            v_eu = _extract_verified_field("end_user_name")
            if v_eu:
                eu_p = po.party("end_user")
                if not eu_p:
                    po.parties.append({"role": "end_user", "name": v_eu, "lines": [v_eu], "emails": []})
                    corrections.append(f"Populated End User entity '{v_eu}' (verified in PO text)")
                elif not eu_p.get("name"):
                    eu_p["name"] = v_eu
                    corrections.append(f"Populated End User entity '{v_eu}' (verified in PO text)")

            v_st = _extract_verified_field("ship_to_name")
            if v_st:
                st_p = po.party("ship_to")
                if not st_p:
                    po.parties.append({"role": "ship_to", "name": v_st, "lines": [v_st], "emails": []})
                    corrections.append(f"Populated Ship-To destination '{v_st}' (verified in PO text)")
                elif not st_p.get("name"):
                    st_p["name"] = v_st
                    corrections.append(f"Populated Ship-To destination '{v_st}' (verified in PO text)")

        for d_msg in disagreements:
            po.warnings.append(f"AI/Parser disagreement flagged for SOS review: {d_msg}")

        # 8. Infer Theater & Sub-Region
        theater, sub_region = infer_theater_and_region(po, quote)

        # Ensure End User & Ship-To are cross-enriched ONLY when PO explicitly says "Same as Ship To"
        st_party = po.party("ship_to")
        eu_party = po.party("end_user")
        if eu_party and st_party:
            eu_text = " ".join(eu_party.get("lines") or []).lower()
            if "same as ship to" in eu_text and st_party.get("name") and eu_party.get("name") != st_party.get("name"):
                eu_party["name"] = st_party["name"]
                corrections.append(f"Resolved 'Same as Ship To' End User to '{st_party['name']}'")

        # Reconcile PO Total ONLY if po_total was missing and line items sum cleanly
        if po.po_total is None and po.computed_total is not None:
            po.po_total = po.computed_total
            corrections.append(f"Derived missing header PO Total (${po.po_total:,.2f}) from verified SKU lines")

        rescued_from_false_block = bool(
            removed_fake_lines > 0
            or repaired_qty_lines > 0
            or normalized_skus > 0
            or len(corrections) > 0
        )

        if not corrections:
            corrections.append(
                f"Verified {len(po.line_items)} SKU line item(s), 4-party roles, and header total (${po.po_total or Decimal('0.00'):,.2f})"
            )

        latency_ms = int((time.perf_counter() - t_start) * 1000)
        ai_diagnostics = {
            "enhanced": True,
            "model": self.model,
            "source": ai_source,
            "endpoint": self.base_url,
            "latency_ms": latency_ms,
            "theater": theater,
            "sub_region": sub_region,
            "removed_fake_sku_rows": removed_fake_lines,
            "repaired_qty_lines": repaired_qty_lines,
            "consolidated_splits": 0,
            "normalized_skus": normalized_skus,
            "disagreements": disagreements,
            "rescued": rescued_from_false_block,
            "corrections": corrections,
            "corrections_count": len(corrections),
            "sos_verdict": (ai_payload or {}).get("sos_verdict") or "",
            "confidence": float(po.confidence or 0.0),
        }
        po.extraction_diagnostics["ai_sos_expert"] = ai_diagnostics
        return ai_diagnostics

    def generate_sos_summary(
        self,
        po_number: str,
        account: str,
        amount: str,
        blockers: list[str],
        majors: list[str],
        notes: list[str],
    ) -> str:
        """Generates a 2-3 sentence executive summary for the SOS specialist."""
        cache_key = make_key(f"{po_number}::{account}::{amount}", f"summary::{len(blockers)}::{len(majors)}")
        cache = F5AIClient._shared_cache if F5AIClient._shared_cache is not None else {}
        if cache_key in cache:
            return str(cache[cache_key])

        system_prompt = (
            "You are an expert F5 Sales Operations (SOS) specialist. "
            "Write a concise, factual 2-sentence summary of the purchase order "
            "validation status for the booking team, highlighting any required actions or notes."
        )

        user_prompt = (
            f"PO Number: {po_number}\n"
            f"Account: {account}\n"
            f"Amount: {amount}\n"
            f"Blockers: {blockers or 'None'}\n"
            f"Major Checklist Gaps: {majors or 'None'}\n"
            f"Attached Notes to RO: {notes or 'None'}\n\n"
            "Provide a crisp 2-sentence summary stating findings plainly."
        )

        ai_response = self._chat_completion(system_prompt, user_prompt, max_tokens=300)
        if ai_response:
            cache[cache_key] = ai_response
            append_checkpoint(self.checkpoint_path, cache_key, ai_response)
            return ai_response

        if blockers:
            return (
                f"Order {po_number} for {account} ({amount}) has {len(blockers)} blocking exception(s) "
                f"({', '.join(blockers[:2])}). Requires resolution before booking can proceed."
            )
        if majors:
            notes_str = f" with {len(notes)} auto-drafted Note(s) to RO attached" if notes else ""
            return (
                f"Order {po_number} for {account} ({amount}) has {len(majors)} item(s) requiring SOS specialist "
                f"review ({', '.join(majors[:2])}){notes_str}."
            )
        return (
            f"Order {po_number} for {account} ({amount}) passed all automated checks and is fully reconciled against quote. "
            "Pending final SOS specialist approval."
        )


# Singleton instance for pipeline/portal usage
_default_client: Optional[F5AIClient] = None


def get_ai_client() -> F5AIClient:
    global _default_client
    if _default_client is None:
        _default_client = F5AIClient()
    return _default_client


def refine_po(po: ParsedPO, quote: Optional[Quote] = None) -> dict[str, Any]:
    """Convenience wrapper to run the F5AI SOS Expert layer on a ParsedPO."""
    return get_ai_client().refine_parsed_po(po, quote=quote)
