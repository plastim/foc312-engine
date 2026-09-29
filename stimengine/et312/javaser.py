"""Minimal reader for the Java Object Serialization stream format (protocol version 2, "0xACED 0005").

Enough for ErosLink files (.elk routines, .eis interactive frames): every ErosLink class writes itself
with a private writeObject() and never calls defaultWriteObject(), so each object's data is a flat
sequence of nested objects and block-data bytes.  `parse()` returns the top-level contents as that
same kind of sequence; `Items` replays it with ObjectInputStream-like calls (read_object / read_int /
read_double / ...), which is how the per-class decoders in elk.py mirror ErosLink's readObject().

Spec: "Java Object Serialization Specification", chapter 6 (Object Serialization Stream Protocol).
Classes that use default field serialisation (no writeObject) are supported too (fields in
ObjectStreamClass order), so a stray java.* object does not stop the parse.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

STREAM_MAGIC = 0xACED
TC_NULL, TC_REFERENCE, TC_CLASSDESC, TC_OBJECT, TC_STRING, TC_ARRAY, TC_CLASS = range(0x70, 0x77)
TC_BLOCKDATA, TC_ENDBLOCKDATA, TC_RESET, TC_BLOCKDATALONG, TC_EXCEPTION = 0x77, 0x78, 0x79, 0x7A, 0x7B
TC_LONGSTRING, TC_PROXYCLASSDESC, TC_ENUM = 0x7C, 0x7D, 0x7E
BASE_HANDLE = 0x7E0000
SC_WRITE_METHOD, SC_SERIALIZABLE, SC_EXTERNALIZABLE, SC_BLOCK_DATA, SC_ENUM = 0x01, 0x02, 0x04, 0x08, 0x10


class JavaSerError(ValueError):
    pass


@dataclass
class ClassDesc:
    name: str
    uid: int
    flags: int
    fields: list[tuple[str, str, str | None]]     # (typecode, name, class name for L/[)
    super: "ClassDesc | None"

    def chain(self) -> list["ClassDesc"]:
        """Superclass first, as the stream orders class data."""
        out, c = [], self
        while c is not None:
            out.append(c)
            c = c.super
        return out[::-1]


@dataclass
class JavaObject:
    cls: ClassDesc
    # per class in the hierarchy: default field values (dict) and/or the writeObject annotation
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    annotations: dict[str, list] = field(default_factory=dict)

    @property
    def class_name(self) -> str:
        return self.cls.name

    def items(self, class_name: str | None = None) -> "Items":
        return Items(self.annotations.get(class_name or self.cls.name, []))

    def __repr__(self) -> str:
        return f"JavaObject({self.cls.name})"


@dataclass
class JavaArray:
    cls: ClassDesc
    values: list


class Items:
    """Sequential reader over a writeObject annotation: objects interleaved with block-data bytes."""

    def __init__(self, items: list) -> None:
        self.items = list(items)
        self.i = 0
        self.buf = b""
        self.pos = 0

    def _bytes(self, n: int) -> bytes:
        while len(self.buf) - self.pos < n:
            if self.i < len(self.items) and isinstance(self.items[self.i], (bytes, bytearray)):
                self.buf = self.buf[self.pos:] + bytes(self.items[self.i])
                self.pos = 0
                self.i += 1
            else:
                raise JavaSerError(f"primitive read of {n} bytes past the block data")
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def read_object(self) -> Any:
        if self.pos < len(self.buf):
            raise JavaSerError("object read with unread block data pending")
        if self.i >= len(self.items):
            raise JavaSerError("object read past the end of the annotation")
        v = self.items[self.i]
        if isinstance(v, (bytes, bytearray)):
            raise JavaSerError("expected an object, found block data")
        self.i += 1
        return v

    def read_string(self) -> str | None:
        v = self.read_object()
        if v is not None and not isinstance(v, str):
            raise JavaSerError(f"expected a string, got {v!r}")
        return v

    def read_bool(self) -> bool:
        return self._bytes(1)[0] != 0

    def read_byte(self) -> int:
        return struct.unpack(">b", self._bytes(1))[0]

    def read_short(self) -> int:
        return struct.unpack(">h", self._bytes(2))[0]

    def read_int(self) -> int:
        return struct.unpack(">i", self._bytes(4))[0]

    def read_long(self) -> int:
        return struct.unpack(">q", self._bytes(8))[0]

    def read_double(self) -> float:
        return struct.unpack(">d", self._bytes(8))[0]

    def read_float(self) -> float:
        return struct.unpack(">f", self._bytes(4))[0]

    def read_utf(self) -> str:
        n = struct.unpack(">H", self._bytes(2))[0]
        return self._bytes(n).decode("utf-8", "replace")


class _Parser:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.p = 0
        self.handles: list[Any] = []

    def u(self, fmt: str) -> Any:
        n = struct.calcsize(fmt)
        if self.p + n > len(self.d):
            raise JavaSerError("unexpected end of stream")
        v = struct.unpack_from(fmt, self.d, self.p)
        self.p += n
        return v[0] if len(v) == 1 else v

    def utf(self) -> str:
        n = self.u(">H")
        s = self.d[self.p:self.p + n]
        self.p += n
        return _mutf8(s)

    def new_handle(self, obj: Any) -> int:
        self.handles.append(obj)
        return len(self.handles) - 1

    def content(self, allow_end: bool = False) -> Any:
        tc = self.u(">B")
        if tc == TC_OBJECT:
            return self.new_object()
        if tc == TC_STRING:
            s = self.utf()
            self.new_handle(s)
            return s
        if tc == TC_LONGSTRING:
            n = self.u(">Q")
            s = _mutf8(self.d[self.p:self.p + n])
            self.p += n
            self.new_handle(s)
            return s
        if tc == TC_NULL:
            return None
        if tc == TC_REFERENCE:
            h = self.u(">I") - BASE_HANDLE
            try:
                return self.handles[h]
            except IndexError:
                raise JavaSerError(f"bad back-reference {h:#x}") from None
        if tc == TC_BLOCKDATA:
            n = self.u(">B")
            b = self.d[self.p:self.p + n]
            self.p += n
            return bytes(b)
        if tc == TC_BLOCKDATALONG:
            n = self.u(">I")
            b = self.d[self.p:self.p + n]
            self.p += n
            return bytes(b)
        if tc == TC_CLASSDESC or tc == TC_PROXYCLASSDESC:
            self.p -= 1
            return self.class_desc()
        if tc == TC_ARRAY:
            return self.new_array()
        if tc == TC_CLASS:
            cd = self.class_desc()
            self.new_handle(cd)
            return cd
        if tc == TC_ENUM:
            cd = self.class_desc()
            h = self.new_handle(None)
            name = self.content()
            self.handles[h] = (cd.name, name)
            return self.handles[h]
        if tc == TC_RESET:
            self.handles.clear()
            return self.content(allow_end)
        if tc == TC_ENDBLOCKDATA and allow_end:
            return _END
        raise JavaSerError(f"unsupported type code {tc:#x} at {self.p - 1}")

    def class_desc(self) -> ClassDesc | None:
        tc = self.u(">B")
        if tc == TC_NULL:
            return None
        if tc == TC_REFERENCE:
            return self.handles[self.u(">I") - BASE_HANDLE]
        if tc == TC_PROXYCLASSDESC:
            h = self.new_handle(None)
            n = self.u(">i")
            names = [self.utf() for _ in range(n)]
            self.annotation()
            cd = ClassDesc("Proxy" + repr(names), 0, SC_SERIALIZABLE, [], None)
            self.handles[h] = cd
            cd.super = self.class_desc()
            return cd
        if tc != TC_CLASSDESC:
            raise JavaSerError(f"expected a class descriptor, got {tc:#x}")
        name = self.utf()
        uid = self.u(">q")
        h = self.new_handle(None)
        flags = self.u(">B")
        nf = self.u(">H")
        fields = []
        for _ in range(nf):
            t = chr(self.u(">B"))
            fname = self.utf()
            cname = None
            if t in "L[":
                cname = self.content()
            fields.append((t, fname, cname))
        cd = ClassDesc(name, uid, flags, fields, None)
        self.handles[h] = cd
        self.annotation()
        cd.super = self.class_desc()
        return cd

    def annotation(self) -> list:
        out = []
        while True:
            v = self.content(allow_end=True)
            if v is _END:
                return out
            out.append(v)

    def prim(self, t: str) -> Any:
        return {
            "B": lambda: self.u(">b"), "C": lambda: chr(self.u(">H")), "D": lambda: self.u(">d"),
            "F": lambda: self.u(">f"), "I": lambda: self.u(">i"), "J": lambda: self.u(">q"),
            "S": lambda: self.u(">h"), "Z": lambda: self.u(">B") != 0,
        }[t]()

    def new_object(self) -> JavaObject:
        cd = self.class_desc()
        obj = JavaObject(cd)
        self.new_handle(obj)
        for c in cd.chain():
            if c.flags & SC_EXTERNALIZABLE:
                if not c.flags & SC_BLOCK_DATA:
                    raise JavaSerError(f"{c.name}: protocol-1 externalizable data is not supported")
                obj.annotations[c.name] = self.annotation()
                continue
            if c.flags & SC_SERIALIZABLE:
                vals = {}
                if not c.flags & SC_WRITE_METHOD:
                    for t, fname, _ in c.fields:
                        vals[fname] = self.content() if t in "L[" else self.prim(t)
                    obj.fields[c.name] = vals
                else:
                    # ErosLink never calls defaultWriteObject(); a class that did would be ambiguous here
                    obj.annotations[c.name] = self.annotation()
        return obj

    def new_array(self) -> JavaArray:
        cd = self.class_desc()
        arr = JavaArray(cd, [])
        self.new_handle(arr)
        n = self.u(">i")
        t = cd.name[1] if cd and len(cd.name) > 1 else "L"
        for _ in range(n):
            arr.values.append(self.content() if t in "L[" else self.prim(t))
        return arr


_END = object()


def _mutf8(b: bytes) -> str:
    # modified UTF-8: NUL is 0xC0 0x80; everything else decodes as UTF-8
    return bytes(b).replace(b"\xc0\x80", b"\x00").decode("utf-8", "replace")


def parse(data: bytes) -> list:
    """Top-level stream contents (objects, strings, block-data bytes), in order."""
    p = _Parser(data)
    if p.u(">H") != STREAM_MAGIC:
        raise JavaSerError("not a Java serialization stream (bad magic)")
    ver = p.u(">H")
    if ver != 5:
        raise JavaSerError(f"unsupported stream version {ver}")
    out = []
    while p.p < len(data):
        out.append(p.content())
    return out


__all__ = ["ClassDesc", "Items", "JavaArray", "JavaObject", "JavaSerError", "parse"]
