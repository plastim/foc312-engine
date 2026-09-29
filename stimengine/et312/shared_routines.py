"""The ET-312 shared routines: 78 ErosLink routines written by ET-312 owners, which ErosTek gave away as a free zip in
2011 ("as-is"; blog.erostek.com, 2011-01-10). ErosTek's site is gone; the Internet Archive still has the zip.

Not part of this source: the user's hub fetches the zip itself (one button on the M5 remote tab), checks it against
the SHA-256 below, and keeps only the .elk files, in the ErosLink cache's "shared" folder (elk.default_cache_dir()).
From there the player and the remote's Load list them with the other routines.

    python -m stimengine.et312.shared_routines            fetch and unpack
    python -m stimengine.et312.shared_routines --zip F    unpack a zip you already have
"""
from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import urllib.request
import zipfile
from pathlib import Path

from .elk import default_cache_dir

URL = "https://web.archive.org/web/2011id_/http://www.erostek.com/Erostek312_routines.zip"
POST = "https://web.archive.org/web/20111208144808/http://blog.erostek.com/2011/01/10/extra-eroslink-routines-free/"
SHA256 = "6eb8a8e98165e1073fb3a0ff3fe2bde15a7646d6f0a42fc55e92b38088ac8070"
SUBDIR = "shared"
MAX_BYTES = 1_000_000          # the zip is 98 KB


class SharedRoutinesError(Exception):
    pass


def folder(cache_dir: str | Path | None = None) -> Path:
    return Path(cache_dir) if cache_dir is not None else default_cache_dir() / SUBDIR


def count(cache_dir: str | Path | None = None) -> int:
    d = folder(cache_dir)
    return len(list(d.glob("*.elk"))) if d.is_dir() else 0


def download(timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(URL, headers={"User-Agent": "PlaStim-foc312"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(MAX_BYTES + 1)
    except OSError as exc:
        raise SharedRoutinesError(f"the Internet Archive did not answer ({exc}); try again later") from None
    return data


def install(data: bytes, cache_dir: str | Path | None = None) -> int:
    """Check the zip and unpack its .elk files into the shared folder (replacing what was there). Returns the count."""
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != SHA256:
        raise SharedRoutinesError("the download is not the known Erostek312_routines.zip (SHA-256 differs): not used")
    dest = folder(cache_dir)
    tmp = dest.with_name(dest.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    n = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            base = Path(name).name                  # the zip is flat; never trust a path inside it
            if base.lower().endswith(".elk") and base == name:
                (tmp / base).write_bytes(z.read(name))
                n += 1
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return n


def fetch(cache_dir: str | Path | None = None) -> int:
    return install(download(), cache_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", type=Path, help="Erostek312_routines.zip you already downloaded")
    a = ap.parse_args()
    n = install(a.zip.read_bytes()) if a.zip else fetch()
    print(f"{n} shared routines in {folder()}")


if __name__ == "__main__":
    main()
