"""SSRF guard and the proxy that carries all headless-browser traffic."""
import asyncio

import pytest

from app.netguard import GuardProxy, UnsafeURL, assert_public_url, resolve_public


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("host,port", [("127.0.0.1", 80), ("localhost", 443), ("10.0.0.5", 443), ("192.168.1.10", 443),
                                       ("169.254.169.254", 80), ("[::1]", 443), ("example.com", 8080), ("intranet", 443),
                                       ("db.internal", 443), ("", 443)])
def test_resolve_public_refuses_internal_destinations(host, port):
    with pytest.raises(UnsafeURL):
        run(resolve_public(host, port))


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "http://user:pw@example.com/", "https://127.0.0.1/admin",
                                 "http://localhost:8000/", "https://example.com:8443/", "javascript:alert(1)", "http://[::1]/"])
def test_assert_public_url_refuses_unsafe_urls(url):
    with pytest.raises(UnsafeURL):
        run(assert_public_url(url))


async def _request(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(4096), 5)
    writer.close()
    return data


def test_guard_proxy_refuses_internal_targets_with_the_real_resolver():
    async def go():
        proxy = GuardProxy()
        port = await proxy.start()
        try:
            for payload in (b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n",
                            b"CONNECT localhost:8000 HTTP/1.1\r\n\r\n",
                            b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n",
                            b"GET http://10.0.0.1/admin HTTP/1.1\r\nHost: 10.0.0.1\r\n\r\n",
                            b"GET ftp://example.com/x HTTP/1.1\r\n\r\n"):
                assert (await _request(port, payload)).startswith(b"HTTP/1.1 403")
            assert proxy.blocked == 5
        finally:
            await proxy.close()
    run(go())


def test_guard_proxy_relays_allowed_traffic_to_the_checked_address():
    async def go():
        seen = []

        async def upstream(reader, writer):
            seen.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        up_port = server.sockets[0].getsockname()[1]

        async def resolver(host, port):          # stands in for DNS: only "allowed.test" resolves
            if host == "allowed.test":
                return "127.0.0.1"
            raise UnsafeURL("blocked")

        proxy = GuardProxy(resolver)
        port = await proxy.start()
        try:
            plain = await _request(port, f"GET http://allowed.test:{up_port}/path?x=1 HTTP/1.1\r\nHost: allowed.test\r\n"
                                         f"Proxy-Connection: keep-alive\r\n\r\n".encode())
            assert plain.endswith(b"ok") and seen[0].startswith(b"GET /path?x=1 HTTP/1.1\r\n")
            assert b"Proxy-Connection" not in seen[0] and b"Connection: close" in seen[0]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"CONNECT allowed.test:{up_port} HTTP/1.1\r\n\r\n".encode())
            assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).startswith(b"HTTP/1.1 200")
            writer.write(b"GET /tunnelled HTTP/1.1\r\n\r\n")          # bytes inside the tunnel are passed through untouched
            assert (await asyncio.wait_for(reader.read(4096), 5)).endswith(b"ok") and seen[1].startswith(b"GET /tunnelled")
            writer.close()
            assert (await _request(port, f"CONNECT other.test:{up_port} HTTP/1.1\r\n\r\n".encode())).startswith(b"HTTP/1.1 403")
        finally:
            await proxy.close()
            server.close()
    run(go())
