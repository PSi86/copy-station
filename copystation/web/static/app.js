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
    no_media: "no media",
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

  document.getElementById("ap").hidden = !data.wifi_ap;

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

// --------------------------------------------------------------------------
// File browser (only wired up when the backend reports the feature is on)
// --------------------------------------------------------------------------

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

const fileState = { device: null, path: "" };
let transcodeAvailable = false;

function joinPath(base, name) {
  return base ? `${base}/${name}` : name;
}

function parentPath(path) {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

function renderFileMessage(msg) {
  document.getElementById("file-list").innerHTML = `<li class="muted">${escapeHtml(msg)}</li>`;
}

function renderBreadcrumb(device, path) {
  const el = document.getElementById("file-path");
  const parts = path ? path.split("/").filter(Boolean) : [];
  let acc = "";
  const crumbs = [`<a href="#" data-path="">${escapeHtml(device)}</a>`];
  for (const p of parts) {
    acc = joinPath(acc, p);
    crumbs.push(`<a href="#" data-path="${escapeHtml(acc)}">${escapeHtml(p)}</a>`);
  }
  el.innerHTML = crumbs.join(' <span class="sep">/</span> ');
}

function renderFileList(entries) {
  const el = document.getElementById("file-list");
  const rows = [];
  if (fileState.path) {
    rows.push(
      `<li class="file dir up"><a href="#" data-dir="${escapeHtml(parentPath(fileState.path))}">../</a></li>`
    );
  }
  if (entries.length === 0 && !fileState.path) {
    renderFileMessage("empty");
    return;
  }
  for (const e of entries) {
    const full = joinPath(fileState.path, e.name);
    if (e.is_dir) {
      rows.push(
        `<li class="file dir"><a href="#" data-dir="${escapeHtml(full)}">${escapeHtml(e.name)}/</a></li>`
      );
    } else {
      const url = `/api/files/download?device=${encodeURIComponent(fileState.device)}&path=${encodeURIComponent(full)}`;
      const tcBtn = transcodeAvailable
        ? `<button class="btn tc" type="button" data-file="${escapeHtml(full)}" title="Transcode this file">⚙</button>`
        : "";
      rows.push(
        `<li class="file">
           <a class="fname" href="${url}" download>${escapeHtml(e.name)}</a>
           <span class="fmeta"><span class="muted fsize">${fmtBytes(e.size)}</span>${tcBtn}</span>
         </li>`
      );
    }
  }
  el.innerHTML = rows.join("");
}

async function loadDir(path) {
  if (!fileState.device) return;
  try {
    const url = `/api/files?device=${encodeURIComponent(fileState.device)}&path=${encodeURIComponent(path)}`;
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) {
      renderFileMessage(`error ${res.status}`);
      return;
    }
    const data = await res.json();
    fileState.path = data.path || "";
    renderBreadcrumb(fileState.device, fileState.path);
    renderFileList(data.entries || []);
  } catch (e) {
    renderFileMessage("disconnected");
  }
}

async function loadVolumes() {
  const sel = document.getElementById("file-volume");
  try {
    const res = await fetch("/api/volumes", { cache: "no-store" });
    if (!res.ok) {
      renderFileMessage("cannot list volumes");
      return;
    }
    const vols = (await res.json()).volumes || [];
    const options = vols
      .map((v) => `<option value="${escapeHtml(v.sys_name)}">${escapeHtml(v.name)} (${escapeHtml(v.sys_name)})</option>`)
      .join("");
    sel.innerHTML = options;
    const tcOut = document.getElementById("tc-output");
    if (tcOut) tcOut.innerHTML = options; // transcode output volume picker
    if (vols.length === 0) {
      fileState.device = null;
      document.getElementById("file-path").textContent = "";
      renderFileMessage("no mass storage attached");
      return;
    }
    if (!fileState.device || !vols.some((v) => v.sys_name === fileState.device)) {
      fileState.device = vols[0].sys_name;
      fileState.path = "";
    }
    sel.value = fileState.device;
    await loadDir(fileState.path);
  } catch (e) {
    renderFileMessage("disconnected");
  }
}

// ---- transcode -----------------------------------------------------------

// A crisp inline download glyph (arrow into a tray); `currentColor` so it takes
// the link colour. Inline SVG avoids depending on an emoji/icon font.
const DL_ICON =
  '<svg class="ic" viewBox="0 0 24 24" width="15" height="15" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
  'stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M12 3v11"/><path d="M8 11l4 4 4-4"/><path d="M5 20h14"/></svg>';

// preset id -> human label, kept fresh from the /api/transcode snapshot so job
// rows can show which preset a job uses (in the queue and while it runs).
const presetLabels = {};
function updatePresetLabels(presets) {
  for (const p of presets || []) presetLabels[p.id] = p.label || p.id;
}

function renderJobs(jobs) {
  const el = document.getElementById("tc-jobs");
  if (!jobs || jobs.length === 0) {
    el.innerHTML = `<li class="muted">no jobs yet</li>`;
    return;
  }
  el.innerHTML = jobs
    .map((j) => {
      // Multi-line layout: on a narrow phone screen the file name, timings,
      // cancel button and progress bar do not fit on one row -- name (+ encoder)
      // on the first line, the progress bar and a stats line below.
      const enc = j.encoder ? ` · ${j.encoder}${j.hw ? " (hw)" : ""}` : "";
      const name = (j.filename || j.input_path || `job ${j.id}`) + enc;
      const presetLabel = presetLabels[j.preset] || j.preset || "";
      const presetChip = presetLabel
        ? `<span class="role preset" title="preset">${escapeHtml(presetLabel)}</span>`
        : "";

      let statusRight = "";
      if (j.status === "done" && j.output_path) {
        const url = `/api/files/download?device=${encodeURIComponent(j.output_device)}&path=${encodeURIComponent(j.output_path)}`;
        statusRight = `<a class="dl" href="${url}" download title="Download" aria-label="Download">${DL_ICON}</a>`;
      } else if (j.status === "error") {
        statusRight = `<span class="role error" title="${escapeHtml(j.error || "")}">error</span>`;
      } else if (j.status === "queued") {
        statusRight = `<span class="role queued">queued</span>`;
      } else if (j.status === "canceled") {
        statusRight = `<span class="role canceled">canceled</span>`;
      }
      const cancelable = j.status === "queued" || j.status === "running";
      const cancel = cancelable
        ? `<button class="btn tc-cancel" type="button" data-job="${j.id}" title="Cancel job">✕</button>`
        : "";

      const bar =
        j.status === "running"
          ? `<div class="storage-track"><div class="storage-used" style="width:${j.percent || 0}%"></div></div>`
          : "";

      let stats = "";
      if (j.status === "running") {
        const parts = [`${j.percent || 0}%`];
        if (j.input_size) parts.push(fmtBytes(j.input_size)); // source size
        parts.push(`elapsed ${fmtDuration(j.elapsed_seconds)}`);
        parts.push(`ETA ${fmtDuration(j.eta_seconds)}`);
        if (j.fps) parts.push(`${Math.round(j.fps)} fps`);
        if (j.speed) parts.push(escapeHtml(j.speed));
        if (j.ram_buffered) parts.push("RAM");
        stats = `<div class="muted jobstats">${parts.join(" · ")}</div>`;
      } else if (j.status === "done") {
        // Show the TRANSCODED file's size (with the source size for context).
        const parts = [];
        if (j.output_size) parts.push(fmtBytes(j.output_size));
        if (j.input_size) parts.push(`from ${fmtBytes(j.input_size)}`);
        if (j.ram_buffered) parts.push("RAM");
        if (parts.length) stats = `<div class="muted jobstats">${parts.join(" · ")}</div>`;
      } else if (j.input_size) {
        stats = `<div class="muted jobstats">${fmtBytes(j.input_size)}</div>`;
      }

      return `<li class="file jobitem">
        <div class="jobhead">
          <span class="jobname">${escapeHtml(name)}</span>
          <span class="jobright">${presetChip}${statusRight}${cancel}</span>
        </div>${bar}${stats}
      </li>`;
    })
    .join("");
}

async function cancelJob(id) {
  try {
    await fetch(`/api/transcode/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch (e) {
    /* transient */
  }
  loadJobs();
}

async function loadJobs() {
  try {
    const res = await fetch("/api/transcode", { cache: "no-store" });
    if (res.ok) {
      const data = await res.json();
      updatePresetLabels(data.presets);
      renderJobs(data.jobs || []);
    }
  } catch (e) {
    /* transient */
  }
}

async function loadPresets() {
  const sel = document.getElementById("tc-preset");
  try {
    const res = await fetch("/api/transcode", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    updatePresetLabels(data.presets);
    sel.innerHTML = (data.presets || [])
      .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.label || p.id)}</option>`)
      .join("");
    const info = document.getElementById("tc-info");
    if (info) {
      if (data.available === false) {
        info.textContent = "ffmpeg not installed — transcoding unavailable";
      } else {
        info.textContent = `board: ${data.board || "?"} · acceleration: ${data.acceleration || "auto"}`;
      }
    }
  } catch (e) {
    /* transient */
  }
}

async function submitTranscode(path, presetId, outputDevice) {
  const preset = presetId || document.getElementById("tc-preset").value;
  const output = outputDevice || document.getElementById("tc-output").value || fileState.device;
  if (!preset || !fileState.device) return;
  try {
    const res = await fetch("/api/transcode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device: fileState.device, path, preset, output_device: output }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      alert(`Transcode failed: ${body.detail || res.status}`);
      return;
    }
    loadJobs();
  } catch (e) {
    alert("Transcode request failed");
  }
}

// ---- file dialog (⚙) ------------------------------------------------------

let deleteAvailable = false;
const dlgState = { path: null };

function pathLabel(p) {
  return ({ hw: "Hardware", "hw+cpu": "Hardware + CPU finish", cpu: "CPU (software)" })[p] || p || "--";
}
function pathClass(p) {
  return ({ hw: "hw", "hw+cpu": "hwcpu", cpu: "cpu" })[p] || "";
}

function baseName(path) {
  return String(path || "").split("/").pop();
}

function openFileDialog(path) {
  const dlg = document.getElementById("file-dialog");
  dlgState.path = path;
  document.getElementById("dlg-title").textContent = baseName(path);
  // Mirror the main transcode controls; preselect the preset chosen there.
  const dlgPreset = document.getElementById("dlg-preset");
  const mainPreset = document.getElementById("tc-preset");
  dlgPreset.innerHTML = mainPreset.innerHTML;
  dlgPreset.value = mainPreset.value;
  const dlgOut = document.getElementById("dlg-output");
  dlgOut.innerHTML = document.getElementById("tc-output").innerHTML;
  dlgOut.value = document.getElementById("tc-output").value || fileState.device;
  for (const id of ["dlg-size", "dlg-codec", "dlg-res", "dlg-fps", "dlg-dur"]) {
    document.getElementById(id).textContent = "…";
  }
  const badge = document.getElementById("dlg-path");
  badge.textContent = "…";
  badge.className = "dlg-badge";
  document.getElementById("dlg-estimate").textContent = "…";
  document.getElementById("dlg-delete").hidden = !deleteAvailable;
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "");
  refreshDialogPlan();
}

function closeDialog() {
  const dlg = document.getElementById("file-dialog");
  if (typeof dlg.close === "function") dlg.close();
  else dlg.removeAttribute("open");
}

async function refreshDialogPlan() {
  const preset = document.getElementById("dlg-preset").value;
  if (!fileState.device || !dlgState.path || !preset) return;
  const url = `/api/transcode/plan?device=${encodeURIComponent(fileState.device)}&path=${encodeURIComponent(dlgState.path)}&preset=${encodeURIComponent(preset)}`;
  try {
    const res = await fetch(url, { cache: "no-store" });
    const badge = document.getElementById("dlg-path");
    const est = document.getElementById("dlg-estimate");
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      badge.textContent = "--";
      est.textContent = b.detail || `error ${res.status}`;
      return;
    }
    const d = await res.json();
    const info = d.info || {};
    document.getElementById("dlg-size").textContent = info.size != null ? fmtBytes(info.size) : "--";
    document.getElementById("dlg-codec").textContent =
      (info.vcodec ? info.vcodec.toUpperCase() : "--") + (info.has_audio ? ` + ${info.acodec || "audio"}` : "");
    document.getElementById("dlg-res").textContent =
      info.width && info.height ? `${info.width}×${info.height}` : "--";
    document.getElementById("dlg-fps").textContent =
      info.fps ? `${Math.round(info.fps * 100) / 100} fps` : "--";
    document.getElementById("dlg-dur").textContent =
      info.duration ? fmtDuration(info.duration) : "--";
    badge.textContent =
      pathLabel(d.path) +
      (d.path === "hw+cpu" && d.out_height ? ` · HW ${d.out_height}p→${d.target_height}p` : "");
    badge.className = "dlg-badge " + pathClass(d.path);
    est.textContent =
      d.estimate_seconds != null ? `~${fmtDuration(d.estimate_seconds)} (from past jobs)` : "no data yet";
  } catch (e) {
    document.getElementById("dlg-estimate").textContent = "unavailable";
  }
}

async function deleteCurrentFile() {
  if (!dlgState.path) return;
  if (!confirm(`Delete "${baseName(dlgState.path)}"? This permanently removes it from the card.`)) return;
  try {
    const url = `/api/files?device=${encodeURIComponent(fileState.device)}&path=${encodeURIComponent(dlgState.path)}`;
    const res = await fetch(url, { method: "DELETE" });
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      alert(`Delete failed: ${b.detail || res.status}`);
      return;
    }
    closeDialog();
    loadDir(fileState.path); // refresh the (parent) listing
  } catch (e) {
    alert("Delete request failed");
  }
}

async function initFeatures() {
  let features = {};
  try {
    const res = await fetch("/api/settings", { cache: "no-store" });
    if (res.ok) features = (await res.json()).features || {};
  } catch (e) {
    return;
  }

  transcodeAvailable = !!features.transcode;
  deleteAvailable = !!features.delete;

  if (features.files) {
    document.getElementById("files-card").hidden = false;
    document.getElementById("file-volume").addEventListener("change", (e) => {
      fileState.device = e.target.value;
      fileState.path = "";
      loadDir("");
    });
    document.getElementById("file-refresh").addEventListener("click", loadVolumes);
    document.getElementById("file-path").addEventListener("click", (e) => {
      const a = e.target.closest("a[data-path]");
      if (!a) return;
      e.preventDefault();
      loadDir(a.getAttribute("data-path"));
    });
    document.getElementById("file-list").addEventListener("click", (e) => {
      const tc = e.target.closest("button.tc[data-file]");
      if (tc) {
        openFileDialog(tc.getAttribute("data-file"));
        return;
      }
      const a = e.target.closest("a[data-dir]");
      if (!a) return;
      e.preventDefault();
      loadDir(a.getAttribute("data-dir"));
    });
  }

  if (transcodeAvailable) {
    document.getElementById("transcode-card").hidden = false;
    document.getElementById("tc-jobs").addEventListener("click", (e) => {
      const btn = e.target.closest("button.tc-cancel[data-job]");
      if (btn) cancelJob(btn.getAttribute("data-job"));
    });

    // File dialog (opened by the ⚙ button on a file).
    const dlg = document.getElementById("file-dialog");
    document.getElementById("dlg-preset").addEventListener("change", refreshDialogPlan);
    document.getElementById("dlg-close").addEventListener("click", closeDialog);
    document.getElementById("dlg-cancel").addEventListener("click", closeDialog);
    document.getElementById("dlg-delete").addEventListener("click", deleteCurrentFile);
    document.getElementById("dlg-transcode").addEventListener("click", () => {
      submitTranscode(
        dlgState.path,
        document.getElementById("dlg-preset").value,
        document.getElementById("dlg-output").value || fileState.device
      );
      closeDialog();
    });
    // Click on the backdrop (outside the body) closes the dialog.
    dlg.addEventListener("click", (e) => { if (e.target === dlg) closeDialog(); });

    await loadPresets();
    loadJobs();
    setInterval(loadJobs, 1500);
  }

  if (features.files) await loadVolumes();
}

poll();
setInterval(poll, POLL_MS);
initFeatures();
