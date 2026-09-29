"""M5 remote tools.

    py -3.13 -m stimengine.remote build [--out DIR]     patterns.bin + config.json (default build/m5)
    py -3.13 -m stimengine.remote load --port COMx      build, then load both onto the remote and reload it
    py -3.13 -m stimengine.remote list --port COMx      what the remote holds
    py -3.13 -m stimengine.remote pair-box --box-port COMy [--house]
                                                        point a box's Wi-Fi at the remote's own network ([direct])
                                                        or back at the house Wi-Fi

The remote must be STOPPED (not armed) to accept a load.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import m5config, pack
from ..device.client import DeviceError
from ..device.transport import TransportError
from .loader import LoaderError, Remote, SerialLink, wait_for_port

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "build" / "m5"


def build(out: Path) -> tuple[bytes, bytes]:
    from ..et312.foc312 import DEFAULT_ELK_DIR
    import tomllib

    with open(ROOT / "config" / "engine.toml", "rb") as f:
        et = tomllib.load(f).get("et312", {})
    pk, notes = pack.collect(elk_dir=et.get("elk_dir", DEFAULT_ELK_DIR), ours_dir=ROOT / "routines")
    data = pack.build(pk)
    cfg = m5config.to_bytes(m5config.build_from_files())
    out.mkdir(parents=True, exist_ok=True)
    (out / "patterns.bin").write_bytes(data)
    (out / "config.json").write_bytes(cfg)
    groups: dict[str, int] = {}
    for e in pk.entries:
        groups[pack.GROUP_NAMES[e.group]] = groups.get(pack.GROUP_NAMES[e.group], 0) + 1
    print(f"patterns.bin: {len(pk.entries)} patterns, {len(data)} bytes ({', '.join(f'{k} {v}' for k, v in groups.items())})")
    for n in notes:
        print("  note:", n)
    print(f"config.json: {len(cfg)} bytes -> {out}")
    return data, cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["build", "load", "list", "pair-box"])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--port", help="the remote's USB port")
    ap.add_argument("--box-port", help="pair-box: the FOC-Stim's USB port")
    ap.add_argument("--house", action="store_true", help="pair-box: back to the house Wi-Fi")
    args = ap.parse_args(argv)
    try:
        if args.action == "build":
            build(args.out)
            return 0
        if args.action == "pair-box":
            if not args.box_port:
                ap.error("--box-port is needed")
            from . import pairbox

            return pairbox.run(args.box_port, args.house)
        if not args.port:
            ap.error("--port is needed")
        wait_for_port(args.port)
        link = SerialLink(args.port)
        try:
            r = Remote(link)
            free = r.hello()
            if args.action == "list":
                for name, size in r.list().items():
                    print(f"{name:16} {size:8d} bytes")
                print(f"free: {free} bytes")
                return 0
            data, cfg = build(args.out)
            for name, blob in (("patterns.bin", data), ("config.json", cfg)):
                r.put(name, blob, progress=lambda s, n, nm=name: print(f"\r{nm}: {s}/{n}", end="", flush=True))
                print()
            r.reload()
            print("loaded; the remote reloaded its patterns and settings")
        finally:
            link.close()
        return 0
    except (LoaderError, m5config.ConfigError, pack.PackError, DeviceError, TransportError) as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
