import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.campaign_api import router
from app.campaign_store import CampaignStore
from app.outbound_calls import OutboundCallService


def test_outbound_api_requires_auth_and_does_not_duplicate_calls(tmp_path):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'calls.db'}")

    async def setup():
        await store.initialize()
        campaign = await store.create_campaign("Campaign", "Business")
        await store.approve_campaign(campaign.id)
        call = await store.create_call(campaign.id, "+919876543210", "Seller")
        return campaign.id, call.id

    class FakeClient:
        def __init__(self):
            self.requests = []

        async def request_call(self, *, phone_number, call_id):
            self.requests.append(call_id)
            return "provider-sid"

    async def verify(request):
        return request.headers.get("x-test-auth") == "approved"

    campaign_id, call_id = asyncio.run(setup())
    app = FastAPI()
    app.include_router(router)
    app.state.campaign_store = store
    fake = FakeClient()
    path = f"/campaigns/{campaign_id}/calls/{call_id}/start"
    try:
        with TestClient(app) as client:
            assert client.post(path).status_code == 403
            app.state.outbound_request_verifier = verify
            assert client.post(path, headers={"x-test-auth": "approved"}).status_code == 503
            app.state.outbound_call_service = OutboundCallService(store, fake)
            assert client.post(f"/campaigns/wrong/calls/{call_id}/start", headers={"x-test-auth": "approved"}).status_code == 404
            response = client.post(path, headers={"x-test-auth": "approved"})
            assert response.status_code == 200
            assert response.json()["exotel_call_sid"] == "provider-sid"
            assert client.post(path, headers={"x-test-auth": "approved"}).status_code == 409
            assert fake.requests == [call_id]
    finally:
        asyncio.run(store.close())
