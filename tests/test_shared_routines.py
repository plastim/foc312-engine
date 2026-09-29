"""The ET-312 shared routines: the hub's download is checked by SHA-256 and only flat .elk files are kept."""
from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from stimengine.et312 import elk, shared_routines as SR


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_a_zip_that_is_not_the_known_one_is_refused(tmp_path):
    with pytest.raises(SR.SharedRoutinesError, match="SHA-256"):
        SR.install(_zip({"a.elk": b"x"}), tmp_path / "shared")
    assert not (tmp_path / "shared").exists()


def test_only_flat_elk_files_are_kept(tmp_path, monkeypatch):
    data = _zip({"one.elk": b"1", "two.ELK": b"2", "settings.eis": b"3", "../evil.elk": b"4", "sub/three.elk": b"5"})
    monkeypatch.setattr(SR, "SHA256", hashlib.sha256(data).hexdigest())
    dest = tmp_path / "cache" / "shared"
    (dest).mkdir(parents=True)
    (dest / "old.elk").write_bytes(b"old")
    assert SR.install(data, dest) == 2
    assert sorted(p.name for p in dest.iterdir()) == ["one.elk", "two.ELK"]      # replaced, nothing outside
    assert not (tmp_path / "cache" / "evil.elk").exists()
    assert SR.count(dest) >= 1                        # glob: "*.elk" (case-sensitive on Linux)


def test_the_shared_folder_is_listed_as_its_own_source(tmp_path):
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "broken.elk").write_bytes(b"not a routine")
    rs = elk.list_routines(None, cache_dir=tmp_path)
    assert [r["source"] for r in rs] == ["shared"] and rs[0]["error"]
