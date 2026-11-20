"""Tests for campaign and call persistence in Milestone 2."""

import pytest
import asyncio

from app.campaign_store import CampaignStore


@pytest.mark.asyncio
async def test_create_campaign_before_approval(tmp_path):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'campaign.db'}")
    await store.initialize()
    try:
        campaign = await store.create_campaign("Seller onboarding", "GTMLeads")

        assert campaign.name == "Seller onboarding"
        assert campaign.business_name == "GTMLeads"
        assert campaign.approved is False

        with pytest.raises(PermissionError):
            await store.create_call(campaign.id, "+919876543210", "Seller A")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_call_claim_is_atomic_and_binding_is_idempotent(tmp_path):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'campaign.db'}")
    await store.initialize()
    try:
        campaign = await store.create_campaign("Campaign", "Business")
        await store.approve_campaign(campaign.id)
        call = await store.create_call(campaign.id, "+919876543210", "Seller")

        claims = await asyncio.gather(store.claim_queued_call(call.id), store.claim_queued_call(call.id))
        assert sum(item is not None for item in claims) == 1
        stored = await store.get_call(call.id)
        assert stored.status == "dialing"
        assert stored.started_at is not None

        assert (await store.bind_exotel_call(call.id, "exotel-123")).exotel_call_sid == "exotel-123"
        assert (await store.bind_exotel_call(call.id, "exotel-123")).exotel_call_sid == "exotel-123"
        with pytest.raises(ValueError):
            await store.bind_exotel_call(call.id, "different-sid")
        assert (await store.get_call(call.id)).exotel_call_sid == "exotel-123"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_approved_campaign_records_calls_reverse_chronologically(tmp_path):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'campaign.db'}")
    await store.initialize()
    try:
        campaign = await store.create_campaign("Seller onboarding", "GTMLeads")
        approved = await store.approve_campaign(campaign.id)

        assert approved is not None
        assert approved.approved is True

        first = await store.create_call(campaign.id, "+919876543210", "Seller A")
        second = await store.create_call(campaign.id, "+918765432109", "Seller B")
        calls = await store.list_calls(campaign.id)

        assert [call.id for call in calls[:2]] == [second.id, first.id]
        assert calls[0].status == "queued"
        assert calls[0].phone_number == "+918765432109"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_carrier_events_correlate_and_never_reverse_terminal_status(tmp_path):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'campaign.db'}")
    await store.initialize()
    try:
        campaign = await store.create_campaign("Campaign", "Business")
        await store.approve_campaign(campaign.id)
        call = await store.create_call(campaign.id, "+919876543210", "Seller")
        await store.claim_queued_call(call.id)

        # The stream can arrive before the dial response saves the provider ID.
        answered = await store.record_carrier_event(call.id, "provider-id", status="answered", stream_sid="stream-id")
        assert answered.exotel_call_sid == "provider-id"
        assert answered.stream_sid == "stream-id"
        assert answered.answered_at is not None
        assert (await store.bind_exotel_call(call.id, "provider-id")).status == "answered"
        repeated = await store.record_carrier_event(call.id, "provider-id", status="answered", stream_sid="stream-id")
        assert repeated.answered_at == answered.answered_at

        for provider_id, stream_id in (("wrong", "stream-id"), ("provider-id", "wrong")):
            with pytest.raises(ValueError):
                await store.record_carrier_event(call.id, provider_id, status="answered", stream_sid=stream_id)

        completed = await store.record_carrier_event(call.id, "provider-id", status="completed")
        repeated = await store.record_carrier_event(call.id, "provider-id", status="completed")
        assert repeated.completed_at == completed.completed_at
        with pytest.raises(ValueError):
            await store.record_carrier_event(call.id, "provider-id", status="answered")
        with pytest.raises(ValueError):
            await store.record_carrier_event(call.id, "provider-id", status="failed")
        assert (await store.get_call(call.id)).status == "completed"

        other = await store.create_call(campaign.id, "+919876543211", "Seller B")
        await store.claim_queued_call(other.id)
        await store.record_carrier_event(other.id, "other-provider", status="busy")
        with pytest.raises(ValueError):
            await store.record_carrier_event(other.id, "other-provider", status="answered", stream_sid="late-stream")
        assert (await store.get_call(other.id)).status == "busy"
    finally:
        await store.close()
