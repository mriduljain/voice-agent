import asyncio
import base64
import binascii
import logging
import uuid
from contextlib import aclosing, suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.telephony_audio import LinearResampler, mulaw_to_pcm16, pcm16_to_mulaw
from app.voice_runtime import LiveVoiceCall, pcm_mime_type

logger = logging.getLogger(__name__)

# Create Exotel router to handle Exotel related endpoints.
# API Router groups related API endpoints together - in this case Exotel's endpoints.
router = APIRouter(prefix="/telephony/exotel", tags=["exotel"])

@router.get("/health")
async def health():
    return {"status": "ok"}


# Each connection gets it own websocket and independent function call (below).
# This websocket connection does not represent or handle a session. That needs
# to be modeled separately via ADK conversation sessions. This websocket is just a transport for audio data.
@router.websocket("/socket")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_text()
            # Here you can process the received data and send a response back if needed.
            await websocket.send_text(message)
    except WebSocketDisconnect:
        print("WebSocket client disconnected")



# Exotel sends different event types:
# - connected: the connection opened.
# - start: identifies the call’s audio stream.
# - media: contains an audio chunk.
# - stop: the stream ended.
@router.websocket("/media")
async def media(websocket: WebSocket):
    """Bridge one Exotel WebSocket to one Gemini Live session."""
    await websocket.accept()

    stream_sid: str | None = None
    call: LiveVoiceCall | None = None
    outbound_task: asyncio.Task[None] | None = None
    inbound_resampler = LinearResampler(input_rate=8000, output_rate=16000)
    outbound_resampler = LinearResampler(input_rate=24000, output_rate=8000)
    outbound_buffer = bytearray()

    async def send_adk_audio(pcm24khz: bytes) -> None:
        nonlocal outbound_buffer
        pcm8khz = outbound_resampler.resample(pcm24khz)
        outbound_buffer.extend(pcm16_to_mulaw(pcm8khz))

        while len(outbound_buffer) >= 160:
            frame = bytes(outbound_buffer[:160])
            del outbound_buffer[:160]
            await websocket.send_json(
                {
                    "event": "media",
                    "stream_sid": stream_sid,
                    "media": {"payload": base64.b64encode(frame).decode("ascii")},
                }
            )

    async def consume_adk_events() -> None:
        if call is None:
            return

        events = call.events()
        async with aclosing(events):
            async for event in events:
                error_code = getattr(event, "error_code", None)
                error_message = getattr(event, "error_message", None)
                if error_code or error_message:
                    raise RuntimeError(f"ADK Live error: {error_code} {error_message}")

                content = getattr(event, "content", None)
                if content:
                    for part in content.parts or []:
                        blob = getattr(part, "inline_data", None)
                        if blob and blob.data:
                            mime_type = pcm_mime_type(getattr(blob, "mime_type", None))
                            if mime_type:
                                await send_adk_audio(blob.data)

    try:
        while True:
            message = await websocket.receive_json()
            event = message.get("event")

            if event == "start":
                start = message.get("start", {})
                stream_sid = start.get("stream_sid") or f"stream-{uuid.uuid4()}"
                logger.info("Started Exotel stream %s", stream_sid)

                runner = getattr(websocket.app.state, "runner", None)
                app_name = getattr(websocket.app.state, "agent_app_name", None)
                if runner is not None and app_name:
                    call = LiveVoiceCall(runner, app_name=app_name)
                    await call.start()
                    outbound_task = asyncio.create_task(consume_adk_events())

            elif event == "media" and stream_sid:
                payload = message.get("media", {}).get("payload")
                if not isinstance(payload, str):
                    continue

                try:
                    carrier_mu_law = base64.b64decode(payload, validate=True)
                except (binascii.Error, ValueError):
                    await websocket.send_json(message)
                    continue

                pcm8khz = mulaw_to_pcm16(carrier_mu_law)
                pcm16khz = inbound_resampler.resample(pcm8khz)

                if call is not None:
                    call.send_pcm(pcm16khz)
                else:
                    await websocket.send_json(message)

            elif event == "stop":
                break

    except WebSocketDisconnect:
        logger.info("Exotel stream %s disconnected", stream_sid)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Exotel stream %s failed", stream_sid)
    finally:
        if outbound_task is not None:
            outbound_task.cancel()
            with suppress(asyncio.CancelledError):
                await outbound_task
        if call is not None:
            await call.close()