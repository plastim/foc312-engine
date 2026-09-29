"""Jobs: the command-line tools (tools/flash_focstim.py, esptool, stimengine.remote) run as subprocesses, their output
captured line by line for the page. At most one flashing job at a time: two flashers on one machine is how a port
gets fought over mid-write."""
from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
import logging
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)
MAX_LINES = 2000
KEEP_JOBS = 30


class JobBusy(RuntimeError):
    """A flashing job is already running."""


class Job:
    def __init__(self, job_id: int, kind: str, cmd: list[str], flash: bool,
                 on_done: Callable[["Job"], None] | None = None) -> None:
        self.id, self.kind, self.cmd, self.flash = job_id, kind, cmd, flash
        self.on_done = on_done              # called once it has finished (on the reader thread)
        self.started = time.time()
        self.lines: list[str] = []
        self.rc: int | None = None
        self.state = "running"
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self.proc = subprocess.Popen(self.cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                         encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
        except OSError as exc:
            self._add(f"could not start: {exc}")
            self.rc, self.state = -1, "failed"
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _add(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LINES:
                del self.lines[: len(self.lines) - MAX_LINES]

    def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            # progress bars rewrite one line with \r: keep the latest state of it
            self._add(raw.rstrip("\n").split("\r")[-1])
        self.rc = self.proc.wait()
        self.state = "ok" if self.rc == 0 else "failed"
        # a plain last line: esptool's own ends at "Hard resetting via RTS pin...", which doesn't read as done
        self._add("Done." if self.rc == 0 else f"Failed (exit code {self.rc}).")
        if self.on_done is not None:
            try:
                self.on_done(self)
            except Exception as exc:  # noqa: BLE001 - a hook must never take the job down with it
                logger.warning("job %s: finish hook failed: %s", self.id, exc)

    def running(self) -> bool:
        return self.state == "running"

    def view(self) -> dict:
        with self._lock:
            lines = list(self.lines)
        return {"id": self.id, "kind": self.kind, "state": self.state, "rc": self.rc, "log": lines,
                "started": self.started}


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self.on_done: dict[str, Callable[[Job], None]] = {}     # per job kind, e.g. record a remote load

    def start(self, kind: str, cmd: list[str], flash: bool = False) -> Job:
        if flash and any(j.flash and j.running() for j in self.jobs.values()):
            raise JobBusy("a flashing job is already running")
        job = Job(next(self._ids), kind, cmd, flash, self.on_done.get(kind))
        self.jobs[job.id] = job
        for old in sorted(self.jobs)[:-KEEP_JOBS]:
            if not self.jobs[old].running():
                del self.jobs[old]
        job.start()
        return job

    def get(self, job_id: int) -> Job | None:
        return self.jobs.get(job_id)

    def recent(self) -> list[dict]:
        out = []
        for j in sorted(self.jobs.values(), key=lambda j: -j.id):
            v = j.view()
            v["log"] = v["log"][-5:]
            out.append(v)
        return out
