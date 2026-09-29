# ErosLink `.elk` routines: format, semantics, and how the importer reproduces them

Code: `javaser.py` (stream reader), `elk.py` (decode → lower → compile → VM), `eroslink_cache.py`
(bundled routines cache). Tests: `tests/test_et312_elk.py`.

## What ErosLink does with a routine (verified)

ErosLink never plays a routine itself. It **compiles** each routine into ET-312 *program modules*, the
same bytecode the box's built-in modes are made of (see `vm.py`), and writes them into the box's
user-mode memory. The box then runs the routine as User 1..7.

On "Store in box" (EEPROM) with an empty box, it writes:

| What | Where |
|---|---|
| module bytes | bank 0: `$8040-$80FF`; bank 1: `$8120-$81FF` |
| module offsets | tables `$8020..` (modules `0x80..`) and `$8100..` (modules `0xA0..`) |
| start module of each User mode | `$8018+n` |
| top user mode | `$8008` and `$41F3` |

After writing, it selects the mode. "Write to RAM" uses `$40C0-$4177` with modules `0xC0..`; the importer
uses the EEPROM layout, which is the only one where every routine fits.

So importing a routine means running ErosLink's compiler. The VM already executes the output.
`ET312Engine.set_mode(routine)` installs the modules as program blocks and starts User 1, just as the box
would after an upload.

## Stage 1: the file

A `.elk` file is a Java serialization stream (`0xACED 0005`), written by `Context.save()` straight onto the
stream:

1. The version string, `"Context V1-20020725"` or `"Context V2-20030519"`.
2. Three serial or licence strings, one more string, and (V2 only) a flag plus 4 bytes.
3. The save date.
4. Four lists, each an `int` count followed by that many objects: triggers, parameter sets, routines,
   presets. In `.elk` files only the routine list is used. It holds 1 to 13 routines, so several
   routines can share one file.
5. A CRC (`long`).

Every ErosLink class writes itself with a private `writeObject()`, and none of them calls
`defaultWriteObject`. Each class's data is therefore a flat run of objects and block data, and
`javaser.Items` replays it in `readObject` order. For each class the data is: a version string, then
fields, then a CRC.

`Routine` holds its name, description and an `int n`, followed by n ingredient objects. Every ingredient
starts with `AbstractIngredient`: version, instance name, "also do" name, and (V2 only) a background
colour.

| Ingredient | Stored fields |
|---|---|
| Channel | channel: A=1, B=2, BOTH=3 |
| Ramp | start %, end %, time s, and-then, intensity/frequency/width flags, full-range (V2) |
| Multi-A Ramp | a Ramp, plus MA → intensity/frequency/width, "MA affects min", other bound |
| Multi-A | start %, end %, intensity/frequency/width flags, full-range (V2) |
| Multi-A Gate | min time s, max time s, on-time from MA, off-time from MA |
| Gate | on time s, off time s |
| Time Goto | time s, and-then |
| Set Value | value %, intensity/frequency/width, then by version: full-range, cancel-ramp (one or per item), value-from ×3 (native / advanced / MA / other channel) |
| Raw | byte text, lightly obfuscated: per-position character offsets, then reversed |
| External Trigger | and-then |
| Gate From | on-from, off-from (set value / MA / advanced) |

`.eis` files are `ET312InteractiveFrame` snapshots, saved slider positions from the Interactive window.
They are not routines, and the importer doesn't read them.

## Stage 2: lowering (`genLowLevel`)

Each ingredient becomes one **Trigger**. A trigger is a module description: the MA range, random range,
control flags, gate, block timer (time limit), and four modulator slots: intensity ramp (`$9C`),
intensity (`$A5`), frequency (`$AE`) and width (`$B7`). An ingredient also produces one **ParameterSet**
for each modulator it drives: value, min, max, rate timer, step, min/max actions and select bits.

- **Names:** every name gets the routine name as a prefix.
- **Chaining:** "Also do X" makes the trigger chain into X, so X's writes go into the same module.
- **Ramp ends:** a ramp's "and then" becomes the modulator's min/max action:
  - `<Reverse>` → `0xFF`
  - `<Restart>` / `<Start Over>` → `0xFD`
  - `<Hold>` → `0xFC`
  - `<Reverse Flip>` → `0xFE`
  - any other name → load that trigger's module
- **Percent scales:**
  - intensity: 127..255, or 0..255 at full range
  - width: 70..255, or 50..255 at full range
  - frequency: inverted, `(100-p)·247/100 + 8`
- **Ramp speed:**
  - The step count is 1–3, chosen so the per-step time is at least one 4 ms tick. Below 1.024 s, the
    step count with the smallest rounding error wins.
  - Timer units are 4 ms, 32 ms and 1.05 s. Multi-A variants move the rate or one bound onto the MA
    register and set the MA range from the other bound.
- **Start:** the routine starts at its first ingredient, or at the one whose instance name is `"1"`.

## Stage 3: compiling (`ET312.Module` / `TriggerModule` / `ModuleSet`)

1. **Build.** For each trigger reached from the start (chains inline, references recurse), build
   `(address, value[, mask])` pairs, with `+0x100` for channel B.
2. **Sort.** Raw bytes stay in order. Otherwise stores come first, then register writes in address order,
   then opcode ops, then stores again.
3. **Deduplicate.** Drop a write that a later write fully covers (after each chained trigger).
4. **Encode**, using the firmware's own forms (`vm.py`):
   - A single register write is `0x80|o v`. Bit 6 means the B page.
   - Consecutive registers become block writes `0x20|n<<2|p a v…`, up to 6 bytes each.
   - A partial write to a select byte becomes `AND ~mask` then `OR bits` (`0x54`/`0x58`).
   - Store / from-store / halve / random use `0x40`/`0x44`/`0x48`/`0x4C`.
   - Raw bytes are copied as they are.
   - Every module ends in `0x00`.
5. **Allocate** modules last-created-first into bank 0 (`0x80..`), then bank 1 (`0xA0..`).
6. **Resolve** trigger-name references to module numbers and re-encode.

## Verification

ErosLink's own jar was run on an emulated, empty ET-312, and its compiler output was compared with this
port. The oracle (scratch only, not in the repo) works like this:

1. Load the `.elk` with ErosLink's `Context`.
2. Run the same `genLowLevel` / `ModuleSet.add` / `resolve_and_generate` / `send` path as the "Write
   routines" button.
3. Capture every memory write through a subclass of `ET312` that fakes `readMemory`/`writeMemory`, with
   an erased EEPROM.

**Result:** all 245 routines in the 123 files byte-match. That covers the 12 bundled routines, the 33
designer examples and the tester's 78 files: 667 modules, including every start vector.

- **Hash table:** `tests/data/elk_eroslink_oracle.json` stores sha1 hashes of the oracle output (hashes
  only), and the tests re-check them whenever the files are present.
- **Sort order:** ErosLink's sort comparator isn't a total order (random-value pairs compare equal to
  everything). So the port uses the JRE 1.4 legacy merge sort that ErosLink shipped with. Modern TimSort
  gives the same bytes for every known file.

**Behaviour in the emulator:** every routine runs.

| Routine | Compared with the built-in | Result |
|---|---|---|
| designer "Climb", "ErsatzClimb" | Climb | same frequency sweep and range (15–390 Hz, width 130) |
| "ErsatzWaves" | Waves | same shape and ranges |
| "ErsatzRhythm" | Rhythm | differs by design ("a variation"): width toggles 70/199, and frequency follows MA over the full range, where Rhythm uses 1..23 |
| "ErsatzIntense2" | the bundled "Intense 2" | its "MA sets frequency, both channels on" analogue |

## Quirks kept on purpose (they are what the box ran)

- **Gate:** "on always" is keyed to the *off* time, and vice versa. The 32 ms / 1 s unit reconciliation
  tests one time but rescales the other. The off-time clamp tests the on time.
- **Gate From:** "off from advanced" tests the *on*-from setting.
- **Multi-A Gate:** the 4 ms → 32 ms unit change divides by 32, not 8.
- **Raw add/and/or/xor:** as values, these are encoded as 2 bytes, where the firmware reads 3. No
  ingredient produces them.

## Bundled routines (read at runtime, never committed)

`py -3.13 -m stimengine.et312.eroslink_cache [--zip …] [--cache …]` extracts the routines from
`ErosLink_Installer.zip`.

- **Where the zip comes from:** the routines are in `install.exe`, an InstallAnywhere self-extractor. It
  holds an appended zip and a nested `Installer.zip`, whose extra fields Python's `zipfile` rejects, so
  the script reads both central directories by hand.
- **Output:** the files go to `~/.stim-engine/eroslink/{bundled,designer}/`, plus a `manifest.json`.
- **Location:** set `$STIM_ENGINE_EROSLINK_CACHE` to override it. It isn't under `%LOCALAPPDATA%`,
  because Microsoft Store Python silently redirects new folders there into its package cache.

The bundled set is: EMS 1, EMS 2, Bee Stings, Challenge, Intense 2, Multi Climb, Program 1, Program 2,
Rhythm 2, Stroke 2, Stroke 3, Torment 2. It also contains 33 designer `.elk` examples and 3 `.eis`
files.

## API

```python
from stimengine.et312 import elk, ET312Engine
rs = elk.list_routines(r"C:\...\mk312\et312")   # bundled first; dicts: name, description, source, bundled,
                                                 # path ("<file>" or "<file>#<i>"), file, index, id
r = elk.load(rs[0]["path"])                      # CompiledRoutine: name, start, modules {num: bytes}, fits_in_box
eng = ET312Engine(r)                             # or eng.set_mode(r); frames report mode_name = routine name
```

## Open ambiguities

- **Presets** (named lists of routines for User 1..6) are decoded but not used. Each routine is loaded
  alone as User 1, which gives the same modules as ErosLink uploading it into the first free slot of an
  empty box.
- **External-trigger routines** (`ExtTriggerIngredient`) compile correctly, but the emulator has no
  trigger input. `$8F` holds the module to run and ctrl bit 1 enables it, but nothing fires it.
- **Out-of-memory routines:** a routine too big for the box would fail in ErosLink. The importer numbers
  its overflow modules `0xC0..` and sets `fits_in_box = False`. No known routine needs this.
