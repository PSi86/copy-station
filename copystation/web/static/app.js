"use strict";

const POLL_MS = 500;

function fmtBytes(n) {
  if (n === null || n === undefined) return "--";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "--";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (x) => String(x).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function fmtClock(epochSeconds) {
  if (!epochSeconds) return "";
  const d = new Date(epochSeconds * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function renderLog(el, events) {
  if (!events || events.length === 0) {
    el.innerHTML = `<li class="muted">no activity yet</li>`;
    return;
  }
  el.innerHTML = events
    .map(
      (e) => `<li class="log-entry ${e.level || "info"}">
        <span class="log-time">${fmtClock(e.time)}</span>
        <span class="log-msg">${e.message}</span>
      </li>`
    )
    .join("");
}

function renderDevices(el, devices) {
  if (!devices || devices.length === 0) {
    el.innerHTML = `<div class="muted">none detected</div>`;
    return;
  }
  const roleLabel = {
    source: "source",
    target: "target",
    empty: "empty",
    candidate: "candidate",
    unused: "unused",
    ignored: "too small",
  };
  el.innerHTML = devices
    .map((d) => {
      const name = d.name || d.node || "device";
      const role = roleLabel[d.role] || d.role || "";
      const dcim = d.has_dcim ? " · DCIM" : "";
      const used =
        d.capacity != null && d.free != null ? d.capacity - d.free : null;
      const size =
        used != null
          ? `${fmtBytes(used)} used / ${fmtBytes(d.capacity)}`
          : fmtBytes(d.capacity);
      const usedPct = d.capacity ? Math.min(100, (used / d.capacity) * 100) : 0;
      return `
        <div class="device">
          <div class="label">
            <span>${name} <span class="role ${d.role}">${role}</span></span>
            <span class="muted">${size}${dcim}</span>
          </div>
          <div class="storage-track"><div class="storage-used" style="width:${usedPct.toFixed(1)}%"></div></div>
        </div>`;
    })
    .join("");
}

function apply(data) {
  const phase = (data.phase || "").toLowerCase();
  const badge = document.getElementById("phase");
  badge.textContent = phase || "--";
  badge.className = "badge " + phase;

  document.getElementById("progress-bar").style.width = `${data.percent || 0}%`;
  document.getElementById("percent").textContent = `${data.percent || 0}%`;
  document.getElementById("transfer-name").textContent = data.transfer_name || "";
  document.getElementById("elapsed").textContent = fmtDuration(data.elapsed_seconds);
  document.getElementById("eta").textContent = fmtDuration(data.eta_seconds);
  document.getElementById("speed").textContent =
    data.speed_bytes ? `${fmtBytes(data.speed_bytes)}/s` : "--";
  document.getElementById("bytes").textContent =
    data.bytes_total ? `${fmtBytes(data.bytes_done)} / ${fmtBytes(data.bytes_total)}` : "--";

  renderDevices(document.getElementById("devices"), data.devices);
  renderLog(document.getElementById("log"), data.events);

  const conn = document.getElementById("conn");
  if (data.error) {
    conn.textContent = `error: ${data.error}`;
  } else {
    conn.textContent = "live";
  }
}

async function poll() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    if (res.ok) apply(await res.json());
    else document.getElementById("conn").textContent = "status unavailable";
  } catch (e) {
    document.getElementById("conn").textContent = "disconnected — retrying…";
  }
}

poll();
setInterval(poll, POLL_MS);
