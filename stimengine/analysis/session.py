"""Load and summarize one engine session directory.

Formats (see stimengine/session.py):
  telemetry.jsonl  {"t": s_since_start, "ts": iso, "kind": str, "data": {...}}
  commands.jsonl   {"t", "ts", "kind", "source", ...}
  meta.json        {"started","ended","summary":{...},"config":{...},"firmware","transport","mode","end_reason",...}

Pure python + numpy. Tolerant of a truncated last line (the engine may die mid-write on a fault).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ELECTRODES = ("a", "b", "c", "d")

# Firmware reports these when nothing is connected (open circuit): skin ~294 ohm placeholder, output ~5 ohm.
OPEN_SKIN_OHM = 294.0
OPEN_OUT_OHM = 5.0
OPEN_TOL = 0.02  # relative

EVENT_KINDS = (
    "signal_start", "signal_stop", "arm", "disarm", "deadman_start", "deadman_clear", "fault",
    "set_master", "set_api_volume", "pattern_start", "pattern_stop", "lease", "beat_first",
)


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # truncated tail (fault mid-write) or garbage line
            if isinstance(rec, dict):
                out.append(rec)
    return out


def latest_session_dir(root: str | Path = "sessions") -> Path | None:
    root = Path(root)
    if not root.exists():
        return None
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and (p / "meta.json").exists())
    return dirs[-1] if dirs else None


@dataclass
class Series:
    """A (t, value) time series."""

    t: np.ndarray
    v: np.ndarray

    def __len__(self) -> int:
        return int(self.t.size)

    @property
    def empty(self) -> bool:
        return self.t.size == 0

    def stats(self) -> dict[str, Any]:
        if self.empty:
            return {"n": 0, "min": None, "max": None, "mean": None, "last": None}
        v = self.v[np.isfinite(self.v)]
        if v.size == 0:
            return {"n": int(self.t.size), "min": None, "max": None, "mean": None, "last": None}
        return {
            "n": int(self.t.size),
            "min": float(v.min()),
            "max": float(v.max()),
            "mean": float(v.mean()),
            "last": float(v[-1]),
        }


@dataclass
class Session:
    dir: Path
    meta: dict[str, Any]
    commands: list[dict]
    telemetry: list[dict]
    _by_kind: dict[str, list[dict]] = field(default_factory=dict, repr=False)

    # ---- basics ------------------------------------------------------------------------------------

    @property
    def duration_s(self) -> float:
        d = (self.meta.get("summary") or {}).get("duration_s")
        if d:
            return float(d)
        ts = [r.get("t", 0.0) for r in self.telemetry] + [r.get("t", 0.0) for r in self.commands]
        return float(max(ts)) if ts else 0.0

    @property
    def transport(self) -> str:
        cfg_dev = (self.meta.get("config") or {}).get("device", {})
        return str(self.meta.get("transport") or cfg_dev.get("transport", "?"))

    @property
    def mode(self) -> str:
        return str(self.meta.get("mode") or "?")

    @property
    def firmware(self) -> str:
        return str(self.meta.get("firmware") or "?")

    @property
    def end_reason(self) -> str:
        return str(self.meta.get("end_reason") or ("open" if not self.meta.get("ended") else "?"))

    @property
    def max_amps_commanded(self) -> float:
        s = (self.meta.get("summary") or {}).get("max_amps_commanded")
        if s is not None:
            return float(s)
        a = [r.get("amps", 0.0) for r in self.commands if r.get("kind") == "amps"]
        return float(max(a)) if a else 0.0

    def _index(self) -> dict[str, list[dict]]:
        if not self._by_kind and self.telemetry:
            for r in self.telemetry:
                self._by_kind.setdefault(str(r.get("kind", "?")), []).append(r)
        return self._by_kind

    def kind(self, kind: str) -> list[dict]:
        return self._index().get(kind, [])

    def kinds(self) -> dict[str, int]:
        return {k: len(v) for k, v in sorted(self._index().items(), key=lambda kv: -len(kv[1]))}

    # ---- series ------------------------------------------------------------------------------------

    def series(self, kind: str, path: str | Iterable[str]) -> Series:
        """Extract data[path] (dotted or list path) for every record of `kind`."""
        keys = path.split(".") if isinstance(path, str) else list(path)
        ts, vs = [], []
        for r in self.kind(kind):
            d: Any = r.get("data", {})
            ok = True
            for k in keys:
                if isinstance(d, dict) and k in d:
                    d = d[k]
                else:
                    ok = False
                    break
            if ok and isinstance(d, (int, float)) and not isinstance(d, bool):
                ts.append(float(r.get("t", 0.0)))
                vs.append(float(d))
        return Series(np.asarray(ts, dtype=float), np.asarray(vs, dtype=float))

    def amps_commanded(self) -> Series:
        """Commanded waveform amplitude (A) from commands.jsonl (step series, held until next)."""
        ts, vs = [], []
        for r in self.commands:
            if r.get("kind") == "amps" and "amps" in r:
                ts.append(float(r.get("t", 0.0)))
                vs.append(float(r["amps"]))
        return Series(np.asarray(ts, dtype=float), np.asarray(vs, dtype=float))

    def rms_currents(self) -> dict[str, Series]:
        return {e: self.series("currents", f"rms_{e}") for e in ELECTRODES}

    def peak_currents(self) -> dict[str, Series]:
        return {e: self.series("currents", f"peak_{e}") for e in ELECTRODES}

    def skin_resistance(self) -> dict[str, Series]:
        return {e: self.series("skin_resistance", f"resistance_{e}") for e in ELECTRODES}

    def output_resistance(self) -> dict[str, Series]:
        return {e: self.series("output_resistance", f"resistance_{e}") for e in ELECTRODES}

    def actual_pulse_frequency(self) -> Series:
        return self.series("signal_stats", "actual_pulse_frequency")

    def v_drive(self) -> Series:
        return self.series("signal_stats", "v_drive")

    def battery_voltage(self) -> Series:
        return self.series("battery", "battery_voltage")

    def battery_soc(self) -> Series:
        return self.series("battery", "battery_soc")

    def device_volume(self) -> Series:
        # proto3 default (0.0) is omitted by MessageToDict -> an empty data dict means volume 0.0
        ts, vs = [], []
        for r in self.kind("device_volume"):
            d = r.get("data") or {}
            ts.append(float(r.get("t", 0.0)))
            vs.append(float(d.get("volume", 0.0)))
        return Series(np.asarray(ts, dtype=float), np.asarray(vs, dtype=float))

    def temp_stm32(self) -> Series:
        s = self.series("system_stats", "focstimv3.temp_stm32")
        return s if not s.empty else self.series("system_stats", "esc1.temp_stm32")

    # ---- derived ----------------------------------------------------------------------------------

    @staticmethod
    def contact_state(skin: Series) -> str:
        """'open' if the firmware's open-circuit placeholder dominates, 'contact' if real values, 'n/a' if none."""
        if skin.empty:
            return "n/a"
        v = skin.v[np.isfinite(skin.v)]
        if v.size == 0:
            return "n/a"
        open_mask = np.abs(v - OPEN_SKIN_OHM) <= OPEN_SKIN_OHM * OPEN_TOL
        frac_open = float(open_mask.mean())
        if frac_open > 0.95:
            return "open"
        if frac_open < 0.5:
            return "contact"
        return "mixed"

    def events(self) -> list[dict]:
        """Timeline of state-changing commands (arm/disarm/deadman/fault/...), plus firmware messages."""
        ev = []
        for r in self.commands:
            k = r.get("kind")
            if k in EVENT_KINDS:
                extra = {kk: vv for kk, vv in r.items() if kk not in ("t", "ts", "kind", "source")}
                ev.append({"t": float(r.get("t", 0.0)), "kind": k, "source": r.get("source"), "detail": extra})
        for r in self.kind("debug_string"):
            msg = (r.get("data") or {}).get("message")
            if msg:
                ev.append({"t": float(r.get("t", 0.0)), "kind": "firmware", "source": "device",
                           "detail": {"message": msg}})
        for r in self.kind("boot"):
            ev.append({"t": float(r.get("t", 0.0)), "kind": "firmware_boot", "source": "device", "detail": {}})
        ev.sort(key=lambda e: e["t"])
        return ev

    def notification_rates(self) -> dict[str, dict[str, float]]:
        """Per kind: count, rate/s, and longest gap between consecutive records."""
        out: dict[str, dict[str, float]] = {}
        dur = max(self.duration_s, 1e-9)
        for k, recs in self._index().items():
            ts = np.asarray([float(r.get("t", 0.0)) for r in recs], dtype=float)
            gap = float(np.diff(ts).max()) if ts.size > 1 else float("nan")
            out[k] = {"count": int(ts.size), "rate_hz": ts.size / dur, "max_gap_s": gap}
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["count"]))

    def command_counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in self.commands:
            k = str(r.get("kind", "?"))
            c[k] = c.get(k, 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    def latency_samples(self) -> np.ndarray:
        """Per-axis-move latency (ms) if the session recorded it (soak.py writes 'latency' command records)."""
        v = [float(r["latency_ms"]) for r in self.commands if r.get("kind") == "latency" and "latency_ms" in r]
        return np.asarray(v, dtype=float)

    # ---- summary ------------------------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        skin = self.skin_resistance()
        outr = self.output_resistance()
        rms = self.rms_currents()
        amps = self.amps_commanded()
        s: dict[str, Any] = {
            "dir": str(self.dir),
            "started": self.meta.get("started"),
            "ended": self.meta.get("ended"),
            "duration_s": round(self.duration_s, 2),
            "transport": self.transport,
            "mode": self.mode,
            "firmware": self.firmware,
            "board": self.meta.get("board"),
            "end_reason": self.end_reason,
            "faulted": bool(self.meta.get("faulted")),
            "max_amps_commanded": self.max_amps_commanded,
            "updates_sent": self.meta.get("updates_sent"),
            "max_latency_ms": (self.meta.get("max_latency_s") or 0.0) * 1000.0,
            "commands": self.command_counts(),
            "notifications": self.notification_rates(),
            "amps_commanded": amps.stats(),
            "rms_current": {e: rms[e].stats() for e in ELECTRODES},
            "skin_resistance": {e: skin[e].stats() for e in ELECTRODES},
            "output_resistance": {e: outr[e].stats() for e in ELECTRODES},
            "contact": {e: self.contact_state(skin[e]) for e in ELECTRODES},
            "actual_pulse_hz": self.actual_pulse_frequency().stats(),
            "v_drive": self.v_drive().stats(),
            "battery_v": self.battery_voltage().stats(),
            "battery_soc": self.battery_soc().stats(),
            "device_volume": self.device_volume().stats(),
            "temp_stm32": self.temp_stm32().stats(),
            "events": self.events(),
        }
        lat = self.latency_samples()
        if lat.size:
            s["latency_ms"] = percentiles(lat)
        return s


def percentiles(samples: np.ndarray | list[float], thresholds_ms=(50, 100, 250, 500)) -> dict[str, Any]:
    """p50/p90/p99/max/mean and count over each threshold."""
    a = np.asarray(samples, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0}
    out: dict[str, Any] = {
        "n": int(a.size),
        "p50": float(np.percentile(a, 50)),
        "p90": float(np.percentile(a, 90)),
        "p99": float(np.percentile(a, 99)),
        "max": float(a.max()),
        "mean": float(a.mean()),
        "over": {str(th): int((a > th).sum()) for th in thresholds_ms},
    }
    return out


def bucketize(series: Series, duration_s: float, buckets: int = 60, how: str = "mean") -> np.ndarray:
    """Resample a series into `buckets` equal time bins over [0, duration]; NaN where empty."""
    out = np.full(buckets, np.nan)
    if series.empty or duration_s <= 0:
        return out
    idx = np.clip((series.t / duration_s * buckets).astype(int), 0, buckets - 1)
    for b in range(buckets):
        sel = series.v[idx == b]
        sel = sel[np.isfinite(sel)]
        if sel.size:
            out[b] = sel.max() if how == "max" else sel.mean()
    return out


_BARS = "▁▂▃▄▅▆▇█"


def sparkline(values: np.ndarray | list[float], lo: float | None = None, hi: float | None = None) -> str:
    """Unicode sparkline; NaN -> space. lo/hi default to the data range (flat -> all low bars)."""
    a = np.asarray(values, dtype=float)
    fin = a[np.isfinite(a)]
    if fin.size == 0:
        return " " * a.size
    lo = float(fin.min()) if lo is None else lo
    hi = float(fin.max()) if hi is None else hi
    span = hi - lo
    chars = []
    for v in a:
        if not np.isfinite(v):
            chars.append(" ")
        elif span <= 0:
            chars.append(_BARS[0])
        else:
            k = int(round((v - lo) / span * (len(_BARS) - 1)))
            chars.append(_BARS[max(0, min(len(_BARS) - 1, k))])
    return "".join(chars)


def load_session(path: str | Path, root: str | Path = "sessions") -> Session:
    """Load sessions/<stamp>/ ('latest' resolves against `root`)."""
    p = Path(path)
    if str(path) == "latest":
        found = latest_session_dir(root)
        if found is None:
            raise FileNotFoundError(f"no sessions under {root}")
        p = found
    if not p.is_dir():
        raise FileNotFoundError(p)
    meta: dict[str, Any] = {}
    mp = p / "meta.json"
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    return Session(
        dir=p,
        meta=meta,
        commands=_read_jsonl(p / "commands.jsonl"),
        telemetry=_read_jsonl(p / "telemetry.jsonl"),
    )
