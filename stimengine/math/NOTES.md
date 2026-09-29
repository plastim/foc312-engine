# What restim's FOC-Stim math actually does (v1.66) — and where it's thin

Map for the "do the math better" work. Everything below is what the *host* does
before `axis_move_to`. The firmware does the rest (see "What lives in firmware").

## The single biggest finding

**The host does almost no stim math for FOC-Stim.** For 3-phase it sends
`alpha, beta` (clamped to the unit circle) plus three raw calibration numbers; for
4-phase it sends four clipped intensities plus five raw calibration numbers. All
position→electrode-current vectoring, pulse shaping, and charge balancing happen
in the firmware. The host's real contribution is the **volume law** and two
**perceptual corrections** (tau, pulse-rate). So "better math" splits cleanly:
anything about *how loud / how even* is host-side and ours to change today;
anything about *which electrode gets what current* is firmware.

## Pipeline (both modes, identical order — `volume.py:pulse_chain`)

1. `volume = clip(master)·clip(api)·clip(inactivity)·clip(external)` — four
   independent 0..1 knobs multiplied. `master` is the GUI knob, `api` is T-code
   V0 *or* a linked volume funscript, `inactivity` is the auto-decay, `external`
   is a second T-code axis. This is the safety spine: everything downstream can
   only multiply by ≤ 1.
2. Carrier clamp: `carrier ∈ [max(300, min_cfg), min(2000, max_cfg)]`.
3. **Tau derating** (`tau_calibration.py`): `volume *= (f·τ + ½)/(f_max·τ + ½)`.
   With τ = 355 µs and f_max = 2000 Hz: 500 Hz → 0.56, 790 Hz → 0.65, 1000 Hz →
   0.71, 2000 Hz → 1.0. It's a strength–duration (chronaxie-style) argument:
   lower carrier = longer half-cycle = more charge per pulse = stronger feel, so
   lower carriers get turned *down*. Note the reference point is `f_max` from the
   device wizard — change your max carrier and every lower carrier's level shifts.
4. **Burst gap** (`burst_gap.py`, default ON): reinterprets the user's "pulse
   frequency" as 1/gap so the *real* repetition rate becomes
   `1/(gap + pulse_width/carrier)`. At 790 Hz, width 11.5, "74 Hz" → ~36 Hz actual.
   This is why the device's `actual_pulse_frequency` telemetry won't match the
   knob — it's by design.
5. **Pulse-rate compensation** (`pulse_frequency_calibration.py`, default ON):
   `volume *= I(0)/I(pf)` with `I(pf) = 0.9115 + 0.00203·pf − 5.76e-6·pf²`
   (fit from the author's own perception data at 1000–2000 Hz carriers, 5–100 Hz
   pulse rates, clipped at 175 Hz). Range of effect: ×1.0 at 0 Hz → ×0.87 at
   ~100 Hz. Small, but it's the only *measured* perceptual curve in the stack.
6. Sensor hook: a plug-in may rewrite position and volume, but
   `volume = clip(new, 0, old)` — **sensors can only turn it down.**
7. Not playing ⇒ volume 0 (media sync gate).
8. `amps = volume · waveform_amplitude_amps` — the configured hard cap
   (0.15 A in the tester's ini; board max 0.20 A). The device never sees "volume", it
   sees a current in amps.

## 3-phase position (`threephase.py`)

* `(α, β)` is a point in the unit disc. Outside → radially clamped to the rim.
* Optional **transform**: mirror β, rotate clockwise by N°, then an affine
  rectangle remap (top/bottom → α range, left/right → β range, with β's sign
  flipped in the centre term — `beta_center = −(left+right)/2`). Re-clamped.
* Optional **map-to-edge**: throws β away and maps α ∈ [−1, 1] onto an arc of
  the rim from `start` to `start+length` degrees. Re-clamped. (Its inverse is
  unimplemented upstream — "who cares?".)
* Calibration goes through untouched: `center` (dB ≤ 0, a reduction applied by
  the firmware when the position is near the middle), `neutral` (up/down bias)
  and `right` (left/right bias). The GUI's "modern" a/b/c dB interface is just a
  reparameterisation of neutral/right via `ud_lr_to_intensity_ratio`; the wire
  format is still neutral/right. **Upstream axis naming is crossed:** `neutral →
  AXIS_CALIBRATION_3_UP`, `right → AXIS_CALIBRATION_3_LEFT`. Kept faithfully.

## 4-phase (`fourphase.py`)

* `e1..e4` clipped to 0..1, sent as `AXIS_ELECTRODE_n_POWER`. That's the whole
  model — no geometry, no coupling. The GUI's patterns and mouse widget generate
  the four numbers; a funscript or T-code can too.
* Calibration: four per-electrode dB offsets (GUI normalises so the loudest is
  0 dB) + `center_reduction` (default 0.07). `AXIS_CALIBRATION_4_CENTER` exists in
  the enum but is **never sent** by v1.66.
* the tester's restim.ini `[calibration_four]` has `center=5.5, a, b, c` and no `d`:
  the `center` key is from an older schema and v1.66 ignores it; `d` defaults to
  0.0 and `center_reduction` to 0.07. Worth re-doing the 4-phase calibration in
  v1.66 before trusting those numbers.

## What lives in firmware (not portable, not ours to change without a fork)

position→per-electrode current vectoring (the real "3-phase math"), the
centre-reduction shaping, pulse train generation (width in carrier cycles, rise
time, interval randomisation), charge balancing, current control loop, and all
limits/self-test. `stim_math/transforms.py` (`ab_to_e123`) in restim is the
*audio*-output version of that vectoring and is a decent sketch of what the
firmware presumably does: half-angle → 3 projections at 120°, subtract the
smallest so one electrode sits at 0, scale by 1/1.5, abs.

## Where it's crude / heuristic (design targets)

1. **One τ for all carriers and all people.** τ is a single user setting
   (355 µs default) and the derating is a two-parameter ratio. Real chronaxie
   varies by fibre type and site; this is the lever for a measured, per-person,
   per-electrode-set curve — and restim's A/B mode is the instrument.
2. **Pulse-rate curve is one quadratic from one person's ratings**, clipped at
   175 Hz, carrier-independent by assumption ("data was extremely close").
3. **Volume is linear in amps.** Perceived intensity vs current is compressive;
   there is no loudness-style law anywhere. The slow-start ramp is linear too.
4. **No accommodation model.** Nothing drifts parameters over time to counter
   habituation; that's left to the human or to patterns.
5. **Position is purely geometric.** Calibration is three (or five) static
   numbers; no per-electrode impedance/contact feedback closes the loop, even
   though the firmware streams per-electrode skin resistance.
6. **Burst-gap hides the true pulse rate** from every other calculation except
   the pulse-rate compensation (which correctly uses the converted value).
7. **4-phase has no position concept at all** — an e1..e4 "pad" is the UI. A
   real 2-D/3-D position model for four electrodes would be new work.

## Port notes

* `stimengine.math` is pure functions on floats; restim's temporal axes
  (`stim_math/axis.py`) are replaced by whatever the control layer interpolates.
* Oracle: 1002 tests (355 three-phase and 480 four-phase full parameter_dict
  comparisons + leaf-function sweeps) at 1e-9 against the vendored v1.66 code.
* `_vendor.py` is the only import from `vendor/`; it goes away when Fork A
  re-homes the protobuf enums.
