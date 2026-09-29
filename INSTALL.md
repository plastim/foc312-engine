# Installing the PlaStim foc312 engine

Step by step, for someone who has never used Python or Git. About 15 minutes. Windows 10 or 11 is the tested
platform; Linux and macOS notes are at the end.

**Read the [safety notes](README.md#safety) before you put electrodes on.**

## What you need

- A **FOC-Stim V4** box ([diglet48/FOC-Stim](https://github.com/diglet48/FOC-Stim)), charged.
- A **USB data cable** for the box. A charge-only cable powers things but shows nothing on the computer; if the box
  never shows up, try another cable first.
- A computer with an internet connection (for the install and for firmware downloads; the app itself runs offline).
- Optional: the **M5 remote** ([OSSM M5 Remote](https://github.com/ortlof/OSSM-M5-Remote) hardware) and a USB-C data
  cable for it.

## 1. Install Python 3.13

1. Go to <https://www.python.org/downloads/windows/> and download the latest **Python 3.13** "Windows installer
   (64-bit)".
2. Run it. On the first screen tick **"Add python.exe to PATH"**, then click **Install Now**.
3. Check it: open **PowerShell** (Start menu, type `powershell`, Enter) and type:

   ```powershell
   py -3.13 --version
   ```

   It should print `Python 3.13.x`. If it says the command is not found, run the installer again and choose
   **Modify**, making sure the **py launcher** is ticked.

## 2. Install Git

The app is downloaded with Git, and one of its parts (the box flasher) is installed from GitHub.

1. Go to <https://git-scm.com/download/win> and run the installer. The default answers on every screen are fine.
2. **Close PowerShell and open a new one** (so it sees Git), then check:

   ```powershell
   git --version
   ```

## 3. Download the app

In PowerShell (each line is one command; press Enter after each):

```powershell
cd $HOME\Documents
git clone https://github.com/plastim/foc312-engine
cd foc312-engine
```

The app now lives in `Documents\foc312-engine`. Everything it keeps (settings, extracted patterns) stays in that
folder or in your user folder; nothing is sent anywhere.

## 4. Install what the app needs

Still in PowerShell, inside `foc312-engine`:

```powershell
py -3.13 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

The first line makes a private Python just for this app (the `venv` folder), so nothing else on your computer is
changed. The second downloads the app's parts into it; it takes a few minutes and prints a lot. It is done when you
get the prompt back and the last lines say `Successfully installed ...`. A red `ERROR` means something went wrong:
see [Troubleshooting](#troubleshooting).

## 5. Start the hub

Double-click **`start-hub.bat`** in the `foc312-engine` folder (make a desktop shortcut to it if you like), or in
PowerShell:

```powershell
.\venv\Scripts\python.exe -m stimengine.app
```

A black window opens (that is the app running; leave it open) and the **hub** opens in your browser at
<http://127.0.0.1:8320>. If the browser does not open by itself, type that address in. To quit, close the black
window or press Ctrl+C in it: the output is brought to zero and the box stopped first.

The app only listens on this computer (`127.0.0.1`); nothing on your network or outside can reach it.

## 6. Connect the box and put the foc312 firmware on it

The box needs the **foc312** firmware (v7 or later). A box straight from diglet48's firmware shows as **stock**.

1. Plug the box in with the USB data cable and switch it on.
2. In the hub, press **Detect**. The box appears with its firmware version.
3. If it needs firmware: on the **Boxes** tab press **Check for updates**, pick the newest foc312 release, press
   **Flash…**, then **Flash now**. Before you do:
   - **take the electrodes off** (the box's screen says "Firmware update / Remove electrodes!"),
   - **switch the M5 remote off** if you have one: the box's Wi-Fi can disturb a flash.

   The hub checks the release's signature and checksum before flashing, and flashes only when you press the button.
   It takes about a minute; the box restarts by itself.
4. If a flash fails part-way: switch the box off and on and flash again. If the box then no longer starts at all,
   hold its **STM32 boot button** while switching it on, and flash again. A box can always be put back on
   diglet48's stock firmware with restim's firmware updater.

## 7. The first session

The hub's **Play** tab has a checklist that ticks itself as you go:

1. **Connect** the engine to the box.
2. **Open the player.** Pick a pattern.
3. Turn the **box's own knob low**. It is the master limit: nothing the app does can go above it.
4. Put the electrodes on (below the waist; see the safety notes).
5. Press **ARM**. The output ramps up slowly from zero.
6. Raise **Level A** and **Level B** a little at a time. If one pad of a channel feels much stronger than the
   other, try its **⇄** button ([why](README.md#why-flip-the-polarity--a--b)).
7. **STOP** any time: the red button, **Space** in the player, or the **Pause** key anywhere on the computer.

## 8. Patterns

Out of the box the player has the PlaStim routines. Two more groups need files you own:

- **The ET-312's 18 built-in modes** (Waves, Stroke, Climb, ...). They are ErosTek's and are not included. If you own
  an ET-312B (firmware v1.6), save a copy of its firmware from your own box as a `.bin` or `.hex` file (the
  buttshock community's tools can read it over the box's serial link), then on the hub's **M5 remote** tab choose
  the file and press **Extract**. Only the pattern data is kept, on your computer.
- **ErosLink routines.** ErosLink's own routines come from its installer (`ErosLink_Installer.zip`). Read them in
  once, from PowerShell in the `foc312-engine` folder:

  ```powershell
  .\venv\Scripts\python.exe -m stimengine.et312.eroslink_cache --zip C:\path\to\ErosLink_Installer.zip
  ```

  Your own `.elk` files: open `config\engine.toml` in Notepad, find the `[et312]` section at the end, and put your
  folder in quotes, using forward slashes:

  ```toml
  [et312]
  elk_dir = "C:/Users/you/Documents/ErosLink"
  ```

Restart the hub after either change.

## 9. The M5 remote (optional)

The PC app is only needed to **set the remote up**. After that the remote plays the patterns by itself, straight to
the box over Wi-Fi, and the PC can be off. The remote's README has the full walk-through with screenshots and
diagrams: **[setting up the M5 remote](https://github.com/plastim/foc312-m5remote#setting-it-up)**. In short:

1. Plug the remote in with USB-C and switch it on. **Detect** shows it as **M5 remote**.
2. On the **M5 remote** tab, flash the newest foc312-m5remote release (**Flash…**, then **Flash now**; the hub
   checks it the same way).
3. On the same tab, set the Wi-Fi and add your box, then press **Load patterns & settings**. The remote gets the
   patterns and the safety limits from the PC; they are never changed on the remote itself.
4. Recommended: switch on **the remote's own Wi-Fi** and pair the box to it (**Box Wi-Fi**, with the box on USB).
   It is a direct link, much faster than a busy house access point. From then on, **switch the remote on before the
   box**: the box gives up on a network after two tries.

## Updating the app

In PowerShell, in the `foc312-engine` folder:

```powershell
git pull
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `git pull` complains that `config\engine.toml` would be overwritten, copy your `elk_dir` line somewhere, run
`git checkout config\engine.toml`, pull again, and put the line back.

Firmware updates for the box and the remote come through the hub (**Check for updates**), never automatically.

## Troubleshooting

| Problem | Try |
|---|---|
| `py` is not recognized | Reinstall Python with **Add to PATH** and the **py launcher** ticked; open a new PowerShell. |
| `git` is not recognized | Install Git (step 2), then open a **new** PowerShell. |
| pip fails on `stm32loader` | Git is missing or PowerShell was opened before Git was installed. |
| Running scripts is disabled | Use the full commands shown here (`.\venv\Scripts\python.exe ...`); they do not need scripts enabled. |
| Detect shows nothing | Another cable (charge-only cables are common), another USB port, box switched on. Close restim or anything else that has the box's port open. |
| The hub page does not open | Is the black window still open? Is another copy already running? Try <http://127.0.0.1:8320>. |
| The box stops by itself | That is a safety trip. Switch the box off and on and start again from zero. The hub shows the box's trip report. |
| The remote cannot find the box | Remote on first, then the box. Re-pair the box to the remote's Wi-Fi from the hub. |

## Linux and macOS

Not tested yet, but expected to work. Install Python 3.13 and Git with your package manager (or python.org /
`xcode-select --install` on macOS), then:

```bash
git clone https://github.com/plastim/foc312-engine
cd foc312-engine
python3.13 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m stimengine.app
```

On Linux, add yourself to the `dialout` group (`sudo usermod -aG dialout $USER`, then log out and in) to use the
serial ports. The global STOP hotkey is Windows-only for now; STOP in the player works everywhere.
