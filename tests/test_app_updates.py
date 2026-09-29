"""Firmware updates (stimengine/app/updates.py): signed GitHub releases, verified before they can be flashed.

A fake GitHub (the real API's response shape: tag_name / assets[name, url, browser_download_url, size, digest]) and
a throwaway signing key; nothing here touches the network, a serial port or the real firmware cache.
"""
from __future__ import annotations

import base64
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from stimengine.app import firmware, updates
from stimengine.app.server import Hub
from tests.test_app_backend import FakeEngine, RecordingJobs, post, run, client_for
from tools import release_firmware, release_keys

ROOT = Path(__file__).resolve().parents[1]
API = updates.API


@pytest.fixture
def key(tmp_path):
    priv, pub = release_keys.generate(tmp_path / "keys")
    return priv, pub


class FakeGitHub:
    """URL -> (status, headers, body). Honours If-None-Match like GitHub (304 with the same ETag)."""

    def __init__(self):
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        self.requests: list[tuple[str, dict]] = []

    def set(self, url, body, status=200, etag=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.routes[url] = (status, {"ETag": etag} if etag else {}, data)

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        if url not in self.routes:
            return 404, {}, b'{"message":"Not Found"}'
        status, h, body = self.routes[url]
        if h.get("ETag") and headers.get("If-None-Match") == h["ETag"]:
            return 304, h, b""
        return status, h, body


def publish(gh: FakeGitHub, key_path: Path, tmp: Path, repo: str, product: str, version: str, image: bytes,
            ext: str, notes: str = "", tamper_image: bytes | None = None, sign: bool = True) -> dict:
    """Make a signed release with the real tool and serve it from the fake GitHub; returns the release JSON."""
    src = tmp / f"src-{product}-{version}"
    src.mkdir(parents=True)
    img = src / f"image-{version}{ext}"
    img.write_bytes(image)
    out = tmp / f"rel-{product}-{version}"
    files = release_firmware.make_release(product, version, img, notes, key_path, out)
    assets = []
    for i, f in enumerate(files):
        body = f.read_bytes()
        if f.name == img.name and tamper_image is not None:
            body = tamper_image
        if not sign and f.name.endswith(".sig"):
            continue
        url = f"{API}/repos/{repo}/releases/assets/{version}{i}"
        gh.set(url, body)
        assets.append({"name": f.name, "url": url, "size": len(body),
                       "browser_download_url": f"https://github.com/{repo}/releases/download/v{version}/{f.name}",
                       "digest": "sha256:" + hashlib.sha256(body).hexdigest()})
    return {"tag_name": f"v{version}", "name": f"{product} v{version}", "published_at": "2026-09-28T20:00:00Z",
            "draft": False, "prerelease": False, "body": notes, "html_url": f"https://github.com/{repo}/releases/tag/v{version}",
            "assets": assets}


@pytest.fixture
def world(tmp_path, key, monkeypatch):
    """A fake GitHub with signed releases for the box (v8, v7) and the remote, an updater on a temp cache."""
    priv, pub = key
    gh = FakeGitHub()
    box_repo, rem_repo = updates.REPOS["box"], updates.REPOS["remote"]
    r8 = publish(gh, priv, tmp_path, box_repo, "foc312", "8", b":0000000108\n", ".hex", "per-direction models")
    r7 = publish(gh, priv, tmp_path, box_repo, "foc312", "7", b":0000000107\n", ".hex", "climb hold")
    draft = dict(r8, tag_name="v9", draft=True)
    gh.set(f"{API}/repos/{box_repo}/releases?per_page=20", [draft, r8, r7], etag='"box-1"')
    rr = publish(gh, priv, tmp_path, rem_repo, "foc312-m5remote", "5", b"\xe9" * 64, ".bin", "remote 5")
    gh.set(f"{API}/repos/{rem_repo}/releases?per_page=20", [rr])
    # stock: the real diglet48 v1.3.2 shape; the image's content does not matter, its hash is pinned
    stock_img = b"stock image bytes"
    sha = hashlib.sha256(stock_img).hexdigest()
    monkeypatch.setitem(updates.KNOWN_STOCK, sha, "1.3.2")
    gh.set(f"{API}/repos/{updates.STOCK_REPO}/releases/latest", {
        "tag_name": "v1.3.2", "html_url": "https://github.com/diglet48/FOC-Stim/releases/tag/v1.3.2",
        "assets": [{"name": "focstim_v4_firmware.hex", "size": len(stock_img), "digest": "sha256:" + sha,
                    "browser_download_url": "https://github.com/diglet48/FOC-Stim/releases/download/v1.3.2/focstim_v4_firmware.hex"}]})
    gh.set("https://github.com/diglet48/FOC-Stim/releases/download/v1.3.2/focstim_v4_firmware.hex", stock_img)
    u = updates.Updater(github=updates.GitHub(gh), cache=tmp_path / "cache", keys=[pub])
    monkeypatch.setattr(firmware, "UPDATER", u)
    return {"gh": gh, "u": u, "pub": pub, "priv": priv, "tmp": tmp_path, "r8": r8}


# ---- signatures -------------------------------------------------------------------------------------------------------
def test_signature_good_bad_tampered_and_unknown_key(tmp_path, key):
    priv, pub = key
    img = tmp_path / "fw.hex"
    img.write_bytes(b":00000001FF\n")
    files = release_firmware.make_release("foc312", "8", img, "n", priv, tmp_path / "out")
    m_bytes, sig = files[1].read_bytes(), files[2].read_bytes()
    m = updates.verify_manifest(m_bytes, sig, [pub])
    assert m["product"] == "foc312" and m["sha256"] == hashlib.sha256(img.read_bytes()).hexdigest()
    updates.verify_image(m, files[0])
    with pytest.raises(updates.UpdateError, match="not signed by PlaStim"):
        updates.verify_manifest(m_bytes.replace(b'"8"', b'"9"'), sig, [pub])            # tampered manifest
    with pytest.raises(updates.UpdateError, match="not signed by PlaStim"):
        updates.verify_manifest(m_bytes, base64.b64encode(b"\0" * 64), [pub])             # a bad signature
    other_priv, other_pub = release_keys.generate(tmp_path / "other")
    with pytest.raises(updates.UpdateError, match="not signed by PlaStim"):
        updates.verify_manifest(m_bytes, sig, [other_pub])                                # an unknown key
    with pytest.raises(updates.UpdateError, match="base64"):
        updates.verify_manifest(m_bytes, b"not base64!", [pub])
    files[0].write_bytes(b":00000001FE\n")                                               # tampered image
    with pytest.raises(updates.UpdateError, match="SHA-256"):
        updates.verify_image(m, files[0])
    # the real key is embedded, and the key tool will not overwrite a key
    assert len(updates.TRUSTED_KEYS) >= 1 and all(len(k) == 64 for k in updates.TRUSTED_KEYS)
    with pytest.raises(FileExistsError):
        release_keys.generate(priv.parent)


def test_a_manifest_cannot_point_outside_its_release(tmp_path, key):
    from tools.release_keys import load_private
    priv, pub = key
    m = json.dumps({"format": updates.FORMAT, "product": "foc312", "version": "8", "file": "../evil.hex",
                    "sha256": "0" * 64, "size": 1}).encode()
    sig = base64.b64encode(load_private(priv).sign(m))
    with pytest.raises(updates.UpdateError, match="outside"):
        updates.verify_manifest(m, sig, [pub])


def test_release_tool_checks_the_image_type(tmp_path, key):
    img = tmp_path / "x.bin"
    img.write_bytes(b"x")
    with pytest.raises(ValueError):
        release_firmware.make_release("foc312", "1", img, "", key[0], tmp_path / "o")


# ---- GitHub ---------------------------------------------------------------------------------------------------------
def test_check_lists_published_releases_latest_first(world):
    res = world["u"].check("box")
    assert res["error"] is None
    assert [r["tag"] for r in res["releases"]] == ["v8", "v7"]                 # the draft is left out
    assert res["releases"][0]["signed"] and res["releases"][0]["notes"] == "per-direction models"
    assert not res["releases"][0]["downloaded"]
    assert res["stock"]["tag"] == "v1.3.2" and res["stock"]["known"]
    # a second check sends the ETag and reuses the answer
    world["u"].check("box")
    sent = [h for url, h in world["gh"].requests if url.endswith("releases?per_page=20")]
    assert sent[-1].get("If-None-Match") == '"box-1"'


def test_missing_repo_and_rate_limit_read_plainly(world, monkeypatch):
    monkeypatch.setitem(updates.REPOS, "remote", "plastim/not-there")
    res = world["u"].check("remote")
    assert res["releases"] == [] and "no public releases yet" in res["error"]
    world["gh"].set(f"{API}/repos/{updates.REPOS['box']}/releases?per_page=20", b"{}", status=403)
    world["u"].gh._etag.clear()
    assert "rate limit" in world["u"].check("box")["error"]


# ---- download, cache, flashing ------------------------------------------------------------------------------------
def test_download_verifies_and_caches(world):
    u = world["u"]
    e = u.download("box", "v8")
    folder = world["tmp"] / "cache" / "foc312" / "8"
    assert sorted(p.name for p in folder.iterdir()) == ["image-8.hex", "manifest.json", "manifest.json.sig", "release.json"]
    assert e["id"] == "release-8" and e["source"] == "release" and e["signed"] and "error" not in e
    lst = firmware.listing()["box"]
    rel = [i for i in lst if i["id"] == "release-8"]
    assert rel and "path" not in rel[0]                                  # no local paths in the page's list
    im, path = firmware.find("box", "release-8")
    assert path == folder / "image-8.hex"
    assert u.check("box")["releases"][0]["downloaded"]


def test_a_tampered_download_leaves_nothing(world):
    priv, gh = world["priv"], world["gh"]
    repo = updates.REPOS["box"]
    bad = publish(gh, priv, world["tmp"], repo, "foc312", "10", b":0000000110\n", ".hex", tamper_image=b":00000001FF\n")
    gh.set(f"{API}/repos/{repo}/releases?per_page=20", [bad])
    world["u"].gh._etag.clear()
    with pytest.raises(updates.UpdateError, match="SHA-256"):
        world["u"].download("box", "v10")
    base = world["tmp"] / "cache" / "foc312"
    assert not base.exists() or not any(base.iterdir())


def test_unsigned_and_wrong_product_releases_are_refused(world):
    priv, gh = world["priv"], world["gh"]
    repo = updates.REPOS["box"]
    unsigned = publish(gh, priv, world["tmp"], repo, "foc312", "11", b":0000000111\n", ".hex", sign=False)
    wrong = publish(gh, priv, world["tmp"], repo, "foc312-m5remote", "12", b"\xe9" * 8, ".bin")
    gh.set(f"{API}/repos/{repo}/releases?per_page=20", [unsigned, wrong])
    world["u"].gh._etag.clear()
    with pytest.raises(updates.UpdateError, match="not signed"):
        world["u"].download("box", "v11")
    with pytest.raises(updates.UpdateError, match="not 'foc312'"):
        world["u"].download("box", "v12")


def test_an_image_changed_after_download_cannot_be_flashed(world):
    world["u"].download("box", "v8")
    (world["tmp"] / "cache" / "foc312" / "8" / "image-8.hex").write_bytes(b":0000000199\n")
    lst = firmware.listing()["box"]
    assert "not verified" in next(i for i in lst if i["id"] == "release-8")["error"]
    st, d = post(Hub(engine=FakeEngine(), jobs=RecordingJobs()), "/api/flash/box",
                 {"port": "COM17", "image": "release-8", "confirm": True, "remote_off": True})
    assert st == 400 and "not verified" in d["error"]


def test_a_downloaded_release_can_be_flashed(world):
    world["u"].download("box", "v8")
    jobs = RecordingJobs()
    st, d = post(Hub(engine=FakeEngine(), jobs=jobs), "/api/flash/box",
                 {"port": "COM17", "image": "release-8", "confirm": True, "remote_off": True})
    assert st == 200
    cmd = jobs.cmds[-1][1]
    assert cmd[2].endswith("image-8.hex") and cmd[4] == hashlib.sha256(b":0000000108\n").hexdigest()


def test_stock_known_builds_download_others_are_links(world, monkeypatch):
    e = world["u"].download_stock("v1.3.2")
    assert e["source"] == "stock" and "error" not in e and e["id"] == "dl-stock-v1.3.2"
    monkeypatch.setattr(updates, "KNOWN_STOCK", {})
    info = world["u"].check_stock()
    assert not info["known"] and "download it from the link" in info["note"]
    with pytest.raises(updates.UpdateError, match="not one the app knows"):
        world["u"].download_stock("v1.3.2")


def test_update_endpoints(world):
    def hub():                    # a hub per request (each runs on its own event loop); the updater is shared
        return Hub(engine=FakeEngine(), jobs=RecordingJobs())
    st, d = post(hub(), "/api/updates/check", {"kind": "box"})
    assert st == 200 and d["releases"][0]["tag"] == "v8"
    st, d = post(hub(), "/api/updates/download", {"kind": "box", "tag": "v8"})
    assert st == 200 and d["image"]["id"] == "release-8" and "path" not in d["image"]
    st, d = post(hub(), "/api/updates/download", {"kind": "remote", "tag": "nope"})
    assert st == 400 and "no release" in d["error"]
    assert post(hub(), "/api/updates/check", {"kind": "toaster"})[0] == 400

    async def last():
        c = await client_for(hub())
        try:
            return await (await c.get("/api/updates")).json()
        finally:
            await c.close()
    assert run(last())["box"]["releases"][0]["tag"] == "v8"


# ---- the page -------------------------------------------------------------------------------------------------------
def test_hub_has_the_update_panels():
    ids = set()

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for k, v in attrs:
                if k == "id":
                    ids.add(v)
    P().feed((ROOT / "app" / "index.html").read_text(encoding="utf-8"))
    for kind in ("box", "remote"):
        for suffix in ("UpCheck", "UpList", "UpChecked", "UpOlder", "UpOlderRow", "Updates"):
            assert kind + suffix in ids, kind + suffix


def test_the_release_key_can_be_passphrase_encrypted(tmp_path):
    """tools/release_keys.py --encrypt: the encrypted copy opens (with the passphrase only) to the same key."""
    import pytest as _pytest
    from tools import release_keys as K
    K.generate(tmp_path)
    plain = K.load_private(tmp_path / K.PRIVATE_NAME)
    with _pytest.raises(ValueError):
        K.encrypt(tmp_path, b"short")                        # a weak passphrase is refused
    enc = K.encrypt(tmp_path, b"correct horse battery staple")
    assert enc.read_bytes().startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY")
    assert K._pub_hex(K.load_private(enc, b"correct horse battery staple")) == K._pub_hex(plain)
    with _pytest.raises(ValueError):
        K.load_private(enc, b"wrong passphrase!!")
    assert K.default_key(tmp_path) == enc                  # signing prefers the encrypted key
    with _pytest.raises(FileExistsError):
        K.encrypt(tmp_path, b"correct horse battery staple")
