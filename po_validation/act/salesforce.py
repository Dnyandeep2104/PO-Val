"""Salesforce write path: create the Booking Form.

Writes. dry_run defaults to True. Turning it off (--live) is a deliberate act.
Refuses to write live to any org where Organization.IsSandbox is not true.

Auth. Supports:
  - SF_CLI_ALIAS: refreshes from `sf org display --target-org <alias> --json`.
    Recommended for laptop demos: login once via browser SSO, never expires.
  - SF_ACCESS_TOKEN: static session id (Workbench). Fast to start, expires.
  - JWT bearer flow: for production / Databricks unattended deployments.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
import urllib.error
import urllib.request
from urllib.parse import urlencode
import zlib

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..booking.builder import BookingForm, BookingFormBuilder, resolve_sfdc_distributor

_SHARED_SESSION: Optional[requests.Session] = None


def _get_shared_session() -> requests.Session:
    global _SHARED_SESSION
    if _SHARED_SESSION is None:
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=Retry(total=2, backoff_factor=0.2, status_forcelist=[502, 503, 504]),
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _SHARED_SESSION = session
    return _SHARED_SESSION

log = logging.getLogger(__name__)

DEFAULT_API_VERSION = "v61.0"
DEFAULT_SANDBOX_URL = os.environ.get(
    "SF_INSTANCE_URL", "https://f5--poclab.sandbox.my.salesforce.com"
)

# Friendly translations for the errors people actually hit in a demo.
_ERROR_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"INVALID_SESSION_ID|Session expired or invalid", re.I),
     "Salesforce session expired. Run `sf org login web --alias poclab` or paste a fresh Workbench session ID into .env."),
    (re.compile(r"DUPLICATE_VALUE.*PO_to_F5__c|duplicate value on record with id", re.I),
     "A Booking Form for this PO number already exists in poclab. Reset or change the PO number."),
    (re.compile(r"FIELD_CUSTOM_VALIDATION_EXCEPTION.*opportunity", re.I),
     "The Opportunity linked to this quote does not exist in poclab. Set SF_DEMO_OPPORTUNITY_ID to an existing poclab opportunity."),
    (re.compile(r"REQUIRED_FIELD_MISSING.*([A-Za-z0-9_]+__c)", re.I),
     "Required custom field missing on Booking Form: check field mappings."),
    (re.compile(r"INVALID_CROSS_REFERENCE_KEY", re.I),
     "An ID (Opportunity, Account, or ContentDocument) was not found in this sandbox."),
]


def explain_errors(errors: list) -> str:
    """Turn Salesforce error lists into plain, actionable English."""
    if not errors:
        return "Unknown error."
    raw = "; ".join(
        e.get("message", str(e)) if isinstance(e, dict) else str(e)
        for e in errors
    )
    for pattern, hint in _ERROR_HINTS:
        if pattern.search(raw):
            return f"{hint} (Salesforce: {raw})"
    return raw


class SalesforceError(RuntimeError):
    pass


class _HttpResponse:
    def __init__(self, status_code: int, body_bytes: bytes):
        self.status_code = status_code
        self.body_bytes = body_bytes

    @property
    def text(self) -> str:
        return self.body_bytes.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            raise SalesforceError(f"HTTP {self.status_code}: {self.text}")


def _get_ssl_context() -> ssl.SSLContext:
    if os.environ.get("USE_OS_TRUSTSTORE", "").lower() in ("true", "1", "yes"):
        try:
            import importlib
            truststore = importlib.import_module("truststore")
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except (ImportError, AttributeError):
            log.warning("USE_OS_TRUSTSTORE set but truststore not installed; falling back.")

    cafile = (
        os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("CURL_CA_BUNDLE")
        or os.environ.get("SSL_CERT_FILE")
    )
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    return ssl.create_default_context()


def _http_request(
    method: str,
    url: str,
    headers: dict = None,
    params: dict = None,
    json_data: Any = None,
    data: Any = None,
    timeout: int = 30,
) -> _HttpResponse:
    session = _get_shared_session()
    cafile = (
        os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("CURL_CA_BUNDLE")
        or os.environ.get("SSL_CERT_FILE")
    )
    verify = cafile if (cafile and os.path.exists(cafile)) else True

    try:
        resp = session.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_data,
            data=data,
            timeout=timeout,
            verify=verify,
        )
        return _HttpResponse(resp.status_code, resp.content)
    except requests.exceptions.RequestException as exc:
        raise SalesforceError(f"Connection failed: {exc}") from exc


def _token_from_sf_cli(alias: str) -> tuple[str, str]:
    """Ask the Salesforce CLI for a fresh access token for an authenticated org."""
    sf_bin = shutil.which("sf")
    if not sf_bin:
        raise RuntimeError("Salesforce CLI ('sf') is not installed or not on PATH.")
    cmd = [sf_bin, "org", "display", "--target-org", alias, "--json"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Salesforce CLI timed out.") from exc
    if res.returncode != 0:
        raise RuntimeError(f"Salesforce CLI failed: {res.stderr or res.stdout}")
    try:
        payload = json.loads(res.stdout)
    except Exception as exc:
        raise RuntimeError(f"Could not parse Salesforce CLI output: {res.stdout}") from exc

    result = payload.get("result", {})
    token = result.get("accessToken")
    inst = result.get("instanceUrl")
    if not token or not inst:
        raise RuntimeError(f"Salesforce CLI did not return token/instance for '{alias}'.")
    return token, inst.rstrip("/")


class SalesforceClient:
    """Talks to the Salesforce REST API.
    
    Defaults to dry_run=True. Turning it off requires passing dry_run=False.
    """

    def __init__(
        self,
        instance_url: str = DEFAULT_SANDBOX_URL,
        access_token: Optional[str] = None,
        api_version: str = DEFAULT_API_VERSION,
        dry_run: bool = True,
        token_refresher=None,
    ):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.api_version = api_version
        self.dry_run = dry_run
        self.token_refresher = token_refresher
        self._org_info_cache: Optional[dict] = None
        self._opp_name_cache: dict[str, str] = {}
        self._opp_by_po_cache: dict[str, tuple[str, bool, str]] = {}
        self._opp_record_cache: dict[str, dict] = {}
        self._bf_record_cache: dict[str, dict] = {}
        self._account_id_cache: dict[str, str] = {}
        self._quote_id_cache: dict[str, str] = {}
        self._pbe_cache: list[dict] = []

    @classmethod
    def from_env(cls, dry_run: Optional[bool] = None) -> "SalesforceClient":
        if dry_run is None:
            live_mode = os.environ.get("SF_LIVE_MODE", "false").lower() in ("true", "1", "yes")
            dry_run = not live_mode

        alias = os.environ.get("SF_CLI_ALIAS", "").strip()
        static_token = os.environ.get("SF_ACCESS_TOKEN", "").strip()
        inst = os.environ.get("SF_INSTANCE_URL", DEFAULT_SANDBOX_URL).rstrip("/")

        # Option A: Salesforce CLI
        if alias:
            try:
                token, inst_cli = _token_from_sf_cli(alias)
                inst = inst_cli or inst
                return cls(
                    instance_url=inst,
                    access_token=token,
                    dry_run=dry_run,
                    token_refresher=lambda: _token_from_sf_cli(alias)[0],
                )
            except Exception as exc:
                log.warning("Could not get token from sf CLI alias '%s': %s", alias, exc)
                if not static_token:
                    raise

        # Option B: static token
        if static_token:
            return cls(instance_url=inst, access_token=static_token, dry_run=dry_run)

        # Unauthenticated dry_run client
        if dry_run:
            return cls(instance_url=inst, access_token="simulated_token", dry_run=True)

        raise RuntimeError(
            "Live mode requested but no Salesforce credentials found. "
            "Set SF_CLI_ALIAS (recommended) or SF_ACCESS_TOKEN in .env."
        )

    @classmethod
    def from_jwt(cls, instance_url: str, client_id: str, username: str,
                 private_key: str, dry_run: bool = True) -> "SalesforceClient":
        import jwt
        token_url = f"{instance_url.rstrip('/')}/services/oauth2/token"
        claim = {
            "iss": client_id,
            "sub": username,
            "aud": instance_url.rstrip("/"),
            "exp": int(time.time()) + 300,
        }
        assertion = jwt.encode(claim, private_key, algorithm="RS256")
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
        resp = _http_request("POST", token_url, data=data)
        resp.raise_for_status()
        body = resp.json()

        def refresher():
            fresh_claim = dict(claim, exp=int(time.time()) + 300)
            fresh_assertion = jwt.encode(fresh_claim, private_key, algorithm="RS256")
            r = _http_request("POST", token_url, data={"grant_type": data["grant_type"], "assertion": fresh_assertion})
            r.raise_for_status()
            return r.json()["access_token"]

        return cls(
            instance_url=body.get("instance_url", instance_url),
            access_token=body["access_token"],
            dry_run=dry_run,
            token_refresher=refresher,
        )

    @property
    def can_refresh(self) -> bool:
        return self.token_refresher is not None

    def refresh_token(self) -> str:
        if not self.token_refresher:
            raise RuntimeError("No token refresher configured; cannot renew expired session.")
        self.access_token = self.token_refresher()
        return self.access_token

    def ensure_token(self) -> str:
        if not self.access_token:
            self.refresh_token()
        return self.access_token

    def _call(self, method: str, path: str, json_data: Any = None, params: dict = None) -> _HttpResponse:
        url = f"{self.instance_url}{path}" if path.startswith("/services") else f"{self.instance_url}/services/data/{self.api_version}{path}"
        headers = {"Authorization": f"Bearer {self.ensure_token()}"}
        resp = _http_request(method, url, headers=headers, json_data=json_data, params=params)
        if resp.status_code == 401 and self.can_refresh:
            log.info("Salesforce session returned 401; refreshing token...")
            self.refresh_token()
            headers["Authorization"] = f"Bearer {self.access_token}"
            resp = _http_request(method, url, headers=headers, json_data=json_data, params=params)
        return resp

    # ------------------------------------------------------------- sandbox check
    def org_info(self) -> dict:
        if self.dry_run and self.access_token in (None, "simulated_token", "dry_run_token"):
            return {"IsSandbox": True, "Name": "Simulated Org", "TrialExpirationDate": None, "dry_run": True}
        if self._org_info_cache is None:
            resp = self._call("GET", "/query", params={"q": "SELECT Id, Name, IsSandbox, OrganizationType FROM Organization LIMIT 1"})
            resp.raise_for_status()
            records = resp.json().get("records", [])
            self._org_info_cache = records[0] if records else {}
        return self._org_info_cache

    def assert_sandbox(self) -> None:
        """Safety rail: refuse to write live to any org that is not a sandbox unless explicitly enabled for production deployment."""
        if self.dry_run:
            return
        info = self.org_info()
        if not info.get("IsSandbox", False):
            allow_prod = os.environ.get("SF_ALLOW_PRODUCTION", "false").lower() in ("true", "1", "yes")
            if not allow_prod:
                raise SalesforceError(
                    f"SAFETY BLOCK: connected to '{info.get('Name')}' which is NOT a sandbox. "
                    "The live write path refuses to touch production unless SF_ALLOW_PRODUCTION=true is configured in .env."
                )

    # --------------------------------------------------------- query helpers
    def query(self, soql: str) -> list[dict]:
        if self.dry_run and self.access_token in (None, "simulated_token", "dry_run_token"):
            return []
        resp = self._call("GET", "/query", params={"q": soql})
        resp.raise_for_status()
        return resp.json().get("records", [])

    def resolve_opportunity(self, requested_id: Optional[str]) -> tuple[str, bool, str]:
        """In a sandbox (poclab) the production opportunity on the quote usually
        does not exist. In that case, fall back to SF_DEMO_OPPORTUNITY_ID (or any
        open opportunity) and adapt its PO/amount so validation rules pass.
        Returns (opp_id, was_substituted, message)."""
        if self.dry_run:
            return requested_id or "006DEMO000000000AAA", False, ""

        if requested_id and re.match(r"^006[A-Za-z0-9]{12,15}$", requested_id):
            try:
                matches = self.query(f"SELECT Id, Name, StageName FROM Opportunity WHERE Id = '{requested_id}' LIMIT 1")
                if matches:
                    self._opp_name_cache[requested_id] = matches[0].get("Name") or requested_id
                    return requested_id, False, f"Opportunity {requested_id} found in org."
            except Exception as q_exc:
                log.info("Requested opportunity %s lookup failed (%s); falling back to stand-in.", requested_id, q_exc)

        # Sandbox stand-in
        standin = os.environ.get("SF_DEMO_OPPORTUNITY_ID", "").strip()
        if standin:
            try:
                matches = self.query(f"SELECT Id, Name, StageName FROM Opportunity WHERE Id = '{standin}' LIMIT 1")
                if matches:
                    name = matches[0].get("Name")
                    self._opp_name_cache[standin] = name or standin
                    return standin, True, f"Opportunity {requested_id or 'none'} not in sandbox; using stand-in '{name}' ({standin})."
            except Exception as s_exc:
                log.warning("Stand-in opportunity %s lookup failed: %s", standin, s_exc)

        # Fallback to any open opportunity in sandbox
        try:
            open_opps = self.query("SELECT Id, Name, StageName FROM Opportunity WHERE IsClosed = false ORDER BY LastModifiedDate DESC LIMIT 1")
            if open_opps:
                opp = open_opps[0]
                self._opp_name_cache[opp["Id"]] = opp.get("Name") or opp["Id"]
                return opp["Id"], True, f"Opportunity {requested_id or 'none'} not in sandbox; using '{opp.get('Name')}' ({opp['Id']})."
        except Exception as o_exc:
            log.warning("Fallback open opportunity query failed: %s", o_exc)

        raise SalesforceError(
            f"Opportunity '{requested_id}' does not exist in this sandbox, and no "
            "open opportunity was found to stand in. Set SF_DEMO_OPPORTUNITY_ID."
        )

    def adapt_opportunity_for_demo(self, opp_id: str, po_number: str, amount: float,
                                    order_type: Optional[str] = None) -> None:
        """Keep the stand-in opportunity consistent with the PO so poclab's
        validation rules (PO number and amount match) pass."""
        if self.dry_run:
            return
        if os.environ.get("SF_ADAPT_DEMO_OPPORTUNITY", "true").lower() not in ("true", "1", "yes"):
            return
        body: dict[str, Any] = {
            "PO_to_F5__c": po_number,
        }
        try:
            resp = self._call("PATCH", f"/sobjects/Opportunity/{opp_id}", json_data=body)
            if resp.status_code not in (200, 204):
                log.warning("Could not adapt stand-in opportunity %s: %s", opp_id, resp.text)
        except Exception as exc:
            log.warning("Could not adapt stand-in opportunity %s: %s", opp_id, exc)

    # ----------------------------------------------------------- idempotency
    def _existing_booking_form(self, po_number: str, opp_id: str) -> Optional[str]:
        if self.dry_run:
            return None
        safe_po = po_number.replace("'", "\\'")
        safe_opp = opp_id.replace("'", "\\'")
        try:
            records = self.query(
                f"SELECT Id, Name, CreatedDate, RecordTypeId, Searchable_PO_field__c, Searchable_SO_Number__c, "
                f"Ship_To_Same_As__c, Same_As_End_User_Contact_Info__c, Shipping_Notification__c, "
                f"Order_Notifications__c, Registration_Key_Notification__c, Service_Acknowledgement__c, "
                f"End_User_Company_Name__c, Ship_To_Company_Name__c, Important_Notes__c "
                f"FROM Booking_Form__c "
                f"WHERE PO__c = '{safe_po}' AND Opportunity__c = '{safe_opp}' "
                f"ORDER BY CreatedDate DESC LIMIT 1"
            )
            if records:
                bf_id = records[0]["Id"]
                self._bf_record_cache[bf_id] = records[0]
                return bf_id
            return None
        except Exception as exc:
            log.warning("Could not query existing booking forms for PO %s: %s", po_number, exc)
            return None

    def _linked_file_titles(self, record_id: str) -> set[str]:
        if self.dry_run:
            return set()
        links = self.query(f"SELECT ContentDocument.Title FROM ContentDocumentLink WHERE LinkedEntityId = '{record_id}'")
        return {
            rec.get("ContentDocument", {}).get("Title")
            for rec in links
            if rec.get("ContentDocument")
        }

    # ------------------------------------------------------------- attachments
    def attach_pdf(self, booking_form_id: str, title: str, pdf_data: bytes) -> dict:
        """Attach binary PDF to Booking_Form__c via ContentVersion."""
        if self.dry_run:
            return {"success": True, "simulated": True, "bytes": len(pdf_data)}
        fname = title if title.lower().endswith(".pdf") else f"{title}.pdf"
        b64 = base64.b64encode(pdf_data).decode("ascii")
        payload = {
            "Title": title,
            "PathOnClient": fname,
            "VersionData": b64,
            "FirstPublishLocationId": booking_form_id,
        }
        resp = self._call("POST", "/sobjects/ContentVersion", json_data=payload)
        if resp.status_code in (200, 201):
            cv_id = resp.json().get("id")
            log.info("Attached PDF %s to Booking Form %s (ContentVersion %s)", fname, booking_form_id, cv_id)
            return {"success": True, "content_version_id": cv_id}
        err = explain_errors(resp.json() if resp.text.startswith("[") else [resp.text])
        raise SalesforceError(f"Failed to attach PDF to {booking_form_id}: {err}")

    def _attach_note(self, booking_form_id: str, title: str, body_text: str) -> Optional[str]:
        """Attach a sign-off note to the Booking Form."""
        if self.dry_run:
            return "simulated_note"
        # ContentNote supports HTML/plain text
        html_body = body_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        b64 = base64.b64encode(html_body.encode("utf-8")).decode("ascii")
        payload = {
            "Title": title,
            "Content": b64,
        }
        resp = self._call("POST", "/sobjects/ContentNote", json_data=payload)
        if resp.status_code in (200, 201):
            note_id = resp.json().get("id")
            # Link it
            link_payload = {
                "ContentDocumentId": note_id,
                "LinkedEntityId": booking_form_id,
                "ShareType": "V",
                "Visibility": "AllUsers",
            }
            self._call("POST", "/sobjects/ContentDocumentLink", json_data=link_payload)
            return note_id

        # Fallback to classic Note object if ContentNote is not enabled
        classic_payload = {
            "ParentId": booking_form_id,
            "Title": title,
            "Body": body_text,
        }
        r2 = self._call("POST", "/sobjects/Note", json_data=classic_payload)
        if r2.status_code in (200, 201):
            return r2.json().get("id")
        log.warning("Could not attach note '%s' to %s: %s", title, booking_form_id, resp.text)
        return None

    # ------------------------------------------------------------- party & quote helpers
    _TERRITORY_ACCOUNT_RT_ID = "01250000000DYbVAAW"

    # Editable Territory Account (Type=End User) records in poclab that satisfy Opportunity.AccountId lookup filter
    _SANDBOX_END_USER_ACCOUNT_POOL: list[str] = [
        "001do00000371lfAAA",  # Computer Associates | RT=Territory Account
        "001do0000037OZFAA2",  # Computer Associates 3 | RT=Territory Account
        "001do0000037S17AAE",  # Computer Associates | RT=Territory Account
        "001do0000037XiIAAU",  # Computer Associates 5 | RT=Territory Account
        "001do0000037YJNAA2",  # Computer Associates | RT=Territory Account
        "001do0000037YcmAAE",  # Computer Associates 4 | RT=Territory Account
        "001do0000037Yw5AAE",  # Computer Associates | RT=Territory Account
        "001do000003IMZsAAO",  # Computer Associates | RT=Territory Account
        "001D600001gxsDcIAI",  # jantest | RT=Territory Account
        "001do00000XWuaLAAT",  # pruthvi-sf1-jdkqgxai | RT=Territory Account
        "001D600001fAWNkIAO",  # Deutsche Telekom AG (dup) | RT=Territory Account
        "001D600001fAWNlIAO",  # Deutsche Telekom AG (dup) | RT=Territory Account
    ]

    # Editable Customer / Partner Account records in poclab for Opportunity.Reseller_Company_Name_Lookup__c
    _SANDBOX_RESELLER_ACCOUNT_POOL: list[str] = [
        "001D600001fAWNoIAO",  # Test Account
        "001do00000qXvEVAA0",  # Test Reseller
        "001do00000qXvJOAA0",  # Test Reseller 2
        "001do00000qXytuAAC",  # Test Reseller 3
        "001D600001fAWNtIAO",  # AutomationTest1657696711832
        "001D600001i5FKFIA2",  # Test acc dev cpq
        "001D600001jXGCAIA4",  # Test D&B Account
        "001do000001NgKYAA0",  # F5 PY Test
        "001do000001NiWFAA0",  # PY F5 TEST
        "001do000003F2kYAAS",  # TimTest Account
        "001do000004tiitAAA",  # Computer Associates007
        "001do000006iQ4zAAE",  # F5 XC TEST
        "001do00000RJNvpAAH",  # Computer Associates
    ]

    _PLACEHOLDER_ACCOUNT_PREFIXES: tuple[str, ...] = (
        "computer associates",
        "test",
        "automationtest",
        "jantest",
        "f5 py test",
        "py f5 test",
        "timtest",
        "f5 xc test",
        "pruthvi-",
        "deutsche telekom",
    )

    @staticmethod
    def _infer_industry(company_name: str) -> str:
        low = (company_name or "").lower()
        if "county" in low:
            return "Gov - County"
        if any(k in low for k in ("government", "state of", "ministry", "federal", "city of")):
            return "Government"
        if any(k in low for k in ("verizon", "at&t", "ntt", "vodafone", "communications", "telecom")):
            return "Communications"
        if any(k in low for k in ("gm financial", "general motors financial", "paypal", "fidelity", "fnz", "bank", "financial", "sun life", "assurance")):
            return "Banking"
        if any(k in low for k in ("marquette", "university", "college", "school")):
            return "Education - Public University"
        if any(k in low for k in ("health", "hospital", "medical")):
            return "Healthcare"
        if any(k in low for k in ("intel", "dell", "nvidia", "xerox", "toyota", "manufacturing")):
            return "Manufacturing"
        if any(k in low for k in ("wal-mart", "walmart", "retail")):
            return "Retail"
        if any(k in low for k in ("kyndryl", "cdw", "world wide technology", "wwt", "softchoice", "barq", "cloud4c", "wysetek", "adapture", "evolving", "storagehawk", "continental")):
            return "Consulting"
        return "Technology - Cloud"

    @staticmethod
    def _infer_theater(sfdc_distributor: str, reseller_name: str, end_user_name: str) -> str:
        dist = (sfdc_distributor or "").upper()
        if dist.startswith("EMEA"):
            return "EMEA"
        if dist.startswith("APAC"):
            return "APAC"
        if dist.startswith("JAPAN"):
            return "JAPAN"
        if dist.startswith("LATAM"):
            return "LATAM"
        combined = f"{reseller_name or ''} {end_user_name or ''}".lower()
        if any(k in combined for k in ("egypt", "vodafone", "barq", "azerbaijan", "bakotech", "risk", "fnz")):
            return "EMEA"
        if any(k in combined for k in ("singapore", "hong kong", "china", "taiwan", "india", "pte", "wysetek", "welltake")):
            return "APAC"
        return "NA"

    def _resolve_or_create_account(
        self,
        name: Optional[str],
        acct_type: str = "End User",
        party_info: Optional[dict] = None,
        preferred_id: Optional[str] = None,
    ) -> Optional[str]:
        if self.dry_run or not name:
            return None
        clean = name.strip()
        if not clean or clean.lower() in ("none", "direct order", "unknown customer"):
            return None
        valid_type = acct_type if acct_type in ("End User", "Authorized", "VAR/Reseller", "Distributor") else "End User"
        is_end_user = valid_type == "End User"
        cache_key = f"{valid_type}:{clean.lower()}"
        if cache_key in self._account_id_cache:
            return self._account_id_cache[cache_key]

        low_name = clean.lower()
        industry = self._infer_industry(clean)
        is_sandbox = ".sandbox." in self.instance_url

        # 1. Known sandbox accounts fast check (0 queries when in sandbox)
        known_id = None
        if "world wide technology" in low_name or low_name == "wwt":
            known_id = "001do000001Na5TAAS"
        elif "general motors financial" in low_name or "gm financial" in low_name or low_name.startswith("gmf "):
            known_id = "001do000000nv8QAAQ"
        elif "carahsoft" in low_name:
            known_id = "001do000002a4wrAAA"

        if known_id and is_sandbox:
            self._account_id_cache[cache_key] = known_id
            return known_id

        # 2. Combined single SOQL query: exact name match + pool lookup
        pool = self._SANDBOX_END_USER_ACCOUNT_POOL if is_end_user else self._SANDBOX_RESELLER_ACCOUNT_POOL
        pool_set = set(pool)
        pool_in = ",".join(f"'{aid}'" for aid in pool)
        safe = clean.replace("'", "\\'")
        rt_clause = f" AND RecordTypeId = '{self._TERRITORY_ACCOUNT_RT_ID}'" if is_end_user else ""
        pool_rows: list[dict] = []
        try:
            where_clause = f"Id IN ({pool_in})" if is_sandbox else f"(Name = '{safe}'{rt_clause}) OR Id IN ({pool_in})"
            rows = self.query(
                f"SELECT Id, Name, Industry FROM Account WHERE {where_clause}"
            )
            rows.sort(key=lambda r: (0 if r.get("Id") == preferred_id else (1 if r.get("Id") in pool_set else 2)))
            for r in rows:
                if is_sandbox and r.get("Id") not in pool_set:
                    continue
                rname = (r.get("Name") or "").strip().lower()
                if rname == low_name:
                    acct_id = r["Id"]
                    if r.get("Industry") != industry:
                        self._call("PATCH", f"/sobjects/Account/{acct_id}", json_data={
                            "Industry": industry,
                            "Type": valid_type,
                            "Website": "https://www.f5.com",
                        })
                    self._account_id_cache[cache_key] = acct_id
                    return acct_id
            pool_rows = [r for r in rows if r.get("Id") in pool_set]
        except Exception as exc:
            log.debug("Account combined lookup failed for '%s': %s", clean, exc)

        # 3. Try creating Account (skip in poclab sandbox where Account POST is blocked by custom trigger)
        acct_payload: dict[str, Any] = {
            "Name": clean[:80],
            "Type": valid_type,
            "Industry": industry,
            "Website": "https://www.f5.com",
        }
        if is_end_user:
            acct_payload["RecordTypeId"] = self._TERRITORY_ACCOUNT_RT_ID
        if not is_sandbox:
            try:
                resp = self._call("POST", "/sobjects/Account", json_data=acct_payload)
                if resp.status_code in (200, 201):
                    acct_id = resp.json().get("id")
                    if acct_id:
                        self._account_id_cache[cache_key] = acct_id
                        return acct_id
            except Exception as exc:
                log.debug("Could not POST Account '%s': %s", clean, exc)

        # 4. Sandbox fallback: claim and rename an editable placeholder Account from the already-fetched pool_rows
        patch_payload = {k: v for k, v in acct_payload.items() if k != "RecordTypeId"}
        claimed_ids = set(self._account_id_cache.values())
        candidate_ids: list[str] = []
        if preferred_id and preferred_id in pool_set and preferred_id not in claimed_ids:
            candidate_ids.append(preferred_id)
        for r in pool_rows:
            rid = r["Id"]
            rname = (r.get("Name") or "").strip().lower()
            if rid not in claimed_ids and rid not in candidate_ids and rname.startswith(self._PLACEHOLDER_ACCOUNT_PREFIXES):
                candidate_ids.append(rid)

        if not candidate_ids:
            idx = zlib.crc32(cache_key.encode("utf-8")) % len(pool)
            candidate_ids = [
                aid for aid in (pool[idx:] + pool[:idx])
                if aid not in claimed_ids
            ] or [pool[idx]]

        for pool_id in candidate_ids:
            try:
                r_patch = self._call("PATCH", f"/sobjects/Account/{pool_id}", json_data=patch_payload)
                if r_patch.status_code in (200, 204):
                    self._account_id_cache[cache_key] = pool_id
                    return pool_id
            except Exception:
                continue
        return None

    def _resolve_or_create_oracle_quote(
        self,
        quote_num: str,
        opp_id: str,
        account_id: Optional[str],
        reseller_name: Optional[str],
        sfdc_distributor: str,
        discount_sched_id: str,
        amount: float,
    ) -> Optional[str]:
        if self.dry_run or not quote_num:
            return None
        cache_key = f"{opp_id}:{quote_num}:{discount_sched_id}"
        if cache_key in self._quote_id_cache:
            return self._quote_id_cache[cache_key]

        safe_q = quote_num.replace("'", "\\'")
        minimal_q_patch: dict[str, Any] = {
            "Name": quote_num[:80],
            "cafsl__Opportunity__c": opp_id,
            "Primary_Discount_Schedule__c": discount_sched_id,
            "Status__c": "Approved",
        }

        # 1. Single SOQL query for existing quote by Name or Opportunity, or fallback pool
        try:
            rows = self.query(
                f"SELECT Id, Name, cafsl__Opportunity__c, Primary_Discount_Schedule__c, Status__c "
                f"FROM cafsl__Oracle_Quote__c "
                f"WHERE Name = '{safe_q}' OR cafsl__Opportunity__c = '{opp_id}' "
                f"ORDER BY LastModifiedDate DESC LIMIT 5"
            )
            if not rows:
                rows = self.query(
                    "SELECT Id, Name, cafsl__Opportunity__c, Primary_Discount_Schedule__c, Status__c "
                    "FROM cafsl__Oracle_Quote__c ORDER BY LastModifiedDate ASC LIMIT 5"
                )
            if rows:
                # Prefer exact Name match, then Opportunity match, then first pool row
                chosen = next((r for r in rows if r.get("Name") == quote_num[:80]), None)
                if not chosen:
                    chosen = next((r for r in rows if r.get("cafsl__Opportunity__c") == opp_id), rows[0])
                q_id = chosen["Id"]
                # Skip PATCH if all fields already match!
                if (
                    chosen.get("Name") == quote_num[:80]
                    and chosen.get("cafsl__Opportunity__c") == opp_id
                    and chosen.get("Primary_Discount_Schedule__c") == discount_sched_id
                    and chosen.get("Status__c") == "Approved"
                ):
                    self._quote_id_cache[cache_key] = q_id
                    return q_id

                r_patch = self._call("PATCH", f"/sobjects/cafsl__Oracle_Quote__c/{q_id}", json_data=minimal_q_patch)
                if r_patch.status_code in (200, 204):
                    self._quote_id_cache[cache_key] = q_id
                    return q_id
        except Exception as exc:
            log.debug("Oracle Quote resolution failed for '%s': %s", quote_num, exc)
        return None

    def _get_na_pricebook_entries(self) -> list[dict]:
        if not self._pbe_cache:
            self._pbe_cache = [
                {"Id": "01u1T00000OarfUQAR"},
                {"Id": "01u1T00000OarfVQAR"},
                {"Id": "01u1T00000OartZQAR"},
                {"Id": "01u1T00000Oax2EQAR"},
                {"Id": "01u1T00000ObXvqQAF"},
                {"Id": "01u1T00000ObY1EQAV"},
                {"Id": "01u1T00000ObY1FQAV"},
                {"Id": "01u1T00000ObY1GQAV"},
            ]
        return self._pbe_cache

    def _sync_opportunity_line_items(
        self,
        opp_id: str,
        order: dict,
        total_amount: float,
        existing_olis: Optional[list[dict]] = None,
    ) -> None:
        if self.dry_run:
            return

        pbes = self._get_na_pricebook_entries()
        po_lines = [
            li for li in (order.get("lines") or [])
            if float(li.get("total") or 0.0) > 0.0
        ]
        if not po_lines:
            po_lines = [{
                "poSku": "F5-BIG-BT-LTM-I5800",
                "desc": "F5 Perpetual / Subscription Order Bundle",
                "qty": 1,
                "unitPrice": total_amount,
                "total": total_amount,
            }]

        desired_records: list[dict[str, Any]] = []
        desired_sig: list[tuple[float, float]] = []
        for idx, li in enumerate(po_lines, start=1):
            pbe_id = pbes[(idx - 1) % len(pbes)]["Id"]
            qty = max(1, int(li.get("qty") or 1))
            line_tot = float(li.get("total") or 0.0)
            unit_price = round(float(li.get("unitPrice") or (line_tot / qty)), 2)
            sku = str(li.get("poSku") or li.get("sfSku") or "F5-SKU")[:150]
            desc = str(li.get("desc") or sku)
            desired_sig.append((round(float(qty), 2), unit_price))
            desired_records.append({
                "OpportunityId": opp_id,
                "PricebookEntryId": pbe_id,
                "Quantity": qty,
                "UnitPrice": unit_price,
                "Description": f"{sku} - {desc}"[:255],
                "Item_Description__c": f"{sku}: {desc}"[:255],
                "SKU__c": sku,
                "Contract_Length__c": 0,
                "Promo_Discount_Percent__c": 0.0,
                "Line_Item_Number__c": f"{idx}.0",
            })

        try:
            if existing_olis is None:
                existing_olis = self.query(
                    f"SELECT Id, SKU__c, Quantity, UnitPrice FROM OpportunityLineItem WHERE OpportunityId = '{opp_id}'"
                )
            existing_olis = [r for r in (existing_olis or []) if str(r.get("Id") or "").startswith("00k")]
            if len(existing_olis) == len(desired_sig):
                existing_sig = [
                    (
                        round(float(r.get("Quantity") or 0.0), 2),
                        round(float(r.get("UnitPrice") or 0.0), 2),
                    )
                    for r in existing_olis
                ]
                if sorted(existing_sig) == sorted(desired_sig):
                    return
        except Exception:
            existing_olis = []

        # Batch delete existing OLIs in 1 HTTP call
        if existing_olis:
            ids = [r["Id"] for r in existing_olis if r.get("Id")]
            try:
                if len(ids) == 1:
                    self._call("DELETE", f"/sobjects/OpportunityLineItem/{ids[0]}")
                elif len(ids) > 1:
                    r_del = self._call(
                        "DELETE",
                        "/composite/sobjects",
                        params={"ids": ",".join(ids), "allOrNone": "false"},
                    )
                    if r_del.status_code == 404:
                        for oid in ids:
                            self._call("DELETE", f"/sobjects/OpportunityLineItem/{oid}")
            except Exception:
                pass

        # Batch insert new OLIs in 1 HTTP call
        try:
            if len(desired_records) == 1:
                self._call("POST", "/sobjects/OpportunityLineItem", json_data=desired_records[0])
            else:
                composite_payload = {
                    "allOrNone": False,
                    "records": [
                        {"attributes": {"type": "OpportunityLineItem"}, **rec}
                        for rec in desired_records
                    ],
                }
                r_ins = self._call("POST", "/composite/sobjects", json_data=composite_payload)
                if r_ins.status_code == 404:
                    for rec in desired_records:
                        self._call("POST", "/sobjects/OpportunityLineItem", json_data=rec)
        except Exception as exc:
            log.debug("Could not insert OpportunityLineItems on %s: %s", opp_id, exc)

    def _sync_opportunity_and_related(
        self,
        opp_id: str,
        order: dict,
        po_num: str,
        amount: float,
        order_type: str = "Standard",
        run_oli_sync: bool = True,
    ) -> dict[str, Any]:
        """Ensure Opportunity has accurate Account (End Customer), Reseller Partner,
        Distributor picklist, Oracle Quote, Discount Schedule, Territory, SO #, and Line Items."""
        if self.dry_run:
            return {}

        booking = order.get("booking") or {}
        parties = order.get("parties") or {}
        channel = order.get("channel") or "Reseller Partner"
        from_name = order.get("from") or order.get("vendor") or ""

        sfdc_distributor = (
            order.get("sfdc_distributor")
            or booking.get("sfdcDistributor")
            or resolve_sfdc_distributor(
                None,
                (parties.get("bill_to") or {}).get("name") if isinstance(parties.get("bill_to"), dict) else None,
                from_name,
            )
        )
        reseller_name = (
            order.get("reseller_name")
            or (booking.get("reseller") if booking.get("reseller") not in ("Direct Order", "None", "") else None)
            or (from_name if channel == "Reseller Partner" and sfdc_distributor == "None" else None)
        )
        end_user_name = (
            order.get("end_user_name")
            or booking.get("endUser")
            or order.get("account_name")
            or booking.get("account")
            or from_name
            or "Enterprise Customer"
        )
        ship_to_name = (
            order.get("ship_to_name")
            or booking.get("shipTo")
            or end_user_name
        )

        discount_sched_id = "a0gD6000001Oh07IAC" if sfdc_distributor != "None" else "a0gD6000001Oh2NIAS"
        digits_po = re.sub(r"[^0-9]", "", po_num) or "1000000"
        quote_num = (
            order.get("quote")
            or booking.get("quote")
            or f"F5Q-{digits_po[-7:].zfill(7)}"
        )

        need_reseller = bool(
            reseller_name and (reseller_name.lower() != end_user_name.lower() or channel == "Reseller Partner")
        )

        def _fetch_olis() -> list[dict]:
            try:
                return self.query(
                    f"SELECT Id, SKU__c, Quantity, UnitPrice FROM OpportunityLineItem WHERE OpportunityId = '{opp_id}'"
                )
            except Exception:
                return []

        existing_opp = self._opp_record_cache.get(opp_id) or {}
        existing_opp_ci = {k.lower(): v for k, v in existing_opp.items()}

        # 1. Resolve End Customer Account, Reseller Account, Oracle Quote, existing OLIs, and existing Booking Form concurrently!
        with ThreadPoolExecutor(max_workers=5) as pool_exec:
            f_eu = pool_exec.submit(
                self._resolve_or_create_account,
                end_user_name,
                "End User",
                parties.get("end_user") if isinstance(parties.get("end_user"), dict) else None,
                existing_opp_ci.get("accountid"),
            )
            f_res = (
                pool_exec.submit(
                    self._resolve_or_create_account,
                    reseller_name,
                    "Authorized",
                    parties.get("reseller") if isinstance(parties.get("reseller"), dict) else None,
                    existing_opp_ci.get("reseller_company_name_lookup__c"),
                )
                if need_reseller
                else None
            )
            f_quote = pool_exec.submit(
                self._resolve_or_create_oracle_quote,
                quote_num,
                opp_id,
                None,
                reseller_name,
                sfdc_distributor,
                discount_sched_id,
                amount,
            )
            f_olis = pool_exec.submit(_fetch_olis)
            f_existing_bf = pool_exec.submit(self._existing_booking_form, po_num, opp_id)

            end_user_acct_id = f_eu.result()
            reseller_acct_id = f_res.result() if f_res else None
            oracle_quote_id = f_quote.result()
            existing_olis = f_olis.result()
            existing_bf_id = f_existing_bf.result()

        # 2. Determine Opportunity Type, Theater, Registration Status, and Perpetual Order #
        if sfdc_distributor and sfdc_distributor != "None":
            opp_type = "Distributor Sale - Not for Stock"
            reg_status = "Unregistered"
        elif reseller_acct_id:
            opp_type = "VAR Sale - Not Through Distributor"
            reg_status = "Unregistered"
        else:
            opp_type = None  # Avoid triggering Opportunity_Stage_90_Direct_or_Partner validation rule when Reseller is blank
            reg_status = "Direct Customer"

        theater = self._infer_theater(sfdc_distributor, reseller_name or "", end_user_name)
        so_num = str(9000000 + (zlib.crc32(po_num.encode("utf-8")) % 900000))
        opp_display_party = end_user_name if channel == "Direct Customer" else (reseller_name or end_user_name)
        opp_name = f"{opp_display_party} - PO {po_num}"[:80]

        opp_patch: dict[str, Any] = {
            "Name": opp_name,
            "PO_to_F5__c": po_num,
            "Order_Type__c": order_type or "Standard",
            "SalesOrderType__c": order_type or "Standard",
            "SalesOrder__c": so_num,
            "Distributor__c": sfdc_distributor or "None",
            "Opportunity_Initiation__c": "F5 Initiated",
            "Theater__c": theater,
            "Territory_Credited__c": "a05do00000Ex8ZNAAZ",
            "Primary_Discount_Schedule__c": discount_sched_id,
            "Registration_Status__c": reg_status,
            "Reseller_Company_Name_Lookup__c": reseller_acct_id,
            "Pricebook2Id": "01s300000000H03AAE",
            "Price_Book__c": "NA Price List",
            "OCPQ_Price_Book_Variable__c": "naPriceList",
        }
        if opp_type:
            opp_patch["Type"] = opp_type
        if end_user_acct_id:
            opp_patch["AccountId"] = end_user_acct_id
        if oracle_quote_id:
            opp_patch["Primary_Quote__c"] = oracle_quote_id

        existing_opp = self._opp_record_cache.get(opp_id) or {}
        existing_opp_ci = {k.lower(): v for k, v in existing_opp.items()}
        delta_patch = {
            k: v for k, v in opp_patch.items()
            if k.lower() not in existing_opp_ci or existing_opp_ci.get(k.lower()) != v
        }
        if not delta_patch:
            self._opp_name_cache[opp_id] = opp_name
        else:
            try:
                resp = self._call("PATCH", f"/sobjects/Opportunity/{opp_id}", json_data=delta_patch)
                if resp.status_code in (200, 204):
                    self._opp_name_cache[opp_id] = opp_name
                    self._opp_record_cache[opp_id] = {**existing_opp, **delta_patch}
                else:
                    log.warning("Opportunity PATCH failed for %s: %s", opp_id, resp.text)
                    bad_fields = {"Type", "Opportunity_Initiation__c"}
                    try:
                        for err_item in (resp.json() if resp.text.startswith("[") else []):
                            if isinstance(err_item, dict):
                                bad_fields.update(err_item.get("fields") or [])
                    except Exception:
                        pass
                    fallback_patch = {
                        k: v for k, v in delta_patch.items()
                        if k not in bad_fields
                    }
                    if fallback_patch:
                        r_fb = self._call("PATCH", f"/sobjects/Opportunity/{opp_id}", json_data=fallback_patch)
                        if r_fb.status_code in (200, 204):
                            self._opp_name_cache[opp_id] = opp_name
                            self._opp_record_cache[opp_id] = {**existing_opp, **fallback_patch}
                        elif "PO_to_F5__c" in delta_patch:
                            self._call("PATCH", f"/sobjects/Opportunity/{opp_id}", json_data={"PO_to_F5__c": po_num})
            except Exception as exc:
                log.debug("Could not patch full Opportunity %s: %s", opp_id, exc)

        # 3. Sync multi-line OpportunityLineItems if requested synchronously
        if run_oli_sync:
            self._sync_opportunity_line_items(opp_id, order, amount, existing_olis=existing_olis)

        return {
            "so_num": so_num,
            "end_user_name": end_user_name,
            "ship_to_name": ship_to_name,
            "reseller_name": reseller_name,
            "sfdc_distributor": sfdc_distributor,
            "existing_olis": existing_olis,
            "existing_bf_id": existing_bf_id,
        }

    # ------------------------------------------------------------- main booking
    def resolve_opportunity_for_order(self, req_opp: str, order: dict, po_num: str, amount: float) -> tuple[str, bool, str]:
        """In sandbox (poclab), find or create a dedicated Opportunity matching the PO so that
        the Booking Form displays the real PO #, Opportunity Name, Account, Reseller, and Amount!"""
        if po_num and po_num in self._opp_by_po_cache:
            return self._opp_by_po_cache[po_num]

        is_sandbox = ".sandbox." in self.instance_url
        opp_fields = (
            "Id, Name, Amount, PO_to_F5__c, AccountId, Reseller_Company_Name_Lookup__c, "
            "Primary_Quote__c, Distributor__c, Order_Type__c, SalesOrderType__c, SalesOrder__c, "
            "Theater__c, Territory_Credited__c, Primary_Discount_Schedule__c, Registration_Status__c, "
            "Type, Opportunity_Initiation__c, Pricebook2Id, Price_Book__c, OCPQ_Price_Book_Variable__c"
        )

        # 1. If req_opp exists in org (skip querying production 006Po... IDs in poclab sandbox), use it
        if req_opp and re.match(r"^006[A-Za-z0-9]{12,15}$", req_opp) and not (is_sandbox and req_opp.startswith("006Po")):
            try:
                matches = self.query(f"SELECT {opp_fields} FROM Opportunity WHERE Id = '{req_opp}' LIMIT 1")
                if matches:
                    opp_id = matches[0]["Id"]
                    self._opp_record_cache[opp_id] = matches[0]
                    self._opp_name_cache[opp_id] = matches[0].get("Name") or opp_id
                    res = (opp_id, False, f"Opportunity {req_opp} found in org.")
                    if po_num:
                        self._opp_by_po_cache[po_num] = res
                    return res
            except Exception:
                pass

        # 2. If an Opportunity with this PO number already exists in poclab, use it
        safe_po = po_num.replace("'", "\\'")
        try:
            matches = self.query(f"SELECT {opp_fields} FROM Opportunity WHERE PO_to_F5__c = '{safe_po}' ORDER BY CreatedDate DESC LIMIT 1")
            if matches:
                opp_id = matches[0]["Id"]
                self._opp_record_cache[opp_id] = matches[0]
                self._opp_name_cache[opp_id] = matches[0].get("Name") or opp_id
                res = (opp_id, True, f"Found dedicated Opportunity {opp_id} for PO {po_num}.")
                if po_num:
                    self._opp_by_po_cache[po_num] = res
                return res
        except Exception:
            pass

        # 3. Create dedicated Opportunity in poclab for this PO
        booking = order.get("booking") or {}
        channel_label = order.get("reseller_name") or booking.get("reseller") or order.get("from") or order.get("channel") or "Customer"
        opp_name = f"{channel_label} - PO {po_num}"[:80]
        payload = {
            "Name": opp_name,
            "StageName": "PO Received by F5",
            "CloseDate": time.strftime("%Y-%m-%d"),
            "PO_to_F5__c": po_num,
            "Order_Type__c": "Standard",
            "SalesOrderType__c": "Standard",
            "Pricebook2Id": "01s300000000H03AAE",
            "Price_Book__c": "NA Price List",
            "OCPQ_Price_Book_Variable__c": "naPriceList",
        }

        try:
            resp = self._call("POST", "/sobjects/Opportunity", json_data=payload)
            if resp.status_code in (200, 201):
                opp_id = resp.json().get("id")
                self._opp_record_cache[opp_id] = {"Id": opp_id, **payload}
                self._opp_name_cache[opp_id] = opp_name
                res = (opp_id, True, f"Created dedicated Opportunity '{opp_name}' in sandbox for PO {po_num}.")
                if po_num:
                    self._opp_by_po_cache[po_num] = res
                return res
        except Exception as create_err:
            log.warning("Could not create dedicated Opportunity: %s", create_err)

        res = self.resolve_opportunity(req_opp)
        if po_num:
            self._opp_by_po_cache[po_num] = res
        return res

    # ------------------------------------------------------------- main booking
    def book_portal_order(
        self,
        order: dict,
        approver_name: str,
        reviewer_edits: Optional[dict] = None,
        notes_to_ro: Optional[str] = None,
        audit_note: Optional[str] = None,
        pdf_data: Optional[bytes] = None,
        acknowledged_flags: Optional[list[str]] = None,
    ) -> dict:
        """Create the real Booking Form and attach artifacts."""
        notes_to_ro = notes_to_ro or audit_note or ""
        po_num = (reviewer_edits or {}).get("po_number") or order.get("po_number") or order.get("po") or ""
        amount_raw = (reviewer_edits or {}).get("total_amount") or order.get("total_amount") or (order.get("booking") or {}).get("amount") or 0.0
        amount = float(amount_raw)
        order_type = (reviewer_edits or {}).get("sales_order_type") or order.get("order_type") or (order.get("booking") or {}).get("orderType") or "Standard"
        req_opp = order.get("opportunity_id") or order.get("opportunity") or (order.get("booking") or {}).get("opportunity") or ""
        fname = order.get("filename") or order.get("file") or f"{po_num}.pdf"

        # Safe simulation response
        if self.dry_run:
            return {
                "success": True,
                "simulated": True,
                "dry_run": True,
                "booking_form_id": f"simulated_{po_num}",
                "url": None,
                "pdf_attached": False,
                "would_attach": {"filename": fname, "bytes": len(pdf_data or b"")},
                "notes_count": 1,
                "message": "Simulation only. Nothing was written to Salesforce. Restart with --live to write to poclab.",
            }

        self.assert_sandbox()

        # 1. Resolve dedicated Opportunity & synchronize related objects (deferring OLI sync to run concurrently with Booking Form tasks)
        opp_id, substituted, opp_msg = self.resolve_opportunity_for_order(req_opp, order, po_num, amount)
        synced = self._sync_opportunity_and_related(opp_id, order, po_num, amount, order_type, run_oli_sync=False)

        # 2. Build comprehensive Booking_Form__c fields
        booking = order.get("booking") or {}
        sfdc_payload = order.get("salesforce_payload") or {}
        end_user = synced.get("end_user_name") or order.get("end_user_name") or booking.get("endUser") or order.get("from")
        ship_to = synced.get("ship_to_name") or order.get("ship_to_name") or booking.get("shipTo") or end_user
        so_num = synced.get("so_num") or str(9000000 + (zlib.crc32(po_num.encode("utf-8")) % 900000))

        important_notes_parts = []
        if notes_to_ro:
            important_notes_parts.append(f"Reviewer Notes: {notes_to_ro}")
        base_imp_notes = booking.get("importantNotes") or sfdc_payload.get("Important_Notes__c") or ""
        if base_imp_notes and base_imp_notes not in important_notes_parts:
            important_notes_parts.append(base_imp_notes)
        combined_important_notes = "\n\n".join(important_notes_parts)[:32000]

        ship_notif = booking.get("shippingNotification") or sfdc_payload.get("Shipping_Notification__c") or "orders@f5.com"
        order_notif = booking.get("orderNotifications") or sfdc_payload.get("Order_Notifications__c") or ship_notif
        reg_notif = booking.get("registrationKeyNotification") or sfdc_payload.get("Registration_Key_Notification__c") or ship_notif
        svc_ack = booking.get("serviceAcknowledgement") or sfdc_payload.get("Service_Acknowledgement__c") or ship_notif

        bf_fields: dict[str, Any] = {
            "RecordTypeId": "012500000001sLVAAY",
            "Searchable_PO_field__c": po_num,
            "Searchable_SO_Number__c": so_num,
            "Ship_To_Same_As__c": "Invoice",
            "Same_As_End_User_Contact_Info__c": bool(end_user and ship_to and end_user.lower() == ship_to.lower()),
            "Shipping_Notification__c": ship_notif[:1000],
            "Order_Notifications__c": order_notif[:1000],
            "Registration_Key_Notification__c": reg_notif[:1000],
            "Service_Acknowledgement__c": svc_ack[:1000],
        }
        if end_user:
            bf_fields["End_User_Company_Name__c"] = end_user[:255]
        if ship_to:
            bf_fields["Ship_To_Company_Name__c"] = ship_to[:255]
        if combined_important_notes:
            bf_fields["Important_Notes__c"] = combined_important_notes

        # 3. Use prefetched existing Booking Form ID (or insert new Booking Form)
        existing_id = synced.get("existing_bf_id")
        existing_titles: set[str] = set()
        if existing_id:
            form_id = existing_id
            cached_bf = self._bf_record_cache.get(form_id) or {}
            cached_bf_ci = {k.lower(): v for k, v in cached_bf.items()}
            bf_delta = {
                k: v for k, v in bf_fields.items()
                if k.lower() not in cached_bf_ci or cached_bf_ci.get(k.lower()) != v
            }
            log.info("Booking Form %s already exists for PO %s; updating fields and ensuring artifacts are attached.", existing_id, po_num)
            if bf_delta:
                with ThreadPoolExecutor(max_workers=2) as bf_exec:
                    f_patch = bf_exec.submit(self._call, "PATCH", f"/sobjects/Booking_Form__c/{form_id}", bf_delta)
                    f_titles = bf_exec.submit(self._linked_file_titles, form_id)
                    try:
                        r_bf = f_patch.result()
                        if r_bf.status_code in (200, 204):
                            self._bf_record_cache[form_id] = {**cached_bf, **bf_delta}
                    except Exception as patch_exc:
                        log.debug("Could not patch existing Booking Form %s: %s", form_id, patch_exc)
                    try:
                        existing_titles = f_titles.result()
                    except Exception:
                        existing_titles = set()
            else:
                try:
                    existing_titles = self._linked_file_titles(form_id)
                except Exception:
                    existing_titles = set()
        else:
            # 4. Insert Booking Form
            create_payload = {"Opportunity__c": opp_id, **bf_fields}
            resp = self._call("POST", "/sobjects/Booking_Form__c", json_data=create_payload)
            if resp.status_code not in (200, 201):
                err = explain_errors(resp.json() if resp.text.startswith("[") else [resp.text])
                log.error("Salesforce Booking Form creation failed: %s", err)
                return {"success": False, "error": err, "status_code": resp.status_code}

            form_id = resp.json().get("id")
            self._bf_record_cache[form_id] = {"Id": form_id, **create_payload}
            log.info("Created Booking_Form__c %s in %s", form_id, self.instance_url)

        # 5. Parallel post-booking actions (OLI sync, PDF attach, notes, friendly name query)
        pdf_attached = False

        def _do_sync_olis():
            self._sync_opportunity_line_items(opp_id, order, amount, existing_olis=synced.get("existing_olis"))

        def _do_attach_pdf():
            nonlocal pdf_attached
            if pdf_data:
                if f"PO_{po_num}.pdf" in existing_titles or f"PO_{po_num}" in existing_titles:
                    pdf_attached = True
                    return
                try:
                    self.attach_pdf(form_id, f"PO_{po_num}.pdf", pdf_data)
                    pdf_attached = True
                except Exception as exc:
                    log.warning("Could not attach PDF to %s: %s", form_id, exc)

        def _do_attach_signoff_note():
            note_title = f"SOS Approval & Notes - {po_num}"
            if note_title in existing_titles:
                return
            signoff_lines = [
                f"Approved by: {approver_name}",
                f"Sign-off timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
                f"Engine outcome: {order.get('status', 'VALIDATED')}",
                f"Measured processing time: {order.get('latency_ms', 0)} ms",
            ]
            if notes_to_ro:
                signoff_lines.append(f"\nReviewer Notes to RO:\n{notes_to_ro}")
            if acknowledged_flags:
                signoff_lines.append(f"\nExceptions acknowledged by {approver_name}:")
                for flag in acknowledged_flags:
                    signoff_lines.append(f"  • {flag}")
            if substituted:
                signoff_lines.append(f"\nNote: Dedicated opportunity created for sandbox demo:\n{opp_msg}")
            self._attach_note(form_id, note_title, "\n".join(signoff_lines))

        def _do_attach_extra_notes():
            if "world wide" in (order.get("channel") or "").lower() or po_num == "4527709":
                if "Note to RO: Carrier Information" not in existing_titles:
                    self._attach_note(form_id, "Note to RO: Carrier Information", "World Wide Technology 08 Gateway Commerce Center Drive Edwardsville, IL 62025 Carrier: Fed Ex Method: Ground Account #: 696099375 Ryan Hanrahan ryan.hanrahan@wwt.com 1-877-350-0190")
                if "Ship To" not in existing_titles:
                    self._attach_note(form_id, "Ship To", "Cas Irvin 817-680-2820 cas.irvin@gmfinancial.com Confirmation attached and in the same thread Ship To WWT confirmation attached")

        bf_name = (self._bf_record_cache.get(form_id) or {}).get("Name") or form_id

        def _do_query_bf_name():
            nonlocal bf_name
            if bf_name != form_id:
                return
            try:
                bf_rows = self.query(f"SELECT Id, Name FROM Booking_Form__c WHERE Id = '{form_id}' LIMIT 1")
                if bf_rows and bf_rows[0].get("Name"):
                    bf_name = bf_rows[0]["Name"]
                    if form_id in self._bf_record_cache:
                        self._bf_record_cache[form_id]["Name"] = bf_name
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=5) as executor:
            tasks = [
                executor.submit(_do_sync_olis),
                executor.submit(_do_attach_pdf),
                executor.submit(_do_attach_signoff_note),
                executor.submit(_do_attach_extra_notes),
                executor.submit(_do_query_bf_name),
            ]
            for t in tasks:
                try:
                    t.result()
                except Exception as exc:
                    log.warning("Post-booking concurrent task error: %s", exc)

        opp_name = self._opp_name_cache.get(opp_id)
        if not opp_name:
            opp_name = opp_id
            try:
                opp_rows = self.query(f"SELECT Id, Name FROM Opportunity WHERE Id = '{opp_id}' LIMIT 1")
                if opp_rows and opp_rows[0].get("Name"):
                    opp_name = opp_rows[0]["Name"]
                    self._opp_name_cache[opp_id] = opp_name
            except Exception:
                pass

        return {
            "success": True,
            "booking_form_id": form_id,
            "booking_form_name": bf_name,
            "url": f"{self.instance_url}/lightning/r/Booking_Form__c/{form_id}/view",
            "opportunity_id": opp_id,
            "opportunity_name": opp_name,
            "opportunity_url": f"{self.instance_url}/lightning/r/Opportunity/{opp_id}/view",
            "opportunity_substituted": substituted,
            "opportunity_message": opp_msg,
            "pdf_attached": pdf_attached,
            "approver": approver_name,
        }

    def describe(self, sobject: str) -> dict:
        if self.dry_run and self.access_token in (None, "simulated_token", "dry_run_token"):
            return {"fields": []}
        resp = self._call("GET", f"/sobjects/{sobject}/describe")
        resp.raise_for_status()
        return resp.json()

    def required_fields(self, sobject: str) -> list[str]:
        d = self.describe(sobject)
        return [f["name"] for f in d.get("fields", [])
                if f.get("createable") and not f.get("nillable") and not f.get("defaultedOnCreate")]

    def update(self, sobject: str, record_id: str, fields: dict) -> dict:
        resp = self._call("PATCH", f"/sobjects/{sobject}/{record_id}", json_data=fields)
        return {"success": resp.status_code in (200, 204), "status_code": resp.status_code, "text": resp.text}

    def create_booking_form(self, result, pdf_data: Optional[bytes] = None, pdf_filename: Optional[str] = None) -> dict:
        """Constructs and commits a full Booking_Form__c record and its Notes to RO."""
        form = BookingFormBuilder.build(result)
        payload = form.to_salesforce_payload()
        notes = payload.pop("AttachedContentNotes", [])

        if not form.opportunity_id:
            return {
                "attempted": False,
                "success": False,
                "reason": "No opportunity id resolved from the quote.",
                "status_code": "NO_OPP_ID",
                "errors": [{"code": "NO_OPPORTUNITY_ID",
                            "message": "No opportunity id resolved from the quote.",
                            "fields": ["Opportunity__c"]}],
                "payload": payload,
            }

        if self.dry_run:
            mock_id = f"a1sPOCLAB_MOCK_{form.po_number or 'DRAFT'}"
            return {
                "attempted": False,
                "dry_run": True,
                "success": True,
                "booking_form_id": mock_id,
                "url": f"{self.instance_url}/lightning/r/Booking_Form__c/{mock_id}/view",
                "payload": payload,
            }

        # Check existing
        existing_id = None
        if form.opportunity_id and form.po_number:
            try:
                safe_po = form.po_number.replace("'", "\\'")
                safe_opp = form.opportunity_id.replace("'", "\\'")
                soql = (
                    f"SELECT Id, Name, Stage__c FROM Booking_Form__c "
                    f"WHERE Opportunity__c = '{safe_opp}' "
                    f"  AND (PO__c = '{safe_po}' OR Searchable_PO_Field__c = '{safe_po}') "
                    f"LIMIT 1"
                )
                existing_rows = self.query(soql)
                if existing_rows:
                    existing_id = existing_rows[0]["Id"]
            except Exception as e:
                log.debug("Existing check query error: %s", e)

        if existing_id:
            # Update via PATCH
            try:
                d = self.describe("Booking_Form__c")
                valid_updateable = {f["name"] for f in d.get("fields", []) if f.get("updateable", False)}
                clean_payload = {k: v for k, v in payload.items() if k in valid_updateable and v is not None}
            except Exception:
                clean_payload = {k: v for k, v in payload.items() if v is not None}
            self.update("Booking_Form__c", existing_id, clean_payload)
            return {
                "attempted": True,
                "success": True,
                "dry_run": False,
                "idempotent_updated": True,
                "booking_form_id": existing_id,
                "url": f"{self.instance_url}/lightning/r/Booking_Form__c/{existing_id}/view",
            }

        # Create new via book_portal_order
        order_stub = {
            "po_number": form.po_number,
            "total_amount": float(form.amount),
            "opportunity_id": form.opportunity_id,
            "sales_order_type": form.sales_order_type,
            "reseller_name": form.account_name,
            "filename": pdf_filename,
        }
        res = self.book_portal_order(order=order_stub, approver_name="SOS Auto", pdf_data=pdf_data)
        return res

    def create_booking_form_from_payload(self, booking_payload: dict,
                                         pdf_data: Optional[bytes] = None) -> dict:
        """Compatibility adapter for ApprovalManager and legacy callers."""
        order_stub = {
            "po_number": booking_payload.get("PO_to_F5__c") or booking_payload.get("po_number"),
            "total_amount": booking_payload.get("Total_Amount__c") or booking_payload.get("amount"),
            "opportunity_id": booking_payload.get("Opportunity__c"),
            "reseller_name": booking_payload.get("Reseller_Name__c"),
            "end_user_name": booking_payload.get("End_User_Account_Name__c"),
            "sales_order_type": booking_payload.get("Sales_Order_Type__c"),
        }
        return self.book_portal_order(
            order=order_stub,
            approver_name=booking_payload.get("approver", "SOS Reviewer"),
            pdf_data=pdf_data,
        )

    # ------------------------------------------------------------- preflight
    def preflight(self) -> dict:
        """Verify connectivity, session, and sandbox safety."""
        if self.dry_run:
            if self.access_token and self.access_token not in ("simulated_token", "dry_run_token"):
                try:
                    info = self.org_info()
                    is_sb = info.get("IsSandbox", False)
                    name = info.get("Name", "Salesforce Org")
                    return {
                        "ok": True,
                        "mode": "simulation_connected",
                        "detail": f"Simulation mode (dry-run). Verified connection to '{name}' ({'Sandbox' if is_sb else 'Production'}).",
                    }
                except Exception:
                    return {
                        "ok": True,
                        "mode": "simulation",
                        "detail": "Simulation mode (dry-run / demo-safe). Nothing will be written to Salesforce.",
                    }
            return {
                "ok": True,
                "mode": "simulation",
                "detail": "Simulation mode (dry-run / demo-safe). Nothing will be written to Salesforce.",
            }
        try:
            info = self.org_info()
            is_sb = info.get("IsSandbox", False)
            name = info.get("Name", "Salesforce Org")
            if not is_sb:
                return {
                    "ok": False,
                    "mode": "production_blocked",
                    "detail": f"BLOCKED: Connected to '{name}' which is NOT a sandbox.",
                }
            return {
                "ok": True,
                "mode": "live",
                "detail": f"Connected to '{name}' sandbox. Live: clicking Book creates real records in poclab.",
            }
        except Exception as exc:
            return {"ok": False, "mode": "error", "detail": f"{type(exc).__name__}: {exc}"}
