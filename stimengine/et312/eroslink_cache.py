"""Rebuild the local cache of ErosLink's own routines from the ErosLink installer (not stored in the repo).

    py -3.13 -m stimengine.et312.eroslink_cache [--zip ErosLink_Installer.zip] [--cache DIR]

The ErosLink CD image (ErosLink_Installer.zip -> install.exe, an InstallAnywhere self-extractor from
2003) carries the routines ErosTek shipped:
  routines/*.elk            the main set (EMS1, EMS2, Bee Stings, Challenge, Intense 2, ...) -> <cache>/bundled/
  routines/designer/*.elk   designer examples (Climb, ErsatzWaves, KitchenSink, ...)          -> <cache>/designer/
  routines/designer/*.eis   interactive-frame snapshots (not routines; copied, not listed)    -> <cache>/designer/
install.exe holds a zip appended after the Windows stub, and it and the nested InstallerData/Installer.zip
both carry extra fields Python's zipfile rejects, so their central directories are walked by hand here.
Defaults: the zip from $STIM_ENGINE_EROSLINK_ZIP or PlaStim's mk312 folder; the cache from
$STIM_ENGINE_EROSLINK_CACHE or ~/.stim-engine/eroslink (elk.default_cache_dir(), which says why not
%LOCALAPPDATA%).  The cache gets a manifest.json (file names, sizes, sha1, source zip sha1).
Rebuilding replaces the cache's .elk/.eis files; it is safe to run again any time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path

from .elk import default_cache_dir

DEFAULT_ZIP = ""              # the ErosLink installer .zip: --zip or $STIM_ENGINE_EROSLINK_ZIP
ROUTINES_DIR = "/InstallAnywhere/routines/"


def _appended_zip_entries(data: bytes) -> dict[str, bytes]:
    """Entries of a zip appended to an executable, read via the central directory (extra fields ignored)."""
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise ValueError("no zip directory found in install.exe")
    _, _, _, _, count, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", data[eocd:eocd + 22])
    base = eocd - cd_size - cd_off                  # where the appended archive starts in the exe
    p = eocd - cd_size
    out: dict[str, bytes] = {}
    for _ in range(count):
        (sig, _vm, _vn, _fl, method, _t, _d, _crc, csize, _usize, nlen, xlen, clen, _dsk, _ia, _ea,
         lho) = struct.unpack("<IHHHHHHIIIHHHHHII", data[p:p + 46])
        if sig != 0x02014B50:
            raise ValueError("corrupt central directory in install.exe")
        name = data[p + 46:p + 46 + nlen].decode("latin-1")
        p += 46 + nlen + xlen + clen
        if name.endswith("/"):
            continue
        off = base + lho
        lnlen, lxlen = struct.unpack("<HH", data[off + 26:off + 30])
        raw = data[off + 30 + lnlen + lxlen:off + 30 + lnlen + lxlen + csize]
        out[name] = raw if method == 0 else zlib.decompress(raw, -15)
    return out


def extract_routines(zip_path: str | os.PathLike) -> dict[str, bytes]:
    """{"bundled/<name>.elk" | "designer/<name>.elk|.eis": bytes} from ErosLink_Installer.zip."""
    with zipfile.ZipFile(zip_path) as outer:
        exe_name = next((n for n in outer.namelist() if n.lower().endswith("install.exe")
                         and not n.startswith("__MACOSX")), None)
        if exe_name is None:
            raise ValueError(f"{zip_path}: no install.exe inside")
        exe = outer.read(exe_name)
    inner = _appended_zip_entries(exe)
    installer = next((v for k, v in inner.items() if k.endswith("InstallerData/Installer.zip")), None)
    if installer is None:
        raise ValueError("install.exe: no InstallerData/Installer.zip")
    out: dict[str, bytes] = {}
    for n, data in _appended_zip_entries(installer).items():
        i = n.find(ROUTINES_DIR)
        if i < 0:
            continue
        rel = n[i + len(ROUTINES_DIR):]
        if "/" not in rel and rel.lower().endswith(".elk"):
            out["bundled/" + rel] = data
        elif rel.lower().startswith("designer/") and rel.lower().endswith((".elk", ".eis")):
            out["designer/" + rel.split("/", 1)[1]] = data
    if not out:
        raise ValueError("no routines found in the installer")
    return out


def rebuild(zip_path: str | os.PathLike, cache_dir: str | os.PathLike | None = None) -> Path:
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    files = extract_routines(zip_path)
    for sub in ("bundled", "designer"):
        d = cache / sub
        d.mkdir(parents=True, exist_ok=True)
        for old in d.iterdir():
            if old.is_file() and old.suffix.lower() in (".elk", ".eis"):
                old.unlink()
    manifest = {"source_zip": str(zip_path),
                "source_zip_sha1": hashlib.sha1(Path(zip_path).read_bytes()).hexdigest(), "files": {}}
    for rel, data in sorted(files.items()):
        (cache / rel).write_bytes(data)
        manifest["files"][rel] = {"bytes": len(data), "sha1": hashlib.sha1(data).hexdigest()}
    (cache / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return cache


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", default=os.environ.get("STIM_ENGINE_EROSLINK_ZIP", DEFAULT_ZIP))
    ap.add_argument("--cache", default=None, help="default: elk.default_cache_dir()")
    a = ap.parse_args(argv)
    if not a.zip:
        ap.error("give the ErosLink installer .zip with --zip (or set STIM_ENGINE_EROSLINK_ZIP)")
    cache = rebuild(a.zip, a.cache)
    files = json.loads((cache / "manifest.json").read_text(encoding="utf-8"))["files"]
    nb = sum(1 for k in files if k.startswith("bundled/"))
    nd = sum(1 for k in files if k.startswith("designer/"))
    print(f"{cache}: {nb} bundled routines, {nd} designer files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
