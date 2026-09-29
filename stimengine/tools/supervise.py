"""Keep the engine running across box trips: restart `serve` after a fault until the box answers again.

    python -m stimengine.tools.supervise --serial COM13 --mode fourphase

An over-current trip latches the box (halt until power-cycle) and the engine exits with a FAULT. This supervisor then
restarts the engine every few seconds. While the box is still latched each start times out ("start failed"); once
PlaStim power-cycles the box the next start connects. Every start comes up DISARMED with master 0, and foc312
restores its setup (pattern, output, routes, shape) with the levels at 0: output only after ARM on the page, which
slow-starts. So the supervisor never brings current back by itself.

Stop: POST http://127.0.0.1:<api port>/stop (the engine exits cleanly, rc 0, and is not restarted), or create
sessions/_logs/supervise.stop before the engine exits. Killing only this process leaves the engine running. An engine
that exits cleanly (stopped via the API, rc 0) is not restarted either.
Logs: sessions/_logs/serve-out.log / serve-err.log (appended, one "=== start" header per run); each run's FAULT
and trip report ("biphasic trip:" lines) are in serve-err.log.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RESTART_DELAY_S = 3.0          # between runs (the box answers ~2 s after power-on)
STOP_FILE = "supervise.stop"


def log(msg: str, fh) -> None:
    line = f"[supervise {datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default="sessions/_logs", help="log directory (relative to the working directory)")
    args, serve_args = ap.parse_known_args(argv)
    logs = Path(args.logs)
    logs.mkdir(parents=True, exist_ok=True)
    stop = logs / STOP_FILE
    stop.unlink(missing_ok=True)
    cmd = [sys.executable, "-m", "stimengine.tools.serve", *serve_args]
    runs = 0
    with open(logs / "serve-out.log", "a", encoding="utf-8") as out, \
            open(logs / "serve-err.log", "a", encoding="utf-8") as err:
        log(f"supervising: {' '.join(cmd)}", err)
        while True:
            runs += 1
            header = f"=== start {runs} {datetime.now():%Y-%m-%d %H:%M:%S} ==="
            out.write(header + "\n"); out.flush()
            err.write(header + "\n"); err.flush()
            child = subprocess.Popen(cmd, stdout=out, stderr=err)
            try:
                rc = child.wait()
            except KeyboardInterrupt:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                log("interrupted: engine stopped, not restarting", err)
                return 0
            if stop.exists():
                log(f"engine exited (rc {rc}); stop file present: not restarting", err)
                stop.unlink(missing_ok=True)
                return rc
            if rc == 0:
                log("engine exited cleanly (rc 0): not restarting", err)
                return 0
            why = {2: "start failed (box latched, off, or unplugged)", 3: "FAULT (see the trip report above)"}
            log(f"engine exited rc {rc}: {why.get(rc, 'error')}; restarting in {RESTART_DELAY_S:.0f} s", err)
            for _ in range(int(RESTART_DELAY_S * 10)):
                if stop.exists():
                    log("stop file present: not restarting", err)
                    stop.unlink(missing_ok=True)
                    return rc
                time.sleep(0.1)


if __name__ == "__main__":
    raise SystemExit(main())
