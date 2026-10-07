"""Firmware updates from GitHub Releases, signed by PlaStim.

Each firmware release (the FOC-Stim fork `foc312`, the M5 remote `foc312-m5remote`) is a GitHub Release with three
assets: the image, `manifest.json` (product, version, file, SHA-256, size, notes) and `manifest.json.sig` (an ed25519
signature over the manifest's exact bytes, made with PlaStim's offline key: tools/release_firmware.py). The app
checks the signature against TRUSTED_KEYS, then the image against the manifest; only then is it flashable. A
compromised GitHub account can publish files, but not a signature this app accepts.

Nothing happens on its own: "check" asks GitHub what exists, "download" fetches and verifies one release into the
local cache, and flashing is still the hub's explicit, confirmed step. The cache keeps working offline.

The remote's firmware comes for two boards from the one repository: the M5 remote's releases (product
`foc312-m5remote`) and the RADR hardware's (product `foc312-m5remote-radr`, "board": "radr", tags `radr-v<version>`).
The product is inside the signed manifest, so an app from before the RADR build refuses a RADR release outright ("is
for 'foc312-m5remote-radr'") and can never offer it for an M5; this one keeps each image's board, and the hub flashes
an image only onto its board.

Stock firmware (diglet48/FOC-Stim, the original author's releases) is not signed by PlaStim. A stock image is
downloadable here only when its SHA-256 is one this app already knows (KNOWN_STOCK) and matches the digest GitHub
reports; any other stock release is shown as a link to download by hand.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

FORMAT = "plastim release v1"

# PlaStim's release-signing public keys (hex). A list so a key can be rotated: add the new key in an app release
# first, sign with it only after that. The private key is kept offline (tools/release_keys.py).
TRUSTED_KEYS = [
    "b74b47bf1553be67f3e9b1e795e96137c4c65a7f14c3debed3869faff090b49b",
]

PRODUCTS = {"box": "foc312", "remote": "foc312-m5remote"}
# every product a kind's repository releases, with the board its image runs on ("" for the box)
KIND_PRODUCTS = {"box": {"foc312": ""}, "remote": {"foc312-m5remote": "m5", "foc312-m5remote-radr": "radr"}}
REPOS = {
    "box": os.environ.get("FOC312_RELEASES_BOX", "plastim/foc312"),
    "remote": os.environ.get("FOC312_RELEASES_REMOTE", "plastim/foc312-m5remote"),
}
STOCK_REPO = "diglet48/FOC-Stim"
STOCK_ASSET = "focstim_v4_firmware.hex"
# stock images this app knows by content (SHA-256 -> version): diglet48's FOC-Stim V4 release builds
KNOWN_STOCK = {
    "a6c79651abb7ead8d9c4be99bc91a2abdf422a11dec6295bd80fb564ff2d164a": "1.3.2",
}
API = "https://api.github.com"
TIMEOUT_S = 10.0


def cache_dir() -> Path:
    if os.environ.get("FOC312_FIRMWARE_CACHE"):
        return Path(os.environ["FOC312_FIRMWARE_CACHE"])
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "PlaStim" / "firmware") if base else Path.home() / ".cache" / "plastim" / "firmware"


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------- verification
def verify_manifest(data: bytes, sig: bytes, keys: list[str] | None = None) -> dict:
    """The parsed manifest if `sig` (base64) is a valid signature over `data` by a trusted key; UpdateError if not."""
    try:
        raw_sig = base64.b64decode(sig.strip(), validate=True)
    except (ValueError, TypeError):
        raise UpdateError("the signature file is not base64") from None
    for k in keys if keys is not None else TRUSTED_KEYS:
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(k)).verify(raw_sig, data)
            break
        except (InvalidSignature, ValueError):
            continue
    else:
        raise UpdateError("not signed by PlaStim (the signature does not match any trusted key)")
    try:
        m = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UpdateError("the manifest is not JSON") from None
    if m.get("format") != FORMAT:
        raise UpdateError(f"unknown manifest format {m.get('format')!r}")
    for field in ("product", "version", "file", "sha256", "size"):
        if field not in m:
            raise UpdateError(f"the manifest has no {field!r}")
    if "/" in str(m["file"]) or "\\" in str(m["file"]) or str(m["file"]) in ("", ".", ".."):
        raise UpdateError("the manifest names a file outside its release")
    return m


def verify_image(m: dict, path: Path) -> None:
    data = path.read_bytes()
    if len(data) != int(m["size"]):
        raise UpdateError(f"{path.name}: size {len(data)} is not the manifest's {m['size']}")
    if hashlib.sha256(data).hexdigest() != str(m["sha256"]).lower():
        raise UpdateError(f"{path.name}: SHA-256 does not match the signed manifest")


# ---------------------------------------------------------------- GitHub
Transport = Callable[[str, dict], "tuple[int, dict, bytes]"]


def _urllib_transport(url: str, headers: dict) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:        # noqa: S310 - https to GitHub only
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() if hasattr(e, "read") else b""


class GitHub:
    """The releases API, unauthenticated (public repositories), with ETags so re-checks cost nothing."""

    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport or _urllib_transport
        self._etag: dict[str, tuple[str, bytes]] = {}

    def get(self, url: str, accept: str = "application/vnd.github+json") -> bytes:
        headers = {"Accept": accept, "User-Agent": "PlaStim-app"}
        if url in self._etag:
            headers["If-None-Match"] = self._etag[url][0]
        try:
            status, resp_headers, body = self.transport(url, headers)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise UpdateError(f"could not reach GitHub ({exc})") from None
        if status == 304 and url in self._etag:
            return self._etag[url][1]
        if status == 404:
            raise UpdateError("not found")
        if status in (403, 429):
            raise UpdateError("GitHub's rate limit for unauthenticated checks was reached: try again later")
        if status != 200:
            raise UpdateError(f"GitHub answered {status}")
        etag = {k.lower(): v for k, v in (resp_headers or {}).items()}.get("etag")
        if etag:
            self._etag[url] = (etag, body)
        return body

    def releases(self, repo: str) -> list[dict]:
        try:
            data = json.loads(self.get(f"{API}/repos/{repo}/releases?per_page=20"))
        except UpdateError as exc:
            if str(exc) == "not found":
                raise UpdateError(f"no public releases yet (github.com/{repo})") from None
            raise
        return [r for r in data if isinstance(r, dict) and not r.get("draft") and not r.get("prerelease")]

    def latest(self, repo: str) -> dict:
        try:
            return json.loads(self.get(f"{API}/repos/{repo}/releases/latest"))
        except UpdateError as exc:
            if str(exc) == "not found":
                raise UpdateError(f"no public releases yet (github.com/{repo})") from None
            raise

    def asset(self, url: str) -> bytes:
        return self.get(url, accept="application/octet-stream")


def _asset(release: dict, name: str) -> dict | None:
    return next((a for a in release.get("assets", []) if a.get("name") == name), None)


# what the hub calls a downloaded release (one name for the firmware everywhere: devices, cards, guide)
RELEASE_NAMES = {"box": "PlaStim firmware", "remote": "PlaStim remote firmware"}
BOARD_RELEASE_NAMES = {"radr": "PlaStim remote firmware for the RADR"}


def manifest_board(kind: str, m: dict) -> str:
    """The board a verified manifest's image runs on ("" for the box); UpdateError if the product is not one of this
    kind's or its "board" word contradicts the product."""
    products = KIND_PRODUCTS[kind]
    if m["product"] not in products:
        raise UpdateError(f"is for {m['product']!r}, not {PRODUCTS[kind]!r}")
    board = products[m["product"]]
    if kind == "remote" and str(m.get("board") or board) != board:
        raise UpdateError(f"the manifest's board {m.get('board')!r} is not its product's ({board!r})")
    return board


def _version_key(v: str) -> tuple:
    """'9' < '10', 'v1.02' < 'v1.10': numeric parts compared as numbers."""
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-_]", v.lstrip("vV")))


def summarize(release: dict) -> dict:
    """What the hub shows for one PlaStim release, before anything is downloaded."""
    return {"tag": release.get("tag_name", ""), "name": release.get("name") or release.get("tag_name", ""),
            "published": release.get("published_at", ""), "notes": (release.get("body") or "").strip(),
            "url": release.get("html_url", ""),
            "signed": bool(_asset(release, "manifest.json") and _asset(release, "manifest.json.sig"))}


# ---------------------------------------------------------------- the updater
class Updater:
    def __init__(self, github: GitHub | None = None, cache: Path | None = None, keys: list[str] | None = None) -> None:
        self.gh = github or GitHub()
        self.cache = cache or cache_dir()
        self.keys = keys
        self.last: dict[str, dict] = {}         # kind -> the last check's result (no network to show it again)

    # -- check
    def check(self, kind: str) -> dict:
        repo = REPOS[kind]
        out: dict = {"kind": kind, "repo": repo, "checked_at": time.time(), "releases": [], "error": None}
        try:
            rels = self.gh.releases(repo)
            out["releases"] = [dict(summarize(r), downloaded=self._cached_version_dir(kind, r.get("tag_name", "")) is not None)
                               for r in rels]
        except UpdateError as exc:
            out["error"] = str(exc)
        if kind == "box":
            out["stock"] = self.check_stock()
        self.last[kind] = out
        return out

    def last_view(self, kind: str) -> dict | None:
        """The last check's result (no network), with "downloaded" read from the cache again."""
        r = self.last.get(kind)
        if not r:
            return None
        r = dict(r, releases=[dict(x, downloaded=self._cached_version_dir(kind, x.get("tag", "")) is not None)
                              for x in r.get("releases") or []])
        st = r.get("stock")
        if st and st.get("tag"):
            r["stock"] = dict(st, downloaded=(self.cache / "stock" / st["tag"] / STOCK_ASSET).is_file())
        return r

    def check_stock(self) -> dict:
        try:
            rel = self.gh.latest(STOCK_REPO)
        except UpdateError as exc:
            return {"error": str(exc), "repo": STOCK_REPO}
        a = _asset(rel, STOCK_ASSET) or {}
        digest = str(a.get("digest") or "")
        sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else ""
        known = sha in KNOWN_STOCK
        return {"repo": STOCK_REPO, "tag": rel.get("tag_name", ""), "url": rel.get("html_url", ""),
                "asset_url": a.get("browser_download_url", ""), "sha256": sha, "known": known,
                "downloaded": (self.cache / "stock" / rel.get("tag_name", "") / STOCK_ASSET).is_file(),
                "note": ("the original FOC-Stim firmware; this build is one the app knows (SHA-256 pinned)" if known
                         else "a stock build this app does not know yet: download it from the link and flash it "
                              "with restim's updater, or update the app")}

    # -- download
    def download(self, kind: str, tag: str) -> dict:
        repo, product = REPOS[kind], PRODUCTS[kind]
        rel = next((r for r in self.gh.releases(repo) if r.get("tag_name") == tag), None)
        if rel is None:
            raise UpdateError(f"no release {tag!r} in github.com/{repo}")
        ma, sa = _asset(rel, "manifest.json"), _asset(rel, "manifest.json.sig")
        if not ma or not sa:
            raise UpdateError(f"release {tag} is not signed (no manifest.json / manifest.json.sig)")
        mbytes = self.gh.asset(ma["url"])
        sig = self.gh.asset(sa["url"])
        m = verify_manifest(mbytes, sig, self.keys)
        try:
            manifest_board(kind, m)
        except UpdateError as exc:
            raise UpdateError(f"release {tag} {exc}") from None
        ia = _asset(rel, m["file"])
        if not ia:
            raise UpdateError(f"release {tag} has no {m['file']!r}")
        image = self.gh.asset(ia["url"])
        folder = self.cache / m["product"] / str(m["version"])
        tmp = folder.with_name(folder.name + ".partial")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            (tmp / "manifest.json").write_bytes(mbytes)
            (tmp / "manifest.json.sig").write_bytes(sig)
            (tmp / m["file"]).write_bytes(image)
            (tmp / "release.json").write_text(json.dumps({"tag": tag, "repo": repo}), encoding="utf-8")
            verify_image(m, tmp / m["file"])
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        shutil.rmtree(folder, ignore_errors=True)
        tmp.rename(folder)
        return self._entry(kind, folder)

    def download_stock(self, tag: str) -> dict:
        info = self.check_stock()
        if info.get("error"):
            raise UpdateError(info["error"])
        if info["tag"] != tag:
            raise UpdateError(f"the latest stock release is {info['tag']}, not {tag}")
        if not info["known"]:
            raise UpdateError("this stock build is not one the app knows: download it by hand from the link")
        data = self.gh.asset(info["asset_url"])
        sha = hashlib.sha256(data).hexdigest()
        if sha != info["sha256"] or sha not in KNOWN_STOCK:
            raise UpdateError("the downloaded stock image does not match its known SHA-256")
        folder = self.cache / "stock" / tag
        folder.mkdir(parents=True, exist_ok=True)
        (folder / STOCK_ASSET).write_bytes(data)
        return self._stock_entry(folder)

    # -- the cache as flashable images (re-verified every time it is listed)
    def _cached_version_dir(self, kind: str, tag: str) -> Path | None:
        for product in KIND_PRODUCTS[kind]:
            base = self.cache / product
            if not base.is_dir():
                continue
            for d in base.iterdir():
                try:
                    if json.loads((d / "release.json").read_text(encoding="utf-8")).get("tag") == tag:
                        return d
                except (OSError, ValueError):
                    continue
        return None

    def _entry(self, kind: str, folder: Path) -> dict:
        e = {"source": "release", "signed": True, "path": None}
        board = KIND_PRODUCTS[kind].get(folder.parent.name, "")       # (the cache folder's product, until verified)
        prefix = f"release-{board}-" if board and board != "m5" else "release-"
        try:
            m = verify_manifest((folder / "manifest.json").read_bytes(), (folder / "manifest.json.sig").read_bytes(),
                                self.keys)
            if m["product"] != folder.parent.name:
                raise UpdateError(f"cached release is for {m['product']!r}")
            board = manifest_board(kind, m)
            verify_image(m, folder / m["file"])
            ver = str(m["version"])
            name = BOARD_RELEASE_NAMES.get(board, RELEASE_NAMES[kind])
            e.update(id=f"{prefix}{ver}", name=f"{name} {ver if ver.startswith('v') else 'v' + ver}",
                     version=ver, sha256=str(m["sha256"]).lower(), file=m["file"],
                     notes=str(m.get("notes") or ""), recommended=False, path=str(folder / m["file"]))
        except (UpdateError, OSError, KeyError) as exc:
            e.update(id=f"{prefix}{folder.name}", name=f"{RELEASE_NAMES[kind]} {folder.name}", version=folder.name,
                     sha256="", file="", notes="", recommended=False, signed=False, error=f"not verified: {exc}")
        if kind == "remote":
            e["board"] = board or "m5"
        return e

    def _stock_entry(self, folder: Path) -> dict:
        path = folder / STOCK_ASSET
        sha = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        e = {"id": f"dl-stock-{folder.name}", "name": f"stock FOC-Stim {folder.name} (diglet48)", "version": folder.name,
             "sha256": sha, "file": STOCK_ASSET, "notes": "the original author's firmware, downloaded from GitHub",
             "recommended": False, "source": "stock", "signed": False, "path": str(path)}
        if sha not in KNOWN_STOCK:
            e["error"] = "not a known stock build"
        return e

    def cached(self, kind: str) -> list[dict]:
        out = []
        for product in KIND_PRODUCTS[kind]:
            base = self.cache / product
            if not base.is_dir():
                continue
            mine = [self._entry(kind, d) for d in sorted(base.iterdir(), reverse=True)
                    if d.is_dir() and not d.name.endswith(".partial")]
            good = [e for e in mine if e.get("signed") and not e.get("error")]
            if good:                                    # per board: the newest verified release is the one to use
                max(good, key=lambda e: _version_key(e["version"]))["recommended"] = True
            out += mine
        if kind == "box" and (self.cache / "stock").is_dir():
            for d in sorted((self.cache / "stock").iterdir(), reverse=True):
                if (d / STOCK_ASSET).is_file():
                    out.append(self._stock_entry(d))
        return out
