# Copy_Station

Autonomous copy station: automatically transfers the footage of a camera
connected via USB as mass storage (e.g. **DJI O4 Air Unit**, `DCIM` folder) onto
a **(micro) SD card**, verifies the transfer and then clears the source. Runs as
a headless systemd service on a **Radxa Cubie A7S** (`radxa-a733-bullseye-cli-r6`)
or a **Raspberry Pi 4 / 5** (Raspberry Pi OS Bookworm 64-bit).

## Flow

```
Ready ──device detected──► Detecting ──source+target ok──► Copying
  ▲                                                          │
  │                                              verify (size+count)
  │                                                          │
  └────────── devices removed ◄── Success ◄──── clear source DCIM
                              (error ⇒ Error, source left untouched)
```

* **Detection:** event-driven -- source and target may be plugged in **in any
  order and at different times**. The set of attached volumes is re-evaluated on
  every USB add/remove event; a transfer starts as soon as two eligible volumes
  are present. Works with whole-disk devices that carry a filesystem directly
  (no partition table) -- the O4 Air Unit exposes its storage this way.
* **Copy:** into a new folder `transfer_<NNNN>_<source_name>` -- nothing is ever
  overwritten. The running number is persisted on the SD card.
* **Verification:** fast comparison of file count and file sizes.
* **Cleanup:** only on success; only the `DCIM` contents are deleted, never
  formatted.
* **Resilience:** if the source or target is unplugged mid-copy the copy aborts
  and reports which side dropped out -- promptly (~1 s) when the whole USB device
  is removed, or once the kernel hits the I/O error when only a card is pulled
  from a still-connected reader. Either way the source is never deleted unless
  verification succeeded.
* **Status:** `Ready / Detecting / Copying / Error` via interchangeable backends
  (log, LEDs, buzzer, WS2812, Grove LED Bar -- freely combinable). On the bar
  backends each newly detected volume blinks green **twice** and then shows a
  **white gauge of how full that volume is for ~3 s** (then the bar rests); a
  connected source with nothing to copy holds **blue** briefly -- so detection is
  unmistakable at a glance.

## Web interface (optional)

Set `web.enabled: true` in the config to host a local status page on **all
network interfaces** (`0.0.0.0:8080` by default). It shows live phase, copy
progress (percent, elapsed, ETA, **speed**), the **detected devices** with their
assigned role (source/target/candidate) and used/total storage, and a scrolling
**activity log** of recent actions (newest first); it is prepared for future
settings. Binding to `0.0.0.0` makes it robust to interfaces going down/up at
runtime -- no per-interface rebinding.

The frontend is a single static page (vanilla JS, no build step) that polls
`/api/status` every 500 ms; the backend is FastAPI (`/docs` for the auto API
docs). Open `http://<device-ip>:8080/`.

## Grove LED Bar v2.0 (optional)

Add `grove_led_bar` to `status.backends` to drive a Seeed Grove LED Bar v2.0
(MY9221) over two GPIO lines. During a copy the bar shows the proportional copy
progress and blinks at 10 Hz. On detection it flashes the whole bar twice and
then shows a **steady fill gauge** of the detected volume for **~3 s** before
resting; the other idle phases light a single steady LED (Ready = green /
segment 3, Error = red / segment 1). Set the `clock_line` / `data_line` offsets
(from `gpioinfo`) in the config.

## WS2812B / NeoPixel strip (optional)

Add `ws2812` to `status.backends` to drive an addressable WS2812B / NeoPixel
strip of **1-10 LEDs** over SPI (MOSI, each data bit encoded as three SPI bits).
A newly detected volume flashes the whole strip green **twice**, then shows a
**steady white gauge** of how full that volume is for **~3 s** before the strip
rests; during a copy the LEDs `1..N` form a **blue** progress bar that blinks at
10 Hz (the same activity pattern as the Grove LED Bar); otherwise the first LED is
steady (Ready = green, Error = red, Success = a short green blink). A source with
nothing to copy holds the whole strip **blue** for a few seconds. Set `led_count`
(1-10) and the `device` (e.g. `/dev/spidev0.0`) in
the config. On the **Raspberry Pi** enable SPI (`dtparam=spi=on`) and wire DIN to
MOSI (**BCM GPIO10 / pin 19**). On the **Cubie A7S** enable the **`spidev on SPI1`**
overlay (in `rsetup` -> Overlays; the backend needs a `/dev/spidev*` node, so this
is required) and wire DIN to **SPI1-MOSI = PD12, header pin 19** -- only MOSI is
used (MISO/CLK/CS stay unconnected). After enabling, find the bus with
`ls /dev/spidev*` (typically `/dev/spidev1.0`) and set it as `device`. Note that
PD12/PD13 (pins 19/21) are the same pins the Grove LED Bar uses, so drive **either**
the Grove bar **or** a WS2812 strip on them, not both.

## Powering off safely

Cutting the supply while the OS card is being written can corrupt it. Shut down
cleanly instead:

```
sudo systemctl poweroff
```

This stops the service, flushes buffers and unmounts the filesystems; once the
board has halted it is safe to remove power. **Always use `poweroff`, never
`reboot`:** on the A733 (tested on `radxa-a733_trixie_cli_t4`) a warm `reboot`
never comes back up -- recovery always needs a full power-cycle (remove power,
then reapply). A normal cold boot is reliable (an earlier "random boot failure"
turned out to be a marginal microSD card -- if cold boot ever flakes, suspect the
card first). The copy itself is safe against a sudden disconnect: the source is
never cleared unless verification succeeded.

### Shutdown button (optional)

Wire a momentary button between a GPIO line and GND and enable
`power.shutdown_button` in the config. Holding it for `hold_seconds` (default 1 s)
runs a clean `systemctl poweroff` -- so the headless station can be shut down
without SSH. Keep `active_low: true` with `bias: pull_up` for a button to GND.

It uses the same libgpiod v1/v2 layer as the LED backends, so it works on the
**Cubie A7S** and on **Raspberry Pi 4 / 5**. Set `line` to the GPIO offset
(`gpioinfo` offset on the Cubie; the **BCM number** on a Pi) and `gpiochip`
accordingly (Pi 4 and most Pi 5 images: `gpiochip0`; older Pi 5 images:
`gpiochip4`).

**Recommended pin -- Raspberry Pi:** wire the button to **GPIO3 (BCM 3, pin 5)**.
It serves both directions with one button, but they are two independent
mechanisms -- keep them apart:

* **Power on / wake from halt** is a **firmware feature of the Pi** and needs **no
  software and no copy-station config at all**: after a `poweroff` the Pi is
  halted, and pulling GPIO3 low boots it. This works on GPIO3 regardless of what
  is in `config.yaml`.
* **Shutting down** while the Pi is running is **not** automatic -- some software
  must react to the pin. Either:
  * enable copy-station's `power.shutdown_button` with `line: 3` (what this
    feature is for), **or**
  * use the OS-native overlay `dtoverlay=gpio-shutdown` in
    `/boot/firmware/config.txt` (defaults to GPIO3).

  Use **only one** of the two for the shutdown side -- a GPIO line is exclusive,
  so enabling both on the same pin makes the second one fail to claim it. If you
  rely on the OS overlay, leave `power.shutdown_button.enabled: false`.

**Recommended pin -- Cubie A7S:** there is no documented wake-from-halt GPIO, so
pick any free line from `gpioinfo`; the button only triggers shutdown, and you
power the board back on by re-applying power. For a concrete example, **PB5
(header pin 12, offset 37)** works when you are not also wiring a buzzer there --
set `line: 37`. The offset follows the same `(bank_letter - 'A') * 32 + pin` rule
as the LED/Grove pins (see *GPIO pins for the status hardware* below).

## Development (without hardware, e.g. Windows)

The core logic runs hardware-free. Simulation run with two local folders:

```
python -m copystation.daemon --verbose simulate <source> <target> --source-name DJI_O4
```

`<source>` must contain a `DCIM` folder, `<target>` is the "SD root".

Tests:

```
pip install -r requirements.txt pytest
pytest
```

## Deployment

Runs on the **Radxa Cubie A7S** (Debian Bullseye through **Trixie**) and on
**Raspberry Pi 4 / 5** (Raspberry Pi OS Bookworm 64-bit). The GPIO layer handles
both libgpiod **v1** (Bullseye) and **v2** (Trixie / newer images), so the same
code and config run across these releases.

### 1. Get the code onto the device

Log in to the device (e.g. `ssh radxa@<device-ip>`) and place the project in
your home directory, so it ends up at `~/copy-station` (e.g.
`/home/radxa/copy-station` on the Cubie, `/home/<user>/copy-station` on a Pi).
Pick **one** of the two ways:

**A) Clone with git (recommended)** -- keeps file permissions intact:

```
sudo apt-get install -y git          # if git is not installed yet
cd ~
git clone https://github.com/PSi86/copy-station.git
cd ~/copy-station
```

**B) ZIP download** -- if you cannot use git. Download the ZIP from GitHub
("Code -> Download ZIP"), copy it to the device and unpack it. A ZIP **loses the
executable bit** of the scripts, which is why the install command below uses
`bash` explicitly.

```
# from your PC, copy the downloaded ZIP to the device:
scp copy-station-main.zip radxa@<device-ip>:~

# then, on the device:
cd ~
unzip copy-station-main.zip       # creates ~/copy-station-main
mv copy-station-main copy-station # rename to ~/copy-station (optional)
cd ~/copy-station
```

### 2. Run the installer

From inside the project directory (`~/copy-station`):

```
sudo bash scripts/install.sh
```

> **Why `bash` and not `./scripts/install.sh`?** A ZIP download (way B) drops the
> executable bit, and `sudo ./scripts/install.sh` on a non-executable file fails
> with the misleading `command not found`. `sudo bash scripts/install.sh` works
> in **both** cases. (After a `git clone` the bit is preserved, so
> `sudo ./scripts/install.sh` works there as well.)

The installer:

* installs dependencies (`rsync`, `python3-pyudev`, `python3-libgpiod`,
  `python3-spidev`, the `gpiod` CLI tools, exFAT support),
* copies the code to `/opt/copystation` and creates a venv with FastAPI/uvicorn
  (PEP 668-safe via `--system-site-packages`),
* **detects the board** and writes `/etc/copystation/config.yaml` from the
  matching example (Cubie / Raspberry Pi / generic) -- so it already contains
  suggested GPIO pins instead of empty placeholders,
* **asks whether to enable the web interface** (default: yes), and
* enables and starts the `copystation` systemd service.

The config file is created only if it does not exist yet; re-running the
installer never overwrites your settings.

### 3. Configure the station

Everything is configured in **`/etc/copystation/config.yaml`**. Edit it with any
editor, e.g.:

```
sudo nano /etc/copystation/config.yaml
```

**Apply changes by restarting the service.** The config is read once at startup;
there is no live reload. You do **not** need to stop it first -- `restart` does
both:

```
sudo systemctl restart copystation
sudo systemctl status copystation     # should say "active (running)"
journalctl -u copystation -f          # follow the live log / errors
```

If the service fails to start after an edit, the log above shows why (usually a
YAML typo or a wrong GPIO line). Fix the file and restart again.

#### Web interface

The installer already asked about this; to change it later, set `web.enabled`
in the config and restart. When enabled it serves on `http://<device-ip>:8080/`
(all interfaces, port from `web.port`). Find the device IP with `ip a`.

#### GPIO pins for the status hardware

The shipped config already contains **suggested** pins, so the station works as
a starting point. Confirm them once for your wiring before relying on the LEDs,
then list the hardware you actually connected in `status.backends` (any
combination of `log`, `led`, `buzzer`, `ws2812`, `grove_led_bar`).

A pin is addressed by a **gpiochip name** plus a **line offset**. To find them:

```
gpiodetect      # lists the chips; the main header chip has the most lines
gpioinfo        # lists every line: its offset ("line  N:") and its name
```

* **Raspberry Pi 4 / 5:** the 40-pin header is on `gpiochip0` (older Pi 5
  images: `gpiochip4`, an alias). The **line offset equals the BCM GPIO
  number** -- e.g. BCM17 -> offset `17`. Pick free BCM pins for your wiring.
* **Cubie A7S:** the header uses Allwinner port names (e.g. `PB0`). In
  `gpioinfo` each line is printed with that name, so to wire to header pin 7
  (`PB0`), find the line shown as `"PB0"` and use the number in its
  `line  N:` column. All main-PIO ports share one gpiochip, and the offset
  follows `(bank_letter - 'A') * 32 + pin` (e.g. `PB0` -> `32`, `PD12` ->
  `108`) -- the shipped offsets use exactly this. Confirm the chip name with
  `gpiodetect`.

The GPIO layer auto-detects libgpiod **v1 and v2**, so the same code/config runs
on the Cubie (Bullseye = v1, **Trixie = v2**) and on Raspberry Pi OS
(Bookworm = v2). The on-device `gpiod` package provides the matching v1/v2 CLI
tools.

Board-specific reference configs:
[config.examples/cubie-a7s.yaml](config.examples/cubie-a7s.yaml) (with the pin
derivation explained) and
[config.examples/raspberry-pi.yaml](config.examples/raspberry-pi.yaml).

### 4. Uninstalling

To remove the station, reverse the installer with:

```
sudo bash scripts/uninstall.sh            # remove service + code, keep config
sudo bash scripts/uninstall.sh --purge    # also delete /etc/copystation
```

It stops and disables the service, removes the systemd unit and `/opt/copystation`,
and unmounts anything left under `/run/copystation`. Your `/etc/copystation/config.yaml`
is kept unless you pass `--purge`. The apt packages the installer pulled in
(`rsync`, `python3-pyudev`, `python3-libgpiod`, `python3-spidev`, `gpiod`, exFAT
tools) are left in place -- remove them by hand if nothing else needs them.

## Source/target detection

Detection is independent of the order the devices are enumerated **and of whether
they are inserted at the same time**. A candidate is any USB *volume* -- either a
partition (`sdb1`) or a whole disk that carries a filesystem directly with no
partition table (`sdc`, as the O4 Air Unit presents it). The board's own OS card
is excluded, and volumes smaller than `identify.min_partition_gb` (default 6 GB)
are ignored. Once two eligible volumes are present:

* **Source** = the smallest volume that contains a `DCIM` folder (and, if
  configured, matches the USB VID/PID allowlist).
* **Target** = the largest of the remaining volumes.
* By default the source must be **smaller** than the target, so the larger
  device is never used as source even if it also carries a `DCIM` folder
  (`identify.require_source_smaller_than_target`).

Before copying, the target's free space is checked against the source's media
size; if it does not fit, the cycle ends in `Error` and the source is untouched.

**Friendly names:** volumes are labelled in the web UI by their filesystem label
or USB model. Because the O4's USB product string is only a serial, you can map a
readable name by USB VID/PID via `identify.device_labels`
(e.g. `2ca3:0020 -> "O4 Lite"`) -- find the VID/PID with `lsusb`.

**Detection speed:** after a USB event the station waits only until the bus is
quiet for `settle_quiet_seconds` (default 1 s) before mounting, capped by
`settle_seconds` (default 2 s). Reliability is unaffected -- a volume that appears
later simply triggers another evaluation.

## Configuration

See [config.example.yaml](config.example.yaml). Without the file the defaults
apply (status only via log, source detection by the `DCIM` folder plus the
capacity rules above).
