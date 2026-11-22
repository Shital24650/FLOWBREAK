import asyncio

import pytest

from app import egress


def test_non_public_and_special_use_ip_ranges_are_rejected():
    for address in (
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "100.64.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
    ):
        with pytest.raises(egress.EgressDenied):
            egress._public_ip(address)


def test_mixed_dns_answers_are_rejected(monkeypatch):
    async def fake_getaddrinfo(self, host, port, type):
        return [
            (2, 1, 6, "", ("93.184.216.34", port)),
            (2, 1, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)

    async def scenario():
        with pytest.raises(egress.EgressDenied):
            await egress.resolve_public_addresses("rebind.example", 443)

    asyncio.run(scenario())


def test_connection_uses_the_resolved_ip_literal(monkeypatch):
    connected = {}

    async def fake_resolve(host, port):
        return [egress.ipaddress.ip_address("93.184.216.34")]

    async def fake_open_connection(host, port):
        connected["host"] = host
        connected["port"] = port
        return object(), object()

    monkeypatch.setattr(egress, "resolve_public_addresses", fake_resolve)
    monkeypatch.setattr(egress.asyncio, "open_connection", fake_open_connection)

    async def scenario():
        await egress._connect_pinned("rebind.example", 443)

    asyncio.run(scenario())
    assert connected == {"host": "93.184.216.34", "port": 443}


def test_proxy_rejects_private_connect_targets():
    async def scenario():
        async with egress.public_egress_proxy() as proxy:
            port = int(proxy.rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT 169.254.169.254:80 HTTP/1.1\r\nHost: 169.254.169.254\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            writer.close()
            await writer.wait_closed()
            assert response.startswith(b"HTTP/1.1 403")

    asyncio.run(scenario())
