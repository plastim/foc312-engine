"""stimengine.et312.fwdata: the built-in modes come from the user's own firmware data; everything else works without."""
import os
from pathlib import Path

import pytest

from stimengine.et312 import fwdata
from stimengine.et312 import modes as M
from stimengine.et312.engine import BuiltinModesUnavailable, ET312Engine
from stimengine.et312.foc312 import Foc312Error, Foc312Runner, pattern_catalog
from stimengine.et312.vm import ET312VM


def _synthetic_image() -> bytes:
    """A made-up image with the firmware's layout: table at 0x1c3e, blocks from 0x2000 (not ErosTek's data)."""
    img = bytearray(b"\xff" * 0x3E00)
    off = 0
    for idx in range(fwdata.NBLOCKS):
        img[fwdata.TABLE + idx] = off
        code = bytes([0x80 | 0x05, idx & 0xFF, 0xC0 | 0x05, (idx * 3) & 0xFF, 0x00, 0x00])   # set $85, set B $185
        a = fwdata.PROG_BASE + 2 * off
        img[a:a + len(code)] = code
        off += len(code) // 2
    return bytes(img)


def test_image_layout_decodes_to_blocks(tmp_path):
    p = tmp_path / "fake.bin"
    p.write_bytes(_synthetic_image())
    d = fwdata.from_image(p)
    assert sorted(d.blocks) == list(range(fwdata.NBLOCKS))
    assert d.blocks[7] == [("set", 0, 0x85, 7), ("set", 1, 0x85, 21)]


def test_json_round_trip_keeps_every_op(tmp_path):
    img = tmp_path / "fake.bin"
    img.write_bytes(_synthetic_image())
    d = fwdata.from_image(img)
    d.blocks[3] = [("blk", 0, 0xAE, bytes.fromhex("b4 08")), ("rand", 0, 0x95)]
    fwdata.to_json(d, tmp_path / "fw.json")
    assert fwdata.from_json(tmp_path / "fw.json").blocks == d.blocks


def test_a_short_file_is_rejected(tmp_path):
    p = tmp_path / "not-firmware.bin"
    p.write_bytes(b"\x00" * 100)
    with pytest.raises(fwdata.FirmwareDataError):
        fwdata.from_image(p)


def test_without_data_builtins_are_absent_but_the_engine_still_works(monkeypatch):
    monkeypatch.setattr(fwdata, "_default", None)
    monkeypatch.setattr(fwdata, "_default_loaded", True)
    e = ET312Engine("waves")                      # no data: starts silent instead of failing
    assert not e.builtins_available and e.mode == 0
    with pytest.raises(BuiltinModesUnavailable):
        e.set_mode("stroke")
    with pytest.raises(ValueError):
        ET312Engine("nope")                       # unknown names are still errors
    u = ET312Engine("user1", user_blocks={0x80: [("set", 0, 0xAE, 0x20)]}, user_start={"user1": 0x80})
    assert u.vm.mem[0xAE] == 0x20                 # user / ErosLink routines run on the core blocks alone
    groups, _, _ = pattern_catalog(None)
    assert "Built-in modes" not in [g["label"] for g in groups]
    run = Foc312Runner(None, {})
    assert run.pattern["id"] is None
    with pytest.raises(Foc312Error):
        run.set_pattern("builtin:waves")


@pytest.mark.skipif(not os.environ.get("STIM_ENGINE_ET312_IMAGE"), reason="set STIM_ENGINE_ET312_IMAGE to your image")
def test_your_image_matches_the_data_in_use():
    """With your own v1.6 firmware image: every mode runs identically from the image and from the loaded data."""
    img = fwdata.from_image(os.environ["STIM_ENGINE_ET312_IMAGE"])
    cur = fwdata.default()
    if cur is None:
        pytest.skip("no other firmware data to compare with")
    for mode in [m for m in M.MODE_NAMES if M.MODE_NAMES[m] not in M.USER_MODES]:
        a, b = ET312VM({**M.CORE_BLOCKS, **img.blocks}, seed=5), ET312VM({**M.CORE_BLOCKS, **cur.blocks}, seed=5)
        for vm in (a, b):
            M.select_mode(vm, mode)
        for t in range(4000):
            for vm in (a, b):
                vm.ma_knob = (t % 2000) / 2000
                vm.tick()
            assert a.mem == b.mem, (M.MODE_NAMES[mode], t)
