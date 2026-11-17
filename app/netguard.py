"""Outbound network safety (SSRF guard). Only public http(s) destinations on ports 80/443 are allowed.
Used for page fetches and, through GuardProxy, for every connection the headless browser opens."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_PORTS = (80, 443)


class UnsafeURL(ValueError):
    pass


_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _ip_allowed(ip: str) -> bool:
    """Only globally routable unicast addresses. `is_global` (not just "not private") also excludes the shared
    address space 100.64.0.0/10: carrier-grade NAT, VPN meshes and some cloud metadata services live there."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if addr.version == 6:
        embedded = addr.ipv4_mapped or addr.sixtofour or (addr.teredo[1] if addr.teredo else None)
        if embedded is not None:                             # an IPv4 address in IPv6 clothing: judge the IPv4 address
            addr = embedded
        elif any(addr in network for network in _NAT64):
            return False
    return addr.is_global and not (addr.is_multicast or addr.is_reserved or addr.is_loopback or addr.is_link_local
                                   or addr.is_unspecified)


async def resolve_public(host: str, port: int) -> str:
    """Resolve a host and return one address, but only if every address it resolves to is public."""
    host = (host or "").strip().strip("[]").lower()
    if port not in ALLOWED_PORTS:
        raise UnsafeURL("Non-standard port")
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeURL("Internal hostname")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if not literal and "." not in host:
        raise UnsafeURL("Internal hostname")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURL("Hostname does not resolve") from exc
    addresses = [info[4][0] for info in infos]
    if not addresses or not all(_ip_allowed(a) for a in addresses):
        raise UnsafeURL("Hostname resolves to a non-public address")
    return addresses[0]


async def assert_public_url(url: str) -> str:
    """Reject non-http(s) URLs, odd ports, credentials, and hosts that resolve to non-public addresses.
    Returns the address that was checked. A caller that goes on to fetch the URL must connect to that address
    (see `pinned`): resolving the name a second time would let it answer differently (DNS rebinding)."""
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError as exc:
        raise UnsafeURL("Malformed URL") from exc
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURL("Only http(s) URLs are allowed")
    if p.username or p.password:
        raise UnsafeURL("Credentials in URL are not allowed")
    return await resolve_public(p.hostname, port or (443 if p.scheme == "https" else 80))


def pinned(url: str, address: str) -> tuple[str, dict, dict]:
    """How to request `url` from exactly `address`: (url with the address as host, extra headers, httpx extensions).
    The Host header and the TLS server name stay the original host, so virtual hosting and certificate checks
    work as usual. The connection is not reused, because it was opened for this one host name."""
    p = urlsplit(url)
    default = 443 if p.scheme == "https" else 80
    host = p.hostname or ""
    literal = f"[{address}]" if ":" in address else address
    target = p._replace(netloc=literal if (p.port or default) == default else f"{literal}:{p.port}").geturl()
    host_header = (f"[{host}]" if ":" in host else host) + ("" if (p.port or default) == default else f":{p.port}")
    return target, {"Host": host_header, "Connection": "close"}, ({"sni_hostname": host} if p.scheme == "https" else {})


class GuardProxy:
    """Tiny local forward proxy for the headless browser.

    The browser is started with this as its only route to the network (loopback included), so every
    connection it opens - main page, redirects, iframes, scripts, websockets - is checked here and
    made to the address that was checked. A page therefore cannot steer the browser to localhost or
    to an internal network, not even through a redirect that page-level request filters never see."""

    def __init__(self, resolver=resolve_public):
        self.resolver = resolver
        self.port = 0
        self.blocked = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2)
            except (asyncio.TimeoutError, Exception):
                pass
            self._server = None

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: str) -> None:
        try:
            writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
        finally:
            writer.close()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)).decode("latin-1")
            request_line, *headers = head.split("\r\n")
            method, target, version = request_line.split(" ", 2)
        except Exception:
            writer.close()
            return
        try:
            if method.upper() == "CONNECT":
                host, _, port_text = target.rpartition(":")
                port, forward = int(port_text), b""
            else:                                        # plain http request in absolute form
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    raise UnsafeURL("Unsupported proxy request")
                host, port = parts.hostname, parts.port or 80
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                kept = [h for h in headers if h and not h.lower().startswith(("proxy-", "connection:"))]
                forward = ("\r\n".join([f"{method} {path} {version}", *kept, "Connection: close"]) + "\r\n\r\n").encode("latin-1")
            address = await self.resolver(host, port)
        except Exception:
            self.blocked += 1
            await self._reply(writer, "403 Forbidden")
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(address, port), 15)
        except Exception:
            await self._reply(writer, "502 Bad Gateway")
            return
        if forward:
            up_writer.write(forward)
        else:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
