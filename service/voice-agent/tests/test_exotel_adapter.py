from fastapi import FastAPI
# Imports a client for exercising our application(test_app below) inside the test process.
# TestClient acts like a client, but delivers HTTP requests and WebSocket messages directly 
# to the application through an in-process transport.
# TestClient sets up a background thread and an async event loop to run the application.
from fastapi.testclient import TestClient

from app.exotel_adapter import router

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

            socket.send_json({
                "event": "media",
                "media": {"payload": "test-audio-placeholder"},
            })

            response = socket.receive_json()

            assert response == {
                "event": "media",
                "media": {"payload": "test-audio-placeholder"},
            }

            socket.send_json({"event": "stop"})