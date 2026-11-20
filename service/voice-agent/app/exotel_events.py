"""Interpret verified Exotel status callbacks for the campaign store.

This module does not authenticate or receive HTTP requests. A future endpoint
must verify the sender before passing it a callback.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from app.campaign_store import CallRecord, CampaignStore


@dataclass(frozen=True)
class CarrierStatus:
    call_id: str
    call_sid: str
    status: str


def parse_status_callback(payload: Mapping[str, object]) -> CarrierStatus:
    """Parse the documented callback fields without accepting unknown states."""
    call_id = payload.get("CustomField")
    call_sid = payload.get("CallSid")
    event = payload.get("EventType")
    status = payload.get("Status")
    if not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 128:
        raise ValueError("Missing or invalid call ID")
    if not isinstance(call_sid, str) or not call_sid.strip() or len(call_sid) > 100:
        raise ValueError("Missing or invalid Exotel call SID")
    if event == "answered":
        mapped = "answered"
    elif event == "terminal" and status in ("completed", "failed", "busy", "no-answer"):
        mapped = status
    else:
        raise ValueError("Unsupported callback event or status")
    return CarrierStatus(call_id=call_id, call_sid=call_sid, status=mapped)


async def apply_verified_status_callback(store: CampaignStore, payload: Mapping[str, object]) -> CallRecord:
    """Apply a callback only after its sender has been verified by the caller."""
    event = parse_status_callback(payload)
    return await store.record_carrier_event(event.call_id, event.call_sid, status=event.status)
