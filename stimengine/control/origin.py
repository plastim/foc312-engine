"""Local-only guard for the HTTP / WebSocket servers (hub :8320, control API :8321, foc312 :8322).

A browser lets any web page send a "simple" cross-origin POST (text/plain body, no preflight) to 127.0.0.1, and
aiohttp's request.json() does not check the content type, so without this check a page on another site open in the
same browser could arm the output, set levels or start a firmware flash. Browsers always send Origin on such
requests (and on WebSocket handshakes), so:
  - a request with an Origin is allowed only from our own local pages (127.0.0.1 / localhost on the hub, control or
    foc312 port, or the server's own port);
  - a request without an Origin (curl, tools, the hub's server-side calls) is allowed;
  - the Host must be local too, so a DNS name re-pointed at 127.0.0.1 (DNS rebinding) cannot pass as same-origin.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from aiohttp import web

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
APP_PORTS = frozenset({8320, 8321, 8322})     # hub, control API, foc312


def _hostname(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):                     # [::1]:8322
        return h[: h.find("]") + 1]
    return h.rsplit(":", 1)[0] if ":" in h else h


def origin_allowed(origin: str | None, own_port: int | None) -> bool:
    if origin is None:
        return True
    try:
        u = urlsplit(origin)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or (u.hostname or "") not in LOCAL_HOSTS:
        return False                           # includes Origin: null (sandboxed pages, file://)
    port = u.port or (443 if u.scheme == "https" else 80)
    return port in APP_PORTS or port == own_port


def origin_guard() -> web.middleware:
    @web.middleware
    async def guard(request: web.Request, handler):
        if _hostname(request.host or "") not in LOCAL_HOSTS:
            return web.json_response({"ok": False, "error": "local requests only"}, status=403)
        sock = request.transport.get_extra_info("sockname") if request.transport else None
        own_port = sock[1] if isinstance(sock, tuple) and len(sock) >= 2 else None
        if not origin_allowed(request.headers.get("Origin"), own_port):
            return web.json_response({"ok": False, "error": "requests from other sites are refused"}, status=403)
        return await handler(request)

    return guard
