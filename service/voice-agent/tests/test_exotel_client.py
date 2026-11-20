"""Test the outbound request without connecting to Exotel."""

import asyncio
import json

import pytest

from app.exotel_client import ExotelCallError, ExotelClient, ExotelConfig


class FakeResponse:
    def __init__(self, status=200, payload=None, content_type="application/json", raw=None):
        self.status = status
        self.content_type = content_type
        body = raw if raw is not None else json.dumps(payload).encode()
        self.content = asyncio.StreamReader()
        self.content.feed_data(body)
        self.content.feed_eof()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

class FakeSession:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def config():
    return ExotelConfig(account_sid="account", api_key="key", api_token="token", caller_id="caller",
                        stream_url="wss://example.test/telephony/exotel/media",
                        status_callback="https://example.test/telephony/exotel/status")


@pytest.mark.asyncio
async def test_request_contains_call_details_and_returns_sid(config):
    session = FakeSession(FakeResponse(payload={"Call": {"Sid": "exotel-123"}}))
    sid = await ExotelClient(config, session).request_call(phone_number="+919876543210", call_id="our-call")
    assert sid == "exotel-123"
    url, request = session.requests[0]
    assert url == "https://api.in.exotel.com/v1/Accounts/account/Calls/connect"
    fields = [(entry[0]["name"], entry[2]) for entry in request["data"]._fields]
    assert fields == [("From", "+919876543210"), ("CallerId", "caller"),
                      ("StreamUrl", config.stream_url + "?call_id=our-call"), ("StreamType", "bidirectional"),
                      ("CustomField", "our-call"), ("StatusCallback", config.status_callback),
                      ("StatusCallbackEvents[]", "answered"), ("StatusCallbackEvents[]", "terminal")]
    assert request["auth"].login == "key"
    assert request["auth"].password == "token"
    assert request["allow_redirects"] is False


@pytest.mark.asyncio
async def test_xml_connect_response(config):
    response = FakeResponse(content_type="application/xml", raw=b"<TwilioResponse><Call><Sid>xml-123</Sid></Call></TwilioResponse>")
    sid = await ExotelClient(config, FakeSession(response)).request_call(phone_number="+919876543210", call_id="our-call")
    assert sid == "xml-123"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"<TwilioResponse><Call>", b"<TwilioResponse><Call/></TwilioResponse>", b'<!DOCTYPE x [<!ENTITY y "bad">]><TwilioResponse><Call><Sid>&y;</Sid></Call></TwilioResponse>'])
async def test_invalid_xml_outcome_is_uncertain(config, body):
    session = FakeSession(FakeResponse(content_type="application/xml", raw=body))
    with pytest.raises(ExotelCallError, match="reconcile before retrying"):
        await ExotelClient(config, session).request_call(phone_number="+919876543210", call_id="our-call")
    assert len(session.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [FakeResponse(status=503), FakeResponse(payload={}), asyncio.TimeoutError()])
async def test_failed_or_uncertain_request_is_not_retried(config, response):
    session = FakeSession(response)
    with pytest.raises(ExotelCallError, match="reconcile before retrying"):
        await ExotelClient(config, session).request_call(phone_number="+919876543210", call_id="our-call")
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_invalid_number_does_not_send_request(config):
    session = FakeSession(FakeResponse())
    with pytest.raises(ValueError):
        await ExotelClient(config, session).request_call(phone_number="not-a-number", call_id="our-call")
    assert session.requests == []
