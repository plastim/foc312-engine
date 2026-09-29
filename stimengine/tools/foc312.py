"""The player on its own, PREVIEW ONLY (no device link, nothing can reach a box):

    python -m stimengine.tools.foc312            # http://127.0.0.1:8322/

To drive a box, run the engine daemon instead; it serves the player on the same port alongside the control API:

    python -m stimengine.tools.serve --serial COM13 --mode fourphase     # stock firmware
    python -m stimengine.tools.serve --sim-fork --mode fourphase         # simulated fork-firmware box
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tomllib
from pathlib import Path

from ..control.foc312_api import Foc312API

ROOT = Path(__file__).resolve().parents[2]


async def run(args: argparse.Namespace) -> int:
    cfg = {}
    if Path(args.config).exists():
        with open(args.config, "rb") as f:
            cfg = tomllib.load(f)
    if args.port:
        cfg.setdefault("foc312", {})["port"] = args.port
    api = Foc312API(None, cfg)
    await api.start()
    print(f"player PREVIEW (no device): http://{api.bind}:{api.port}/   Ctrl-C to quit")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await api.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="player preview (no device)")
    ap.add_argument("--config", default=str(ROOT / "config" / "engine.toml"))
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
