"""The local-only guard (stimengine/control/origin.py): other sites' pages cannot drive the servers."""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from stimengine.control.origin import origin_allowed, origin_guard


def test_origin_rules():
    assert origin_allowed(None, 9999)                                  # curl, tools, server-side calls
    for ok in ("http://127.0.0.1:8320", "http://localhost:8322", "http://127.0.0.1:8321", "http://[::1]:8322"):
        assert origin_allowed(ok, None), ok
    assert origin_allowed("http://127.0.0.1:45678", 45678)             # the server's own (test) port
    for bad in ("https://evil.example", "http://127.0.0.1:9000", "null", "http://192.168.1.5:8322",
                "file://", "http://evil.example:8322"):
        assert not origin_allowed(bad, 45678), bad


def test_guard_on_a_server():
    async def go():
        app = web.Application(middlewares=[origin_guard()])
        app.router.add_post("/cmd", lambda r: web.json_response({"ok": True}))
        async with TestClient(TestServer(app, host="127.0.0.1")) as c:
            assert (await c.post("/cmd", data="{}")).status == 200                                 # no Origin
            assert (await c.post("/cmd", data="{}", headers={"Origin": "http://127.0.0.1:8320"})).status == 200
            r = await c.post("/cmd", data='{"cmd":"arm"}', headers={"Origin": "https://evil.example",
                                                                     "Content-Type": "text/plain"})
            assert r.status == 403
            assert (await c.post("/cmd", data="{}", headers={"Host": "rebound.example:8322"})).status == 403
    asyncio.run(go())
