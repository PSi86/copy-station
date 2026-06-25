# E-Paper display support — implementation plan

Status: **proposed** (awaiting go-ahead). Branch: `feat/epaper-display`.

Adds an e-paper status backend that renders a rich status frame: the transfer
progress bar, the used/free storage of source and target, and the current phase
(ready / detecting / copying / error / success). The backend slots into the
existing `status` backend abstraction exactly like `ws2812` / `grove_led_bar`.

## 1. Scope

First PR (this branch):

* Driver/preset abstraction designed for *many* e-paper panels from day one.
* Two panels shipped and selectable by config:
  * **Waveshare 1.54″** — 200×200, controller **SSD1681** (primary target).
  * **Waveshare 2.9″** — 296×128, controller **SSD1680**. The **WeAct 2.9″ BW**
    is the same SSD1680 and runs on the identical driver.
* All four target panels are black/white (1bpp).

Roadmap (not this PR, but the abstraction reserves the slot):

* **WeAct 3.7″ BW** — 480×280, controller **SSD1677** (larger init differences,
  cannot be validated without the panel).
* Three-colour ("B"/"C") variants — out of scope; the layout stays 1bpp.

## 2. Hardware matrix

| Panel | Resolution | Controller | Driver class | Partial | Status |
|-------|-----------|------------|--------------|---------|--------|
| Waveshare 1.54″ V2 | 200×200 | SSD1681 | `ssd168x` | yes | done (primary) |
| Waveshare 2.9″ V2 | 296×128 | SSD1680 | `ssd168x` | yes | done |
| Waveshare 2.13″ HAT (V4) | 250×122 | SSD1680 | `ssd168x` | yes | done (preset only) |
| Waveshare 2.13″ HAT+ | 250×122 | SSD1680 | `ssd168x` | yes | done (preset only) |
| WeAct 2.9″ BW | 296×128 | SSD1680 | `ssd168x` | yes | done (same driver) |
| WeAct 3.7″ BW | 480×280 | SSD1677 | `ssd1677` | yes | roadmap |

The 2.13″ HAT / HAT+ are native 122×250 (note: 122 is not a multiple of 8 -- the
1px-per-pixel pack pads each row to 16 bytes, which the controller's RAM X window
already expects) and gate panel power on BCM18, so their presets default
`pwr: 18`.

SSD1680 and SSD1681 share the command set (driver output control `0x01`, data
entry `0x11`, RAM windows `0x44/0x45`, counters `0x4E/0x4F`, write RAM `0x24`,
update control `0x21/0x22`, master activation `0x20`, border `0x3C`). They differ
only in the values derived from the panel's width/height, so **one parameterised
`ssd168x` driver covers both**. SSD1677 uses 16-bit gate parameters and a
slightly different init — its own class later.

### Parameters that must be configurable (the abstraction boundary)

Derived from the matrix and the panel datasheets, the driver must expose:

1. `controller` — `ssd1680` | `ssd1681` (alias of `ssd1680` family) | `ssd1677`.
2. `width` × `height` — logical panel resolution.
3. `rotation` — 0 / 90 / 180 / 270, so the module can be mounted in any
   orientation; the layout also uses it to pick a portrait vs landscape design.
4. `mirror` — optional x/y mirror for panels wired the other way round.
5. SPI: `device` (`/dev/spidevX.Y`) and `spi_speed_hz` (e-paper-safe, ~4 MHz).
6. GPIO control lines: `dc`, `rst`, `busy` (+ `gpiochip`), optional `pwr` (panel
   power-enable on newer Waveshare revisions), optional `cs` GPIO (default: use
   the SPI hardware chip-select, no GPIO needed).
7. `busy_active_high` — SSD168x signal BUSY high; some panels invert.
8. `full_refresh_every` — anti-ghost budget: force a full refresh after N partials.
9. `partial_min_interval` — throttle for partial updates (default 2.0 s).

Panel choice is normally a one-word **preset** (`model:`) that expands to
`controller`+`width`+`height`; every field stays individually overridable.

## 3. Architecture & data feed

Decision (confirmed): the e-paper backend **reads the shared `StationState`
snapshot** — the same source of truth the web UI polls. Everything the display
must show already lives there ([state.py](../copystation/state.py) `snapshot()`):
`phase`, `percent`, `bytes_done/total`, `source`/`target` `StorageInfo`
(label/capacity/used/free), `devices`, `error`, `transfer_name`, `speed`/`eta`.
No new data plumbing through the indicator interface.

Consequences:

* `build_indicator()` gains an optional `state` argument; the daemon passes the
  `StationState` it already creates. The LED/buzzer/ws2812/grove backends ignore
  it (signature only), so nothing else changes.
* The `StatusIndicator` methods (`set_state` / `set_progress` / `set_fill` /
  `signal`) become light "the model may have changed" nudges for the e-paper
  (it samples `state` on its own cadence), and keep their existing meaning for
  the LED backends. No interface additions.
* `StatusHub.set_storage()` / `set_devices()` already update `StationState`, so
  the display sees storage and device changes for free.

## 4. Module layout

Mirrors the existing per-backend structure and keeps the *pure* logic
hardware-free and unit-testable (like `effects.py` / `leds_for` / `encode_pixels`
today):

```
copystation/status/epaper/
  __init__.py        EpaperBackend(StatusIndicator): render thread, lifecycle,
                     reads StationState, drives policy -> driver.
  model.py           ViewModel dataclass + build_view(snapshot) -> ViewModel
                     (pure: maps the snapshot dict to what the screen shows).
  policy.py          decide(prev, new, counters, now) -> FULL | PARTIAL | SKIP
                     (pure: the full-vs-partial-vs-ghost logic). Fully testable.
  layout.py          render(view, width, height, rotation) -> 1bpp PIL image
                     (pure: text + bars via Pillow). Testable in-memory.
  drivers/
    __init__.py      get_driver(controller, cfg) -> EpaperDriver
    base.py          EpaperDriver: SPI+GPIO primitives (reset, send_command,
                     send_data, wait_busy), abstract init/display_full/
                     display_partial/clear/sleep.
    ssd168x.py       SSD1680/SSD1681 (parameterised by width/height).
    ssd1677.py       3.7″ (roadmap; stub with NotImplementedError until validated).
```

## 5. Render policy — full vs partial vs skip (the core)

E-paper truth, matching the requirements:

* **Full refresh** flashes black/white, ~1–2 s, clears all ghosting — must be
  infrequent.
* **Partial refresh** is fast (~0.3 s), no flash, but accumulates faint ghosts.
* Drawing **darker** pixels onto white (a growing bar, a device appearing in a
  blank area) is clean via partial. **Erasing** (black→white) is what ghosts —
  so a vanished device / a shrunk bar needs a full refresh.

The render thread wakes ~every 0.5 s, builds a `ViewModel` from the snapshot,
and `policy.decide(...)` returns one of FULL / PARTIAL / SKIP. Rules, in priority
order:

1. **First frame** after start → FULL.
2. **Phase changed** (ready↔detecting↔copying↔error↔success) → FULL. A phase
   change rewrites the big status word and the visible regions; replacing text
   via partial ghosts badly, and phase changes are rare.
3. **Something must clear to white** → FULL. Detected when: a device disappears
   (device count dropped), progress reset to 0 from >0, a storage bar shrank, or
   the source was cleared (its used-space dropped). This is the explicit
   "device removed ⇒ full refresh" requirement.
4. **Anti-ghost budget** reached: `partials_since_full >= full_refresh_every`
   (default ~20) → FULL. Cleans the numeric ghosting that small text re-renders
   leave behind during a long copy.
5. **Additive change** (progress bar grew, used-space grew, a device appeared in
   a previously-white area) **and** `partial_min_interval` (2 s) elapsed →
   PARTIAL.
6. Otherwise → SKIP (dedup / throttled), exactly like the LED backends' frame
   de-dup.

Resulting behaviour over a copy (matches the brief):

* Copy starts → FULL once (new "Copying" layout).
* Bar advances → PARTIAL every ~2 s (clean, bar only grows).
* ~Every 20 partials → one FULL to wipe numeric ghosts.
* Device freshly detected onto a blank row → PARTIAL (drawing black on white).
* A device is unplugged → FULL (a filled region must go truly white again).
* Copy finishes (copying→success) → FULL.

Implementation note: the first version does **whole-frame partial** (entire image
pushed with the partial waveform) — standard for these small panels and keeps the
policy about *when*, not *where*. Windowed/bounding-box partial is a later
optimisation.

## 6. Layout (adapts to aspect ratio)

`layout.render()` draws with Pillow into a 1bpp image, then rotates to the panel.
Two designs chosen by aspect ratio (see the mockup in the PR/chat):

* **Square (1.54″, 200×200), portrait stack:** title `Copy_Station` + version;
  big status word; transfer bar + %; source bar + `used/total`; target bar +
  `used/total`; footer `speed · ETA` during a copy.
* **Wide (2.9″/3.7″, landscape):** left column = title + big status word + % +
  speed/ETA; right column = transfer bar, source bar, target bar stacked.

Storage bars render the honest 1-bit way: **used = solid black**, **free = white
with a 1px frame**. Phases other than copying hide the transfer bar and show a
phase-appropriate line (e.g. detecting shows the detected device's fill; error
shows the error text). Status words: `Ready / Detecting / Copying / Error / Done`.

## 7. Stop / shutdown frame

Decision (confirmed): on `close()` the backend does a **full refresh to a clean
"powered off" frame** — `Copy_Station`, the code version (`__version__`, currently
`0.1.0`), and `Power off` — then deep-sleeps the panel. Because e-paper retains
its image without power, this leaves an unambiguous, ghost-free final image rather
than a frozen "copying" screen.

This lives in `close()`, so it fires both on a clean daemon exit and via the
systemd `ExecStopPost` path: `leds-off` calls `build_indicator(config,
start=False)` (no render loop) then `close()`. The command name `leds-off` stays
for compatibility; for e-paper it means "draw the off frame and sleep".

## 8. Configuration

New `status.epaper` block (defaults in [config.py](../copystation/config.py),
documented in `config.example.yaml`). Presets keep it to one line for the common
case:

```yaml
status:
  backends: [log, epaper]
  epaper:
    model: waveshare-1.54        # expands controller + width + height
    # --- overridable; shown values are the Raspberry Pi defaults ---
    device: /dev/spidev0.0
    spi_speed_hz: 4000000
    gpiochip: gpiochip0
    dc: 25                       # BCM / line offset
    rst: 17
    busy: 24
    pwr: null                    # optional panel power-enable pin
    rotation: 0                  # 0 | 90 | 180 | 270
    mirror: false
    busy_active_high: true
    full_refresh_every: 20       # force a full refresh after N partials
    partial_min_interval: 2.0    # seconds between partial updates
```

Presets:

```python
_EPAPER_PRESETS = {
  "waveshare-1.54": {"controller": "ssd1681", "width": 200, "height": 200},
  "waveshare-2.9":  {"controller": "ssd1680", "width": 296, "height": 128},
  "weact-2.9":      {"controller": "ssd1680", "width": 296, "height": 128},
  "weact-3.7":      {"controller": "ssd1677", "width": 480, "height": 280},  # roadmap
}
```

Standard Waveshare→Raspberry-Pi wiring (the defaults): `VCC→3V3`, `GND→GND`,
`DIN→MOSI/BCM10`, `CLK→SCLK/BCM11`, `CS→CE0/BCM8` (hardware CS), `DC→BCM25`,
`RST→BCM17`, `BUSY→BCM24`. Cubie A7S: `device: /dev/spidev1.0` and free Allwinner
offsets for `dc/rst/busy` — a worked example goes into
`config.examples/cubie-a7s.yaml`.

## 9. Lifecycle & threading

Same shape as `Ws2812Backend`:

* A daemon render thread; `start=False` opens nothing and only the stop frame is
  drawn on `close()` (the `leds-off` path).
* The thread samples `state.snapshot()`, runs the policy, and calls the driver.
  A full refresh blocks on BUSY for ~1–2 s, but it is a dedicated thread, so it
  never blocks the transfer (just like the other backends' render threads).
* `set_state/set_progress/set_fill/signal` set a lightweight "maybe dirty" flag;
  the loop coalesces them — high-frequency `update_progress` pushes do **not**
  cause high-frequency refreshes (the 2 s throttle governs).
* With `state=None` (leds-off) the loop never starts; `close()` draws the off
  frame and sleeps.

## 10. Dependencies

* **Pillow** (decision confirmed). On the device: apt `python3-pil` added to
  `scripts/install.sh` (consistent with `python3-spidev`/`python3-libgpiod`, used
  via the venv's `--system-site-packages`). For dev/tests on Windows: add
  `Pillow` to the test deps note in `requirements.txt`. The import is lazy inside
  `layout.py`, so non-e-paper users never need it at runtime.
* Reuses existing `spidev` (as ws2812) and the libgpiod v1/v2 shim
  ([gpio.py](../copystation/status/gpio.py)) incl. `open_input_lines` for BUSY.

## 11. Wiring changes (existing files)

* `status/__init__.py`: `build_indicator(config, state=None, start=True)`;
  `_create_backend("epaper", cfg, state, start)` → `EpaperBackend(...)`.
* `daemon.py`: pass `state` into `build_indicator` in `run_simulation` and
  `run_daemon`. `run_leds_off` keeps `build_indicator(config, start=False)`.
* No change to `StatusHub` (display reads state); `set_storage`/`set_devices`
  already update the state the display reads.

## 12. Tests (all hardware-free)

* `policy.py`: first-frame→FULL; phase change→FULL; device removed / bar shrink /
  progress reset→FULL; budget exhausted→FULL; bar grew + interval→PARTIAL;
  throttled / no change→SKIP.
* `model.py`: snapshot dict → ViewModel mapping (percent, source/target figures,
  device count, error text).
* `layout.py`: render a ViewModel → assert image size, that the progress fill
  width tracks the percentage, that the status word and storage bars produce
  non-white regions (pixel/bbox assertions). Skips if Pillow is absent.
* `drivers/ssd168x.py`: stub `spidev` + a fake gpiod module (as `test_ws2812.py`
  stubs spidev and `test_gpio.py` injects gpiod) → assert the init command
  sequence, BUSY polling, and that full vs partial use different update commands.
* backend/config: preset expansion merges into defaults;
  `build_indicator` builds the e-paper backend with an injected fake driver and
  renders without hardware; `leds-off` draws exactly one off frame.

## 13. Docs

* New README section "E-Paper display (optional)": supported panels, wiring
  table, the full-vs-partial behaviour, the preset list, and the SPI-contention
  note below.
* `config.example.yaml` + `config.examples/{raspberry-pi,cubie-a7s}.yaml`: an
  `epaper` block, commented like the existing backends.

## 14. Risks & open hardware-validation points

* **SPI contention with WS2812:** the ws2812 backend abuses SPI MOSI with strict
  timing and no chip-select, so it cannot share a bus with the e-paper. Document:
  put the e-paper on its own SPI bus/CS, or use *either* ws2812 *or* e-paper.
* **Panel init quirks:** exact `0x01`/`0x11`/window values, the partial waveform,
  and BUSY polarity must be confirmed on each panel; WeAct modules occasionally
  need a panel-specific LUT. Validate 1.54″ first, then 2.9″.
* **Hardware CS vs GPIO CS:** default to spidev hardware CE0; expose an optional
  `cs` GPIO if a panel turns out picky.
* **Refresh cost:** full refreshes are slow and stress the panel; the policy's
  budget + throttle keep them rare. No refresh ever blocks the copy (own thread).

## 15. Implementation order (suggested commits)

1. `config.py` presets + defaults + `config.example.yaml` / `config.examples/*`.
2. `status/epaper/drivers/{base,ssd168x}.py` (+ `ssd1677` stub).
3. `status/epaper/{model,policy,layout}.py` (pure, with tests alongside).
4. `status/epaper/__init__.py` backend (render thread, close/off frame).
5. Wire `build_indicator(state=...)`, `daemon.py`, factory `"epaper"`.
6. `scripts/install.sh` apt `python3-pil`; `requirements.txt` test note.
7. README + config docs.
8. Tests for policy/model/layout/driver/backend/config.
