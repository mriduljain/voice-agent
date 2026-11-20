import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from app.campaign_store import CampaignStore
from app.exotel_adapter import router
from app.outbound_calls import OutboundCallService
import app.exotel_adapter as exotel_adapter


def test_callback_and_stream_correlation_with_simulated_exotel(tmp_path):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'calls.db'}")

    async def setup():
        await store.initialize()
        campaign = await store.create_campaign("Campaign", "Business")
        await store.approve_campaign(campaign.id)
        call = await store.create_call(campaign.id, "+919876543210", "Seller")
        await store.claim_queued_call(call.id)
        return call.id

    call_id = asyncio.run(setup())
    app = FastAPI()
    app.include_router(router)
    app.state.campaign_store = store

    async def verify(connection):
        return connection.headers.get("x-test-auth") == "approved"

    try:
        with TestClient(app) as client:
            callback = {"CustomField": call_id, "CallSid": "sid-1", "EventType": "answered", "Status": "in-progress"}
            assert client.post("/telephony/exotel/status", data=callback).status_code == 403
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/telephony/exotel/outbound-media?call_id={call_id}"):
                    pass

            # These test-only verifiers are not configured in the real application.
            app.state.exotel_callback_verifier = verify
            app.state.exotel_stream_verifier = verify
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/telephony/exotel/outbound-media?call_id={call_id}", headers={"x-test-auth": "approved"}) as ws:
                    ws.send_json({"event": "start", "start": {"stream_sid": "stream-1", "call_sid": "sid-1", "custom_parameters": {"call_id": "wrong"}}})
                    ws.receive_json()
            assert client.post("/telephony/exotel/status", data=callback, headers={"x-test-auth": "approved"}).status_code == 200
            with client.websocket_connect("/telephony/exotel/outbound-media", headers={"x-test-auth": "approved"}) as ws:
                ws.send_json({"event": "start", "start": {"stream_sid": "stream-1", "call_sid": "sid-1", "custom_parameters": {"call_id": call_id}}})
                ws.send_json({"event": "media", "media": {"payload": "AAA="}})
                assert ws.receive_json()["event"] == "media"
                ws.send_json({"event": "stop"})

            finished = dict(callback, EventType="terminal", Status="completed")
            assert client.post("/telephony/exotel/status", data=finished, headers={"x-test-auth": "approved"}).status_code == 200
            assert client.post("/telephony/exotel/status", data=callback, headers={"x-test-auth": "approved"}).status_code == 409

        saved = asyncio.run(store.get_call(call_id))
        assert saved.status == "completed"
        assert saved.exotel_call_sid == "sid-1"
        assert saved.stream_sid == "stream-1"
    finally:
        asyncio.run(store.close())


def test_complete_simulated_outbound_flow_with_early_events(tmp_path):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'flow.db'}")

    async def setup():
        await store.initialize()
        campaign = await store.create_campaign("Campaign", "Business")
        await store.approve_campaign(campaign.id)
        return await store.create_call(campaign.id, "+919876543210", "Seller")

    call = asyncio.run(setup())
    app = FastAPI()
    app.include_router(router)
    app.state.campaign_store = store

    async def verify(connection):
        return connection.headers.get("x-test-auth") == "approved"

    app.state.exotel_callback_verifier = verify
    app.state.exotel_stream_verifier = verify
    auth = {"x-test-auth": "approved"}

    try:
        with TestClient(app) as http:
            class FakeExotel:
                async def request_call(self, *, phone_number, call_id):
                    assert phone_number == call.phone_number
                    # Both events arrive before the API response containing the SID.
                    callback = {"CustomField": call_id, "CallSid": "sid-1", "EventType": "answered", "Status": "in-progress"}
                    assert http.post("/telephony/exotel/status", data=callback, headers=auth).status_code == 200
                    with http.websocket_connect("/telephony/exotel/outbound-media", headers=auth) as ws:
                        ws.send_json({"event": "start", "start": {"stream_sid": "stream-1", "call_sid": "sid-1", "custom_parameters": {"call_id": call_id}}})
                        ws.send_json({"event": "media", "media": {"payload": "AAA="}})
                        assert ws.receive_json()["event"] == "media"
                        ws.send_json({"event": "stop"})
                    return "sid-1"

            result = asyncio.run(OutboundCallService(store, FakeExotel()).start_call(call.id))
            assert (result.status, result.exotel_call_sid, result.stream_sid) == ("answered", "sid-1", "stream-1")
            terminal = {"CustomField": call.id, "CallSid": "sid-1", "EventType": "terminal", "Status": "completed"}
            assert http.post("/telephony/exotel/status", data=terminal, headers=auth).status_code == 200

        assert asyncio.run(store.get_call(call.id)).status == "completed"
    finally:
        asyncio.run(store.close())


def test_outbound_stream_passes_matching_campaign_context_to_runtime(tmp_path, monkeypatch):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'context.db'}")

    async def setup():
        await store.initialize()
        campaign = await store.create_campaign("Summer campaign", "Example business")
        await store.approve_campaign(campaign.id)
        call = await store.create_call(campaign.id, "+919876543210", "Seller A")
        await store.claim_queued_call(call.id)
        return call.id

    seen = []

    class FakeCall:
        def __init__(self, *args, **kwargs):
            self.session_id = "test-session"
            seen.append(kwargs["business_hint"])

        async def start(self):
            pass

        async def events(self):
            blob = SimpleNamespace(data=bytes(9600), mime_type="audio/pcm;rate=24000")
            yield SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=blob)]))

        async def close(self):
            pass

    monkeypatch.setattr(exotel_adapter, "LiveVoiceCall", FakeCall)
    call_id = asyncio.run(setup())
    app = FastAPI()
    app.include_router(router)
    app.state.campaign_store = store
    app.state.runner = object()
    app.state.agent_app_name = "test"

    async def verify(connection):
        return True

    app.state.exotel_stream_verifier = verify
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/telephony/exotel/outbound-media") as ws:
                ws.send_json({"event": "start", "start": {"stream_sid": "stream-1", "call_sid": "sid-1", "custom_parameters": {"call_id": call_id}}})
                assert ws.receive_json()["event"] == "media"
                ws.send_json({"event": "stop"})
        assert len(seen) == 1
        context = json.loads(seen[0][seen[0].index("{"):])
        assert context == {"campaign": "Summer campaign", "business": "Example business", "seller": "Seller A"}
        assert "+919876543210" not in seen[0]
    finally:
        asyncio.run(store.close())


def test_outbound_audio_and_context_through_live_runtime(tmp_path):
    store = CampaignStore(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")

    async def setup():
        await store.initialize()
        campaign = await store.create_campaign("Autumn campaign", "Example business")
        await store.approve_campaign(campaign.id)
        call = await store.create_call(campaign.id, "+919876543210", "Seller A")
        await store.claim_queued_call(call.id)
        return call.id

    class FakeSessionService:
        def __init__(self):
            self.deleted = []

        async def create_session(self, **kwargs):
            return SimpleNamespace(id="test-session")

        async def delete_session(self, **kwargs):
            self.deleted.append(kwargs)

    class FakeRunner:
        def __init__(self):
            self.session_service = FakeSessionService()
            self.requests = []

        async def run_live(self, **kwargs):
            queue = kwargs["live_request_queue"]
            self.requests.append(await queue.get())
            self.requests.append(await queue.get())
            blob = SimpleNamespace(data=bytes(9600), mime_type="audio/pcm;rate=24000")
            yield SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=blob)]))

    call_id = asyncio.run(setup())
    runner = FakeRunner()
    app = FastAPI()
    app.include_router(router)
    app.state.campaign_store = store
    app.state.runner = runner
    app.state.agent_app_name = "test"

    async def verify(connection):
        return True

    app.state.exotel_stream_verifier = verify
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/telephony/exotel/outbound-media") as ws:
                ws.send_json({"event": "start", "start": {"stream_sid": "stream-1", "call_sid": "sid-1", "custom_parameters": {"call_id": call_id}, "media_format": {"sample_rate": 8000}}})
                ws.send_json({"event": "media", "media": {"payload": "AAA="}})
                assert ws.receive_json()["event"] == "media"
                ws.send_json({"event": "stop"})

        context_text = runner.requests[0].content.parts[0].text
        context = json.loads(context_text[context_text.index("{"):])
        assert context == {"campaign": "Autumn campaign", "business": "Example business", "seller": "Seller A"}
        assert runner.requests[1].blob.mime_type == "audio/pcm;rate=16000"
        assert len(runner.requests[1].blob.data) == 4
        assert len(runner.session_service.deleted) == 1
    finally:
        asyncio.run(store.close())
