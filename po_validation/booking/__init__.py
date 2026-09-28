"""Booking form generator package for F5 SOS automation.

Constructs Salesforce Booking_Form__c payloads and auto-drafts
standardized Notes to Revenue Operations (RO).
"""

from .builder import BookingForm, BookingNote, BookingFormBuilder

__all__ = ["BookingForm", "BookingNote", "BookingFormBuilder"]
