"""F5AI client for PO validation and SOS booking form assistance.

Connects to F5's internal OpenAI-compatible gateway (https://f5ai.pd.f5net.com/openai/)
to provide intelligent exception explanations and auto-drafted SOS review notes.
Includes deterministic fallbacks if the internal endpoint is unreachable or token is expired.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("po_validation.ai")


def load_env_file():
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
                if k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        logger.debug(f"Could not load .env: {e}")


load_env_file()


class F5AIClient:
    """Client for F5's internal AI gateway."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 15,
    ):
        self.base_url = (
            base_url
            or os.environ.get("F5_OPENAI_BASE_URL")
            or "https://f5ai.pd.f5net.com/openai/"
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("F5AI_API_KEY", "")
        self.model = model or os.environ.get("F5AI_MODEL") or "claude-opus-4-6"
        self.timeout = timeout

        # Verified SSL context supporting corporate CA bundle
        self.ssl_ctx = self._get_ssl_context()

    @staticmethod
    def _get_ssl_context() -> ssl.SSLContext:
        insecure = os.environ.get("F5AI_INSECURE_TLS", "").lower() in ("true", "1", "yes")
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

        cafile = (
            os.environ.get("REQUESTS_CA_BUNDLE") or
            os.environ.get("CURL_CA_BUNDLE") or
            os.environ.get("SSL_CERT_FILE")
        )
        if cafile and os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
        return ssl.create_default_context()

    def _chat_completion(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        """Send chat completion request to F5AI gateway."""
        if not self.api_key or self.api_key == "YOUR_API_KEY_HERE":
            return None

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 800,
        }

        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=self.timeout) as resp:
                if resp.status == 200:
                    res_json = json.loads(resp.read().decode("utf-8"))
                    choices = res_json.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "").strip()
        except urllib.error.HTTPError as e:
            logger.warning(f"F5AI API returned HTTP {e.code}: {e.reason}")
        except Exception as e:
            logger.warning(f"F5AI call failed: {e}")

        return None

    def generate_sos_summary(self, po_number: str, account: str, amount: str,
                             blockers: list[str], majors: list[str], notes: list[str]) -> str:
        """Generates a 2-3 sentence executive summary for the SOS specialist."""
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

        ai_response = self._chat_completion(system_prompt, user_prompt)
        if ai_response:
            return ai_response

        # High-precision deterministic fallback
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
