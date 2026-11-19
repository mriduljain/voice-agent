"""Tests for campaign and call persistence in Milestone 2."""

import pytest

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
