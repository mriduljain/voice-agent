import asyncio
from contextlib import aclosing

from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agent import app as agent_app


async def generate_greeting() -> bytes:
    audio = bytearray()

    async with InMemoryRunner(app=agent_app) as runner:
        session = await runner.session_service.create_session(
            app_name=agent_app.name,
            user_id="local-greeting-test",
        )

        queue = LiveRequestQueue()
        queue.send_content(
            types.Content(
                role="user",
                parts=[types.Part(text='Say exactly: "Hi, G T M Leads."')],
            )
        )

        events = runner.run_live(
            user_id=session.user_id,
            session_id=session.id,
            live_request_queue=queue,
            run_config=RunConfig(
                streaming_mode=StreamingMode.BIDI,
                response_modalities=["AUDIO"],
            ),
        )

        try:
            async with aclosing(events):
                async with asyncio.timeout(30):
                    async for event in events:
                        error_code = getattr(event, "error_code", None)
                        error_message = getattr(event, "error_message", None)
                        if error_code or error_message:
                            raise RuntimeError(
                                f"ADK Live error: {error_code} {error_message}"
                            )

                        if event.content:
                            for part in event.content.parts or []:
                                blob = part.inline_data
                                if blob and blob.data and blob.mime_type in (
                                    "audio/pcm",
                                    "audio/pcm;rate=24000",
                                ):
                                    audio.extend(blob.data)

                        status = getattr(event, "interaction_status", None)
                        if event.turn_complete and status in (None, "IDLE"):
                            break
        finally:
            queue.close()

    if not audio:
        raise RuntimeError("No PCM audio was collected from Gemini Live.")

    return bytes(audio)



