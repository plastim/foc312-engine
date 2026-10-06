"""The ET-312 shared routines (stimengine/et312/shared_routines.py): they ship in patterns/et312-shared, the .elk files
of ErosTek's 2011 zip unchanged, and the player, the remote's pack and the hub list them with no download. A copy an
older version fetched into the ErosLink cache's "shared" folder is still read, and adds no duplicates."""
from __future__ import annotations

import hashlib
import io
import shutil
import urllib.request
import zipfile

import pytest

from stimengine.app.server import Hub
from stimengine.et312 import elk, my_patterns as MP, shared_routines as SR
from stimengine.et312.foc312 import Foc312Runner, pattern_catalog
from stimengine.remote import pack
from tests.test_app_backend import FakeEngine, client_for, run

N_FILES, N_ROUTINES = 78, 163
GROUP = "ET-312 shared routines"
# sha256 over "<name>\0<sha256 of the file>\n" for every shipped .elk, sorted by name: the folder as it was taken from
# the archived zip (SR.SHA256) on 2026-10-06. A changed, added or removed file changes it.
MANIFEST = "590e77ec13bcc3d116fbe7d4a56a1e5f51af868c184534e448c7821bd87c534e"


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _shared_items(groups) -> list[dict]:
    return next((g["items"] for g in groups if g["label"] == GROUP), [])


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A fresh install: the folder that ships with the app, an empty ErosLink cache, nothing in My patterns, and no
    network (any download fails the test). Returns the cache folder."""
    monkeypatch.setattr(SR, "BUNDLED", SR.BUNDLED_DEFAULT)
    cache = tmp_path / "eroslink"
    monkeypatch.setenv("STIM_ENGINE_EROSLINK_CACHE", str(cache))
    monkeypatch.setattr(Hub, "_engine_cfg", staticmethod(lambda: {}))

    def no_network(*_a, **_k):
        raise AssertionError("the shared routines must not need a download")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    return cache


# ---- the shipped folder ------------------------------------------------------------------------------------------
def test_the_shipped_folder_is_the_archived_zip_unchanged():
    files = sorted(SR.BUNDLED_DEFAULT.glob("*.elk"), key=lambda p: p.name)
    assert len(files) == N_FILES
    m = "".join(f"{p.name}\0{hashlib.sha256(p.read_bytes()).hexdigest()}\n" for p in files)
    assert hashlib.sha256(m.encode()).hexdigest() == MANIFEST
    readme = (SR.BUNDLED_DEFAULT / "README.md").read_text(encoding="utf-8")
    for must in (SR.SHA256, SR.POST, "Not affiliated with or endorsed by ErosTek", "/issues"):
        assert must in readme


def test_all_78_parse_and_compile():
    n = 0
    for f in sorted(SR.BUNDLED_DEFAULT.glob("*.elk")):
        ctx = elk.read_context(f.read_bytes())
        assert ctx.routines, f.name
        for r in ctx.routines:
            elk.compile_routine(r)
            n += 1
    assert n == N_ROUTINES


def test_compare_finds_every_difference(tmp_path, monkeypatch):
    data = _zip({"one.elk": b"1", "two.elk": b"2", "three.elk": b"3", "settings.eis": b"s", "sub/x.elk": b"x"})
    with pytest.raises(SR.SharedRoutinesError, match="SHA-256"):
        SR.compare(data, tmp_path)
    monkeypatch.setattr(SR, "SHA256", hashlib.sha256(data).hexdigest())
    (tmp_path / "one.elk").write_bytes(b"1")
    (tmp_path / "two.elk").write_bytes(b"changed")
    (tmp_path / "extra.elk").write_bytes(b"e")
    assert SR.compare(data, tmp_path) == ["missing: three.elk", "not in the zip: extra.elk",
                                          "different bytes: two.elk"]
    (tmp_path / "two.elk").write_bytes(b"2")
    (tmp_path / "three.elk").write_bytes(b"3")
    (tmp_path / "extra.elk").unlink()
    assert SR.compare(data, tmp_path) == []


# ---- listed out of the box ---------------------------------------------------------------------------------------
def test_listed_on_a_fresh_install_with_no_network(fresh):
    items = _shared_items(pattern_catalog(None, mine=MP.folder(), ours=MP.ours_folder())[0])
    assert len(items) == N_ROUTINES and not any(i["disabled"] for i in items)
    runner = Foc312Runner(config={})
    groups, elk_by_id, err = runner.catalog()
    assert err is None and len(_shared_items(groups)) == N_ROUTINES
    first = _shared_items(groups)[0]
    runner.set_pattern(first["id"])                   # playable: picked as any other routine
    assert runner.pattern["id"] == first["id"]
    assert {r["source"] for r in elk.list_routines(None)} == {"shared"}
    assert elk.load(elk_by_id[first["id"]]["path"]).source == "shared"


def test_a_fetched_copy_adds_no_duplicates(fresh):
    fetched = fresh / SR.SUBDIR                       # where an older version's "Get the ET-312 shared routines" put them
    shutil.copytree(SR.BUNDLED_DEFAULT, fetched, ignore=shutil.ignore_patterns("*.md", ".git*"))
    rs = elk.list_routines(None)
    assert len(rs) == N_ROUTINES and {r["source"] for r in rs} == {"shared"}
    assert {elk._split_ref(r["path"])[0].parent for r in rs} == {fetched}     # the fetched copy first: picks stay
    assert len(_shared_items(pattern_catalog(None, mine=MP.folder())[0])) == N_ROUTINES
    pk, _ = pack.collect(firmware=None, mine_dir=MP.folder())
    assert len([e for e in pk.entries if e.mode is None]) == N_ROUTINES
    # half of them fetched: still each routine once
    for f in sorted(fetched.glob("*.elk"))[::2]:
        f.unlink()
    assert len(elk.list_routines(None)) == N_ROUTINES
    # and one copied into My patterns is listed there as a duplicate, not twice
    f = sorted(SR.BUNDLED_DEFAULT.glob("*.elk"))[0]
    MP.add(f.name, f.read_bytes())
    assert [x["duplicate_of"] for x in MP.view()["files"]] == [GROUP]
    assert len(_shared_items(pattern_catalog(None, mine=MP.folder())[0])) == N_ROUTINES


def test_the_remote_pack_includes_them(fresh):
    pk, notes = pack.collect(firmware=None, mine_dir=MP.folder(), ours_dir=MP.ours_folder())
    routines = [e for e in pk.entries if e.mode is None]
    assert len(routines) == N_ROUTINES
    assert [n for n in notes if "built-in modes" not in n] == []      # none left out (that note: no firmware data)
    assert {e.group for e in routines} == {pack.GROUP_YOURS}          # the group a fetched copy went to
    pack.parse(pack.build(pk))                                        # a pack the remote's parser accepts


def test_the_hub_says_they_are_included(fresh):
    async def go():
        c = await client_for(Hub(engine=FakeEngine()))
        try:
            r = await c.get("/api/remote/patterns")
            d = await r.json()
            gone = await c.post("/api/patterns/shared", json={})       # the download button's endpoint is gone
            return r.status, d, gone.status
        finally:
            await c.close()

    st, d, gone = run(go())
    assert st == 200 and d["shared"] == {"path": str(SR.BUNDLED_DEFAULT), "count": N_FILES}
    assert {g["name"]: g["count"] for g in d["groups"]}["Your routines"] == N_ROUTINES
    assert gone in (404, 405)


# ---- the cache's shared folder (an older version's download) -----------------------------------------------------
def test_the_cache_shared_folder_is_still_listed_as_shared(tmp_path):
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "broken.elk").write_bytes(b"not a routine")
    rs = elk.list_routines(None, cache_dir=tmp_path)
    assert [r["source"] for r in rs] == ["shared"] and rs[0]["error"]
