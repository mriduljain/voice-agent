from types import SimpleNamespace

import pytest

from app.exotel_verification import DirectSourceVerifier


@pytest.mark.asyncio
async def test_direct_source_uses_peer_not_forwarded_header():
    verifier = DirectSourceVerifier(["192.0.2.0/24", "2001:db8::/32"])
    assert await verifier(SimpleNamespace(scope={"client": ("192.0.2.10", 123)}, headers={}))
    assert await verifier(SimpleNamespace(scope={"client": ("2001:db8::1", 123)}, headers={}))
    assert not await verifier(SimpleNamespace(scope={"client": ("198.51.100.1", 123)}, headers={"cf-connecting-ip": "192.0.2.10"}))
    assert not await verifier(SimpleNamespace(scope={"client": None}, headers={}))
    assert not await verifier(SimpleNamespace(scope={"client": ("invalid", 123)}, headers={}))


@pytest.mark.parametrize("networks", [[], ["0.0.0.0/0"], ["::/0"], ["not-an-ip"]])
def test_invalid_source_configuration_fails_closed(networks):
    with pytest.raises(ValueError):
        DirectSourceVerifier(networks)
