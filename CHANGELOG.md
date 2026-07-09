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

#### Video transcoding (optional)
- **ffmpeg** transcoding/resolution change from the web UI (`/api/transcode`),
  writing to a `Transcoded/` folder on the target volume (also downloadable).
  Single-worker queue, configurable presets, cancel from the UI.
- **Board-aware hardware acceleration** with automatic **CPU fallback**
  (`transcode.acceleration`, `fallback_to_cpu`): uses the board's hardware
  encoder when the installed ffmpeg exposes it (e.g. `h264_v4l2m2m` on the Pi 4),
  otherwise software. The Pi 5 has no hardware encoder; the Cubie A7S's encoders
  are only reachable via GStreamer OpenMAX (not ffmpeg), so both use the CPU.
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
