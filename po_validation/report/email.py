"""Emailing the SOS team when a PO needs a person.

Three delivery routes, because which one you can use depends on what IT
will grant rather than on what is technically nicest:

  GraphEmailSink     Microsoft Graph sendMail. Needs an app registration
                     with Mail.Send. The proper answer.
  PowerAutomateSink  POST to an HTTP-triggered Power Automate flow, which
                     sends the mail. Prady already runs a flow on the PO
                     inbox, so this route needs no new app registration
                     and is usually the fastest to get approved.
  SmtpEmailSink      Plain SMTP relay, if one is available internally.

All three take the same rendered email, so swapping between them is a
config change.

The email is written to be *actionable*: the PO number, what failed, and
what the person is expected to do. A notification that says "PO 4501706557
failed validation" makes someone open the PDF and start from scratch,
which is the work we are trying to remove.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Optional

from ..models import Outcome, Status, ValidationResult
from .writer import Sink

log = logging.getLogger(__name__)

SEVERITY_ORDER = {"BLOCKER": 0, "MAJOR": 1, "MINOR": 2, "INFO": 3}

SUBJECT = {
    Outcome.REJECTED:
        "PO {po} — does not match the quote, needs to go back to the reseller",
    Outcome.NEEDS_REVIEW:
        "PO {po} — needs review before booking",
    Outcome.DUPLICATE:
        "PO {po} — possible duplicate submission",
    Outcome.EXTRACTION_FAILED:
        "PO {po} — could not be read automatically",
}

INTRO = {
    Outcome.REJECTED:
        "This purchase order contradicts the quote it references, or is "
        "missing something mandatory. It needs to go back to the reseller "
        "rather than be corrected on our side.",
    Outcome.NEEDS_REVIEW:
        "This purchase order is broadly in order but something needs a "
        "person before it can be booked.",
    Outcome.DUPLICATE:
        "A purchase order with this number has already been processed. "
        "Please confirm whether this is a resend or a genuinely new order.",
    Outcome.EXTRACTION_FAILED:
        "The document could not be read automatically, so no validation "
        "was possible. This one needs handling manually.",
}


def render_email(result: ValidationResult) -> tuple[str, str, str]:
    """Return (subject, html_body, plain_body)."""
    po, q = result.po, result.quote
    ref = po.po_number or po.source_id.split("/")[-1]
    subject = SUBJECT.get(result.outcome,
                          "PO {po} — needs attention").format(po=ref)

    failed = sorted(result.by_status(Status.FAIL),
                    key=lambda f: SEVERITY_ORDER.get(f.severity.value, 9))
    skipped = result.by_status(Status.SKIP)

    facts = [
        ("PO number", po.po_number),
        ("Quote referenced", po.quote_number),
        ("Reseller / layout", po.layout),
        ("PO date", po.po_date),
        ("PO total", po.po_total),
        ("Quote total", q.total if q else None),
        ("Lines on the PO", len(po.line_items) or None),
        ("Source file", po.source_id.split("/")[-1]),
    ]
    facts = [(k, v) for k, v in facts if v not in (None, "")]

    # ---- plain text ----
    lines = [subject, "=" * len(subject), "",
             INTRO.get(result.outcome, ""), ""]
    for k, v in facts:
        lines.append(f"  {k}: {v}")
    if failed:
        lines += ["", f"What needs attention ({len(failed)}):"]
        for f in failed:
            lines.append(f"  [{f.severity.value}] {f.message}")
    if po.line_items:
        lines += ["", "Line items read from the PO:"]
        for li in po.line_items:
            lines.append(f"  {li.part_number or '(no SKU on PO)'}  "
                         f"qty {li.quantity}  {li.total_price}")
    if skipped:
        lines += ["", f"{len(skipped)} check(s) could not run "
                      f"(usually missing reference data, not a problem "
                      f"with the PO)."]
    lines += ["", f"Validation run {result.run_id} against the "
                  f"{result.checklist} checklist."]
    plain = "\n".join(lines)

    # ---- html ----
    def esc(v):
        return html.escape(str(v))

    rows = "".join(
        f'<tr><td style="padding:4px 14px 4px 0;color:#5F5E5A;">{esc(k)}</td>'
        f'<td style="padding:4px 0;"><b>{esc(v)}</b></td></tr>'
        for k, v in facts)

    colour = {"BLOCKER": "#A32D2D", "MAJOR": "#854F0B", "MINOR": "#5F5E5A"}
    issues = "".join(
        f'<li style="margin-bottom:8px;">'
        f'<span style="color:{colour.get(f.severity.value, "#5F5E5A")};'
        f'font-weight:bold;">{esc(f.severity.value)}</span> — {esc(f.message)}'
        f'</li>' for f in failed)

    items = "".join(
        f'<tr><td style="padding:3px 14px 3px 0;">'
        f'{esc(li.part_number or "(no SKU on PO)")}</td>'
        f'<td style="padding:3px 14px 3px 0;text-align:right;">{esc(li.quantity)}</td>'
        f'<td style="padding:3px 0;text-align:right;">{esc(li.total_price)}</td></tr>'
        for li in po.line_items)

    body = f"""<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;
color:#2C2C2A;line-height:1.5;max-width:760px;">
<p style="margin:0 0 14px;">{esc(INTRO.get(result.outcome, ''))}</p>
<table style="border-collapse:collapse;margin-bottom:18px;">{rows}</table>
{f'<p style="margin:0 0 6px;"><b>What needs attention</b></p><ul style="margin:0 0 18px;padding-left:20px;">{issues}</ul>' if issues else ''}
{f'<p style="margin:0 0 6px;"><b>Line items read from the PO</b></p><table style="border-collapse:collapse;margin-bottom:18px;">{items}</table>' if items else ''}
<p style="margin:0;color:#888780;font-size:12px;">
Validation run {esc(result.run_id)} against the {esc(result.checklist)}
checklist. {len(skipped)} check(s) could not run, usually because reference
data was unavailable rather than anything wrong with the PO.</p>
</div>"""

    return subject, body, plain


INTERNAL_RULE_PREFIXES = ("parser_", "confidence_", "arithmetic_", "diagnostics_", "ocr_")


def _sanitize_for_reseller(msg: str) -> str:
    """Clean internal jargon, system names, and technical tokens from reseller-facing text."""
    cleaned = msg
    # Strip internal severity labels
    cleaned = re.sub(r"^\[(?:BLOCKER|MAJOR|MINOR|INFO)\]\s*", "", cleaned)
    # Replace internal data source names
    cleaned = re.sub(r"does not exist in (?:stub|snowflake|sfdc)", "could not be located in F5 records", cleaned, flags=re.I)
    cleaned = re.sub(r"\bstub\b", "F5 system records", cleaned, flags=re.I)
    return cleaned.strip()


def render_reseller_response_email(result: ValidationResult) -> str:
    """Draft a professional, ready-to-send discrepancy email to the reseller/distributor."""
    po = result.po
    q = result.quote
    po_num = po.po_number or "UNKNOWN"
    quote_num = po.quote_number or "UNKNOWN"

    reseller_name = "Orders"
    if po.reseller_name:
        reseller_name = po.reseller_name
    elif po.layout and po.layout not in ("generic", "failed"):
        reseller_name = po.layout.title()

    recipient_email = getattr(po, "reseller_email", None) or ""

    failed_checks = sorted(result.by_status(Status.FAIL),
                           key=lambda f: SEVERITY_ORDER.get(f.severity.value, 9))

    bullets = []
    for f in failed_checks:
        # Filter out internal engine/parser diagnostic rules that are not reseller issues
        if any(f.rule_id.lower().startswith(p) for p in INTERNAL_RULE_PREFIXES):
            continue
        clean_msg = _sanitize_for_reseller(f.message)
        bullets.append(f"  • {clean_msg}")

    issues_text = "\n".join(bullets) if bullets else "  • Order details do not reconcile with the referenced quote."
    to_line = f"To: {recipient_email}\n" if recipient_email else ""

    return f"""{to_line}Subject: Action Required: Purchase Order #{po_num} Discrepancy (Quote #{quote_num})

Dear {reseller_name} Orders Team,

Thank you for submitting purchase order #{po_num} referencing F5 quote #{quote_num}.

During our automated purchase order validation, the following issue(s) were identified that require correction before we can book this order:

{issues_text}

Required Next Steps:
1. Please review the referenced quote #{quote_num}.
2. Issue an amended purchase order addressing the discrepancy noted above.
3. Reply to this email or send the updated PO directly to purchaseorders@f5.com.

If you have questions regarding the quote pricing, terms, or configuration, please contact your dedicated F5 Account Representative.

Best regards,
F5 Sales Operations (SOS)
purchaseorders@f5.com
"""



class _EmailSinkBase(Sink):
    """Only emails outcomes that need a person. A queue that announces
    every success gets muted, and then it announces nothing."""

    def __init__(self, recipients: list[str],
                 outcomes: Optional[set[Outcome]] = None,
                 dry_run: bool = True):
        self.recipients = recipients
        self.outcomes = outcomes or {
            Outcome.REJECTED, Outcome.NEEDS_REVIEW,
            Outcome.DUPLICATE, Outcome.EXTRACTION_FAILED}
        self.dry_run = dry_run

    def emit(self, result: ValidationResult) -> None:
        if result.outcome not in self.outcomes:
            return
        subject, body, plain = render_email(result)
        if self.dry_run:
            log.info("[dry-run] would email %s: %s",
                     ", ".join(self.recipients), subject)
            print(f"\n--- email (dry run) to {', '.join(self.recipients)} ---")
            print(plain)
            return
        try:
            self.send(subject, body, plain)
        except Exception as exc:
            # A failed notification must never fail the run. The finding is
            # already durable in the ledger and the findings table.
            log.error("email send failed for %s: %s", result.po.source_id, exc)

    def send(self, subject: str, body_html: str, body_plain: str) -> None:
        raise NotImplementedError


class GraphEmailSink(_EmailSinkBase):
    """Microsoft Graph sendMail.

    Needs an Azure app registration with the application permission
    Mail.Send, and a mailbox to send as. Ask for this if you can; it is the
    route with the clearest audit trail.
    """

    def __init__(self, recipients, tenant_id: str, client_id: str,
                 client_secret: str, sender: str, **kw):
        super().__init__(recipients, **kw)
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.sender = sender
        self._token: Optional[str] = None

    def _access_token(self) -> str:
        if self._token:
            return self._token
        import urllib.request
        from urllib.parse import urlencode
        data = urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        self._token = body["access_token"]
        return self._token

    def send(self, subject, body_html, body_plain):
        import urllib.request
        body = json.dumps({
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": a}} for a in self.recipients],
            },
            "saveToSentItems": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://graph.microsoft.com/v1.0/users/{self.sender}/sendMail",
            data=body,
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status not in (200, 202):
                    raise RuntimeError(f"Graph sendMail status: {resp.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Graph sendMail {exc.code}: {exc.read().decode('utf-8')[:300]}")


class PowerAutomateSink(_EmailSinkBase):
    """POST the email to an HTTP-triggered Power Automate flow.

    Usually the quickest route to approval: a flow already watches the PO
    inbox, and an HTTP trigger needs no new app registration. The flow
    receives subject, body and recipients and does the sending.
    """

    def __init__(self, recipients, flow_url: str, **kw):
        super().__init__(recipients, **kw)
        self.flow_url = flow_url

    def send(self, subject, body_html, body_plain):
        import urllib.request
        body = json.dumps({
            "to": self.recipients,
            "subject": subject,
            "bodyHtml": body_html,
            "bodyText": body_plain,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.flow_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status >= 300:
                    raise RuntimeError(f"Power Automate flow status: {resp.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Power Automate flow {exc.code}: {exc.read().decode('utf-8')[:300]}")


class SmtpEmailSink(_EmailSinkBase):
    def __init__(self, recipients, host: str, sender: str, port: int = 25,
                 username: Optional[str] = None, password: Optional[str] = None,
                 use_tls: bool = False, **kw):
        super().__init__(recipients, **kw)
        self.host, self.port, self.sender = host, port, sender
        self.username, self.password, self.use_tls = username, password, use_tls

    def send(self, subject, body_html, body_plain):
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(body_plain)
        msg.add_alternative(body_html, subtype="html")
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(msg)
