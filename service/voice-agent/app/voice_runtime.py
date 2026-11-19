"""ADK Live runtime shared by carrier adapters.

This module owns one call's Gemini Live session and audio queue. Carrier-specific
transport code should translate carrier messages into PCM blobs and PCM blobs
back into carrier media events.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress

from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.genai import types

logger = logging.getLogger(__name__)

ADK_INPUT_PCM_MIME = "audio/pcm;rate=16000"
ADK_OUTPUT_PCM_MIME_PREFIX = "audio/pcm"


def pcm_mime_type(mime_type: str | None) -> str:
    """Accept an ADK PCM MIME type with or without a sample-rate parameter."""
    if mime_type and mime_type.startswith(ADK_OUTPUT_PCM_MIME_PREFIX):
        return mime_type
    return ""


async def _maybe_await(value):
    """Await an async result or return an immediate sync result."""
    if inspect.isawaitable(value):
        return await value
    return value


class LiveVoiceCall:
    """One isolated ADK Live conversation for one telephone or browser call."""

    def __init__(
        self,
        runner: Runner,
        *,
        app_name: str,
        business_hint: str = "",
    ) -> None:
        self.runner = runner
        self.app_name = app_name
        self.business_hint = business_hint
        self.user_id = f"voice-user-{uuid.uuid4()}"
        self.session_id: str | None = None
        self.queue = LiveRequestQueue()

    async def start(self) -> None:
        session = await _maybe_await(
            self.runner.session_service.create_session(
                app_name=self.app_name,
                user_id=self.user_id,
            )
        )
        self.session_id = session.id

        if self.business_hint:
            self.queue.send_content(
                types.Content(
                    role="user",
                    parts=[types.Part(text=self.business_hint)],
                )
            )

    def send_pcm(self, pcm_bytes: bytes) -> None:
        """Send an input PCM chunk to the open Gemini Live session."""
        if pcm_bytes:
            self.queue.send_realtime(
                types.Blob(data=pcm_bytes, mime_type=ADK_INPUT_PCM_MIME)
            )

    def events(self) -> AsyncIterator[object]:
        """Return one call's live event async generator."""
        if not self.session_id:
            raise RuntimeError("LiveVoiceCall.start() must be called before events()")

        return self.runner.run_live(
            user_id=self.user_id,
            session_id=self.session_id,
            live_request_queue=self.queue,
            run_config=RunConfig(
                streaming_mode=StreamingMode.BIDI,
                response_modalities=["AUDIO"],
                output_audio_transcription=types.AudioTranscriptionConfig(),
                input_audio_transcription=types.AudioTranscriptionConfig(),
            ),
        )

    async def close(self) -> None:
        """Close queue and delete this call's ADK session when supported."""
        self.queue.close()

        if self.session_id:
            with suppress(Exception):
                await _maybe_await(
                    self.runner.session_service.delete_session(
                        app_name=self.app_name,
                        user_id=self.user_id,
                        session_id=self.session_id,
                    )
                )
        logger.info("Closed voice call session %s", self.session_id)
