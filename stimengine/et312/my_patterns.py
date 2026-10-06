"""My patterns: the user's own ErosLink routine files (.elk), in one folder inside the program folder.

    my-patterns/            your .elk files: "My patterns" in the player, "Your routines" on the M5 remote
    my-patterns/<name>/     a subfolder (one level): its own group in the player, "My patterns: <name>"
    routines/               PlaStim's own routines ("Our routines" in the player and on the remote)

The PC player's pattern list (foc312.py pattern_catalog), the remote's pattern pack (remote/pack.py collect) and the
hub (app/server.py: add, remove, open the folder) all read these. A file copied in by hand (Explorer) shows in the
player the next time its list is asked for (the catalogue rescans when a folder changes: signature()), and on the
remote after its next Load. The folder is created on first use, with a README.txt, and is ignored by git, so an
update never touches it. [et312] elk_dir in config/engine.toml still works (an older way: "Your routines").

Files with the same bytes are listed once, wherever they are (elk.list_routines): a routine already in the ET-312
shared routines, copied here too, stays in that group.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

from ..paths import ROOT

FOLDER_NAME = "my-patterns"
FOLDER = ROOT / FOLDER_NAME          # read through folder() at call time (tests point it elsewhere)
OURS = ROOT / "routines"
GROUP = "My patterns"
EXT = ".elk"
IGNORED_EXTS = {".eis": "an ErosLink settings file (.eis), not a routine: not needed here"}
MAX_FILE_BYTES = 512 * 1024          # the largest routine file known is 11 KB
MAX_FILES = 200                      # per add
NAME_MAX = 100                       # characters before the extension
README = "README.txt"
README_TEXT = """My patterns
===========

Copy ErosLink routine files (.elk) into this folder.

- The PC player lists them under "My patterns" the next time you open its pattern list (no restart needed).
- The M5 remote gets them with its next "Load patterns & settings" (hub, M5 remote tab), under "yours".
- A subfolder becomes its own group in the player ("My patterns: <subfolder>"). One level only.
- .eis files (ErosLink settings) are not needed and are ignored.
- A file that can't be read is shown on the hub's M5 remote tab with the reason.

You can also add and remove files from the hub: M5 remote tab, Pattern files, "Add patterns...".
"""
_SKIP = {README.lower(), "desktop.ini", "thumbs.db"}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class MyPatternsError(ValueError):
    """A file refused (the message says why, for the page)."""


def folder() -> Path:
    return Path(FOLDER)


def ours_folder() -> Path:
    return Path(OURS)


def ensure(root: Path | None = None) -> Path:
    """The folder, created with its README.txt on first use."""
    root = Path(root) if root is not None else folder()
    root.mkdir(parents=True, exist_ok=True)
    readme = root / README
    if not readme.exists():
        try:
            readme.write_text(README_TEXT, encoding="utf-8")
        except OSError:
            pass
    return root


def group_dirs(root: Path | None = None) -> list[tuple[str, Path]]:
    """(group label, folder): the folder itself, then each subfolder (one level, by name)."""
    root = Path(root) if root is not None else folder()
    out = [(GROUP, root)]
    if root.is_dir():
        subs = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]
        out += [(f"{GROUP}: {d.name}", d) for d in sorted(subs, key=lambda d: d.name.lower())]
    return out


def sources(elk_dir: str | os.PathLike | None = None, *, ours: str | os.PathLike | None = None,
            mine: str | os.PathLike | None = None) -> list[tuple[str, Path, str | None]]:
    """The folders after the ErosLink cache, for elk.list_routines(more=...), in the order duplicates are resolved
    (the first copy of a file wins): our routines, [et312] elk_dir ("user"), my patterns and its subfolders."""
    out: list[tuple[str, Path, str | None]] = []
    if ours:
        out.append(("ours", Path(ours), None))
    if elk_dir:                       # "" = none (Path("") would be the current folder)
        out.append(("user", Path(elk_dir), None))
    if mine:
        out += [("mine", d, label) for label, d in group_dirs(Path(mine))]
    return out


def signature(dirs) -> tuple:
    """A cheap fingerprint of the .elk files in these folders (names, sizes, times; their subfolders' names): when it
    changes, the pattern list is read again. No file is opened."""
    sig = []
    for d in dirs:
        d = Path(d)
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            sig.append((str(d), e.name, "dir"))
                        elif e.name.lower().endswith(EXT):
                            st = e.stat()
                            sig.append((str(d), e.name, st.st_size, st.st_mtime_ns))
                    except OSError:
                        sig.append((str(d), e.name, "?"))
        except OSError:
            sig.append((str(d), None))
    return tuple(sorted(sig, key=repr))


# ---- names -------------------------------------------------------------------------------------------------------

def safe_name(raw: str) -> str:
    """A file name that can only land in the folder itself: the last part of any path, no characters Windows
    refuses, no leading dot, no device names, a lower-case extension. Raises MyPatternsError when nothing is left.
    Percent escapes are decoded first (some clients send "..%2Fx.elk" for "../x.elk"), so neither form gets out."""
    base = re.split(r"[\\/]", unquote(str(raw or "")))[-1]
    base = _BAD_CHARS.sub("_", base).strip().rstrip(". ")
    stem, dot, ext = base.rpartition(".")
    if not dot:
        raise MyPatternsError("only .elk files (ErosLink routines) can be added")
    stem = stem.strip().lstrip(". ").strip()
    if not stem:
        raise MyPatternsError("the file has no name")
    if stem.split(".")[0].lower() in _RESERVED:
        stem = "_" + stem
    return f"{stem[:NAME_MAX].rstrip('. ')}.{ext.lower()}"


def _resolve_rel(rel: str, root: Path) -> Path:
    """A listed file ("name.elk" or "subfolder/name.elk") inside the folder; anything else raises."""
    parts = [p for p in re.split(r"[\\/]", str(rel or "")) if p]
    if not 1 <= len(parts) <= 2 or any(p in (".", "..") or ":" in p for p in parts):
        raise MyPatternsError("not a file in My patterns")
    p = root.joinpath(*parts)
    try:
        rp, rr = p.resolve(), root.resolve()
    except OSError:
        raise MyPatternsError("not a file in My patterns") from None
    if rr not in (rp.parent, rp.parent.parent) or rp == rr:
        raise MyPatternsError("not a file in My patterns")
    if len(parts) == 1 and parts[0].lower() == README.lower():
        raise MyPatternsError("the README stays")
    if not rp.is_file():
        raise MyPatternsError(f"{parts[-1]} is not there (removed already? press Rescan)")
    return rp


# ---- what is there -----------------------------------------------------------------------------------------------

def _earlier_hashes(elk_dir=None, ours=None) -> dict[str, str]:
    """sha1 of every .elk listed before My patterns -> its group's name, for "listed there already"."""
    from . import elk
    cache = elk.default_cache_dir()
    named = [("ErosLink", cache / "bundled"), ("ErosLink examples", cache / "designer"),
             ("ET-312 shared routines", cache / "shared")]
    if ours:
        named.append(("Our routines", Path(ours)))
    if elk_dir:
        named.append(("Your routines (elk_dir)", Path(elk_dir)))
    out: dict[str, str] = {}
    for label, d in named:
        if not d.is_dir():
            continue
        for f in d.glob("*" + EXT):
            try:
                out.setdefault(hashlib.sha1(f.read_bytes()).hexdigest(), label)
            except OSError:
                pass
    return out


def view(root: Path | None = None, *, elk_dir=None, ours=None) -> dict:
    """What the hub shows: the folder, and every file in it (and its subfolders) with its routines, or why it is not
    used. Nothing is created."""
    from . import elk
    root = Path(root) if root is not None else folder()
    files: list[dict] = []
    earlier = _earlier_hashes(elk_dir, ours) if root.is_dir() else {}
    seen: dict[str, str] = {}
    for label, d in group_dirs(root):
        if not d.is_dir():
            continue
        sub = "" if d == root else d.name
        for f in sorted((q for q in d.iterdir() if q.is_file()), key=lambda q: q.name.lower()):
            if f.name.lower() in _SKIP or f.name.startswith("."):
                continue
            rel = f"{sub}/{f.name}" if sub else f.name
            item = {"rel": rel, "name": f.name, "group": label, "folder": sub, "size": 0, "routines": [],
                    "error": None, "ignored": None, "duplicate_of": None}
            files.append(item)
            try:
                item["size"] = f.stat().st_size
            except OSError:
                pass
            ext = f.suffix.lower()
            if ext != EXT:
                item["ignored"] = IGNORED_EXTS.get(ext, "not an .elk file: not used")
                continue
            try:
                data = f.read_bytes()
            except OSError as exc:
                item["error"] = f"could not be read ({exc.strerror or exc}); still being copied?"
                continue
            try:
                ctx = elk.read_context(data)
            except Exception as exc:  # noqa: BLE001 - the reason is shown, the rest still listed
                item["error"] = f"not a readable ErosLink routine file ({exc})"
                continue
            item["routines"] = [r.name for r in ctx.routines]
            if not ctx.routines:
                item["error"] = "the file holds no routines"
            h = hashlib.sha1(data).hexdigest()
            if h in earlier:
                item["duplicate_of"] = earlier[h]
            elif h in seen:
                item["duplicate_of"] = f"{GROUP} ({seen[h]})"
            else:
                seen[h] = rel
    return {"path": str(root), "exists": root.is_dir(), "files": files, "max_bytes": MAX_FILE_BYTES,
            "max_files": MAX_FILES}


# ---- add / remove / open -----------------------------------------------------------------------------------------

def check(name: str, data: bytes) -> tuple[str, list[str]]:
    """(safe file name, its routine names) for a file that may be added; MyPatternsError says why not."""
    from . import elk
    fname = safe_name(name)
    ext = Path(fname).suffix.lower()
    if ext in IGNORED_EXTS:
        raise MyPatternsError(IGNORED_EXTS[ext])
    if ext != EXT:
        raise MyPatternsError("only .elk files (ErosLink routines) can be added")
    if len(data) > MAX_FILE_BYTES:
        raise MyPatternsError(f"too big for a routine file ({len(data) // 1024} KB; at most {MAX_FILE_BYTES // 1024} KB)")
    if not data:
        raise MyPatternsError("the file is empty")
    try:
        ctx = elk.read_context(data)
    except Exception as exc:  # noqa: BLE001
        raise MyPatternsError(f"not a readable ErosLink routine file ({exc})") from None
    if not ctx.routines:
        raise MyPatternsError("the file holds no routines")
    return fname, [r.name for r in ctx.routines]


def add(name: str, data: bytes, root: Path | None = None) -> dict:
    """Check one file and write it into the folder (created on first use). The same bytes under the same name are
    not written twice; another file under a taken name gets " (2)", " (3)", ... Returns {name, rel, routines, same}."""
    fname, routines = check(name, data)
    root = ensure(root)
    stem, ext = fname[: -len(EXT)], EXT
    target, n = root / fname, 1
    while target.exists():
        try:
            if target.read_bytes() == data:
                return {"name": target.name, "rel": target.name, "routines": routines, "same": True}
        except OSError:
            pass
        n += 1
        target = root / f"{stem} ({n}){ext}"
    tmp = target.with_name(target.name + ".part")       # not *.elk while it is written: never listed half-done
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return {"name": target.name, "rel": target.name, "routines": routines, "same": False}


def _recycle(path: Path) -> bool:
    """To the Windows Recycle Bin (SHFileOperationW, FOF_ALLOWUNDO, no dialogs). False when that isn't possible."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    if ctypes.sizeof(ctypes.c_void_p) != 8:          # the structure below is the 64-bit layout
        return False

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None,     # pFrom: double-null terminated
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except OSError:
        return False
    return rc == 0 and not op.fAnyOperationsAborted and not path.exists()


def remove(rel: str, root: Path | None = None) -> str:
    """Remove one file of the folder: to the Recycle Bin where there is one ("recycled"), else deleted ("deleted")."""
    root = Path(root) if root is not None else folder()
    p = _resolve_rel(rel, root)
    if _recycle(p):
        return "recycled"
    p.unlink()
    return "deleted"


def _launch(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))                             # noqa: S606 - Explorer on our own folder
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_folder(root: Path | None = None) -> Path:
    """Show the folder in Explorer (created first, with its README)."""
    root = ensure(root)
    _launch(root)
    return root


__all__ = ["FOLDER", "OURS", "GROUP", "MAX_FILE_BYTES", "MAX_FILES", "MyPatternsError", "folder", "ours_folder",
           "ensure", "group_dirs", "sources", "signature", "safe_name", "view", "check", "add", "remove",
           "open_folder"]
