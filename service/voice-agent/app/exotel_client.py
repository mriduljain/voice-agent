"""Prepare and send an Exotel Voice AI call request.

This client is not connected to an API route or worker yet.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

import aiohttp


class ExotelCallError(Exception):
    """The outcome of the request is unknown; do not automatically retry."""


@dataclass(frozen=True)
class ExotelConfig:
    account_sid: str
    api_key: str = field(repr=False)
    api_token: str = field(repr=False)
    caller_id: str
    stream_url: str
    status_callback: str
    api_base_url: str = "https://api.in.exotel.com"

    def __post_init__(self) -> None:
        if not all((self.account_sid, self.api_key, self.api_token, self.caller_id)):
            raise ValueError("Exotel account, credentials and caller ID are required")
        for value, scheme in ((self.api_base_url, "https"), (self.stream_url, "wss"), (self.status_callback, "https")):
            parsed = urlsplit(value)
            if parsed.scheme != scheme or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError(f"Expected a valid {scheme} URL without credentials or fragment")
        if len(self.stream_url) > 600:
            raise ValueError("Exotel stream URL exceeds 600 characters")


class ExotelClient:
    def __init__(self, config: ExotelConfig, session: aiohttp.ClientSession) -> None:
        self.config = config
        self.session = session

    async def request_call(self, *, phone_number: str, call_id: str) -> str:
        """Return Exotel's SID; failures must be reconciled before retrying."""
        if not phone_number.startswith("+") or not phone_number[1:].isdigit() or not 7 <= len(phone_number[1:]) <= 15:
            raise ValueError("Phone number must be in E.164 format")
        if not call_id or len(call_id) > 128:
            raise ValueError("Call ID must contain between 1 and 128 characters")

        stream = urlsplit(self.config.stream_url)
        query = parse_qsl(stream.query, keep_blank_values=True)
        if any(key == "call_id" for key, _ in query):
            raise ValueError("Stream URL already contains a call_id")
        stream_url = urlunsplit((stream.scheme, stream.netloc, stream.path,
                                 urlencode([*query, ("call_id", call_id)]), ""))
        if len(stream_url) > 600:
            raise ValueError("Exotel stream URL exceeds 600 characters")

        form = aiohttp.FormData(default_to_multipart=True)
        for key, value in (
            ("From", phone_number),
            ("CallerId", self.config.caller_id),
            ("StreamUrl", stream_url),
            ("StreamType", "bidirectional"),
            ("CustomField", call_id),
            ("StatusCallback", self.config.status_callback),
            ("StatusCallbackEvents[]", "answered"),
            ("StatusCallbackEvents[]", "terminal"),
        ):
            form.add_field(key, value)

        url = f"{self.config.api_base_url.rstrip('/')}/v1/Accounts/{quote(self.config.account_sid, safe='')}/Calls/connect"
        try:
            async with self.session.post(
                url, data=form,
                auth=aiohttp.BasicAuth(self.config.api_key, self.config.api_token),
                timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False,
            ) as response:
                if response.status < 200 or response.status >= 300:
                    raise ExotelCallError(f"Exotel request returned HTTP {response.status}; reconcile before retrying")
                try:
                    try:
                        body = await response.content.readexactly(65537)
                    except asyncio.IncompleteReadError as exc:
                        body = exc.partial
                    if len(body) > 65536:
                        raise ValueError("Response too large")
                    text = body.decode("utf-8")
                    if response.content_type in ("application/xml", "text/xml"):
                        if "<!DOCTYPE" in text.upper():
                            raise ValueError("XML declarations are not supported")
                        root = ElementTree.fromstring(text)
                        sid = root.findtext("./Call/Sid") if root.tag == "TwilioResponse" else None
                    else:
                        sid = json.loads(text)["Call"]["Sid"]
                except (ValueError, TypeError, KeyError, ElementTree.ParseError, UnicodeError):
                    raise ExotelCallError("Invalid Exotel response; reconcile before retrying") from None
                if not isinstance(sid, str) or not sid.strip() or len(sid) > 100:
                    raise ExotelCallError("Invalid Exotel call SID; reconcile before retrying")
                return sid
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ExotelCallError("Exotel request failed or timed out; reconcile before retrying") from exc
