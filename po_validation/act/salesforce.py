"""Salesforce write path: create the Booking Form.

Auth. Supports JWT bearer flow, static token, and environment-based configuration.
Writes. dry_run defaults to True. Turning it off is a deliberate act.
An automated system that creates records in a sales system should be
hard to fire by accident.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import time
import urllib.error
from urllib.parse import urlencode
import urllib.request
from typing import Any, Optional

from ..booking.builder import BookingFormBuilder

log = logging.getLogger(__name__)

DEFAULT_API_VERSION = "v61.0"
DEFAULT_SANDBOX_URL = os.environ.get("SF_INSTANCE_URL", "https://f5--e2e.sandbox.my.salesforce.com")


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


def _http_request(method: str, url: str, headers: dict = None,
                  params: dict = None, json_data: Any = None,
                  data: Any = None, timeout: int = 30) -> _HttpResponse:
    """Zero-dependency HTTP client using Python standard library."""
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

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return _HttpResponse(r.status, r.read())
    except urllib.error.HTTPError as exc:
        return _HttpResponse(exc.code, exc.read())
    except Exception as exc:
        raise SalesforceError(f"Connection to Salesforce failed: {exc}") from exc


class SalesforceClient:
    def __init__(self, instance_url: str, access_token: str,
                 api_version: str = DEFAULT_API_VERSION, dry_run: bool = True):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.api_version = api_version
        self.dry_run = dry_run

    # ----------------------------------------------------------------- auth
    @classmethod
    def from_jwt(cls, login_url: str, client_id: str, username: str,
                 private_key: str | bytes, api_version: str = DEFAULT_API_VERSION,
                 dry_run: bool = True) -> "SalesforceClient":
        """Connected-app JWT bearer flow."""
        try:
            import jwt as pyjwt
        except ImportError:
            raise SalesforceError(
                "PyJWT is required for Connected-App JWT auth. "
                "Install with: pip install pyjwt cryptography")

        login_url = login_url.rstrip("/")
        assertion = pyjwt.encode(
            {"iss": client_id, "sub": username, "aud": login_url,
             "exp": int(time.time()) + 180},
            private_key, algorithm="RS256",
        )
        resp = _http_request(
            "POST",
            f"{login_url}/services/oauth2/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                  "assertion": assertion},
            timeout=30)
        if resp.status_code != 200:
            raise SalesforceError(
                f"JWT auth failed ({resp.status_code}): {resp.text}. "
                f"Check the connected app is pre-authorised for {username} "
                f"and that the cert matches the uploaded one.")
        body = resp.json()
        return cls(body["instance_url"], body["access_token"], api_version, dry_run)

    @classmethod
    def from_secrets(cls, secrets, scope: str = "SalesKeyVaultScope",
                     sandbox: bool = True, dry_run: bool = True) -> "SalesforceClient":
        return cls.from_jwt(
            login_url=("https://test.salesforce.com" if sandbox
                       else "https://login.salesforce.com"),
            client_id=secrets.get(scope=scope, key="sfdc-client-id"),
            username=secrets.get(scope=scope, key="sfdc-username"),
            private_key=secrets.get(scope=scope, key="sfdc-private-key")
                                .replace("\\n", "\n"),
            dry_run=dry_run,
        )

    @classmethod
    def from_password(cls, username: str, password: str,
                      security_token: str = "",
                      login_url: str = "https://test.salesforce.com",
                      api_version: str = DEFAULT_API_VERSION,
                      dry_run: bool = True) -> "SalesforceClient":
        """Logs in via Salesforce Partner SOAP API with username + password + security token."""
        import xml.etree.ElementTree as ET
        from xml.sax.saxutils import escape
        from urllib.parse import urlparse

        login_url = login_url.rstrip("/")
        soap_ver = api_version.lstrip("v")
        soap_endpoint = f"{login_url}/services/Soap/u/{soap_ver}"
        full_password = f"{password}{security_token}"
        safe_username = escape(username)
        safe_password = escape(full_password)
        soap_body = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            '<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">'
            '<env:Body>'
            '<n1:login xmlns:n1="urn:partner.soap.sforce.com">'
            f'<n1:username>{safe_username}</n1:username>'
            f'<n1:password>{safe_password}</n1:password>'
            '</n1:login>'
            '</env:Body>'
            '</env:Envelope>'
        )
        headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "login"}
        resp = _http_request("POST", soap_endpoint, headers=headers, data=soap_body, timeout=30)
        if resp.status_code != 200:
            try:
                err_root = ET.fromstring(resp.text)
                fault = err_root.find(".//faultstring")
                if fault is not None and fault.text:
                    raise SalesforceError(f"Login failed: {fault.text}")
            except SalesforceError:
                raise
            except Exception:
                pass
            raise SalesforceError(f"Login failed ({resp.status_code}): {resp.text}")

        try:
            root = ET.fromstring(resp.text)
            ns = {"soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
                  "p": "urn:partner.soap.sforce.com"}
            sess_elem = root.find(".//p:sessionId", ns)
            url_elem = root.find(".//p:serverUrl", ns)
            if sess_elem is None or not sess_elem.text:
                raise SalesforceError(f"No sessionId in login response: {resp.text[:300]}")
            session_id = sess_elem.text
            server_url = url_elem.text if url_elem is not None else login_url
            parsed = urlparse(server_url)
            instance_url = f"{parsed.scheme}://{parsed.netloc}"
            return cls(instance_url, session_id, api_version, dry_run)
        except Exception as exc:
            raise SalesforceError(f"Failed to parse login response: {exc}")

    @classmethod
    def from_token(cls, instance_url: Optional[str] = None,
                   token: Optional[str] = None, **kw) -> "SalesforceClient":
        instance_url = instance_url or os.environ.get("SF_INSTANCE_URL", DEFAULT_SANDBOX_URL)
        token = token or os.environ.get("SF_ACCESS_TOKEN", "DRAFT_POCLAB_TOKEN")
        return cls(instance_url, token, **kw)

    @classmethod
    def from_env(cls, sandbox_url: Optional[str] = None) -> "SalesforceClient":
        """Loads client from environment variables, defaulting to poclab sandbox."""
        instance_url = (
            sandbox_url
            or os.environ.get("SF_INSTANCE_URL")
            or DEFAULT_SANDBOX_URL
        )
        token = os.environ.get("SF_ACCESS_TOKEN", "DRAFT_POCLAB_TOKEN")
        dry_run_str = os.environ.get("SF_DRY_RUN", "true").lower()
        dry_run = dry_run_str in ("true", "1", "yes")
        return cls(instance_url=instance_url, access_token=token, dry_run=dry_run)

    # -------------------------------------------------------------- plumbing
    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.instance_url}/services/data/{self.api_version}/{path.lstrip('/')}"

    def query(self, soql: str) -> list[dict]:
        records, url, params = [], self._url("query"), {"q": soql}
        while True:
            r = _http_request("GET", url, headers=self.headers, params=params, timeout=30)
            if r.status_code != 200:
                raise SalesforceError(f"SOQL failed ({r.status_code}): {r.text}")
            body = r.json()
            records.extend(body.get("records", []))
            if body.get("done", True) or not body.get("nextRecordsUrl"):
                break
            url = f"{self.instance_url}{body['nextRecordsUrl']}"
            params = None
        return records

    def describe(self, sobject: str) -> dict:
        r = _http_request("GET", self._url(f"sobjects/{sobject}/describe"),
                          headers=self.headers, timeout=30)
        if r.status_code != 200:
            raise SalesforceError(f"describe {sobject} failed "
                                  f"({r.status_code}): {r.text}")
        return r.json()

    def required_fields(self, sobject: str) -> list[str]:
        d = self.describe(sobject)
        return [f["name"] for f in d["fields"]
                if f["createable"] and not f["nillable"] and not f["defaultedOnCreate"]]

    def update(self, sobject: str, record_id: str, fields: dict) -> dict:
        url = self._url(f"sobjects/{sobject}/{record_id}")
        r = _http_request("PATCH", url, headers=self.headers, json_data=fields, timeout=30)
        return {"success": r.status_code in (200, 204), "status_code": r.status_code, "text": r.text}

    def check(self) -> dict:
        """Preflight access-check against Salesforce Sandbox."""
        out: dict[str, Any] = {"ok": False, "instance": self.instance_url}

        # 1. Test REST API access via /limits
        limits_resp = _http_request("GET", self._url("limits"),
                                    headers=self.headers, timeout=30)
        if limits_resp.status_code == 401:
            out["detail"] = f"401 Unauthorized: token expired or invalid for {self.instance_url}."
            return out
        if limits_resp.status_code != 200:
            out["detail"] = f"HTTP {limits_resp.status_code} on /limits: {limits_resp.text}"
            return out

        limits_data = limits_resp.json()
        daily_api = limits_data.get("DailyApiRequests", {})
        out["api_calls_remaining"] = f"{daily_api.get('Remaining')} / {daily_api.get('Max')}"

        # 2. Org info
        try:
            org_rows = self.query("SELECT Id, Name, IsSandbox FROM Organization")
            if org_rows:
                out["org_id"] = org_rows[0].get("Id")
                out["org_name"] = org_rows[0].get("Name")
                out["is_sandbox"] = org_rows[0].get("IsSandbox")
        except Exception as exc:
            out["org_query_error"] = str(exc)

        # 3. Check Booking_Form__c access
        try:
            d = self.describe("Booking_Form__c")
            out["booking_form_found"] = True
            out["crud"] = {k: d[k] for k in ("createable", "updateable", "queryable")}
            out["required_fields"] = self.required_fields("Booking_Form__c")
        except Exception as exc:
            out["booking_form_found"] = False
            out["booking_form_error"] = str(exc)

        # 4. Check Booking_Form__c record count
        try:
            cnt_resp = _http_request("GET", self._url("query"),
                                     headers=self.headers,
                                     params={"q": "SELECT COUNT() FROM Booking_Form__c"},
                                     timeout=30)
            if cnt_resp.status_code == 200:
                out["booking_form_record_count"] = cnt_resp.json().get("totalSize", 0)
        except Exception:
            pass

        out["ok"] = True
        return out

    # --------------------------------------------------------------- writes
    def create_booking_form(self, result) -> dict:
        """Constructs and commits a full Booking_Form__c record and its Notes to RO.

        In dry-run mode, validates the payload and returns the simulated audit record
        without mutating the Salesforce sandbox.
        """
        form = BookingFormBuilder.build(result)
        payload = form.to_salesforce_payload()
        notes = payload.pop("AttachedContentNotes", [])

        if not form.opportunity_id:
            return {"attempted": False,
                    "reason": "No opportunity id resolved from the quote.",
                    "payload": payload}

        if self.dry_run:
            mock_id = f"a1sPOCLAB_MOCK_{form.po_number or 'DRAFT'}"
            return {
                "attempted": False,
                "dry_run": True,
                "sandbox_instance": self.instance_url,
                "reason": "dry_run=True; simulated Booking_Form__c payload generated safely for sandbox.",
                "booking_form_name": form.booking_form_name,
                "amount": float(form.amount),
                "order_type": form.sales_order_type,
                "notes_count": len(form.notes),
                "payload": payload,
                "simulated_url": f"{self.instance_url}/lightning/r/Booking_Form__c/{mock_id}/view",
            }

        # Introspect schema and send only fields that exist and are createable in this sandbox
        try:
            d = self.describe("Booking_Form__c")
            valid_createable = {f["name"] for f in d.get("fields", []) if f.get("createable", False)}
            clean_payload = {k: v for k, v in payload.items() if k in valid_createable and v is not None}
            if "Opportunity__c" in valid_createable and "Opportunity__c" in payload:
                clean_payload["Opportunity__c"] = payload["Opportunity__c"]
        except Exception:
            clean_payload = {k: v for k, v in payload.items() if v is not None}

        r = _http_request("POST", self._url("sobjects/Booking_Form__c/"),
                          headers=self.headers, json_data=clean_payload, timeout=30)
        body = _safe_json(r)

        if r.status_code == 201:
            rec_id = body.get("id")
            notes_created = 0
            for note in form.notes:
                try:
                    b64_content = base64.b64encode(note.body.encode("utf-8")).decode("utf-8")
                    note_resp = _http_request(
                        "POST",
                        self._url("sobjects/ContentNote/"),
                        headers=self.headers,
                        json_data={"Title": note.title, "Content": b64_content},
                        timeout=30)
                    if note_resp.status_code == 201:
                        note_id = note_resp.json().get("id")
                        _http_request(
                            "POST",
                            self._url("sobjects/ContentDocumentLink/"),
                            headers=self.headers,
                            json_data={
                                "ContentDocumentId": note_id,
                                "LinkedEntityId": rec_id,
                                "ShareType": "V",
                                "Visibility": "AllUsers",
                            },
                            timeout=30)
                        notes_created += 1
                except Exception as exc:
                    log.warning("Could not attach ContentNote '%s': %s", note.title, exc)

            return {
                "attempted": True,
                "success": True,
                "booking_form_id": rec_id,
                "url": f"{self.instance_url}/lightning/r/Booking_Form__c/{rec_id}/view",
                "notes_attached": notes_created,
                "payload": payload,
            }

        errors = body if isinstance(body, list) else [body]
        return {
            "attempted": True,
            "success": False,
            "status_code": r.status_code,
            "errors": [{"code": e.get("errorCode"),
                        "message": e.get("message"),
                        "fields": e.get("fields")} for e in errors],
            "payload": payload,
        }


def _safe_json(resp: _HttpResponse) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"message": resp.text[:500], "errorCode": "NON_JSON_RESPONSE"}
