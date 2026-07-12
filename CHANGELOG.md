# Changelog

All notable changes to Copy_Station are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/PSi86/copy-station/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/PSi86/copy-station/releases/tag/v1.0.1
[1.0.0]: https://github.com/PSi86/copy-station/releases/tag/v1.0.0
