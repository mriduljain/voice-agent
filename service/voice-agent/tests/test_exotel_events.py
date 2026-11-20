import pytest

from app.campaign_store import CampaignStore
from app.exotel_events import CarrierStatus, apply_verified_status_callback, parse_status_callback


def test_answered_callback_uses_event_type():
    result = parse_status_callback({"CustomField": "our-id", "CallSid": "exotel-id", "EventType": "answered", "Status": "in-progress"})
    assert result == CarrierStatus("our-id", "exotel-id", "answered")


@pytest.mark.parametrize("status", ["completed", "failed", "busy", "no-answer"])
def test_terminal_callback(status):
    result = parse_status_callback({"CustomField": "our-id", "CallSid": "exotel-id", "EventType": "terminal", "Status": status})
    assert result.status == status


@pytest.mark.parametrize("payload", [
    {},
    {"CustomField": "our-id", "CallSid": "", "EventType": "answered"},
    {"CustomField": "our-id", "CallSid": "exotel-id", "EventType": "terminal", "Status": "in-progress"},
    {"CustomField": "our-id", "CallSid": "exotel-id", "EventType": "ringing", "Status": "answered"},
])
def test_invalid_callbacks_are_rejected(payload):
    with pytest.raises(ValueError):
        parse_status_callback(payload)


@pytest.mark.asyncio
async def test_callback_applied_before_dial_response(tmp_path):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'calls.db'}")
    await store.initialize()
    try:
        campaign = await store.create_campaign("Campaign", "Business")
        await store.approve_campaign(campaign.id)
        call = await store.create_call(campaign.id, "+919876543210", "Seller")
        await store.claim_queued_call(call.id)
        payload = {"CustomField": call.id, "CallSid": "exotel-id", "EventType": "answered", "Status": "in-progress"}
        result = await apply_verified_status_callback(store, payload)
        assert result.status == "answered"
        assert result.exotel_call_sid == "exotel-id"
        assert (await store.bind_exotel_call(call.id, "exotel-id")).status == "answered"
        assert (await apply_verified_status_callback(store, payload)).status == "answered"
    finally:
        await store.close()
