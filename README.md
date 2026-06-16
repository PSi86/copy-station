# Copy_Station

Autonomous copy station: automatically transfers the footage of a camera
connected via USB as mass storage (e.g. **DJI O4 Air Unit**, `DCIM` folder) onto
a **(micro) SD card**, verifies the transfer and then clears the source. Runs as
a systemd service on a **Radxa Cubie A7S** (`radxa-a733-bullseye-cli-r6`,
headless).

## Flow

```
Ready ──device detected──► Detecting ──source+target ok──► Copying
  ▲                                                          │
  │                                              verify (size+count)
  │                                                          │
  └────────── devices removed ◄── Success ◄──── clear source DCIM
                              (error ⇒ Error, source left untouched)
```

* **Copy:** into a new folder `transfer_<NNNN>_<source_name>` -- nothing is ever
  overwritten. The running number is persisted on the SD card.
* **Verification:** fast comparison of file count and file sizes.
* **Cleanup:** only on success; only the `DCIM` contents are deleted, never
  formatted.
* **Status:** `Ready / Detecting / Copying / Error` via interchangeable backends
  (log, LEDs, buzzer, WS2812, Grove LED Bar -- freely combinable).

## Web interface (optional)

Set `web.enabled: true` in the config to host a local status page on **all
network interfaces** (`0.0.0.0:8080` by default). It shows live phase, copy
progress (percent, elapsed, ETA), and the capacity/used/free space of both mass
storages; it is prepared for future settings. Binding to `0.0.0.0` makes it
robust to interfaces going down/up at runtime -- no per-interface rebinding.

The frontend is a single static page (vanilla JS, no build step) that polls
`/api/status` every 500 ms; the backend is FastAPI (`/docs` for the auto API
docs). Open `http://<device-ip>:8080/`.

## Grove LED Bar v2.0 (optional)

Add `grove_led_bar` to `status.backends` to drive a Seeed Grove LED Bar v2.0
(MY9221) over two GPIO lines. During a copy the bar shows the proportional fill
and blinks at 10 Hz; when idle a single LED is steady (Ready = green / segment 3,
Detecting = yellow / segment 2, Error = red / segment 1). Set the `clock_line` /
`data_line` offsets (from `gpioinfo`) in the config.

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

## Deployment (Cubie)

```
sudo ./scripts/install.sh
```

Installs dependencies (`rsync`, `python3-pyudev`, `python3-libgpiod`), copies the
code to `/opt/copystation`, creates `/etc/copystation/config.yaml` and enables
the service.

Determine the GPIO pins for LEDs/buzzer before the first hardware test:

```
gpiodetect
gpioinfo
```

and enter them in `/etc/copystation/config.yaml` under `status.led` /
`status.buzzer`, then set `status.backends` accordingly.

## Source/target detection

Detection is independent of the order the devices are enumerated. Among the USB
partitions (the Cubie's own OS card is excluded, and partitions smaller than
`identify.min_partition_gb`, default 6 GB, are ignored):

* **Source** = the smallest partition that contains a `DCIM` folder (and, if
  configured, matches the USB VID/PID allowlist).
* **Target** = the largest of the remaining partitions.
* By default the source must be **smaller** than the target, so the larger
  device is never used as source even if it also carries a `DCIM` folder
  (`identify.require_source_smaller_than_target`).

Before copying, the target's free space is checked against the source's media
size; if it does not fit, the cycle ends in `Error` and the source is untouched.

## Configuration

See [config.example.yaml](config.example.yaml). Without the file the defaults
apply (status only via log, source detection by the `DCIM` folder plus the
capacity rules above).
