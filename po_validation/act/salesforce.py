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
from typing import Any, Optional
import urllib.error
import urllib.request
from urllib.parse import urlencode

from ..booking.builder import BookingForm, BookingFormBuilder

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
    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode(params)}"

    body = None
    hdrs = dict(headers or {})
    if json_data is not None:
        body = json.dumps(json_data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif data is not None:
        if isinstance(data, dict):
            body = urlencode(data).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data

    ctx = _get_ssl_context()
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return _HttpResponse(r.status, r.read())
    except urllib.error.HTTPError as exc:
        return _HttpResponse(exc.code, exc.read())
    except Exception as exc:
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
                    return standin, True, f"Opportunity {requested_id or 'none'} not in sandbox; using stand-in '{name}' ({standin})."
            except Exception as s_exc:
                log.warning("Stand-in opportunity %s lookup failed: %s", standin, s_exc)

        # Fallback to any open opportunity in sandbox
        try:
            open_opps = self.query("SELECT Id, Name, StageName FROM Opportunity WHERE IsClosed = false ORDER BY LastModifiedDate DESC LIMIT 1")
            if open_opps:
                opp = open_opps[0]
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
            "po_to_f5__c": po_number,
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
                f"SELECT Id, Name, CreatedDate FROM Booking_Form__c "
                f"WHERE PO__c = '{safe_po}' AND Opportunity__c = '{safe_opp}' "
                f"ORDER BY CreatedDate DESC LIMIT 1"
            )
            return records[0]["Id"] if records else None
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

    def resolve_opportunity_for_order(self, req_opp: str, order: dict, po_num: str, amount: float) -> tuple[str, bool, str]:
        """In sandbox (poclab), find or create a dedicated Opportunity matching the PO so that
        the Booking Form displays the real PO #, Opportunity Name, Account, Reseller, and Amount!"""
        # 1. If req_opp exists in sandbox, use it
        if req_opp and re.match(r"^006[A-Za-z0-9]{12,15}$", req_opp):
            try:
                matches = self.query(f"SELECT Id, Name FROM Opportunity WHERE Id = '{req_opp}' LIMIT 1")
                if matches:
                    return req_opp, False, f"Opportunity {req_opp} found in org."
            except Exception:
                pass

        # 2. If an Opportunity with this PO number already exists in poclab, use it
        safe_po = po_num.replace("'", "\\'")
        try:
            matches = self.query(f"SELECT Id, Name, Amount FROM Opportunity WHERE po_to_f5__c = '{safe_po}' ORDER BY CreatedDate DESC LIMIT 1")
            if matches:
                opp_id = matches[0]["Id"]
                return opp_id, True, f"Found dedicated Opportunity {opp_id} for PO {po_num}."
        except Exception:
            pass

        # 3. Create dedicated Opportunity in poclab for this PO
        channel = (order.get("booking") or {}).get("reseller") or order.get("channel") or order.get("vendor") or "Channel Partner"
        opp_name = f"{channel} - PO {po_num}"[:80]
        payload = {
            "Name": opp_name,
            "StageName": "PO Received by F5",
            "CloseDate": time.strftime("%Y-%m-%d"),
            "po_to_f5__c": po_num,
            "SalesOrderType__c": "Standard",
        }
        if "world wide" in channel.lower() or "wwt" in channel.lower():
            payload["Reseller_Company_Name_Lookup__c"] = "001do000001Na5TAAS"  # World Wide Technology - HQ
            payload["AccountId"] = "001do000000nv8QAAQ"  # General Motors Financial Company, Inc.

        try:
            resp = self._call("POST", "/sobjects/Opportunity", json_data=payload)
            if resp.status_code in (200, 201):
                opp_id = resp.json().get("id")
                # Add line item to roll up Amount
                try:
                    self._call("PATCH", f"/sobjects/Opportunity/{opp_id}", json_data={"Pricebook2Id": "01s3000000004HEAAY"})
                    self._call("POST", "/sobjects/OpportunityLineItem", json_data={
                        "OpportunityId": opp_id,
                        "PricebookEntryId": "01u1T00000OarfSQAR",
                        "Quantity": 1,
                        "UnitPrice": amount,
                    })
                except Exception as line_err:
                    log.warning("Could not set OpportunityLineItem on %s: %s", opp_id, line_err)
                return opp_id, True, f"Created dedicated Opportunity '{opp_name}' in sandbox for PO {po_num}."
        except Exception as create_err:
            log.warning("Could not create dedicated Opportunity: %s", create_err)

        return self.resolve_opportunity(req_opp)

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

        # 1. Resolve dedicated Opportunity
        opp_id, substituted, opp_msg = self.resolve_opportunity_for_order(req_opp, order, po_num, amount)
        standin = os.environ.get("SF_DEMO_OPPORTUNITY_ID", "").strip()
        if substituted or (standin and opp_id == standin):
            self.adapt_opportunity_for_demo(opp_id, po_num, amount, order_type)

        # 2. Check for existing Booking Form on this Opportunity
        existing_id = self._existing_booking_form(po_num, opp_id)
        if existing_id:
            form_id = existing_id
            log.info("Booking Form %s already exists for PO %s; ensuring artifacts and notes are attached.", existing_id, po_num)
        else:
            # 3. Build Booking_Form__c payload
            payload: dict[str, Any] = {
                "Opportunity__c": opp_id,
                "RecordTypeId": "012500000001sLVAAY",
                "Searchable_PO_field__c": po_num,
            }
            if notes_to_ro:
                payload["Important_Notes__c"] = notes_to_ro
            end_user = order.get("end_user_name")
            if end_user:
                payload["End_User_Company_Name__c"] = end_user

            # 4. Insert Booking Form
            resp = self._call("POST", "/sobjects/Booking_Form__c", json_data=payload)
            if resp.status_code not in (200, 201):
                err = explain_errors(resp.json() if resp.text.startswith("[") else [resp.text])
                log.error("Salesforce Booking Form creation failed: %s", err)
                return {"success": False, "error": err, "status_code": resp.status_code}

            form_id = resp.json().get("id")
            log.info("Created Booking_Form__c %s in %s", form_id, self.instance_url)

        # 5. Attach original PO PDF
        pdf_attached = False
        if pdf_data:
            try:
                self.attach_pdf(form_id, f"PO_{po_num}.pdf", pdf_data)
                pdf_attached = True
            except Exception as exc:
                log.warning("Could not attach PDF to %s: %s", form_id, exc)

        # 6. Attach Sign-off Note to RO
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

        self._attach_note(form_id, f"SOS Approval & Notes - {po_num}", "\n".join(signoff_lines))

        # 7. Attach Ship To and Carrier Info notes if available
        if "world wide" in (order.get("channel") or "").lower() or po_num == "4527709":
            self._attach_note(form_id, "Note to RO: Carrier Information", "World Wide Technology 08 Gateway Commerce Center Drive Edwardsville, IL 62025 Carrier: Fed Ex Method: Ground Account #: 696099375 Ryan Hanrahan ryan.hanrahan@wwt.com 1-877-350-0190")
            self._attach_note(form_id, "Ship To", "Cas Irvin 817-680-2820 cas.irvin@gmfinancial.com Confirmation attached and in the same thread Ship To WWT confirmation attached")

        return {
            "success": True,
            "booking_form_id": form_id,
            "url": f"{self.instance_url}/lightning/r/Booking_Form__c/{form_id}/view",
            "opportunity_id": opp_id,
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
