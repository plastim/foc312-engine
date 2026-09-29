"""py -3.13 -m stimengine.app [--port 8320] [--no-browser]"""
from __future__ import annotations

import argparse
import logging
import webbrowser

from aiohttp import web

from .server import DEFAULT_PORT, Hub


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the PlaStim app hub: boxes, the M5 remote, the player")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="do not open the page")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    hub = Hub()
    url = f"http://127.0.0.1:{args.port}/"

    async def opened(_app: web.Application) -> None:
        print(f"hub on {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)

    hub.app.on_startup.append(opened)
    web.run_app(hub.app, host="127.0.0.1", port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
