"""Source checks for a direct, non-proxied Exotel connection.

For Cloudflare Tunnel the network peer is cloudflared, not Exotel; do not use
this verifier there. Enforce provider source ranges at a trusted ingress.
"""

from ipaddress import ip_address, ip_network


class DirectSourceVerifier:
    """Allow configured source networks using only the socket peer address.

    The server must be configured not to rewrite ASGI client addresses from
    forwarded headers, and no reverse proxy may sit between Exotel and it.
    Network allowlisting is not cryptographic authentication.
    """

    def __init__(self, networks: list[str]) -> None:
        if not networks:
            raise ValueError("At least one source network is required")
        self.networks = tuple(ip_network(value, strict=True) for value in networks)
        if any(network.prefixlen == 0 for network in self.networks):
            raise ValueError("An unrestricted source network is not allowed")

    async def __call__(self, connection) -> bool:
        peer = connection.scope.get("client")
        if not peer or not isinstance(peer[0], str):
            return False
        try:
            address = ip_address(peer[0])
        except ValueError:
            return False
        return any(address in network for network in self.networks)
