from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit


class EgressDenied(Exception):
    pass


def _public_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not address.is_global:
        raise EgressDenied("The destination resolves to a non-public IP address.")
    return address


async def resolve_public_addresses(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    normalized = host.rstrip(".").lower()
    if not normalized or normalized in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        raise EgressDenied("Local and metadata hostnames are not allowed.")
    if normalized.endswith((".localhost", ".local", ".internal", ".test", ".home.arpa", ".lan", ".corp", ".invalid")):
        raise EgressDenied("Local and internal hostnames are not allowed.")

    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(
                normalized, port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise EgressDenied("The destination hostname could not be resolved.") from exc
        addresses = {_public_ip(item[4][0]) for item in answers}
    else:
        addresses = {_public_ip(str(literal))}

    if not addresses:
        raise EgressDenied("The destination has no usable public address.")
    return sorted(addresses, key=lambda item: (item.version, int(item)))


async def _connect_pinned(host: str, port: int):
    # Resolve once, reject mixed public/private answers, and connect to one of
    # those exact IP literals. asyncio therefore cannot perform a second DNS lookup.
    addresses = await resolve_public_addresses(host, port)
    last_error: OSError | None = None
    for address in addresses:
        try:
            return await asyncio.open_connection(str(address), port)
        except OSError as exc:
            last_error = exc
    raise EgressDenied("Could not connect to a validated public destination.") from last_error


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.write_eof()
        except (AttributeError, OSError, RuntimeError):
            pass


async def _relay(client_reader, client_writer, upstream_reader, upstream_writer) -> None:
    tasks = [
        asyncio.create_task(_pipe(client_reader, upstream_writer)),
        asyncio.create_task(_pipe(upstream_reader, client_writer)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        upstream_writer.close()
        client_writer.close()
        await asyncio.gather(upstream_writer.wait_closed(), client_writer.wait_closed(), return_exceptions=True)


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    upstream_writer = None
    try:
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        if len(header) > 32768:
            raise EgressDenied("Proxy request headers are too large.")
        lines = header.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ", 2)

        if method.upper() == "CONNECT":
            authority = target
            if authority.startswith("["):
                host, _, port_text = authority[1:].partition("]:")
            else:
                host, separator, port_text = authority.rpartition(":")
                if not separator:
                    raise EgressDenied("CONNECT requires an explicit port.")
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise EgressDenied("Invalid destination port.")
            upstream_reader, upstream_writer = await _connect_pinned(host, port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await _relay(reader, writer, upstream_reader, upstream_writer)
            return

        parsed = urlsplit(target)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise EgressDenied("Only absolute public HTTP(S) proxy requests are allowed.")
        if parsed.scheme != "http":
            raise EgressDenied("HTTPS requests must use CONNECT tunnelling.")
        port = parsed.port or 80
        if not 1 <= port <= 65535:
            raise EgressDenied("Invalid destination port.")
        upstream_reader, upstream_writer = await _connect_pinned(parsed.hostname, port)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        forwarded = [f"{method} {path} {version}"]
        forwarded.extend(line for line in lines[1:] if line and not line.lower().startswith("proxy-"))
        upstream_writer.write(("\r\n".join(forwarded) + "\r\n\r\n").encode("latin-1"))
        await upstream_writer.drain()
        await _relay(reader, writer, upstream_reader, upstream_writer)
    except (EgressDenied, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
        try:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError):
        writer.close()
        await writer.wait_closed()
    finally:
        if upstream_writer is not None and not upstream_writer.is_closing():
            upstream_writer.close()
            await upstream_writer.wait_closed()


@asynccontextmanager
async def public_egress_proxy():
    """Local HTTP proxy that pins each outbound connection to a vetted public IP."""
    server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    try:
        yield f"http://127.0.0.1:{address[1]}"
    finally:
        server.close()
        await server.wait_closed()
