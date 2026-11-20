"""Coordinate a reserved call record with a carrier request."""

from typing import Protocol

from app.campaign_store import CallRecord, CampaignStore
from app.exotel_client import ExotelCallError


class CallClient(Protocol):
    async def request_call(self, *, phone_number: str, call_id: str) -> str: ...


class CallUnavailableError(Exception):
    """The call does not exist or is no longer queued."""


class OutboundCallService:
    """Request one call at most once; uncertain outcomes require reconciliation."""

    def __init__(self, store: CampaignStore, client: CallClient) -> None:
        self.store = store
        self.client = client

    async def start_call(self, call_id: str) -> CallRecord:
        call = await self.store.claim_queued_call(call_id)
        if call is None:
            raise CallUnavailableError("Call not found or not queued")
        try:
            sid = await self.client.request_call(phone_number=call.phone_number, call_id=call.id)
        except ExotelCallError:
            await self.store.mark_dial_outcome_unknown(call.id)
            raise
        return await self.store.bind_exotel_call(call.id, sid)
