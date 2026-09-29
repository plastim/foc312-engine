"""Make a signed PlaStim firmware release: the image, manifest.json and manifest.json.sig.

    py -3.13 tools/release_firmware.py --product foc312 --version 8 --image ../foc312/.pio/build/focstim_v4/firmware.hex \\
        --notes "one model per direction" [--key PATH] [--out DIR] [--publish]

--product    foc312 (the FOC-Stim fork, .hex) or foc312-m5remote (the M5 remote, merged .bin)
--version    the release version; the GitHub tag is v<version>
--key        the PRIVATE signing key (default: plastim-release.key.enc, else plastim-release.key, in the
             release-signing folder; the encrypted one asks for its passphrase)
--out        where the three files go (default build/releases/<product>/<version>/)
--publish    also run `gh release create v<version> --repo plastim/<product>` with the three files attached.
             Only once the repository exists and you have checked the files.

The signature covers manifest.json's exact bytes; the manifest carries the image's SHA-256 and size. The app
(stimengine/app/updates.py) refuses a release whose signature does not match one of its TRUSTED_KEYS.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.release_keys import default_key, load_private  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "plastim release v1"
PRODUCTS = {"foc312": ".hex", "foc312-m5remote": ".bin"}
GITHUB_OWNER = "plastim"


def build_manifest(product: str, version: str, image: Path, notes: str, min_host: str) -> bytes:
    data = image.read_bytes()
    m = {"format": FORMAT, "product": product, "version": str(version), "file": image.name,
         "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "notes": notes,
         "min_host": min_host, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return (json.dumps(m, indent=1, sort_keys=True) + "\n").encode("utf-8")


def make_release(product: str, version: str, image: Path, notes: str, key: Path, out: Path,
                 min_host: str = "") -> list[Path]:
    if product not in PRODUCTS:
        raise ValueError(f"product must be one of {sorted(PRODUCTS)}")
    if image.suffix.lower() != PRODUCTS[product]:
        raise ValueError(f"a {product} image is a {PRODUCTS[product]} file, not {image.name}")
    out.mkdir(parents=True, exist_ok=True)
    dest = out / image.name
    if dest.resolve() != image.resolve():
        shutil.copyfile(image, dest)
    manifest = build_manifest(product, version, dest, notes, min_host)
    sig = base64.b64encode(load_private(key).sign(manifest)) + b"\n"
    (out / "manifest.json").write_bytes(manifest)
    (out / "manifest.json.sig").write_bytes(sig)
    return [dest, out / "manifest.json", out / "manifest.json.sig"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--product", required=True, choices=sorted(PRODUCTS))
    ap.add_argument("--version", required=True)
    ap.add_argument("--image", required=True, type=Path)
    ap.add_argument("--notes", default="")
    ap.add_argument("--min-host", default="")
    ap.add_argument("--key", type=Path, default=None)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args(argv)
    out = a.out or ROOT / "build" / "releases" / a.product / str(a.version)
    files = make_release(a.product, str(a.version), a.image, a.notes, a.key or default_key(), out, a.min_host)
    for f in files:
        print(f"{f}  ({f.stat().st_size} B)")
    print("sha256", hashlib.sha256(files[0].read_bytes()).hexdigest())
    if a.publish:
        cmd = ["gh", "release", "create", f"v{a.version}", "--repo", f"{GITHUB_OWNER}/{a.product}",
               "--title", f"{a.product} v{a.version}", "--notes", a.notes or f"{a.product} v{a.version}",
               *map(str, files)]
        print("running:", " ".join(cmd))
        return subprocess.run(cmd).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
