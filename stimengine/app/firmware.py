"""Firmware images the app can flash, from three sources:
  - local builds: the sibling projects' release folders (../foc312/release/manifest.json, FOC-Stim .hex, and
    ../foc312-m5remote/release/manifest.json, the M5 remote's merged .bin): "source": "local";
  - PlaStim releases downloaded from GitHub and verified against PlaStim's signing key (updates.py): "release";
  - known stock builds downloaded from diglet48's releases (updates.py): "stock".
Every image is checked again whenever the list is read (SHA-256; releases also their signature); an image that fails
is still listed, with "error", and cannot be flashed.

Manifest format: {"images": [{"id", "name", "version", "file" (relative to the manifest), "sha256", "notes",
"recommended"}]}.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..paths import FIRMWARE_DIR, REMOTE_DIR
from . import updates

# the release folders of the sibling projects (foc312, foc312-m5remote); later also GitHub releases
BOX_MANIFEST = FIRMWARE_DIR / "release" / "manifest.json"
REMOTE_MANIFEST = REMOTE_DIR / "release" / "manifest.json"

_hash_cache: dict[tuple[str, float, int], str] = {}
UPDATER: updates.Updater | None = None


def updater() -> updates.Updater:
    global UPDATER
    if UPDATER is None:
        UPDATER = updates.Updater()
    return UPDATER


def sha256_of(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    if key not in _hash_cache:
        _hash_cache[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _hash_cache[key]


def load(manifest: Path) -> list[dict]:
    if not manifest.exists():
        return []
    try:
        images = json.loads(manifest.read_text(encoding="utf-8")).get("images", [])
    except (json.JSONDecodeError, OSError) as exc:
        return [{"id": "manifest", "name": manifest.name, "error": f"manifest unreadable: {exc}"}]
    out = []
    for im in images:
        e = {"id": str(im.get("id", "")), "name": str(im.get("name", "")), "version": str(im.get("version", "")),
             "sha256": str(im.get("sha256", "")).lower(), "file": str(im.get("file", "")),
             "notes": str(im.get("notes", "")), "recommended": bool(im.get("recommended", False)),
             "source": "local", "signed": False}
        path = manifest.parent / e["file"]
        if not e["file"] or not path.is_file():
            e["error"] = "file missing"
        elif sha256_of(path) != e["sha256"]:
            e["error"] = "SHA-256 does not match the manifest"
        out.append(e)
    return out


def _public(e: dict) -> dict:
    return {k: v for k, v in e.items() if k != "path"}


def listing() -> dict:
    return {"box": load(BOX_MANIFEST) + [_public(e) for e in updater().cached("box")],
            "remote": load(REMOTE_MANIFEST) + [_public(e) for e in updater().cached("remote")]}


def find(kind: str, image_id: str) -> tuple[dict, Path]:
    """The image `image_id` of `kind` ("box" / "remote") and its file; ValueError unless it is flashable."""
    manifest = BOX_MANIFEST if kind == "box" else REMOTE_MANIFEST
    for e in load(manifest):
        if e["id"] == image_id:
            if "error" in e:
                raise ValueError(f"image {image_id!r}: {e['error']}")
            return e, manifest.parent / e["file"]
    for e in updater().cached(kind):                    # downloaded releases / stock, verified again right now
        if e["id"] == image_id:
            if "error" in e:
                raise ValueError(f"image {image_id!r}: {e['error']}")
            return e, Path(e["path"])
    raise ValueError(f"no {kind} image {image_id!r}")
