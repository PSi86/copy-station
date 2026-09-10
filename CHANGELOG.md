# Changelog

All notable changes to Copy_Station are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.2] - 2026-09-10

Detection fix from a field report on a Raspberry Pi 4 running Raspberry Pi OS
Bookworm: a DJI O4 Lite that the Pi enumerated fine as USB storage never showed
up in Copy_Station.

### Fixed
- **Whole-disk devices such as the DJI O4 are detected on Raspberry Pi OS
  Bookworm.** udev reported `ID_FS_TYPE=exfat` together with
  `ID_PART_TABLE_TYPE=dos` for the whole disk, while the kernel created no
  partition on it. Copy_Station skipped every disk that carries a partition
  table, on the assumption that its partitions are the candidates -- here there
  were none, so the unit was missing from the station and from the file browser
  alike. The `dos` comes from libblkid: up to util-linux 2.38 its DOS partition
  prober excludes FAT and NTFS boot sectors but not exFAT, whose boot sector
  carries the same `55AA` signature as an MBR. 2.39 added the exFAT check;
  Bookworm ships 2.38.1, Debian 13 Trixie 2.41 -- which is why the Trixie boards
  this was developed on never showed it. Whether a disk is partitioned is now
  decided by the partitions the kernel actually created (the `partition`
  attribute in sysfs), not by udev's guess. A disk the kernel did split up is
  still handled via its partitions, now also when udev reports a filesystem on
  the disk itself.
- A **Walksnail air unit** showed the same symptom on the same Pi and is now
  listed too. It cannot be a copy source yet: it records to the root of its
  storage, not to a `DCIM` folder.

## [1.2.1] - 2026-09-06

A re-image of the A733 station turned up two silent failures and dated one
warning: an access point that raised itself at every boot despite being switched
off, an installer that did not recognise the very board it has a special case
for, and a warm-reboot bug that the current kernel has fixed.

### Fixed
- **The WiFi AP no longer comes up on its own at boot when it is switched off.**
  Switching the AP off only ran `nmcli connection down` and left the profile on
  disk -- and that profile carries `autoconnect yes`, so NetworkManager
  auto-activated it about 19 s into every boot, long before the daemon starts.
  The station broadcast its SSID, accepted WPA2 clients and handed out DHCP
  leases (1 h) for ~20 s until the startup reconcile brought it down again, all
  while the persisted state said off. Measured on a Cubie A7S: hotspot up at
  boot +19 s, taken down at boot +39 s. Switching the AP off now also deletes
  the profile, and the startup reconcile deletes a leftover one even when the AP
  is already down -- so a persisted "off" holds *during* the boot, not just
  after it. Nothing is lost by deleting: every bring-up rebuilds the profile
  from the config anyway. `autoconnect` keeps its remaining job, holding a
  *running* AP up across an interface flap. Switching off is idempotent with it:
  with no profile left `nmcli connection down` exits 10 ("does not exist"), which
  is the state being asked for and no longer surfaces as a warning.
- **`scripts/install.sh` did not recognise the Radxa Cubie A7S.**
  `/proc/device-tree/model` reads `sun60iw2` -- the Allwinner sunxi SoC name, not
  a marketing name -- which matched none of the installer's
  `cubie|radxa|a733|a7s|allwinner` patterns. So on the exact board those branches
  exist for, the installer silently skipped both the GStreamer install (leaving
  hardware transcoding to fall back to the CPU, since the OMX elements need
  `gstreamer1.0-tools`/`-plugins-bad`) and the board-specific config example
  (installing the generic one with placeholder pins instead). Both patterns now
  include `sun`, matching `encoders.py:detect_board()`, which had it right all
  along -- which is why the daemon reported `board: cubie` on a station whose
  installer had treated it as generic.

### Changed
- **The A733 warm-reboot warning is now qualified by kernel version.** The README
  stated flatly that a warm `reboot` never comes back on the A733 and that
  `poweroff` plus a physical power-cycle is the only way. That was measured on
  kernel `6.6.98-3-aw2511`; on `6.6.98-4-aw2511` a warm reboot recovers -- 3 of 3
  attempts, back in 42-52 s, each with a new boot id and the service active. The
  section now gives both cases in a table and says to check `uname -r`, because
  the old blanket advice sends someone to the device for no reason on a current
  image while still being correct on an older one. It also notes that
  `copystation.service` reaches `active` about 30 s into the boot, so a status
  check made sooner reads `inactive` with nothing wrong, and that `boot_id` is the
  way to tell a real reboot from a connection that merely came back.

### Added
- **`docs/backup-and-restore.md`** -- what to save before re-imaging a card, and
  how to put it back. The point of it is the runtime overlay
  `/var/lib/copystation/user-settings.json`: it **wins over `config.yaml`**, so a
  station restored from its config alone comes back subtly different from the one
  that was backed up -- the access point down, the default preset reset -- for no
  visible reason. The document also names what provably does NOT need saving (the
  AP's NetworkManager profile is deleted and re-created from the config on every
  raise, so backing it up would be backing up a cache), and warns that GPIO line
  offsets are kernel-dependent while header pins are not, which is why a `gpioinfo`
  capture belongs in the backup. Linked from the README's Contents and from
  "Uninstalling", the step right before a re-image.

## [1.2.0] - 2026-09-06

Access-point release from field feedback on a Raspberry Pi 4 that lost its
Ethernet/SSH connection whenever the AP was up: the setups that break an existing
LAN are now named at startup, the captive portal no longer occupies the LAN side,
and an AP without a hardware button can finally be switched off again.

### Added
- **WiFi AP switch in the web interface.** The header carries a `WiFi AP on/off`
  button whenever the AP is configured (i.e. it has a password), so a station with
  no user button attached is no longer stuck with `wifi_ap.enabled` until someone
  edits the config. It gives the same feedback as the button press (display badge,
  WS2812 blink code) and persists the same state; switching it off asks first,
  because it drops the caller's own connection when the page is open over the AP.
  New endpoint `POST /api/wifi_ap` (`{"enabled": true|false}`).
- **`wifi-ap` CLI subcommand** (`python -m copystation.daemon ... wifi-ap
  on|off|toggle|status`) as the rescue path when neither a button nor a reachable
  web interface exists. Writes the same persisted state, so it survives a restart.
- **AP address conflict check.** Before the AP is raised, its subnet is compared
  against the addresses already configured on the machine and a clash is warned
  about with both sides named (`... eth0 is on 192.168.1.0/24 which OVERLAPS the AP
  subnet 192.168.1.0/24`). An overlapping subnet -- or an AP address the station
  already carries -- routes LAN replies out over Wi-Fi and kills every established
  Ethernet/SSH connection the moment the AP comes up.
- README section **"Running the AP while the station is on a LAN"**: subnet choice,
  the NetworkManager-vs-`dhcpcd` clash, and the commands to tell them apart.
- **The installer warns about a second network stack.** With the AP enabled, an
  active `dhcpcd` or `systemd-networkd` alongside NetworkManager is reported at
  install time: both manage the same interfaces, and the first `nmcli` call -- the
  AP bring-up -- is what makes them collide, taking the wired connection (SSH
  included) with it.
- **A link to the project** at the bottom of the web interface -- the GitHub mark
  plus the repository name, on its own line below the connection status. Both the
  spelled-out name and the inline SVG follow from the same fact: a phone reading
  this page is usually joined to the station's access point and has no internet,
  so it cannot follow the link there and then (the name is what it takes away) and
  an icon from a CDN would not load at all.
- The **installer prints how to reach the station** when it is done: the web URL,
  whether a login is required, the WLAN SSID/password, the URL over the AP and how
  to switch the AP on -- instead of leaving the values to be dug out of the YAML. A
  self-chosen password is *not* echoed (it would land in the install log); only the
  shipped default is, which is public anyway.

### Security
- The shipped configs now carry a **default password `copystation`** for both the
  WLAN access point and the (still disabled) web auth -- see *Changed* below. It is
  published in this repository and therefore **not a secret**: anyone in radio range
  can join the AP and reach the file browser, including delete. Set your own
  `wifi_ap.password` (and consider `web.auth.enabled: true`) for any station whose
  cards should stay private.

### Changed
- Runtime AP switching (button, web, CLI) now goes through **one controller**
  (`wifi_ap.ApController`), so every trigger gives the same feedback, persists the
  same state and keeps the captive portal in step.
- The **captive portal** binds the **AP address only** instead of `0.0.0.0`, and is
  started/stopped together with the AP. A wildcard bind occupied port 80 on the LAN
  side too and answered LAN requests with a redirect to an address only AP clients
  can reach. The DNS drop-in is still written before the AP comes up (NetworkManager's
  dnsmasq reads it at activation).
- **The shipped configs are set up to just work** (`config.example.yaml` and both
  board configs): the **captive portal is on**, and the **WiFi AP and web auth carry
  a ready-made password** (`copystation`, the same string for both, so there is one
  credential to remember and enabling auth is a one-word change). `web.auth.enabled`
  stays **false** -- the password is only pre-filled, not activated. Rationale: an AP
  without a password never comes up, and an AP without the captive portal is the
  classic "joined the WLAN, page will not open". Read the *Security* note above
  before deploying as-is.
- `config.example.yaml` (the fallback config for boards the installer does not
  recognise) now has **`web.enabled: true`**, like both board configs -- the
  installer's prompt defaults to enabling it anyway, and the captive portal it now
  ships with exists precisely to serve that interface.
- A captive portal configured **without** the web interface is now only a *warning*
  when the AP can actually be raised (config, persisted state or a button); otherwise
  it is an informational line. The portal ships enabled, so a station deliberately
  running without a web interface should not be scolded on every start.
- **The network is treated as something that arrives, not as a precondition.** The
  service is deliberately *not* ordered after `network-online.target`: copying cards
  is this station's job and must not wait for a network it does not need (that
  ordering would cost every boot the `wait-online` delay -- up to its timeout with a
  cable but no DHCP server). The daemon handles it at runtime instead.

### Fixed
- **`web.host` set to a concrete address is now reported.** With the AP configured,
  a non-wildcard `web.host` makes the interface listen on that one address, so it is
  refused over the AP -- the daemon warns and, more importantly, no longer prints the
  `Web interface over the AP: http://<ap-ip>:<port>/` line for a bind that does not
  cover that address.
- **Boot race on a concrete `web.host`.** The service could start before DHCP had
  assigned the address; the bind then failed for good and left no web interface at
  all until the next restart. A host address that is not there *yet* is no longer
  treated as an error: the server thread waits for it in the background (1 s, backing
  off to 10 s, with a note in the journal) and binds the moment it appears, while the
  daemon carries straight on -- the copy functionality is available immediately and
  independently of the network. A genuine bind failure (port taken, no permission)
  still raises at startup as before.

## [1.1.1] - 2026-07-25

Diagnostics release from field feedback on a Raspberry Pi 4: two startup failures
that reported themselves misleadingly (an anonymous GPIO `EBUSY`, and a web
interface logged as up while its socket never bound) now name their cause.

### Added
- **GPIO conflict check at startup.** The configured lines of every *enabled*
  status backend and user button are compared before any hardware is opened, and a
  line claimed twice is reported with both sides
  (`GPIO line conflict: gpiochip0 line 17 is claimed by status.grove_led_bar.data_line
  and status.epaper.rst ...`). Previously only the feature initialised second failed,
  with a bare `Device or resource busy` and no hint at the first owner. Pins the
  panel preset supplies (the 2.13" HATs' `pwr: 18`) are included.

### Changed
- The example configs (which `install.sh` copies to `/etc/copystation/config.yaml`)
  no longer enable the **Grove LED bar** out of the box -- `status.backends` is
  `[log]` on both boards now. An enabled backend claims its GPIO lines whether the
  hardware is connected or not, so the pre-enabled bar silently blocked the pins of
  the hardware the user actually wired.
- The **Raspberry Pi example config** no longer pre-assigns colliding pins: the Grove
  LED bar moved to BCM6/BCM5 (was BCM18/BCM17 -- the e-paper's `pwr`/`rst`) and the
  buzzer to BCM12 (was BCM24 -- the e-paper's `busy`), so the status backends can be
  enabled side by side. Same for the Pi hints in `config.example.yaml`.

### Fixed
- The web interface is now logged as **listening only once uvicorn has actually
  bound** the socket. A failed bind (e.g. a `web.host` that does not exist on the
  machine -> `[Errno 99] cannot assign requested address`) is reported as
  `Web interface could not be started: ...` with the cause, instead of a success line
  followed by an unattributed uvicorn error; the AP URL is no longer advertised for
  an interface that is down.

## [1.1.0] - 2026-07-24

Transcode usability release: the output goes where the source lives, the browser
stutter hint stops crying wolf, and the whole batch is reported consistently on the
main progress bar (like the e-paper).

### Added
- **Transcode output location** (`transcode.output_location`, toggleable in the web
  UI's Transcode card and persisted in the user-settings overlay). Transcoded files
  are always written to the **source file's own medium**, either in a `Transcoded/`
  folder **next to the original** (`same`, the default) or in one **central
  `Transcoded/` folder at the medium's root** (`central`).
- The **main progress bar** now shows a running transcode, the same way the e-paper
  does: the whole-queue percent, elapsed and remaining time, the current file with
  its job position (`job X/N`) and the encode fps. The Transcode card keeps the
  per-file rows (each with its own bar) and the finished results with download links.

### Changed
- The in-browser preview **"may stutter" hint** is now driven only by resolution
  (clearly above Full HD, `preview.max_direct_height`). A 540p HEVC clip or a 1080p
  `.mkv` no longer trips it -- codec and container are ignored; a source the browser
  cannot decode at all is still handled by the player's own fallback.

### Fixed
- The transcode **queue ETA** now spans every pending job: a queued job with no
  learned estimate of its own is extrapolated from the running job's projected time,
  so the total no longer collapses to just the current file.
- The **elapsed time** on the main bar (and the e-paper batch view) is now the whole
  queue's wall time instead of resetting to each file's own elapsed at every file.
- The Allwinner OMX **HEVC** encoder's magenta bottom strip is removed by applying
  the conformance-window crop the encoder omits (a no-re-encode SPS-crop remux for a
  single-pass job, and a padding-row crop before the CPU downscale for a two-stage
  job).

### Removed
- The transcode **output-volume picker** (from the Transcode card and the file/folder
  dialogs) and the `output_device` request field: the output always follows the
  source's medium now, so there is nothing to choose.

## [1.0.1] - 2026-07-12

Documentation/estimate refinement only -- no behaviour change to an existing
deployment beyond the per-job time estimate shown for a fresh install.

### Changed
- Completed the **"Source 4K H.265 (HEVC)"** throughput table in the README with
  on-device measurements, and seeded the matching `DEFAULT_PERF` estimate keys so a
  fresh install estimates these jobs up front instead of learning them on the first
  run:
  - **Radxa Cubie A7S** HEVC-source column (1080p H.264 59 fps, 720p H.264 25,
    540p H.264 61, 720p H.265 7). The A733's HEVC hardware decoder is **faster than
    its H.264 decoder**, so HEVC-source single-pass transcodes run faster than the
    H.264-source ones.
  - **Raspberry Pi 4** HEVC → 540p H.264 (17 fps), the last missing cell. The Pi 4's
    HEVC → H.264 transcodes are **HEVC-decode-bound** (~22 fps 4K HEVC decode; the HW
    H.264 encode adds almost nothing); with a 100 fps source, 540p stays on the HW
    encoder while 720p/1080p exceed H.264 level 4.0's MB/s budget and fall back to
    the CPU. Documented in a new footnote.

## [1.0.0] - 2026-07-12

First tagged release. It rolls up the autonomous copy station together with the
optional status backends (LEDs, buzzer, WS2812, Grove LED Bar, e-paper) and adds
three optional feature areas on top: a self-hosted **WiFi access point**, **web
file access/download**, and **video transcoding** -- all off by default, so
existing status-only deployments are unaffected.

### Added

#### WiFi access point (optional)
- Host a WLAN access point via **NetworkManager** (`nmcli`, `ipv4.method shared`
  gives DHCP + NAT) so the web interface is reachable in the field without an
  existing network. Config block `wifi_ap` (SSID, WPA2 password, band, channel,
  IPv4). The daemon raises it on start when enabled.
- Toggle the AP from a **user button** (`wifi_ap` action; recommended
  `triple_click`) with instant feedback: an **e-paper `WiFi` badge**, a matching
  web header badge, and dedicated **WS2812 blink codes** (cyan = on, amber = off).
- The AP on/off state is now **persisted** (in the shared `user_settings_file`)
  and **survives a restart independent of `wifi_ap.enabled`** -- the overlay wins
  over the config, so a runtime toggle sticks; `enabled` is only the initial
  value. On start the daemon reconciles a stale-up AP back down when it should be
  off.
- Optional **captive portal** (`wifi_ap.captive_portal`): a NetworkManager
  dnsmasq drop-in points all DNS at the AP and a small port-80 redirect server
  sends clients to the web UI, so a joining device auto-opens the interface and
  stays on the AP.

#### Web file access & download (optional)
- Read-only **file browser + download** of the attached USB mass storage
  (`/api/volumes`, `/api/files`, `/api/files/download`) with a Files panel in the
  single-page frontend. Only USB volumes are exposed -- the OS/root device is
  never listed. Volumes are mounted read-only under a separate base and reaped
  when idle; path traversal (`..`, symlinks) is refused.
- Optional **HTTP Basic auth** for the whole interface (`web.auth`), off by
  default and fail-safe (rejects when enabled without a password).
- **In-browser preview / playback** (`/api/files/stream`): clicking a file now
  plays the **original** in place (an `<video>`/`<img>` in a modal) instead of
  downloading it -- instant, no wait. The stream is served **inline** with a real
  content type and honours HTTP Range requests, so playback seeks and buffers
  without fetching the whole (multi-GB) file. **Download moved to the ⚙ dialog**
  (and is also offered inside the preview). Streaming obeys the same
  `allow_download` gate as a download (exposed as a `download` capability flag).
- **"Transcode for smooth playback" hint** (`preview` block, `/api/files/preview-info`):
  sources a browser/SoC can't play smoothly (4K, HEVC, ...) still play the original
  (which may stutter -- the Cubie's VPU decodes 4K60 at ~0.55x realtime, a hardware
  ceiling), and the player shows a hint with a shortcut into the transcode dialog.
  So a user gets an instant rough look and starts a transcode only when they want
  smooth playback -- rather than always paying a transcode wait up front.

#### Video transcoding (optional)
- **ffmpeg** transcoding/resolution change from the web UI (`/api/transcode`),
  writing to a `Transcoded/` folder on the target volume (also downloadable).
  Single-worker queue, configurable presets, two-click cancel from the UI (a
  first click arms the button so a long encode is never aborted by a stray click).
- **Auto-transcode after a copy** (`transcode.auto_transcode`, or the web UI's
  *Auto-transcode after copy* switch): a successful copy automatically queues a
  transcode of every just-copied video file (onto the target, using the default
  preset). The **source card is unmounted before the batch starts**, so it can be
  removed while the transcodes run on the target alone.
  - The switch and default preset are **read only when a copy finishes**, so both
    can be changed at any time (including mid-copy) to decide per copy.
  - Toggle auto-transcode from a **user button** (the new `auto_transcode`
    action) as well as the web UI; the **e-paper panel shows an `Auto` badge**
    while it is enabled (beside the `WiFi` badge) and a WS2812 strip plays a
    purple blink on each toggle (`AUTO_TRANSCODE_ENABLED`/`DISABLED` signals).
  - A **persisted default preset** (`transcode.default_preset`): chosen in the
    *Transcode* card, preselected in the per-file/folder ⚙ dialogs, and used by
    auto-transcode; the first configured preset until one is chosen. The default
    preset and the auto-transcode toggle are saved via
    `POST /api/transcode/settings` to the **single runtime-settings overlay**
    (`user_settings_file`, default `/var/lib/copystation/user-settings.json`), so a
    web-UI change survives a restart without rewriting the commented config.
  - All runtime-mutable settings (auto-transcode, default preset, WiFi AP state)
    now live in **one** overlay file, each under its own section, via a shared
    **self-healing store** that drops sections/keys a software update
    renamed/removed on load (only the overlay is cleaned; `config.yaml` is never
    touched).
  - **Queue visibility**: `GET /api/transcode` now reports a `queue` aggregate
    (pending count, position `i/n`, overall percent, total remaining time). The
    web UI shows a queue summary + an **overall progress bar**, and the **e-paper
    panel** shows `Transcode i/n`, the whole-queue bar and the total ETA (with the
    current file's own progress as text).
  - A **batch runs as one queue under a single `Transcoding` phase** (holding the
    operation lock for the whole run), so the display no longer flashes between
    files and the copy daemon can never slip a copy between two transcode files.
- **Folder (batch) transcoding**: the ⚙ on a folder queues one independent job
  per video file inside it under a single preset -- not one "folder job", so each
  file picks its own hardware/CPU path and appears and cancels individually. The
  dialog shows up front whether the files are handled uniformly or split across
  the encoders (per-file HW / HW+CPU / CPU badges plus a count summary), via
  `GET /api/transcode/folder-plan` and `POST /api/transcode/folder`.
- **Board-aware hardware acceleration** with automatic **CPU fallback**
  (`transcode.acceleration`, `fallback_to_cpu`): uses the board's hardware
  encoder when it is present, otherwise software.
  - **Raspberry Pi 4**: hardware **H.264 encode** (`h264_v4l2m2m`) **and** the same
    HEVC hardware **decode** as the Pi 5 (`-hwaccel drm`), so an HEVC source is
    decoded *and* encoded in hardware in one pass (`h264_v4l2m2m` with `-hwaccel
    drm`; the hardware-decode offload was extended to decorate hardware encoders,
    not just the CPU one). Measured on-device: 4K HEVC → 720p H.264 ~0.54×, a near-
    full-hardware pass. Two limits: the H.264 encoder defaults to H.264 **level 4.0**
    (~1080p30), so a **1080p output above 30 fps** (e.g. from a 4K60 source) exceeds
    it and falls back to the CPU (a framerate limit, enforced since kernel 6.6.31 —
    ≤30 fps 1080p and 720p60 stay in hardware); and there is no 4K H.264 hardware
    decoder (4K H.264 decodes on the CPU). Per-board estimate seeds now include the
    Pi 4. **Bug fix:** `available_encoders()`
    dropped ffmpeg encoders listed with no capability flags (`V.....`), which is how
    the Pi's `h264_v4l2m2m`/`hevc_v4l2m2m` wrappers appear — so the Pi 4 hardware
    encoder was never actually selected before; it now is.
  - **Raspberry Pi 5**: no hardware *encoder* exists, so every output is a
    software `libx264`/`libx265` encode -- but HEVC (H.265) *input* is now
    hardware-**decoded** via ffmpeg `-hwaccel drm` (the Pi 5's 4Kp60 HEVC block,
    `/dev/video19`), offloading the decode so the CPU is free for the encode
    (measured ~1.4× faster on a 4K HEVC → 1080p job on-device). Applied
    automatically for HEVC sources and falls back to software decode if the
    hardware path fails; the job row shows `cpu (hevc hw-decode)` when used. H.264
    input has no hardware decoder on the Pi 5 and decodes on the CPU.
    `acceleration: cpu` forces pure software (no hardware decode). The per-board
    duration-estimate seeds now include the Pi 5 (measured 4K60 H.264 and HEVC).
  - **Radxa Cubie A7S (Allwinner A733)**: a **GStreamer OpenMAX** pipeline --
    hardware-**decode** (`omxh264dec`/`omxhevcvideodec`), downscale **in the
    decoder** (its `scale` property, 1/2 or 1/4) and hardware-**encode** H.264
    (`omxh264videoenc`). A 4K→1080p clip (an exact 1/2) is a single hardware pass
    with the CPU essentially idle (~0.7× real-time for 4K60). The encoder's own
    scaler is not used -- it leaves a thin magenta line on the bottom row -- so a
    target that is not a clean 1/2-step (e.g. 720p from 4K) is hardware-downscaled
    to the nearest larger clean size and **finished to the exact height by a short
    ffmpeg CPU pass**. Bitrate is height- and framerate-aware (raise a preset's
    `bitrate` to raise quality; `crf` has no effect on the hardware encoder). **H.265
    output is hardware-encoded too** (`omxhevcvideoenc`) -- a clean 1/2-step H.265
    target (e.g. 1080p) is a single hardware pass ~10x faster than CPU `libx265`
    (used when the installed GStreamer exposes the element -- older Radxa images
    shipped it non-functional -- else it falls back to the CPU; it also needs a
    current mesa/libgbm). A source with non-AAC audio or an unusual container falls
    back to the CPU (audio is stream-copied on the hardware path). A stall watchdog
    kills a wedged OMX pipeline so a stuck hardware job never hangs the station.
    (This replaces the earlier assumption that the A733 encoders were unreachable
    from Linux -- ffmpeg cannot reach them, but GStreamer OMX can.)
- **RAM output buffering** (`transcode.ram_buffer`): the input streams from the
  card while the output is staged in a size-capped `tmpfs` and written back in one
  bulk write, so the card is never read and written at once. Input size is
  irrelevant (only the output is buffered); capped at `ram_buffer_fraction` of
  free RAM.
- A running transcode is a **first-class station phase** (`TRANSCODING`) that
  overrides every status indicator and is mutually exclusive with copying (a copy
  blocks a transcode and vice versa). Progress bar on the **LEDs** (purple on
  WS2812) and **e-paper** (bar, file name, encoder, size, fps, elapsed/ETA), plus
  elapsed/remaining time on the running job in the web UI.

#### Shared / infrastructure
- `copystation/volumes.py`: reusable USB-volume enumeration + OS-exclusion, shared
  by the device watcher and the web file browser.
- In-process `operation_lock` serialising a copy evaluation and a transcode job.
- Installer pulls in `ffmpeg`; notes NetworkManager for the AP. New config
  examples for all blocks; README sections for every feature.

### Changed
- Default software transcode `preset` is **`veryfast`** (SBC-friendly; much faster
  than `medium` for a modest size increase). `preset` is ignored by hardware
  encoders.
- The web app factory takes the config plus optional browse/transcode managers;
  the daemon wires them through the status hub.

### Fixed
- Transcoding **never overwrites an existing output** of the same name -- it falls
  back to `<stem>_2`, `_3`, ... This matters most for a folder batch where two
  sources map to the same output name (`DJI_0001.MP4` + `DJI_0001.MOV` both ->
  `DJI_0001_<preset>.mp4`); previously the second job silently clobbered the
  first. `.lrv` low-resolution proxy files are excluded from folder submission.
- The web UI **disables browsing, downloads and the ⚙ controls while a copy or
  transcode holds the volumes** (a hint explains why, and only the running job's
  Cancel stays live), instead of letting those requests 503 against the busy
  device.
- A **canceled transcode again trains the duration-estimate model**: the source
  is now probed while the volume is still mounted, so a long-enough canceled job's
  sample is no longer silently dropped by a re-probe that failed because the
  volume had already been unmounted.
- Transcode output `EROFS`/read-only failures when the target card was also
  browsed: `mount_rw` now drops any other mount of the device, verifies
  writability up front, and fails early with a clear message instead of after
  minutes of encoding.
- A failed transcode is now surfaced on **every** backend (ERROR phase on the
  e-paper and LEDs), not only the web job row.
- Downloads send a real content type (e.g. `video/mp4`) so restricted clients
  (captive-portal webview) no longer save a `.mp4` as `.bin`.
- The finished-job row shows the **transcoded** file size, not the source size.
- AP button feedback and the display badge appear immediately on the press
  (before the slow `nmcli` call), and a startup diagnostic warns when the AP is
  configured but the web interface is disabled.

[Unreleased]: https://github.com/PSi86/copy-station/compare/v1.2.2...HEAD
[1.2.2]: https://github.com/PSi86/copy-station/releases/tag/v1.2.2
[1.2.1]: https://github.com/PSi86/copy-station/releases/tag/v1.2.1
[1.2.0]: https://github.com/PSi86/copy-station/releases/tag/v1.2.0
[1.1.1]: https://github.com/PSi86/copy-station/releases/tag/v1.1.1
[1.1.0]: https://github.com/PSi86/copy-station/releases/tag/v1.1.0
[1.0.1]: https://github.com/PSi86/copy-station/releases/tag/v1.0.1
[1.0.0]: https://github.com/PSi86/copy-station/releases/tag/v1.0.0
