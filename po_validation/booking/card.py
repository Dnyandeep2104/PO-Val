"""Outlook Actionable Message & 1-Click Booking Card generator.

Renders Microsoft Adaptive Card JSON (and HTML preview) for Outlook
allowing SOS specialists to review PO validation and approve Salesforce
booking form creation with a single click.
"""

from __future__ import annotations

import html
import json
import os
from typing import Any

from .builder import BookingForm


def render_adaptive_card(form: BookingForm, ai_summary: str, instance_url: str = None) -> dict[str, Any]:
    """Generates an Outlook Actionable Message Adaptive Card (v1.4)."""
    base_sf_url = (instance_url or os.environ.get("SF_INSTANCE_URL", "https://f5--poclab.sandbox.my.salesforce.com")).rstrip("/")
    notes_facts = [
        {"title": n.title, "value": n.body.replace("\n", " | ")}
        for n in form.notes
    ]

    actions: list[dict[str, Any]] = []

    # Only present 1-Click Approve & Book if there are no blocking order issues
    if not form.order_issues:
        actions.append({
            "type": "Action.Submit",
            "title": "✅ 1-Click Approve & Book",
            "style": "positive",
            "data": {
                "action": "create_booking_form",
                "booking_form_name": form.booking_form_name,
                "po_number": form.po_number,
                "opportunity_id": form.opportunity_id,
            }
        })

    opp_url = (
        f"{base_sf_url}/lightning/r/Opportunity/{form.opportunity_id}/view"
        if form.opportunity_id else base_sf_url
    )
    actions.append({
        "type": "Action.OpenUrl",
        "title": "⚠️ Review in Salesforce (poclab)" if form.order_issues else "🔍 View in Salesforce (poclab)",
        "url": opp_url,
    })

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "Container",
                "style": "emphasis",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": f"📋 F5 SOS Booking Approval — PO #{form.po_number or 'UNKNOWN'}",
                        "weight": "Bolder",
                        "size": "Medium",
                    },
                    {
                        "type": "TextBlock",
                        "text": ai_summary,
                        "wrap": True,
                        "spacing": "Small",
                    }
                ]
            },
            {
                "type": "FactSet",
                "spacing": "Medium",
                "facts": [
                    {"title": "Account", "value": form.account_name or "N/A"},
                    {"title": "Booked Amount", "value": f"{form.currency} ${form.amount:,.2f}"},
                    {"title": "F5 Quote #", "value": form.f5_quote_number or "N/A"},
                    {"title": "Order Type", "value": form.sales_order_type},
                    {"title": "Distributor", "value": form.distributor},
                    {"title": "Reseller", "value": form.reseller_name},
                    {"title": "Key Recipient", "value": form.registration_key_notification or "From Opp"},
                    {"title": "Order Issues", "value": "Yes (Requires Review)" if form.order_issues else "No (Clean)"},
                ]
            },
        ],
        "actions": actions,
    }

    if notes_facts:
        card["body"].append({
            "type": "TextBlock",
            "text": "📝 Auto-Drafted Notes to Revenue Operations (RO):",
            "weight": "Bolder",
            "spacing": "Medium",
        })
        card["body"].append({
            "type": "FactSet",
            "facts": notes_facts,
        })

    return card


def render_html_email_preview(form: BookingForm, ai_summary: str, instance_url: str = None) -> str:
    """Renders a responsive, HTML-escaped email card for Outlook."""
    base_sf_url = (instance_url or os.environ.get("SF_INSTANCE_URL", "https://f5--poclab.sandbox.my.salesforce.com")).rstrip("/")
    opp_url = (
        f"{base_sf_url}/lightning/r/Opportunity/{html.escape(form.opportunity_id)}/view"
        if form.opportunity_id else base_sf_url
    )

    esc_po = html.escape(str(form.po_number or "UNKNOWN"))
    esc_account = html.escape(str(form.account_name or "N/A"))
    esc_summary = html.escape(str(ai_summary or ""))
    esc_quote = html.escape(str(form.f5_quote_number or "N/A"))
    esc_order_type = html.escape(str(form.sales_order_type or ""))
    esc_disti = html.escape(str(form.distributor or ""))
    esc_reseller = html.escape(str(form.reseller_name or ""))
    esc_key = html.escape(str(form.registration_key_notification or "Direct / Opp Contact"))
    esc_currency = html.escape(str(form.currency or "USD"))

    notes_html = "".join(
        f"<li style='margin-bottom:8px;'><strong>{html.escape(n.title)}:</strong><br/>"
        f"<pre style='background:#f4f4f4;padding:8px;border-radius:4px;white-space:pre-wrap;'>{html.escape(n.body)}</pre></li>"
        for n in form.notes
    )

    if form.order_issues:
        action_button = f"""
        <a href="{opp_url}" style="background: #e28400; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 4px; font-weight: bold; display: inline-block;">
          ⚠️ Review Findings & Exceptions in Salesforce
        </a>
        """
    else:
        action_button = f"""
        <a href="#" style="background: #0070d2; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 4px; font-weight: bold; display: inline-block;">
          🚀 1-Click Approve & Book in Salesforce
        </a>
        """

    return f"""
    <div style="font-family: Arial, sans-serif; max-width: 650px; border: 1px solid #e1e4e8; border-radius: 8px; padding: 20px; background: #ffffff;">
      <div style="border-bottom: 2px solid #0056b3; padding-bottom: 12px; margin-bottom: 16px;">
        <h2 style="color: #0056b3; margin: 0 0 6px 0;">F5 SOS Automated Booking Approval</h2>
        <p style="margin: 0; color: #555; font-size: 14px;"><strong>PO #{esc_po}</strong> &bull; {esc_account}</p>
      </div>

      <div style="background: #eef6fc; border-left: 4px solid #0070d2; padding: 12px; border-radius: 4px; margin-bottom: 16px; font-size: 14px; color: #16325c;">
        <strong>Executive Summary:</strong> {esc_summary}
      </div>

      <table style="width: 100%; border-collapse: collapse; margin-bottom: 16px; font-size: 13px;">
        <tr><td style="padding: 6px 0; color: #666;"><strong>Booked Amount:</strong></td><td style="padding: 6px 0; font-weight: bold; color: #2e844a;">{esc_currency} ${form.amount:,.2f}</td></tr>
        <tr><td style="padding: 6px 0; color: #666;"><strong>F5 Quote #:</strong></td><td style="padding: 6px 0;">{esc_quote}</td></tr>
        <tr><td style="padding: 6px 0; color: #666;"><strong>Sales Order Type:</strong></td><td style="padding: 6px 0;">{esc_order_type}</td></tr>
        <tr><td style="padding: 6px 0; color: #666;"><strong>Distributor:</strong></td><td style="padding: 6px 0;">{esc_disti}</td></tr>
        <tr><td style="padding: 6px 0; color: #666;"><strong>Reseller:</strong></td><td style="padding: 6px 0;">{esc_reseller}</td></tr>
        <tr><td style="padding: 6px 0; color: #666;"><strong>Key Notification:</strong></td><td style="padding: 6px 0;">{esc_key}</td></tr>
      </table>

      {f"<div style='margin-bottom:16px;'><strong style='color:#333;'>Auto-Drafted Notes to RO:</strong><ul style='padding-left:18px;margin-top:6px;'>" + notes_html + "</ul></div>" if form.notes else ""}

      <div style="margin-top: 24px; text-align: center;">
        {action_button}
      </div>
    </div>
    """
