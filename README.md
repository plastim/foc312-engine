# PlaStim foc312 engine

**The ET-312, on better hardware.**
ET-312 emulation on the FOC-Stim V4.

The **ET-312B** is a long-running e-stim box, still one of the best-known in the hobby. It's loved for its built-in
patterns: Waves, Stroke, Climb, Orgasm and more, each shaping the pulses over time in its own way, with an **MA
(Multi Adjust)** knob that changes how each one moves. Its companion software, ErosLink, lets people write their own patterns
(`.elk` files), and a lot of them have been shared over the years.

foc312 brings that to the **[FOC-Stim V4](https://github.com/diglet48/FOC-Stim)**, a modern open-hardware e-stim
box. It plays the ET-312's patterns and ErosLink routines **with the original timing and behaviour**, closely enough
that they feel like the patterns ET-312 owners know. The three audio modes are the exception (see
[Limitations](#limitations)). It's an independent re-implementation, built from what the e-stim community has
openly documented about the ET-312 over the years. No ErosTek software or data is included: if you own an
ET-312, the app reads the built-in patterns from your own box's firmware file. foc312 is not affiliated with or
endorsed by ErosTek.

Then it plays all of that through hardware the 312 never had:

- **Current-controlled output.** The ET-312 sets a voltage and the current lands wherever your skin puts it, so
  what you feel drifts as the pads warm up and the skin changes. The FOC-Stim measures the current of every pulse
  and corrects the next ones, so you keep getting the charge that was asked for.
- **Four electrodes, any wiring.** Channels A and B each go on **any pair** of the four electrodes, in either
  direction, changed live with no rewiring: the same pad on both channels, a triangle across three pads, A/B split
  across four.
- **Pulse shapes** the 312 can't make: rounded, soft square, triangle, and a continuous taper from rounded towards
  square, all at the same charge per pulse. They make a real difference in feel.
- **Safety in layers.** A hard current cap, slow start, a deadman that ramps to zero if control goes quiet, a
  per-pulse over-current stop in the box, and the box's own knob as a master limit that no software can exceed.
- **Wireless and battery powered**, with the **M5 remote**: a handheld that plays everything by itself, no
  computer needed.
- **Your data stays home.** Everything runs on your own computer, and nothing is sent anywhere.

**Restim still works.** The foc312 firmware keeps every stock FOC-Stim mode, so
[restim](https://github.com/diglet48/restim) (diglet48's app for continuous three- and four-phase waveforms,
funscripts and audio) works on the same box, as before, with no reflashing. Use one or the other: close this app
before opening restim, and the other way round.

### Where it excels

- **Faithful patterns.** The ET-312's modes with their original timing, the MA knob's behaviour in each mode, the
  ramp on a mode change, and the Advanced settings. ErosLink routines play as they would on an ET-312.
- **Steady sensation.** The strength holds as skin and pads change, instead of creeping up as you sweat or fading as
  a pad dries.
- **Wiring as a setting.** Move a channel to another pair of pads, share a pad between both channels, or flip a
  channel's polarity ([why](#why-flip-the-polarity)), instantly, with nothing to unplug.
- **Shapes and balance.** Pulse shapes the 312 can't make. Every pulse is charge-balanced, and the output
  transformers block DC entirely.
- **It tells you what happened.** Live measured current, and a trip report when the box's protection stops the
  output: what was measured, where in the pulse, and what was asked for.

### Made for PlaStim electrodes

[PlaStim electrodes](https://plastim.net/) are built around one idea: **separate contacts for separate nerves, and
you choose which ones each channel drives.**
- The Pinnacle has a corona side and a frenulum side.
- The Clarifier is a bipolar ring.
- The probes have multi-zone heads.
- The ball bar and underball rings reach further.

That's more contacts than a two-channel box can use at once, which is why PlaStim made the
[Switch 1](https://plastim.net/switch-1/). foc312 is the same idea built into the box:

- **Wire four contacts once, then re-route them live.** Plug four PlaStim contacts into the four outputs. Channels A
  and B each go to any pair, changed from the player or the remote without touching a cable. For example, the
  **3-2-1 Blastoff** is A from the corona side to a base ring, and B from the frenulum side to an underball ring.
  Swap the order with a couple of knob turns and you'll feel why the order matters.
- **Shared-contact (tri-phase) setups are built in.** A probe head as the common for both channels (A: head to the
  corona side, B: head to the frenulum side) is just two routes that share an electrode.
- **Switching resets sensitivity.** Nerves left alone for a few seconds come back fresh. Moving a channel to
  another pair and back does that without unplugging anything. A Switch 1 on one of the outputs still adds more
  zones.
- **Surface area does what the design says.** The FOC-Stim holds the current constant, so when the contact area
  changes (a bigger or smaller electrode, or the Switch 1's A+B position), only **where** the sensation
  concentrates changes: towards the smaller electrode. On a voltage-driven box, adding area also changes **how much**
  current flows, so focus and strength shift together.
- **Polarity becomes a real control.** On a Pinnacle-to-base-ring channel, a flip moves the sensation from an
  intense corona focus to a harder shaft sensation ([why](#why-flip-the-polarity)).

### Limitations

- **No audio modes.** The ET-312's Audio 1, 2 and 3 play from a sound input, and there isn't one here. For
  audio-driven stim, use restim.
- **It's not a 312 on the wire.** It doesn't speak the ET-312's serial protocol, so software that controls a real
  312 (ErosLink live, the link cable between two boxes, other 312 apps) can't connect to it. Import `.elk` files
  instead.
- **Strength isn't on the 312's scale.** Levels map to current, up to the cap you set, not to the 312's output
  voltage. Level 50 won't feel like level 50 on a 312: start low.
- **Pulse limits.** Up to 400 pulses per second per channel (a 312 goes a little higher). A and B take turns,
  never pulsing at the same instant, and together they top out around 800 to 1000 pulses per second (fewer with
  wide pulses).
- **Pulse widths are 40 to 400 µs, in 20 µs steps.** The engine adjusts the strength so a smooth sweep still feels
  smooth. Very short pulses come out rounded, so the 312's hard-edged square can only be approached, not copied.
- **Settings reach the box 50 times a second.** The pattern engine runs at the 312's full 244 Hz, but anything
  that changes faster than every 20 ms is sampled.
- **Dry skin plus wide pulses can hit the box's drive limit.** At a high skin resistance the box scales the
  current down. Good pads and a little gel fix it.
- **Two channels.** Four electrodes, but two channels (A and B), as on the 312.

### Where this is going: PlaStim Sedecim

foc312 shows what this approach can do: faithful ET-312 patterns with per-pulse current control, any-pair wiring,
shaped pulses and layered safety, on hardware you can own today. The same ideas are the foundation of **PlaStim
Sedecim**, a 16-electrode box in development that takes them much further.

| | |
|---|---|
| **Hub** (`http://127.0.0.1:8320`) | find your boxes and remote, flash firmware, connect, settings |
| **Player** | patterns, levels, MA, routing, pulse shapes; a **Pop out** always-on-top mini panel |
| **M5 remote** | a handheld that plays the same patterns without the computer |
| **Hotkeys** | **Pause** = STOP from anywhere (Windows); Ctrl+Alt+arrows change the levels |

> **Status:** early and actively developed. Windows is the tested platform; Linux and macOS should work but are
> not tested yet.

## Start here

**New to this? Follow the [installation guide](INSTALL.md)**, step by step from a bare Windows computer to your
first session: Python, the app, flashing the box, the M5 remote, patterns, troubleshooting.

This is the home of the PlaStim foc312 family. You only install this app; it puts the firmware on your devices:

| Project | What it is |
|---|---|
| **foc312-engine** (this) | the PC app: hub, player, safety stack. **Start here.** |
| [foc312](https://github.com/plastim/foc312) | the firmware for the FOC-Stim box (a fork of diglet48's). The hub flashes it. |
| [foc312-m5remote](https://github.com/plastim/foc312-m5remote) | the firmware for the handheld M5 remote. The hub flashes it. |

## What you need

- A **FOC-Stim V4** box ([diglet48/FOC-Stim](https://github.com/diglet48/FOC-Stim)). The app flashes it with the
  **foc312** firmware ([plastim/foc312](https://github.com/plastim/foc312)) for you.
- A USB data cable (a charge-only cable shows nothing).
- Optional: the **M5 remote**, the [OSSM M5 Remote](https://github.com/ortlof/OSSM-M5-Remote) hardware with the
  [foc312-m5remote](https://github.com/plastim/foc312-m5remote) firmware.
- Python 3.13 (a one-click installer is planned).

## Install and run

The [installation guide](INSTALL.md) has every step. In short, with Python 3.13 and Git installed:

```powershell
git clone https://github.com/plastim/foc312-engine
cd foc312-engine
py -3.13 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\start-hub.bat
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

## Why flip the polarity

Each channel has a direction: which of its two pads is negative during the first, stronger half of every pulse.
**The sensation is strongest under the pad that is negative in that first half.** So the player's **⇄ A** and
**⇄ B** buttons move the focus from one pad to the other without moving anything:
- when one pad feels sharp and the other barely at all, a flip swaps them;
- with pads of different sizes or in different places, one direction usually feels better;
- the flip is immediate, so compare the two directions back and forth.

The difference is biggest with the lopsided ET-312-style pulses (a short strong half and a long weak one). Both
directions are equally safe: every pulse is balanced (the same charge each way), so nothing builds up under either
pad.

## Patterns

The player and the remote use three kinds of patterns:

- **The ET-312's 18 built-in modes** (Waves, Stroke, Climb, ...). They belong to ErosTek and are **not included**:
  extract them from your own ET-312's firmware file on the hub's **M5 remote** tab (the data stays on your computer).
- **ErosLink routines** (`.elk` files). A good start is the **ET-312 shared routines** that ErosTek gave away in
  2011 ([the original post](https://web.archive.org/web/20111208144808/http://blog.erostek.com/2011/01/10/extra-eroslink-routines-free/)):
  one button in the hub fetches them from the Internet Archive. Or point the app at a folder of your own; see
  [INSTALL.md, step 8](INSTALL.md#8-patterns).
- **PlaStim routines** in `routines/` (coming).

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
