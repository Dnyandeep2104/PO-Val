"""Test fakes and in-memory mocks for Salesforce and Azure Blob."""

from __future__ import annotations

import base64
import json
from typing import Any, Optional


class FakeSalesforceServer:
    """In-memory mock for Salesforce REST API during testing."""

    def __init__(self, is_sandbox: bool = True, org_name: str = "poclab Sandbox"):
        self.is_sandbox = is_sandbox
        self.org_name = org_name
        self.opportunities = {
            "006Po00000jNR0pIAG": {"Id": "006Po00000jNR0pIAG", "Name": "Acme Opportunity", "StageName": "Discovery", "IsClosed": False},
            "006DEMO000000001": {"Id": "006DEMO000000001", "Name": "Demo Stand-in Opp", "StageName": "Negotiation", "IsClosed": False},
        }
        self.booking_forms: dict[str, dict] = {}
        self.content_versions: list[dict] = []
        self.content_notes: list[dict] = []
        self.content_links: list[dict] = []
        self.calls: list[tuple[str, str, Any]] = []
        self._next_id = 100

    def handle(self, method: str, url: str, headers: dict = None,
               params: dict = None, json_data: Any = None, data: Any = None, **kwargs):
        self.calls.append((method, url, json_data))

        # 1. Query
        if "/query" in url:
            q = (params or {}).get("q", "")
            if "FROM Organization" in q:
                return 200, {"records": [{"Id": "00D000000000001", "Name": self.org_name, "IsSandbox": self.is_sandbox}]}
            if "FROM Opportunity" in q:
                recs = list(self.opportunities.values())
                if "WHERE Id = '" in q:
                    opp_id = q.split("WHERE Id = '")[1].split("'")[0]
                    recs = [r for r in recs if r["Id"] == opp_id]
                return 200, {"records": recs, "totalSize": len(recs)}
            if "FROM Booking_Form__c" in q:
                if "COUNT()" in q:
                    return 200, {"totalSize": len(self.booking_forms)}
                recs = list(self.booking_forms.values())
                if "WHERE PO_to_F5__c = '" in q:
                    po = q.split("WHERE PO_to_F5__c = '")[1].split("'")[0]
                    recs = [r for r in recs if r.get("PO_to_F5__c") == po]
                return 200, {"records": recs, "totalSize": len(recs)}
            if "FROM ContentDocumentLink" in q:
                return 200, {"records": self.content_links, "totalSize": len(self.content_links)}
            return 200, {"records": []}

        # 2. Describe Booking_Form__c
        if "/sobjects/Booking_Form__c/describe" in url:
            return 200, {
                "fields": [
                    {"name": "Opportunity__c", "createable": True, "updateable": True, "nillable": False, "defaultedOnCreate": False},
                    {"name": "PO_to_F5__c", "createable": True, "updateable": True, "nillable": False, "defaultedOnCreate": False},
                    {"name": "PO__c", "createable": True, "updateable": True, "nillable": True, "defaultedOnCreate": False},
                    {"name": "Total_Amount__c", "createable": True, "updateable": True, "nillable": True, "defaultedOnCreate": False},
                    {"name": "Sales_Order_Type__c", "createable": True, "updateable": True, "nillable": True, "defaultedOnCreate": False},
                    {"name": "Status__c", "createable": True, "updateable": True, "nillable": True, "defaultedOnCreate": False},
                    {"name": "Reseller_Name__c", "createable": True, "updateable": True, "nillable": True, "defaultedOnCreate": False},
                    {"name": "Stage__c", "createable": True, "updateable": True, "nillable": True, "defaultedOnCreate": False},
                ]
            }

        # 3. Create Booking_Form__c
        if method == "POST" and "/sobjects/Booking_Form__c" in url:
            self._next_id += 1
            rec_id = f"a1sPOCLAB{self._next_id}"
            record = dict(json_data or {})
            record["Id"] = rec_id
            self.booking_forms[rec_id] = record
            return 201, {"id": rec_id, "success": True}

        # 4. Patch Opportunity
        if method == "PATCH" and "/sobjects/Opportunity/" in url:
            opp_id = url.split("/sobjects/Opportunity/")[1].split("?")[0]
            if opp_id in self.opportunities:
                self.opportunities[opp_id].update(json_data or {})
            return 204, {}

        # 5. Patch Booking_Form__c
        if method == "PATCH" and "/sobjects/Booking_Form__c/" in url:
            rec_id = url.split("/sobjects/Booking_Form__c/")[1].split("?")[0]
            if rec_id in self.booking_forms:
                self.booking_forms[rec_id].update(json_data or {})
            return 204, {}

        # 6. Create ContentVersion (PDF)
        if method == "POST" and "/sobjects/ContentVersion" in url:
            self._next_id += 1
            cv_id = f"068TEST{self._next_id}"
            self.content_versions.append({"id": cv_id, "payload": json_data})
            return 201, {"id": cv_id, "success": True}

        # 7. Create ContentNote
        if method == "POST" and "/sobjects/ContentNote" in url:
            self._next_id += 1
            cn_id = f"069TEST{self._next_id}"
            self.content_notes.append({"id": cn_id, "payload": json_data})
            return 201, {"id": cn_id, "success": True}

        # 8. Create ContentDocumentLink
        if method == "POST" and "/sobjects/ContentDocumentLink" in url:
            self.content_links.append(json_data)
            return 201, {"success": True}

        return 404, {"error": "Not Found"}


class FakeHttpResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.body_bytes = json.dumps(payload).encode("utf-8") if isinstance(payload, (dict, list)) else (payload or b"")

    @property
    def text(self) -> str:
        return self.body_bytes.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")
