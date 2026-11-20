import asyncio

import pytest
import pytest_asyncio

from app.campaign_store import CampaignStore
from app.exotel_client import ExotelCallError
from app.outbound_calls import CallUnavailableError, OutboundCallService


class FakeClient:
    def __init__(self, *, fail=False):
        self.requests = []
        self.fail = fail

    async def request_call(self, *, phone_number, call_id):
        self.requests.append((phone_number, call_id))
        if self.fail:
            raise ExotelCallError("Request timed out")
        return "exotel-sid"


@pytest_asyncio.fixture
async def queued_call(tmp_path):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'calls.db'}")
    await store.initialize()
    campaign = await store.create_campaign("Campaign", "Business")
    await store.approve_campaign(campaign.id)
    call = await store.create_call(campaign.id, "+919876543210", "Seller")
    try:
        yield store, call
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_only_one_concurrent_request_reaches_carrier(queued_call):
    store, call = queued_call
    client = FakeClient()
    service = OutboundCallService(store, client)
    results = await asyncio.gather(service.start_call(call.id), service.start_call(call.id), return_exceptions=True)
    assert sum(isinstance(result, CallUnavailableError) for result in results) == 1
    assert client.requests == [(call.phone_number, call.id)]
    saved = await store.get_call(call.id)
    assert saved.status == "dialing"
    assert saved.exotel_call_sid == "exotel-sid"


@pytest.mark.asyncio
async def test_uncertain_outcome_cannot_be_retried(queued_call):
    store, call = queued_call
    client = FakeClient(fail=True)
    service = OutboundCallService(store, client)
    with pytest.raises(ExotelCallError):
        await service.start_call(call.id)
    assert (await store.get_call(call.id)).status == "dial_outcome_unknown"
    with pytest.raises(CallUnavailableError):
        await service.start_call(call.id)
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_unknown_update_does_not_erase_bound_call(queued_call):
    store, call = queued_call
    await store.claim_queued_call(call.id)
    await store.bind_exotel_call(call.id, "exotel-sid")
    await store.mark_dial_outcome_unknown(call.id)
    saved = await store.get_call(call.id)
    assert saved.status == "dialing"
    assert saved.exotel_call_sid == "exotel-sid"
