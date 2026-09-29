"""py -3.13 -m stimengine.app [--port 8320] [--no-browser]"""
from __future__ import annotations

import argparse
import logging
import socket
import webbrowser

from aiohttp import web

from .server import DEFAULT_PORT, Hub


def _port_taken(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the PlaStim app hub: boxes, the M5 remote, the player")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="do not open the page")
    args = ap.parse_args(argv)
    url = f"http://127.0.0.1:{args.port}/"
    if _port_taken(args.port):          # most likely the hub is already running: open that one, don't crash
        print(f"The hub is already running (port {args.port} is taken): opening {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    hub = Hub()

    async def opened(_app: web.Application) -> None:
        print(f"The PlaStim hub is running at {url}\nLeave this window open; close it (or press Ctrl+C) to quit.",
              flush=True)
        if not args.no_browser:
            webbrowser.open(url)

    hub.app.on_startup.append(opened)
    web.run_app(hub.app, host="127.0.0.1", port=args.port, print=None, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
