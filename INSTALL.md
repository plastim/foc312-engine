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

The quickest way: open **PowerShell** (Start menu, type `powershell`, Enter) and type

```powershell
winget install -e --id Python.Python.3.13
```

(The first time, winget asks you to accept its source agreements: type `Y` and Enter.)

Or by hand: on <https://www.python.org/downloads/windows/>, find the newest **Python 3.13.x** in the list (not 3.14)
and download its **"Windows installer (64-bit)"**. Run it; on the first screen tick **"Add python.exe to PATH"**,
then click **Install Now**.

Check it: **close PowerShell and open a new one**, then type:

   ```powershell
   py -3.13 --version
   ```

   It should print `Python 3.13.x`. If it says the command is not found, run the installer again and choose
   **Modify**, making sure the **py launcher** is ticked.

## 2. Install Git

The app is downloaded with Git, and one of its parts (the box flasher) is installed from GitHub.

1. In PowerShell: `winget install -e --id Git.Git`. Or by hand: download the installer from
   <https://git-scm.com/download/win> and run it; the default answers on every screen are fine.
2. **Close PowerShell and open a new one** (so it sees Git), then check:

   ```powershell
   git --version
   ```

## 3. Download the app

In PowerShell (each line is one command; press Enter after each):

```powershell
cd $HOME
git clone https://github.com/plastim/foc312-engine
cd foc312-engine
explorer .
```

The app now lives in your user folder, `C:\Users\<you>\foc312-engine`; the last line opens it in File Explorer
(handy for step 5). It isn't put in Documents on purpose: when cloud sync has moved Documents, it would land in a
folder File Explorer doesn't show as Documents.
Everything it keeps (settings, extracted patterns) stays in that folder or in your user folder; nothing is sent
anywhere.

## 4. Install what the app needs

Still in PowerShell, inside `foc312-engine`:

```powershell
py -3.13 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

The first line makes a private Python just for this app (the `venv` folder), so nothing else on your computer is
changed. The second downloads the app's parts into it; it takes a few minutes and prints a lot. It is done when you
get the prompt back and a line near the end says `Successfully installed ...`. Lines starting `[notice]` after it
(a newer pip is available) can be ignored. A red `ERROR` means something went wrong: see
[Troubleshooting](#troubleshooting).

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

## 6. Connect the box and put the PlaStim firmware on it

The box needs the **PlaStim firmware** (foc312, v7 or later). A box straight from diglet48's firmware shows as **stock**.

1. Plug the box in with the USB data cable and switch it on.
2. In the hub, press **Detect**. The box appears with its firmware version. The first Detect can take up to a minute
   (each USB device is asked what it is); later ones take a second.
3. If it needs firmware, first **take the electrodes off** (the box's screen says "Firmware update / Remove
   electrodes!") and **switch the M5 remote off** if you have one (the box's Wi-Fi can disturb a flash). Then, on
   the **Boxes** tab, in **Box firmware**:
   1. **Check for updates**: the newest foc312 release is listed, marked *latest*.
   2. **Download** next to it. It's checked against PlaStim's signature and added to the **Image** list.
   3. In **Image**, choose it: *PlaStim firmware v…* (recommended).
   4. Tick **"The M5 remote is switched off"**.
   5. **Flash…**, read the confirmation, then **Flash now**.

   It takes about a minute; the box restarts by itself and the log ends with the new firmware's version and
   **Done.**
4. If a flash fails part-way: switch the box off and on and flash again. If the box then no longer starts at all,
   hold its **STM32 boot button** while switching it on, and flash again. A box can always be put back on
   diglet48's stock firmware: **Check for updates** also offers *Stock FOC-Stim* (diglet48's release), or use
   restim's firmware updater.

## 7. The first session

The hub's **Play** tab has a checklist that ticks itself as you go:

1. **Connect** the engine to the box.
2. **Open the player.** Pick a pattern.
3. Turn the **box's own knob low**. It is the master limit: nothing the app does can go above it.
4. Put the electrodes on (below the waist; see the safety notes).
5. Press **ARM**. The output ramps up slowly from zero.
6. Raise **Level A** and **Level B** a little at a time. If one pad of a channel feels much stronger than the
   other, try its **⇄** button ([why](README.md#why-flip-the-polarity)).
7. **STOP** any time: the red button, **Space** in the player, or the **Pause** key anywhere on the computer.

## 8. Patterns

The player starts with the **ET-312 shared routines**: 78 files (over 150 routines) that ET-312 owners wrote and
ErosTek gave away (below). They come with the app, in its `patterns\et312-shared` folder, so there is nothing to
download. The M5 remote gets them with its first **Load patterns & settings**.

**Your own `.elk` files go in My patterns**, a folder inside the app folder: `foc312-engine\my-patterns`. On the hub's
**M5 remote** tab, under **Pattern files**, **My patterns**:

- **Add patterns…** picks `.elk` files to copy in (or drop them onto that box). Each file is checked first; one that
  isn't a readable ErosLink routine file is refused, with the reason.
- **Open folder** opens it in Explorer: copying files in by hand works just as well. The player sees them the next
  time you open its pattern list (no restart), and the M5 remote gets them with its next **Load patterns & settings**.
- A **subfolder** of My patterns becomes a group of its own in the player ("My patterns: *name*"). One level only.
- The list shows every file with its routines; a file that can't be used says why. **Remove** sends a file to the
  Recycle Bin.
- A routine that is already listed elsewhere (say, a copy of one of the shared routines) is listed only once.

The folder is created the first time you use it, with a short `README.txt`. Updating the app (`git pull`) never
touches it.

- **The ET-312's 18 built-in modes** (Waves, Stroke, Climb, ...). They are ErosTek's and are not included. If you own
  an ET-312B (firmware v1.6), save a copy of its firmware from your own box as a `.bin` or `.hex` file (the
  [buttshock](https://github.com/buttshock) community's tools can read it over the box's serial link), then on the
  hub's **M5 remote** tab choose the file and press **Extract**. Only the pattern data is kept, on your computer.
- **ErosLink routines.** ErosLink's own routines come from its installer (`ErosLink_Installer.zip`, from ErosTek;
  if you own an ET-312 you may still have it). Read them in once, from PowerShell in the `foc312-engine` folder:

  ```powershell
  .\venv\Scripts\python.exe -m stimengine.et312.eroslink_cache --zip C:\path\to\ErosLink_Installer.zip
  ```

- **The ET-312 shared routines** (included). In 2011 ErosTek offered a free zip of 78 ErosLink routine files written
  by ET-312 owners, "as-is" ([their post, archived](https://web.archive.org/web/20111208144808/http://blog.erostek.com/2011/01/10/extra-eroslink-routines-free/)).
  The app ships the zip's `.elk` files unchanged, in `patterns\et312-shared` (its
  [README](patterns/et312-shared/README.md) has the zip's SHA-256 and how to check the folder against the archived
  zip). If an older version of the app downloaded them for you, that copy is still read and each routine is listed
  once. 32 of them are the same as ErosLink's own designer examples; the app lists each routine only once.

The built-in modes and ErosLink's own routines show in the player after the box is disconnected and connected again
in the hub; files in My patterns need nothing of the kind.

**Advanced: a folder of your own elsewhere.** Instead of (or as well as) My patterns, the app can read one more
folder, set in `config\engine.toml`: find the `[et312]` section at the end and put the folder in quotes, using forward
slashes. Its routines are listed as **Your routines**. Restart the hub after changing it.

```toml
[et312]
elk_dir = "C:/Users/you/ET-312 routines"
```

## 9. The M5 remote (optional)

The PC app is only needed to **set the remote up**. After that the remote plays the patterns by itself, straight to
the box over Wi-Fi, and the PC can be off. The remote's README has the full walk-through with screenshots and
diagrams: **[setting up the M5 remote](https://github.com/plastim/foc312-m5remote#setting-it-up)**. In short:

1. Plug the remote in with USB-C and switch it on. **Detect** shows it as **M5 remote**.
2. On the **M5 remote** tab, in **M5 remote firmware**: **Check for updates**, **Download** the latest release
   (`v…`; the `radr-v…` releases are for the RADR hardware and show as **[RADR]**), choose it in **Image**, then
   **Flash…** and **Flash now**. The remote must be stopped (not playing). The log ends
   with **Done.** and the remote restarts.
3. In **Settings the M5 remote gets**: leave **The remote and the boxes talk over** on *the remote's own Wi-Fi
   network* (recommended: a direct link, much faster than a busy house access point), make up a network name and a
   password (8 to 63 characters), and with the box plugged in press **Add** (it fills in the box's MAC). Press
   **Save settings**.
4. Press **Load patterns & settings**. The remote gets the patterns and the safety limits from the PC; they are never
   changed on the remote itself. Load again whenever you change a setting or add patterns.
5. Switch the remote **on**, and with the box on USB press **Join the remote's Wi-Fi** in **Box Wi-Fi**. From then
   on, **switch the remote on before the box**: the box gives up on a network after two tries.
6. **Use it:** unplug both, remote on, then the box. Knob 4's press opens the options: with more than one box, pick
   it on the *Box* line (knob 1 moves, knob 1's press changes it). Knob 1's press opens the patterns; pick one. Turn
   the box's own knob low, put the electrodes on, press the **MX button** to start, and raise **Level A** (knob 2) and
   **Level B** (knob 3) slowly: they start at zero. **Knob 1** is the remote's master (a share of the box's own
   knob, starting at 100 %); **knob 4** is MA. The MX button stops, any time. The PC can be off.

## Updating the app

In PowerShell, in the `foc312-engine` folder:

```powershell
git pull
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Your `my-patterns` folder is not part of the download, so updating never changes it. If `git pull` complains that
`config\engine.toml` would be overwritten (you set `elk_dir` there), copy your `elk_dir` line somewhere, run
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
