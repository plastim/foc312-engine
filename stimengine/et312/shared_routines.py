"""The ET-312 shared routines: 78 ErosLink routines written by ET-312 owners, which ErosTek gave away as a free zip in
2011 ("as-is"; blog.erostek.com, 2011-01-10). ErosTek's site is gone; the Internet Archive still has the zip.

They ship with the app, in patterns/et312-shared/ (the zip's .elk files, its bytes and names unchanged; the folder's
README.md says where they come from). The player and the remote's Load list them as "ET-312 shared routines" with no
download. A copy fetched by an older version into the ErosLink cache's "shared" folder (elk.default_cache_dir()) is
still read, first, so a pattern picked from it stays picked; the same files are listed once (elk.list_routines: the
first copy of a file wins), so there are never two of a routine.

    python -m stimengine.et312.shared_routines            check the shipped folder against the archived zip
    python -m stimengine.et312.shared_routines --zip F    ... against a copy of the zip you already have
"""
from __future__ import annotations

import argparse
import hashlib
import io
import urllib.request
import zipfile
from pathlib import Path

from ..paths import ROOT
from .elk import default_cache_dir

URL = "https://web.archive.org/web/2011id_/http://www.erostek.com/Erostek312_routines.zip"
POST = "https://web.archive.org/web/20111208144808/http://blog.erostek.com/2011/01/10/extra-eroslink-routines-free/"
SHA256 = "6eb8a8e98165e1073fb3a0ff3fe2bde15a7646d6f0a42fc55e92b38088ac8070"
SUBDIR = "shared"              # the ErosLink cache's folder an older version fetched them into
MAX_BYTES = 1_000_000          # the zip is 98 KB
BUNDLED_DEFAULT = ROOT / "patterns" / "et312-shared"
BUNDLED = BUNDLED_DEFAULT      # read through bundled_folder() at call time (tests point it elsewhere)


class SharedRoutinesError(Exception):
    pass


def bundled_folder() -> Path:
    """The copy that ships with the app."""
    return Path(BUNDLED)


def folder(cache_dir: str | Path | None = None) -> Path:
    """The ErosLink cache's "shared" folder: where an older version put its download (listed before the shipped copy)."""
    return Path(cache_dir) if cache_dir is not None else default_cache_dir() / SUBDIR


def folders(cache: str | Path | None = None) -> list[Path]:
    """Where shared routines are read from, in order: a fetched copy in the ErosLink cache (`cache`), then the shipped
    one. The bytes are the same (the fetch was checked by SHA-256), so each file is listed once, from the first."""
    return [(Path(cache) if cache is not None else default_cache_dir()) / SUBDIR, bundled_folder()]


def count() -> int:
    """The .elk files that ship with the app."""
    d = bundled_folder()
    return len(list(d.glob("*.elk"))) if d.is_dir() else 0


def download(timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(URL, headers={"User-Agent": "PlaStim-foc312"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(MAX_BYTES + 1)
    except OSError as exc:
        raise SharedRoutinesError(f"the Internet Archive did not answer ({exc}); try again later") from None
    return data


def zip_routines(data: bytes) -> dict[str, bytes]:
    """The .elk files of the known zip, by name; SharedRoutinesError if it is not that zip."""
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != SHA256:
        raise SharedRoutinesError("not the known Erostek312_routines.zip (SHA-256 differs)")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist() if n.lower().endswith(".elk") and Path(n).name == n}


def compare(data: bytes, dest: str | Path | None = None) -> list[str]:
    """Differences between the zip's .elk files and the folder (the shipped one by default); [] when identical."""
    want = zip_routines(data)
    d = Path(dest) if dest is not None else bundled_folder()
    have = {p.name: p.read_bytes() for p in d.glob("*.elk")} if d.is_dir() else {}
    out = [f"missing: {n}" for n in sorted(set(want) - set(have))]
    out += [f"not in the zip: {n}" for n in sorted(set(have) - set(want))]
    out += [f"different bytes: {n}" for n in sorted(set(want) & set(have)) if want[n] != have[n]]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", type=Path, help="Erostek312_routines.zip you already downloaded")
    a = ap.parse_args()
    data = a.zip.read_bytes() if a.zip else download()
    diffs = compare(data)
    if diffs:
        print(*diffs, sep="\n")
        raise SystemExit(f"{bundled_folder()} differs from the archived zip")
    print(f"{count()} files in {bundled_folder()}: the same as the archived zip (SHA-256 {SHA256})")


if __name__ == "__main__":
    main()
