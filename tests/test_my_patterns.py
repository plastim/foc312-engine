"""My patterns (stimengine/et312/my_patterns.py): the user's own .elk folder, read by the PC player's list, the M5
remote's pack and the hub's Add / Remove / Open folder. Temporary folders only; no routine data is needed: a stand-in
reader turns b"ELK:<name>" into one synthetic routine (tests/test_et312_elk.py's Climb) and refuses anything else."""
from __future__ import annotations

import aiohttp
import pytest

from stimengine.app.server import Hub
from stimengine.et312 import elk, my_patterns as MP
from stimengine.et312.foc312 import Foc312Runner, pattern_catalog
from stimengine.remote import pack
from tests.test_app_backend import FakeEngine, client_for, run
from tests.test_et312_elk import _climb_like


def good(name: str) -> bytes:
    return b"ELK:" + name.encode()


@pytest.fixture
def fake_elk(tmp_path, monkeypatch):
    """A stand-in .elk reader, and an empty ErosLink cache in tmp_path/cache (its shared folder made)."""
    def read_context(data: bytes):
        if not data.startswith(b"ELK:"):
            raise ValueError("not a Java serialization stream")
        r = _climb_like()
        r.name = data[4:].decode() or "Untitled"
        return elk.ContextFile([r], [])

    monkeypatch.setattr(elk, "read_context", read_context)
    cache = tmp_path / "cache"
    (cache / "shared").mkdir(parents=True)
    monkeypatch.setenv("STIM_ENGINE_EROSLINK_CACHE", str(cache))
    monkeypatch.setattr(Hub, "_engine_cfg", staticmethod(lambda: {}))     # no elk_dir from this PC's engine.toml
    return cache


def _labels(groups):
    return {g["label"]: [i["name"] for i in g["items"]] for g in groups}


# ---- names ---------------------------------------------------------------------------------------------------------
def test_names_are_made_safe():
    assert MP.safe_name("..\\..\\Windows\\evil.elk") == "evil.elk"
    assert MP.safe_name("a/b/../c.ELK") == "c.elk"
    assert MP.safe_name("..%2F..%2Fx.elk") == MP.safe_name("..%5C..%5Cx.elk") == "x.elk"
    assert MP.safe_name("CON.elk") == "_CON.elk"
    assert MP.safe_name("..hidden.elk") == "hidden.elk"
    assert MP.safe_name('we<i>rd:"name"?.elk') == "we_i_rd__name__.elk"
    assert MP.safe_name("x" * 300 + ".elk") == "x" * MP.NAME_MAX + ".elk"
    for bad in ("noext", "", "../", ".elk", "C:\\"):
        with pytest.raises(MP.MyPatternsError):
            MP.safe_name(bad)


# ---- add -----------------------------------------------------------------------------------------------------------
def test_add_checks_each_file(fake_elk):
    root = MP.folder()
    assert not root.exists()
    res = MP.add("Mine.elk", good("Mine"))
    assert res == {"name": "Mine.elk", "rel": "Mine.elk", "routines": ["Mine"], "same": False}
    assert (root / "Mine.elk").read_bytes() == good("Mine") and (root / MP.README).exists()    # created on first use
    assert MP.add("Mine.elk", good("Mine"))["same"] is True                  # the same file again: not doubled
    assert MP.add("Mine.elk", good("Other"))["name"] == "Mine (2).elk"       # another file under a taken name
    for name, data, why in (("bad.elk", b"garbage", "not a readable ErosLink routine"),
                            ("notes.txt", good("x"), "only .elk"),
                            ("frame.eis", good("x"), ".eis"),
                            ("empty.elk", b"", "empty"),
                            ("big.elk", b"ELK:" + b"x" * MP.MAX_FILE_BYTES, "too big")):
        with pytest.raises(MP.MyPatternsError, match=why):
            MP.add(name, data)
    assert sorted(p.name for p in root.iterdir()) == ["Mine (2).elk", "Mine.elk", MP.README]


def _form(*files):
    fd = aiohttp.FormData()
    for name, data in files:
        fd.add_field("files", data, filename=name, content_type="application/octet-stream")
    return fd


def test_upload_good_bad_wrong_extension_and_traversal(fake_elk, tmp_path):
    async def go():
        c = await client_for(Hub(engine=FakeEngine()))
        try:
            r = await c.post("/api/patterns/mine", data=_form(
                ("Good.elk", good("Good")), ("Broken.elk", b"\xac\xed nope"), ("readme.txt", b"hello"),
                ("Settings.eis", b"ELK:x"), ("../../escape.elk", good("Escape")),
                ("..\\..\\escape2.elk", good("Escape 2"))))
            d = await r.json()
            assert r.status == 200 and d["ok"]
            assert sorted(a["name"] for a in d["added"]) == ["Good.elk", "escape.elk", "escape2.elk"]
            why = {x["name"]: x["error"] for x in d["refused"]}
            assert set(why) == {"Broken.elk", "readme.txt", "Settings.eis"}
            assert "not a readable" in why["Broken.elk"] and ".elk" in why["readme.txt"] and ".eis" in why["Settings.eis"]
            r = await c.post("/api/patterns/mine", data=_form(("Nope.elk", b"junk")))
            d = await r.json()
            assert r.status == 200 and not d["ok"] and not d["added"]              # all refused: says why, adds nothing
            r = await c.post("/api/patterns/mine", data=_form(("Huge.elk", b"ELK:" + b"x" * (MP.MAX_FILE_BYTES + 10))))
            assert "too big" in (await r.json())["refused"][0]["error"]
            r = await c.post("/api/patterns/mine", json={"files": []})
            assert r.status == 400
            r = await c.get("/api/patterns/mine")
            v = await r.json()
            return v
        finally:
            await c.close()

    v = run(go())
    root = MP.folder()
    assert sorted(p.name for p in root.iterdir()) == sorted(["Good.elk", "escape.elk", "escape2.elk", MP.README])
    assert not (tmp_path / "escape.elk").exists() and not (root.parent.parent / "escape.elk").exists()
    assert v["exists"] and v["path"] == str(root)
    assert [(f["rel"], f["routines"], f["error"]) for f in v["files"]] == [
        ("escape.elk", ["Escape"], None), ("escape2.elk", ["Escape 2"], None), ("Good.elk", ["Good"], None)]


# ---- the player's list: rescanned, no restart ------------------------------------------------------------------------
def test_the_player_sees_a_file_copied_in_by_hand(fake_elk):
    run_ = Foc312Runner(config={})
    groups = run_.catalog()[0]
    assert _labels(groups).get("My patterns") == []                 # shown even when empty: where files go
    root = MP.folder()
    (root / "Sub").mkdir(parents=True)
    (root / "Copied.elk").write_bytes(good("Copied"))                # as Explorer would: no hub involved
    (root / "Sub" / "Deep.elk").write_bytes(good("Deep"))
    (root / "Broken.elk").write_bytes(b"not a routine")
    (root / "ignored.eis").write_bytes(b"x")
    MP.ours_folder().mkdir(parents=True)
    (MP.ours_folder() / "Ours.elk").write_bytes(good("Ours"))
    groups = run_.catalog()[0]                                        # the same runner: no restart
    lab = _labels(groups)
    assert lab["My patterns"] == ["Broken (can't be read)", "Copied"]
    assert lab["My patterns: Sub"] == ["Deep"] and lab["Our routines"] == ["Ours"]
    broken = next(i for g in groups for i in g["items"] if i["name"].startswith("Broken"))
    assert broken["disabled"] and "Broken.elk" in broken["description"]   # listed with the reason, never played
    copied = next(i for g in groups for i in g["items"] if i["name"] == "Copied")
    run_.set_pattern(copied["id"])
    assert run_.pattern["name"] == "Copied"
    with pytest.raises(Exception):
        run_.set_pattern(broken["id"])
    (root / "Copied.elk").unlink()
    assert "Copied" not in _labels(run_.catalog()[0])["My patterns"]


def test_an_unchanged_folder_is_not_read_again(fake_elk, monkeypatch):
    run_ = Foc312Runner(config={})
    MP.ensure()
    (MP.folder() / "A.elk").write_bytes(good("A"))
    run_.catalog()
    calls = []
    real = elk.list_routines
    monkeypatch.setattr(elk, "list_routines", lambda *a, **k: calls.append(1) or real(*a, **k))
    run_.catalog()
    run_.catalog()
    assert calls == []                                                # only folder listings, no file read
    (MP.folder() / "B.elk").write_bytes(good("B"))
    assert "B" in _labels(run_.catalog()[0])["My patterns"] and calls == [1]


# ---- the remote's pack -------------------------------------------------------------------------------------------------
def test_the_remote_pack_includes_my_patterns_and_routines(fake_elk):
    MP.add("Mine.elk", good("Mine"))
    (MP.folder() / "Sub").mkdir()
    (MP.folder() / "Sub" / "Deep.elk").write_bytes(good("Deep"))
    (MP.folder() / "Bad.elk").write_bytes(b"junk")
    MP.ours_folder().mkdir(parents=True)
    (MP.ours_folder() / "Ours.elk").write_bytes(good("Ours"))
    pk, notes = pack.collect(firmware=None, ours_dir=MP.ours_folder(), mine_dir=MP.folder())
    got = [(e.name, e.group) for e in pk.entries if e.mode is None]
    assert sorted(got) == sorted([("Mine", pack.GROUP_YOURS), ("Deep", pack.GROUP_YOURS), ("Ours", pack.GROUP_OURS)])
    assert any(n.startswith("Bad.elk: unreadable") for n in notes)
    pack.parse(pack.build(pk))                                        # a pack the remote's parser accepts


def test_a_routine_already_present_is_listed_once(fake_elk):
    (fake_elk / "shared" / "Shared one.elk").write_bytes(good("Shared"))
    MP.add("copy of shared.elk", good("Shared"))
    MP.add("twice.elk", good("Twice"))
    (MP.folder() / "Sub").mkdir()
    (MP.folder() / "Sub" / "twice again.elk").write_bytes(good("Twice"))
    lab = _labels(pattern_catalog(None, mine=MP.folder())[0])
    assert lab["ET-312 shared routines"] == ["Shared"] and lab["My patterns"] == ["Twice"]
    assert "My patterns: Sub" not in lab                              # its only file is a copy: no empty group
    pk, _ = pack.collect(firmware=None, mine_dir=MP.folder())
    assert sorted(e.name for e in pk.entries if e.mode is None) == ["Shared", "Twice"]
    dup = {f["rel"]: f["duplicate_of"] for f in MP.view()["files"]}
    assert dup == {"copy of shared.elk": "ET-312 shared routines", "Sub/twice again.elk": "My patterns (twice.elk)",
                   "twice.elk": None}


def test_elk_dir_still_works(fake_elk, tmp_path):
    old = tmp_path / "old routines"
    old.mkdir()
    (old / "Old.elk").write_bytes(good("Old"))
    run_ = Foc312Runner(config={"et312": {"elk_dir": str(old)}})
    assert _labels(run_.catalog()[0])["Your routines"] == ["Old"]
    pk, _ = pack.collect(firmware=None, elk_dir=str(old), mine_dir=MP.folder())
    assert [(e.name, e.group) for e in pk.entries if e.mode is None] == [("Old", pack.GROUP_YOURS)]


# ---- remove / open -----------------------------------------------------------------------------------------------------
def test_remove(fake_elk, monkeypatch):
    MP.add("One.elk", good("One"))
    MP.add("Two.elk", good("Two"))
    (MP.folder() / "Sub").mkdir()
    (MP.folder() / "Sub" / "Three.elk").write_bytes(good("Three"))
    binned = []
    monkeypatch.setattr(MP, "_recycle", lambda p: binned.append(p.name) or p.unlink() or True)

    async def go():
        c = await client_for(Hub(engine=FakeEngine()))
        try:
            out = []
            for body in ({"rel": "One.elk"}, {"rel": "Sub/Three.elk"}, {"rel": "../One.elk"},
                         {"rel": "..\\..\\x.elk"}, {"rel": "README.txt"}, {"rel": "One.elk"}, {"rel": "C:/x.elk"},
                         {"rel": "Sub/../../x.elk"}, {}):
                r = await c.post("/api/patterns/mine/remove", json=body)
                out.append((r.status, (await r.json()).get("how")))
            return out
        finally:
            await c.close()

    out = run(go())
    assert out[:2] == [(200, "recycled"), (200, "recycled")] and all(s == 400 for s, _ in out[2:])
    assert binned == ["One.elk", "Three.elk"]
    assert sorted(p.name for p in MP.folder().iterdir()) == ["README.txt", "Sub", "Two.elk"]
    monkeypatch.setattr(MP, "_recycle", lambda p: False)               # no Recycle Bin: deleted
    assert MP.remove("Two.elk") == "deleted" and not (MP.folder() / "Two.elk").exists()


def test_open_folder_creates_it_and_shows_it(fake_elk, monkeypatch):
    shown = []
    monkeypatch.setattr(MP, "_launch", shown.append)
    st_d = run(_post(Hub(engine=FakeEngine()), "/api/patterns/mine/open", {}))
    assert st_d[0] == 200 and shown == [MP.folder()] and (MP.folder() / MP.README).exists()


async def _post(hub, path, body, headers=None):
    c = await client_for(hub)
    try:
        r = await c.post(path, json=body, headers=headers or {})
        return r.status, await r.json()
    finally:
        await c.close()


def test_the_endpoints_refuse_other_sites(fake_elk, monkeypatch):
    shown = []
    monkeypatch.setattr(MP, "_launch", shown.append)
    MP.add("Keep.elk", good("Keep"))
    evil = {"Origin": "https://evil.example"}

    async def go():
        c = await client_for(Hub(engine=FakeEngine()))
        try:
            out = [(await c.get("/api/patterns/mine", headers=evil)).status,
                   (await c.post("/api/patterns/mine", data=_form(("X.elk", good("X"))), headers=evil)).status,
                   (await c.post("/api/patterns/mine/remove", json={"rel": "Keep.elk"}, headers=evil)).status,
                   (await c.post("/api/patterns/mine/open", json={}, headers=evil)).status,
                   (await c.post("/api/patterns/mine/remove", json={"rel": "Keep.elk"},
                                 headers={"Host": "rebound.example:8320"})).status]
            return out
        finally:
            await c.close()

    assert run(go()) == [403] * 5
    assert (MP.folder() / "Keep.elk").exists() and not (MP.folder() / "X.elk").exists() and shown == []


def test_the_hub_lists_my_patterns_with_the_pack(fake_elk):
    MP.add("Mine.elk", good("Mine"))
    st, d = run(_get(Hub(engine=FakeEngine()), "/api/remote/patterns"))
    assert st == 200 and d["mine_dir"] == {"path": str(MP.folder()), "exists": True}
    assert {g["name"]: g["count"] for g in d["groups"]}["Your routines"] == 1


async def _get(hub, path):
    c = await client_for(hub)
    try:
        r = await c.get(path)
        return r.status, await r.json()
    finally:
        await c.close()


@pytest.mark.skipif(not list((elk.default_cache_dir()).glob("*/*.elk")), reason="no real .elk files on this machine")
def test_a_real_routine_file_is_accepted(tmp_path, monkeypatch):
    f = sorted(elk.default_cache_dir().glob("*/*.elk"))[0]
    res = MP.add(f.name, f.read_bytes())
    assert res["routines"] and (MP.folder() / res["name"]).read_bytes() == f.read_bytes()
