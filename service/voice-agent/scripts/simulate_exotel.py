#!/usr/bin/env python3
"""Send a mono PCM WAV to the local Exotel bridge and save its response."""

import argparse
import asyncio
import base64
import json
import wave
from pathlib import Path
from urllib.parse import urlparse

import aiohttp


async def simulate(args: argparse.Namespace) -> None:
    with wave.open(str(args.input), "rb") as source:
        rate = source.getframerate()
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or rate not in (8000, 16000, 24000):
            raise ValueError("Input must be mono 16-bit PCM WAV at 8000, 16000, or 24000 Hz")
        pcm = source.readframes(source.getnframes())

    received = bytearray()
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(args.url) as ws:
            await ws.send_json({"event": "start", "start": {
                "stream_sid": "local-simulation", "call_sid": "local-simulation",
                "media_format": {"sample_rate": rate},
            }})

            async def receive() -> None:
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        event = json.loads(message.data)
                        if event.get("event") == "media":
                            received.extend(base64.b64decode(event["media"]["payload"], validate=True))
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise RuntimeError(f"WebSocket error: {ws.exception()}")

            receiver = asyncio.create_task(receive())
            try:
                frame_size = rate * 2 // 50
                for offset in range(0, len(pcm), frame_size):
                    if receiver.done():
                        await receiver
                        raise RuntimeError("Server closed the WebSocket before input finished")
                    await ws.send_json({"event": "media", "stream_sid": "local-simulation", "media": {
                        "payload": base64.b64encode(pcm[offset:offset + frame_size]).decode("ascii")
                    }})
                    await asyncio.sleep(0.02)
                await asyncio.wait_for(asyncio.shield(receiver), timeout=args.wait)
            except asyncio.TimeoutError:
                pass
            finally:
                if not ws.closed:
                    await ws.send_json({"event": "stop", "stream_sid": "local-simulation"})
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)

    if not received:
        print("No audio received. Check the server logs and Gemini configuration.")
        return
    with wave.open(str(args.output), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(received)
    print(f"Saved {len(received) / (rate * 2):.2f} seconds of audio to {args.output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Mono PCM16 WAV containing caller audio")
    parser.add_argument("--output", type=Path, default=Path("exotel-response.wav"))
    parser.add_argument("--url", default="ws://127.0.0.1:8000/telephony/exotel/media")
    parser.add_argument("--wait", type=float, default=10, help="Seconds to wait after input ends")
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme != "ws" or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        parser.error("URL must be an unencrypted localhost WebSocket")
    if args.wait <= 0:
        parser.error("--wait must be positive")
    asyncio.run(simulate(args))


if __name__ == "__main__":
    main()
