"""Per-session on-disk record: every notification, every command, and a meta summary.

Layout: sessions/<UTC timestamp>/{telemetry.jsonl, commands.jsonl, meta.json}.
Designed to be called from the engine loop: it never raises (errors are logged and counted).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

logger = logging.getLogger("engine.session")

FLUSH_INTERVAL_S = 1.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SessionLogger:
    """Append-only jsonl writer for one engine session."""

    def __init__(self, root: str | Path = "sessions", name: str | None = None) -> None:
        stamp = name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = Path(root) / stamp
        self.meta: dict[str, Any] = {
            "started": _now_iso(),
            "ended": None,
            "summary": {"max_amps_commanded": 0.0, "commands": 0, "notifications": 0, "duration_s": 0.0},
        }
        self._t0 = time.monotonic()
        self._files: dict[str, Any] = {}
        self._last_flush = time.monotonic()
        self.write_errors = 0
        self._closed = False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._write_meta()
        except OSError:
            logger.exception("session dir %s not writable", self.dir)
            self.write_errors += 1

    # ---- writers ---------------------------------------------------------------------------------

    def set_meta(self, **fields: Any) -> None:
        self.meta.update(fields)
        self._write_meta()

    def log_telemetry(self, kind: str, body: Any) -> None:
        rec = {"t": round(time.monotonic() - self._t0, 4), "ts": _now_iso(), "kind": kind}
        if isinstance(body, Message):
            rec["data"] = MessageToDict(body, preserving_proto_field_name=True)
        elif isinstance(body, dict):
            rec["data"] = body
        else:
            rec["data"] = repr(body)
        self.meta["summary"]["notifications"] += 1
        self._append("telemetry.jsonl", rec)

    def log_command(self, kind: str, source: str = "engine", **fields: Any) -> None:
        rec = {"t": round(time.monotonic() - self._t0, 4), "ts": _now_iso(), "kind": kind, "source": source}
        rec.update(fields)
        s = self.meta["summary"]
        s["commands"] += 1
        amps = fields.get("amps")
        if amps is not None and amps > s["max_amps_commanded"]:
            s["max_amps_commanded"] = float(amps)
        self._append("commands.jsonl", rec)

    def maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self._last_flush >= FLUSH_INTERVAL_S:
            self.flush()

    def flush(self) -> None:
        self._last_flush = time.monotonic()
        for f in self._files.values():
            try:
                f.flush()
            except OSError:
                self.write_errors += 1

    def close(self, reason: str = "normal", **fields: Any) -> None:
        if self._closed:
            return
        self._closed = True
        self.meta["ended"] = _now_iso()
        self.meta["end_reason"] = reason
        self.meta["summary"]["duration_s"] = round(time.monotonic() - self._t0, 2)
        self.meta.update(fields)
        self._write_meta()
        for f in self._files.values():
            try:
                f.close()
            except OSError:
                self.write_errors += 1
        self._files.clear()

    # ---- internals -------------------------------------------------------------------------------

    def _append(self, name: str, rec: dict) -> None:
        if self._closed:
            return
        try:
            f = self._files.get(name)
            if f is None:
                f = open(self.dir / name, "a", encoding="utf-8")  # noqa: SIM115 - kept open for the session
                self._files[name] = f
            f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
            self.maybe_flush()
        except Exception:  # noqa: BLE001 - logging must never break the control loop
            self.write_errors += 1
            if self.write_errors in (1, 10, 100):
                logger.exception("session write failed (%d so far)", self.write_errors)

    def _write_meta(self) -> None:
        try:
            (self.dir / "meta.json").write_text(json.dumps(self.meta, indent=2, default=str), encoding="utf-8")
        except Exception:  # noqa: BLE001
            self.write_errors += 1


__all__ = ["SessionLogger"]
