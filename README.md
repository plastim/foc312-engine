# PlaStim foc312 engine

The PC app for **FOC-Stim** boxes running the **foc312** firmware: ET-312-style patterns on two independent
channels, any wiring and polarity, several pulse shapes, the full safety stack, and the **M5 remote**.

Everything runs on your own computer. Nothing is sent anywhere.

| | |
|---|---|
| **Hub** (`http://127.0.0.1:8320`) | find your boxes and remote, flash firmware, connect, settings |
| **Player** | patterns, levels, MA, routing, pulse shapes; a **Pop out** always-on-top mini panel |
| **M5 remote** | a handheld that plays the same patterns without the computer |
| **Hotkeys** | **Pause** = STOP from anywhere (Windows); Ctrl+Alt+arrows change the levels |

> **Status:** early and actively developed. Windows is the tested platform; Linux and macOS should work but are
> not tested yet.

## What you need

- A **FOC-Stim V4** box ([diglet48/FOC-Stim](https://github.com/diglet48/FOC-Stim)). The app flashes it with the
  **foc312** firmware ([plastim/foc312](https://github.com/plastim/foc312)) for you.
- A USB data cable (a charge-only cable shows nothing).
- Optional: the **M5 remote**, the [OSSM M5 Remote](https://github.com/ortlof/OSSM-M5-Remote) hardware with the
  [foc312-m5remote](https://github.com/plastim/foc312-m5remote) firmware.
- Python 3.13 (a one-click installer is planned).

## Install

```powershell
git clone https://github.com/plastim/foc312-engine
cd foc312-engine
py -3.13 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

On Linux / macOS use `python3.13 -m venv venv` and `venv/bin/python`. On Linux, add yourself to the `dialout` group
to use serial ports.

## Run

```powershell
.\venv\Scripts\python.exe -m stimengine.app
```

The hub opens in your browser. The **Play** tab walks you through the first session:

1. **Plug the box in** with USB, switch it on, press **Detect**.
2. **Firmware:** the box needs foc312 v7 or later. If it shows *stock*, flash it on the **Boxes** tab (switch the
   M5 remote off and take the electrodes off first).
3. **Connect** the engine to the box.
4. **Open the player:** pick a pattern, turn the box's own knob low (it is the master limit), put the electrodes on,
   press **ARM** (the output ramps up slowly from zero), raise Level A and B a little at a time.
5. **STOP** any time: the red button, Space in the player, or Pause anywhere.

Quit with Ctrl+C in the terminal: the engine brings the output to zero and stops the box first.

## Safety

- Never place electrodes on or across the chest, head or neck; keep the current path below the waist.
- Do not use with a pacemaker or other implanted electronics, a heart condition, epilepsy, or when pregnant.
- Start with everything at zero and increase slowly. Stop at any pain or discomfort.
- Never while driving, operating machinery, asleep, or in water.
- If the box stops by itself (a safety trip), switch it off and on and start again from zero.

The engine enforces its own limits on top of the box's: a hard current cap, a slow start on every ARM, a deadman
(no control for 2 s ramps the output to zero), and sensor inputs that can only lower the output. The foc312
firmware keeps the FOC-Stim's own safety envelope (self-test, per-pulse over-current stop, 0.2 A body cap, 4 s comms
keepalive). **This is not a medical device. You use it at your own risk.**

## Patterns

The player and the remote use three kinds of patterns:

- **The ET-312's 18 built-in modes** (Waves, Stroke, Climb, ...). They belong to ErosTek and are **not included**:
  extract them from your own ET-312's firmware file on the hub's **M5 remote** tab (the data stays on your computer).
- **ErosLink routines** (`.elk` files): point the app at your ErosLink folder.
- **PlaStim routines** in `routines/`.

Not affiliated with or endorsed by ErosTek. ET-312 is a trademark of its owner.

## Firmware updates

The hub checks the [foc312 releases](https://github.com/plastim/foc312/releases) and
[foc312-m5remote releases](https://github.com/plastim/foc312-m5remote/releases) when you ask it to. Releases are
signed by PlaStim; the app only flashes firmware whose signature and checksum match, and never flashes on its own.

## Development

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

Sibling projects, each its own repository, cloned next to this one:

- [foc312](https://github.com/plastim/foc312): the firmware (a fork of diglet48/FOC-Stim).
- [foc312-m5remote](https://github.com/plastim/foc312-m5remote): the remote. Its C core is tested from this
  project's suite against the Python engine.

`stimengine/paths.py` finds them (or set `FOC312_FIRMWARE_DIR` / `FOC312_M5REMOTE_DIR`).

## Credits

- [diglet48](https://github.com/diglet48): the FOC-Stim hardware and firmware, and
  [restim](https://github.com/diglet48/restim) (MIT), included in `vendor/restim/` as a reference.
- [ortlof](https://github.com/ortlof/OSSM-M5-Remote): the OSSM M5 Remote hardware (CC BY-SA 4.0).
- The buttshock community, for what is publicly known about the ET-312.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md): free to use, study, modify and share for any noncommercial purpose. For
commercial use, talk to PlaStim. Contributions are welcome under a contributor agreement (see `CONTRIBUTING.md`).
Third-party parts keep their own licenses (`NOTICE`).

Support the project: [Patreon](https://www.patreon.com/plastim) · [PlaStim store](https://plastim.net/)
