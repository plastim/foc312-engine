"""The hub page (app/) and the player page (foc312/): every element the scripts look up exists, the tabs are wired,
the pop-out button is there, and (if node is installed) both scripts parse."""
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: dict[str, dict] = {}
        self.tabs: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids[a["id"]] = {"tag": tag, **a}
        if a.get("role") == "tab":
            self.tabs.append(a)


def _parse(path: Path) -> _Ids:
    p = _Ids()
    p.feed(path.read_text(encoding="utf-8"))
    return p


def _literal_ids(js: str) -> set[str]:
    return set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))


def test_hub_ids_tabs_and_panels():
    html = _parse(ROOT / "app" / "index.html")
    js = (ROOT / "app" / "hub.js").read_text(encoding="utf-8")
    missing = sorted(i for i in _literal_ids(js) if i not in html.ids)
    assert not missing, f"hub.js looks up ids that app/index.html lacks: {missing}"
    names = [t["data-tab"] for t in html.tabs]
    assert names == ["play", "boxes", "remote", "settings"]
    for t in html.tabs:
        assert t["aria-controls"] == "panel-" + t["data-tab"] and t["aria-controls"] in html.ids
    for prefix in ("box", "remote", "load", "pair"):              # showJob(prefix, ...)
        for suffix in ("Job", "JobState", "JobTitle", "JobLog"):
            assert prefix + suffix in html.ids, prefix + suffix
    for kind in ("box", "remote"):                                # image(kind), imageInfo(kind)
        assert kind + "Image" in html.ids and kind + "ImageInfo" in html.ids
    # the flash needs the remote-off tick and a confirm step
    assert html.ids["remoteOff"]["type"] == "checkbox"
    assert "boxConfirm" in html.ids and "boxFlashGo" in html.ids
    assert '/static/hub.js' in (ROOT / "app" / "index.html").read_text(encoding="utf-8")


def test_player_ids_nav_and_popout():
    html = _parse(ROOT / "player" / "index.html")
    js = (ROOT / "player" / "app.js").read_text(encoding="utf-8")
    missing = sorted(i for i in _literal_ids(js) if i not in html.ids)
    assert not missing, f"player/app.js looks up ids that player/index.html lacks: {missing}"
    assert html.ids["popout"]["tag"] == "button" and "popoutNote" in html.ids
    assert "documentPictureInPicture" in js and "requestWindow" in js
    page = (ROOT / "player" / "index.html").read_text(encoding="utf-8")
    assert 'href="http://127.0.0.1:8320/"' in page                  # the nav's Hub link
    # the pop-out wires the same commands the page uses, and STOP comes first in it
    assert js.index('data-pip="stop"') < js.index('data-pip="arm"')
    for cmd in ('send("stop")', 'send("arm")', 'send("levels", { a: v })', 'send("levels", { b: v })',
                'send("ma", { value: v })'):
        assert cmd in js
    # the page keys also listen in the pop-out, and it keeps the heartbeat going
    assert 'doc.addEventListener("keydown", onHotkey)' in js and "pipWin.setInterval" in js


@pytest.mark.parametrize("path", ["app/hub.js", "player/app.js"])
def test_scripts_parse(path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--check", str(ROOT / path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
