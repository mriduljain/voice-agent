import asyncio
import base64
import binascii
import logging
import time
import uuid
from contextlib import aclosing

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.telephony_audio import LinearResampler
from app.voice_runtime import LiveVoiceCall, pcm_mime_type

logger = logging.getLogger("uvicorn.error")

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
    carrier_sample_rate = 8000
    inbound_resampler = LinearResampler(input_rate=8000, output_rate=16000)
    outbound_resampler = LinearResampler(input_rate=24000, output_rate=8000)
    outbound_buffer = bytearray()
    started_at = time.monotonic()
    stats = {"in_frames": 0, "in_bytes": 0, "invalid_frames": 0,
             "out_frames": 0, "out_bytes": 0, "adk_events": 0, "pcm_events": 0}
    input_sizes: set[int] = set()
    output_mimes: set[str] = set()
    end_reason = "connection ended"

    async def send_adk_audio(pcm24khz: bytes, *, flush: bool = False) -> None:
        outbound_buffer.extend(outbound_resampler.resample(pcm24khz))

        # Exotel recommends at least 3,200 bytes per message and multiples of
        # 320 bytes to avoid gaps in bidirectional playback.
        frame_bytes = 3200
        if flush and outbound_buffer and len(outbound_buffer) < frame_bytes:
            outbound_buffer.extend(b"\0" * (frame_bytes - len(outbound_buffer)))
        elif flush and len(outbound_buffer) % frame_bytes:
            outbound_buffer.extend(b"\0" * (frame_bytes - len(outbound_buffer) % frame_bytes))
        while len(outbound_buffer) >= frame_bytes or (flush and outbound_buffer):
            frame = bytes(outbound_buffer[:frame_bytes])
            del outbound_buffer[:frame_bytes]
            await websocket.send_json(
                {
                    "event": "media",
                    "stream_sid": stream_sid,
                    "media": {"payload": base64.b64encode(frame).decode("ascii")},
                }
            )
            stats["out_frames"] += 1
            stats["out_bytes"] += len(frame)
            if stats["out_frames"] == 1:
                logger.info("First carrier output stream_sid=%s elapsed_ms=%.0f frame_bytes=%s", stream_sid, (time.monotonic() - started_at) * 1000, len(frame))

    async def consume_adk_events() -> None:
        if call is None:
            return

        events = call.events()
        async with aclosing(events):
            async for event in events:
                stats["adk_events"] += 1

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
                                stats["pcm_events"] += 1
                                output_mimes.add(mime_type[:100])
                                if stats["pcm_events"] == 1:
                                    logger.info("First Gemini audio stream_sid=%s elapsed_ms=%.0f mime=%s bytes=%s", stream_sid, (time.monotonic() - started_at) * 1000, mime_type[:100], len(blob.data))
                                await send_adk_audio(blob.data)

                if getattr(event, "turn_complete", False):
                    await send_adk_audio(b"", flush=True)

        logger.info(
            "ADK event stream ended stream_sid=%s adk_events=%s pcm_events=%s",
            stream_sid,
            stats["adk_events"],
            stats["pcm_events"],
        )

    try:
        while True:
            message = await websocket.receive_json()
            event = message.get("event")

            if event == "start":
                start = message.get("start", {})
                stream_sid = start.get("stream_sid") or f"stream-{uuid.uuid4()}"
                call_sid = start.get("call_sid")
                media_format = start.get("media_format", {})
                try:
                    carrier_sample_rate = int(media_format.get("sample_rate", 8000))
                except (TypeError, ValueError):
                    carrier_sample_rate = 8000
                if carrier_sample_rate not in (8000, 16000, 24000):
                    carrier_sample_rate = 8000

                inbound_resampler = LinearResampler(
                    input_rate=carrier_sample_rate,
                    output_rate=16000,
                )
                outbound_resampler = LinearResampler(
                    input_rate=24000,
                    output_rate=carrier_sample_rate,
                )
                outbound_buffer.clear()

                logger.info(
                    "Started Exotel stream stream_sid=%s call_sid=%s declared_sample_rate=%s sample_rate=%s encoding=%s channels=%s",
                    stream_sid,
                    call_sid,
                    media_format.get("sample_rate"),
                    carrier_sample_rate,
                    media_format.get("encoding"),
                    media_format.get("channels"),
                )

                runner = getattr(websocket.app.state, "runner", None)
                app_name = getattr(websocket.app.state, "agent_app_name", None)
                if runner is not None and app_name:
                    call = LiveVoiceCall(
                        runner,
                        app_name=app_name,
                        business_hint='Say exactly: "Hi, G T M Leads."',
                    )
                    await call.start()
                    logger.info("Created Gemini Live session=%s", call.session_id)
                    outbound_task = asyncio.create_task(consume_adk_events())

            elif event == "media" and stream_sid:
                payload = message.get("media", {}).get("payload")
                if not isinstance(payload, str):
                    continue

                try:
                    carrier_pcm = base64.b64decode(payload, validate=True)
                except (binascii.Error, ValueError):
                    stats["invalid_frames"] += 1
                    logger.warning("Ignoring invalid Base64 Exotel media stream_sid=%s", stream_sid)
                    continue

                if len(carrier_pcm) % 2:
                    stats["invalid_frames"] += 1
                    logger.warning("Ignoring odd-length PCM payload stream_sid=%s", stream_sid)
                    continue

                stats["in_frames"] += 1
                stats["in_bytes"] += len(carrier_pcm)
                if len(input_sizes) < 20:
                    input_sizes.add(len(carrier_pcm))
                if stats["in_frames"] == 1:
                    logger.info("First carrier input stream_sid=%s elapsed_ms=%.0f frame_bytes=%s", stream_sid, (time.monotonic() - started_at) * 1000, len(carrier_pcm))

                pcm16khz = inbound_resampler.resample(carrier_pcm)

                if call is not None:
                    call.send_pcm(pcm16khz)
                else:
                    await websocket.send_json(message)

            elif event == "stop":
                end_reason = "stop"
                break

    except WebSocketDisconnect:
        end_reason = "disconnect"
        logger.info("Exotel stream %s disconnected", stream_sid)
    except asyncio.CancelledError:
        end_reason = "cancelled"
        raise
    except Exception:
        end_reason = "error"
        logger.exception("Exotel stream %s failed", stream_sid)
    finally:
        if outbound_task is not None:
            outbound_task.cancel()
            try:
                await outbound_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Gemini event stream failed stream_sid=%s", stream_sid)
        if call is not None:
            await call.close()
        logger.info("Audio summary stream_sid=%s reason=%s duration_s=%.2f rate=%s input_frames=%s input_bytes=%s input_frame_sizes=%s invalid_frames=%s adk_events=%s gemini_pcm_events=%s gemini_mimes=%s output_frames=%s output_bytes=%s buffered_bytes=%s", stream_sid, end_reason, time.monotonic() - started_at, carrier_sample_rate, stats["in_frames"], stats["in_bytes"], sorted(input_sizes), stats["invalid_frames"], stats["adk_events"], stats["pcm_events"], sorted(output_mimes), stats["out_frames"], stats["out_bytes"], len(outbound_buffer))
