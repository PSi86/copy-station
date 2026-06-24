# Implementation plan: read-only OS root (power-loss hardening)

Status: **proposal / not yet implemented.** This document is the design for making
the Copy_Station's OS resilient to sudden power loss, plus the tooling to update
the OS and apps despite the read-only root.

## 1. Goal & motivation

The station is powered from a power bank and is often disconnected by simply
removing power. Cutting power while the OS card is being written can corrupt the
ext4 root filesystem. Goal: mount `/` **read-only** with a **tmpfs overlay** so
that at runtime nothing is written to the SD root — a power cut can then no longer
corrupt the OS. Writes during runtime land in RAM and are discarded on the next
boot.

Non-goals: this does not fix the A733 warm-`reboot` bug (reboots still require a
full power-cycle — see README "Powering off safely"), and it does not protect the
**target** SD card (which is written during a copy by design; that path is already
crash-safe because the source is only cleared after a verified copy).

## 2. What must stay persistent

Inventory of state the station owns, and where it must live:

| Data | Today | Under overlay |
| --- | --- | --- |
| App code + venv (`/opt/copystation`) | OS root | must be persistent |
| Config (`/etc/copystation/config.yaml`) | OS root | must be persistent |
| systemd unit (`/etc/systemd/system/copystation.service`) | OS root | frozen in the read-only lower (fine) |
| Transfer counter (`<target>/.copystation/counter`) | **on the target SD** | unaffected — not local |
| Mount base (`/run/copystation/mnt`) | tmpfs (`/run`) | already volatile — fine |
| Logs (journald) | `/var/log` | volatile under overlay (acceptable) or persist to data partition |
| apt-installed system deps (`rsync`, `python3-pyudev`, `python3-libgpiod`, `gpiod`, exFAT) | OS root | frozen in the read-only lower (fine, but new installs need the overlay handling) |

Key takeaway: the only **local** persistent state is the **app** and the
**config**. The transfer counter is on the target card, so it survives regardless.

## 3. Architecture

Two layers:

1. **Read-only root + tmpfs overlay** via the Debian `overlayroot` package
   (initramfs-based). `/` becomes an overlay of a read-only lower (the SD root)
   and a tmpfs upper (RAM). Runtime writes go to RAM and vanish on reboot.

2. **A separate persistent, writable partition** (label `cs-data`, mounted at
   `/data`) that holds the app + config + logs, bind-mounted back over their usual
   paths. This is what lets **app/config updates happen with the overlay still on**
   — only OS-level changes need the overlay toggled.

```
/  (overlay: ro lower [SD rootfs] + rw upper [tmpfs])
├── opt/copystation      ← bind mount from /data/copystation/opt   (persistent)
├── etc/copystation      ← bind mount from /data/copystation/etc   (persistent)
└── var/log/copystation  ← bind mount from /data/copystation/log   (persistent, optional)
/data  (ext4, writable, NOT overlayed)
```

A bind mount "punches" a writable hole through the overlay for that path, because
the bind is applied from `/etc/fstab` (which is read from the lower at boot).

## 4. Mechanism: `overlayroot`

- Install: `apt install overlayroot` (Debian Trixie on the Cubie; also works on
  Raspberry Pi OS — alternatively the Pi's `raspi-config` "Overlay File System").
- Config file: `/etc/overlayroot.conf`
  - `overlayroot="tmpfs"` → enable RAM overlay
  - `overlayroot=""` → disabled (normal writable root)
  - optionally `overlayroot_cfgdisk="disabled"`
- Activation requires `update-initramfs -u` after a config change.
- `overlayroot-chroot`: helper that mounts the underlying (lower) root read-write
  and chroots into it, to make **persistent** changes while the overlay is active.
- Emergency: add `overlayroot=disabled` to the kernel cmdline to boot writable once
  (recovery if a config change breaks boot).

Why `overlayroot` over a hand-rolled initramfs overlay: it is packaged, tested,
swap-aware, and ships the `overlayroot-chroot` escape hatch.

## 5. The enable/disable script — `scripts/overlay.sh`

A single root script: `sudo bash scripts/overlay.sh {enable|disable|status}`.

Because a warm `reboot` never recovers on the A733, the script **never reboots by
itself** — it applies the change and prints a clear "power-cycle now" instruction.
(On a Pi, where `reboot` works, it may offer `--reboot`.)

- `status`
  - Report whether `/` is currently an overlay: `findmnt -n -o FSTYPE /` → `overlay`
    means active.
  - Report the configured intent from `/etc/overlayroot.conf`.
  - Report whether a pending change needs a power-cycle (configured ≠ active).

- `enable`
  1. `apt-get install -y overlayroot` if missing (must be run while writable — see
     guard below).
  2. Write `overlayroot="tmpfs"` to `/etc/overlayroot.conf`.
     - If the overlay is **already active**, the conf is read-only → edit it via
       `overlayroot-chroot` (writes to the lower).
     - If writable, edit directly.
  3. `update-initramfs -u` (inside `overlayroot-chroot` if active).
  4. Print: "Overlay armed. Power-cycle the device to apply (remove and reapply
     power — do NOT use reboot on the A733)."

- `disable`
  1. Write `overlayroot=""` to `/etc/overlayroot.conf` (via `overlayroot-chroot`
     because the overlay is active → root is read-only).
  2. `update-initramfs -u` (in chroot).
  3. Print: "Overlay disabled. Power-cycle to boot writable."

Guards / safety:
- Refuse to run if the persistent data partition is expected but not mounted
  (so we never freeze without app/config being persistent).
- Detect platform (Cubie vs Pi) for the reboot wording.
- `bash -n`-clean; mirror the `install.sh` convention (invoke via `bash`).

## 6. Update workflows

### 6a. App / config update — overlay STAYS ON (the common case)

Because `/opt/copystation` and `/etc/copystation` are bind-mounted from `/data`,
these are writable and persistent even with the overlay active.

```
cd /data/copy-station      # keep the git checkout on the data partition
git pull
sudo bash scripts/install.sh
sudo systemctl restart copystation
```

Notes:
- `install.sh`'s apt steps are **no-ops** when the packages are already present
  (they live on the frozen lower); `apt-get update` writes to the tmpfs upper and
  is harmlessly discarded.
- The venv lives under `/opt/copystation/venv` → on `/data` → `pip install` into it
  persists. So even app updates that pull **new pip packages** need no overlay
  toggle.
- Editing `/etc/copystation/config.yaml` persists (bind-mounted) — no toggle.

### 6b. Installing new SYSTEM software / OS updates — needs the overlay handled

New **apt** packages (or any change outside `/data`) land on the read-only root, so
they need the overlay off for the change to persist. Two ways:

- One-off package, overlay stays armed:
  ```
  sudo overlayroot-chroot apt-get update
  sudo overlayroot-chroot apt-get install -y <package>
  # then power-cycle so the running overlay sits over the updated lower
  ```
- Bigger session (dist-upgrade, multiple changes):
  ```
  sudo bash scripts/overlay.sh disable     # then power-cycle -> writable
  sudo apt-get update && sudo apt-get upgrade
  # ... whatever else ...
  sudo bash scripts/overlay.sh enable      # then power-cycle -> frozen
  ```

Rule of thumb: **`/data` changes = no toggle; root changes = overlay off (or
`overlayroot-chroot`) + power-cycle.**

## 7. Data partition setup — `scripts/setup-data-partition.sh`

Creating the persistent partition is the most invasive step; offer two paths.

- **Preferred (offline, on a host PC):** shrink the SD's ext4 rootfs and create a
  new ext4 partition in the freed space (label `cs-data`), using
  `parted`/`gparted` + `resize2fs`. Safer because the rootfs is not mounted.
- **Online (on the device):** if the image leaves free space after rootfs, create
  the partition with `parted`, `mkfs.ext4 -L cs-data`, and reboot. Riskier; do it
  before enabling the overlay.

Then the setup script (run once, overlay OFF):
1. `mkdir -p /data && mount /dev/disk/by-label/cs-data /data`
2. Migrate existing data: copy `/opt/copystation` → `/data/copystation/opt`,
   `/etc/copystation` → `/data/copystation/etc`, create `/data/copystation/log`.
3. Add to `/etc/fstab`:
   ```
   LABEL=cs-data  /data                 ext4  defaults,noatime  0  2
   /data/copystation/opt  /opt/copystation      none  bind  0  0
   /data/copystation/etc  /etc/copystation      none  bind  0  0
   /data/copystation/log  /var/log/copystation  none  bind  0  0
   ```
4. Ensure mount ordering: `/data` must mount before the binds (fstab order +
   `x-systemd.requires` if needed) and before `copystation.service` (the unit
   already has `After=multi-user.target`; add `RequiresMountsFor=/opt/copystation
   /etc/copystation`).

MVP fallback (no data partition): keep the whole root overlayed and do **all**
updates (app + OS) via `overlayroot-chroot` / disable-enable. Less convenient, but
zero repartitioning. Recommend shipping the MVP first, then layering the data
partition.

## 8. `install.sh` changes (overlay-aware)

- Detect overlay state (`findmnt -n -o FSTYPE /`).
- If a `/data` partition exists, target `/data/copystation/{opt,etc}` and rely on
  the bind mounts (paths stay `/opt/copystation`, `/etc/copystation`).
- If the overlay is **active** and an apt install is required (a dependency is
  missing), refuse with a clear message: "root is read-only; run
  `scripts/overlay.sh disable`, power-cycle, re-run, then re-enable" — rather than
  silently installing into tmpfs.
- Keep `install.sh` runnable with the overlay **on** for pure app/config updates.

## 9. Edge cases & risks

- **tmpfs exhaustion:** all runtime writes to `/` consume RAM. Keep app writes off
  `/` (they already go to `/data`, `/run`, and the target SD). Cap the overlay
  tmpfs size and consider a tiny disk-usage check in the daemon's status. Watch
  `/var/log` (journald) and apt caches.
- **Volatile logs:** journald under overlay is lost on power-cycle. Acceptable for
  an appliance; optionally bind `/var/log/journal` to `/data` for persistent logs.
- **Clock / no RTC:** read-only root + no RTC means time relies on NTP at boot;
  apt and TLS need a correct clock (we already hit this — see the apt
  "no installation candidate" episode). Ensure `systemd-timesyncd` is enabled.
- **A733 reboot bug:** scripts must power-cycle, never `reboot`.
- **Boot breakage recovery:** if a bad overlay config blocks boot, recover via the
  `overlayroot=disabled` kernel cmdline, or mount the SD on a host and set
  `overlayroot=""`.
- **Mount ordering:** the data partition and its binds must be up before the
  service; encode the dependency (fstab + `RequiresMountsFor`).
- **First-boot vs frozen config:** config edits must go through the writable
  `/data` bind; document that editing config no longer needs a toggle, but adding
  a brand-new hardware backend that needs a new apt package does.
- **swap:** ensure no swapfile on `/` that conflicts; `overlayroot` is swap-aware
  but verify on the A733 image.

## 10. Testing plan (on a spare SD first)

1. Enable overlay, power-cycle, verify `findmnt /` shows `overlay` and the service
   runs.
2. Write a file to `/` (e.g. `/root/test`), power-cycle, verify it is gone.
3. Verify `/opt/copystation`, `/etc/copystation`, and a config edit persist across
   a power-cycle.
4. Pull power during **idle** and during a **copy**; verify the OS boots cleanly
   every time (repeat several times — this is the whole point).
5. Run the update workflows: app-only update with overlay on; an apt install via
   `overlayroot-chroot`; a full `disable → change → enable` cycle.
6. Fill the overlay tmpfs deliberately and confirm graceful behaviour / a clear
   error rather than a silent hang.

## 11. Rollback

- Temporary: boot with `overlayroot=disabled` on the kernel cmdline.
- Permanent: `scripts/overlay.sh disable` + power-cycle (root writable again); the
  data partition and binds can stay (harmless) or be reverted by removing the
  fstab lines and moving data back.

## 12. Deliverables / ordered implementation steps

1. `scripts/overlay.sh` (enable/disable/status), `overlayroot`-based, no auto-reboot.
2. README section "Read-only OS (power-loss hardening)" documenting the model and
   the update workflows, cross-linked from "Powering off safely".
3. (Phase 2) `scripts/setup-data-partition.sh` + fstab binds + migration; make
   `install.sh` overlay-/data-aware; add `RequiresMountsFor` to the unit.
4. (Phase 2) optional persistent journald to `/data`.
5. Validate on a spare SD per the testing plan before rolling onto the live card.

## 13. Open decisions

- **Data partition now or MVP first?** Recommend: ship `overlay.sh` (whole-root,
  chroot-based updates) first; add the data partition once the basic overlay is
  proven on the hardware.
- **Data partition size** (e.g. 2-4 GB) and how it is created (offline host vs
  online `parted`).
- **Persistent vs volatile logs.**
- **Pi parity:** use the same `overlayroot` package on the Pi, or defer to
  `raspi-config`'s overlay there.
