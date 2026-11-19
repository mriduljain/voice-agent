from fastapi import FastAPI
# Imports a client for exercising our application(test_app below) inside the test process.
# TestClient acts like a client, but delivers HTTP requests and WebSocket messages directly 
# to the application through an in-process transport.
# TestClient sets up a background thread and an async event loop to run the application.
from fastapi.testclient import TestClient
import base64
import struct
from types import SimpleNamespace

from app.exotel_adapter import router
import app.exotel_adapter as exotel_adapter

# uv run pytest tests/test_exotel_adapter.py -v

#pytest discovers functions that start with "test_" and runs them as tests. 
#This test checks the websocket echo functionality.
#This test doesn't need Uvicorn as it is calling the router directly via FastAPI's TestClient. 
# This is a unit test, not an integration test.
def test_websocket_echo():
    # Create a FastAPI app and include the Exotel router for testing.
    test_app = FastAPI()
    test_app.include_router(router)

    # TestClient provides the machinery to exercise test_app object without Uvicorn.
    # It is a wrapping process to hold/instantiate client object as well as router/server object 
    # and communication between the two via in-memory websocket. So we test the application’s 
    # WebSocket behavior, while leaving Uvicorn, network ports and actual network transport out of this test.
    with TestClient(test_app) as client:
        with client.websocket_connect("/telephony/exotel/socket") as socket:
            socket.send_text("hello")
            assert socket.receive_text() == "hello"

            socket.send_text("second message")
            assert socket.receive_text() == "second message"

#uv run pytest tests/test_exotel_adapter.py::test_media_echo -v

def test_media_echo():
    test_app = FastAPI()
    test_app.include_router(router)

    with TestClient(test_app) as client:
        with client.websocket_connect("/telephony/exotel/media") as socket:
            socket.send_json({
                "event": "start",
                "start": {"stream_sid": "test-stream"},
            })

            pcm = struct.pack("<4h", 0, 1000, 2000, 3000)

            socket.send_json({
                "event": "media",
                "media": {"payload": base64.b64encode(pcm).decode("ascii")},
            })

            response = socket.receive_json()

            assert response["event"] == "media"
            assert base64.b64decode(response["media"]["payload"]) == pcm

            socket.send_json({"event": "stop"})


def test_live_audio_is_resampled_and_short_turn_is_flushed(monkeypatch):
    class FakeCall:
        def __init__(self, *args, **kwargs):
            self.session_id = "session"

        async def start(self):
            pass

        async def events(self):
            blob = SimpleNamespace(data=struct.pack("<4800h", *([1000] * 4800)), mime_type="audio/pcm;rate=24000")
            yield SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=blob)]))
            blob = SimpleNamespace(data=struct.pack("<120h", *([1000] * 120)), mime_type="audio/pcm;rate=24000")
            yield SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=blob)]), turn_complete=True)

        async def close(self):
            pass

    monkeypatch.setattr(exotel_adapter, "LiveVoiceCall", FakeCall)
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.runner = object()
    test_app.state.agent_app_name = "test"

    with TestClient(test_app) as client:
        with client.websocket_connect("/telephony/exotel/media") as socket:
            socket.send_json({"event": "start", "start": {"stream_sid": "test-stream", "media_format": {"sample_rate": 8000}}})
            first = base64.b64decode(socket.receive_json()["media"]["payload"])
            remainder = base64.b64decode(socket.receive_json()["media"]["payload"])
            assert len(first) == 3200
            assert len(remainder) == 3200
            assert struct.unpack("<1600h", first) == (1000,) * 1600
            assert struct.unpack("<1600h", remainder) == (1000,) * 40 + (0,) * 1560
            socket.send_json({"event": "stop"})
