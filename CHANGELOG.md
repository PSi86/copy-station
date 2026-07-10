# Changelog

All notable changes to Copy_Station are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Three optional feature areas were added on top of the copy station: a self-hosted
**WiFi access point**, **web file access/download**, and **video transcoding** --
all off by default, so existing status-only deployments are unaffected.

### Added

#### WiFi access point (optional)
- Host a WLAN access point via **NetworkManager** (`nmcli`, `ipv4.method shared`
  gives DHCP + NAT) so the web interface is reachable in the field without an
  existing network. Config block `wifi_ap` (SSID, WPA2 password, band, channel,
  IPv4). The daemon raises it on start when enabled.
- Toggle the AP from a **user button** (`wifi_ap` action; recommended
  `triple_click`) with instant feedback: an **e-paper `WiFi` badge**, a matching
  web header badge, and dedicated **WS2812 blink codes** (cyan = on, amber = off).
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
  plays it in place (an `<video>`/`<img>` in a modal) instead of downloading it.
  The stream is served **inline** with a real content type and honours HTTP Range
  requests, so a video seeks and buffers without fetching the whole (multi-GB)
  file. **Download moved to the ⚙ dialog** (and is also offered inside the
  preview). Streaming obeys the same `allow_download` gate as a download (exposed
  as a `download` capability flag); a codec the browser can't decode (e.g. HEVC in
  Chrome) falls back to a download prompt.
- **Live HLS transcoding for the preview** (`preview` block, `/api/files/preview/*`):
  sources a browser can't play smoothly -- 4K, HEVC, mkv, ... -- are transcoded
  **on the fly** to a seekable 1080p30 H.264 HLS stream so they can be reviewed
  without downloading or stuttering. The playlist is a full VOD (seek anywhere);
  each segment is transcoded independently on demand (`ffmpeg` input-seek), so a
  jump just fetches that segment. On the Cubie the **hardware encoder** is used --
  ffmpeg does the seek and stream-copy, piped into a GStreamer OMX
  decode->scale->encode -- so the seek never touches the OMX pipeline; other boards
  use ffmpeg on the CPU. Browser-friendly sources (H.264 <=1080p) still play
  directly (no transcode). Previews are serialised (single hardware encoder) and
  only run while the station is idle. `hls.js` is vendored locally for browsers
  without native HLS (the AP is offline).

#### Video transcoding (optional)
- **ffmpeg** transcoding/resolution change from the web UI (`/api/transcode`),
  writing to a `Transcoded/` folder on the target volume (also downloadable).
  Single-worker queue, configurable presets, two-click cancel from the UI (a
  first click arms the button so a long encode is never aborted by a stray click).
- **Folder (batch) transcoding**: the ⚙ on a folder queues one independent job
  per video file inside it under a single preset -- not one "folder job", so each
  file picks its own hardware/CPU path and appears and cancels individually. The
  dialog shows up front whether the files are handled uniformly or split across
  the encoders (per-file HW / HW+CPU / CPU badges plus a count summary), via
  `GET /api/transcode/folder-plan` and `POST /api/transcode/folder`.
- **Board-aware hardware acceleration** with automatic **CPU fallback**
  (`transcode.acceleration`, `fallback_to_cpu`): uses the board's hardware
  encoder when it is present, otherwise software.
  - **Raspberry Pi 4**: ffmpeg `h264_v4l2m2m` (H.264). The Pi 5 has no hardware
    encoder and uses the CPU.
  - **Radxa Cubie A7S (Allwinner A733)**: a **GStreamer OpenMAX** pipeline --
    hardware-**decode** (`omxh264dec`/`omxhevcvideodec`), downscale **in the
    decoder** (its `scale` property, 1/2 or 1/4) and hardware-**encode** H.264
    (`omxh264videoenc`). A 4K→1080p clip (an exact 1/2) is a single hardware pass
    with the CPU essentially idle (~0.7× real-time for 4K60). The encoder's own
    scaler is not used -- it leaves a thin magenta line on the bottom row -- so a
    target that is not a clean 1/2-step (e.g. 720p from 4K) is hardware-downscaled
    to the nearest larger clean size and **finished to the exact height by a short
    ffmpeg CPU pass**. Bitrate is height- and framerate-aware (raise a preset's
    `bitrate` to raise quality; `crf` has no effect on the hardware encoder). H.265
    *output* has no hardware encoder and stays on the CPU; a source with non-AAC
    audio or an unusual container also falls back to the CPU (audio is stream-copied
    on the hardware path). A stall watchdog kills a wedged OMX pipeline so a stuck
    hardware job never hangs the station.
    (This replaces the earlier assumption that the A733 encoders were unreachable
    from Linux -- ffmpeg cannot reach them, but GStreamer OMX can. Note the A733
    exposes only an H.264 OMX *encoder*, not the once-assumed `omxhevcvideoenc`.)
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
