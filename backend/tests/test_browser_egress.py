import asyncio

from playwright.async_api import async_playwright

from app.egress import public_egress_proxy


def test_chromium_cannot_bypass_proxy_for_loopback_targets():
    async def scenario():
        reached = False

        async def local_target(reader, writer):
            nonlocal reached
            reached = True
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(local_target, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        try:
            async with public_egress_proxy() as proxy:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(
                        headless=True,
                        proxy={"server": proxy, "bypass": "<-loopback>"},
                        args=["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
                    )
                    page = await browser.new_page()
                    response = None
                    try:
                        response = await page.goto(f"http://127.0.0.1:{target_port}/", timeout=5000)
                    except Exception:
                        pass
                    await browser.close()
            assert response is not None and response.status == 403, "Chromium did not receive the proxy's explicit private-target denial"
            assert reached is False, "Chromium reached a loopback target instead of using the egress proxy"
        finally:
            target.close()
            await target.wait_closed()

    asyncio.run(scenario())
