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

  // A copy or transcode owns the cards' volumes: disable the file/download/job
  // controls (they would 503 against the busy device) until it finishes.
  setUiBusy(phase === "transcoding" || phase === "copying", phase);
}

function setUiBusy(busy, phase) {
  if (busy === uiBusy) {
    return;
  }
  uiBusy = busy;
  document.body.classList.toggle("busy", busy);
  const hint = document.getElementById("busy-hint");
  if (hint) {
    hint.hidden = !busy;
    const what = phase === "copying" ? "A copy is in progress" : "A transcode is running";
    hint.textContent =
      `${what} — browsing, downloads and new jobs are paused until it finishes. ` +
      `You can still cancel the running job.`;
  }
  // Re-render so links become inert / buttons disable immediately, without
  // waiting for the next navigation or job poll.
  if (fileState.device) {
    renderFileList(fileState.entries || []);
  }
  renderJobs(lastJobs);
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

const fileState = { device: null, path: "", entries: [] };
let transcodeAvailable = false;
// True while the station holds the removable volumes (a copy or a transcode is
// running): browsing, downloading and starting new jobs would hit the busy
// device and 503, so the controls are disabled and a hint is shown. Only the
// running job's Cancel button stays live.
let uiBusy = false;
let lastJobs = [];
// A running job's Cancel takes two clicks: the first arms it (turns into
// "Cancel?"), a second click within a few seconds actually cancels -- so a
// minutes-long encode is never aborted by a single stray click.
let armedCancelId = null;
let armedCancelTimer = null;
const CANCEL_ARM_MS = 4000;

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
  const dis = uiBusy ? " disabled" : "";
  if (fileState.path) {
    const up = uiBusy
      ? `<span class="muted">../</span>`
      : `<a href="#" data-dir="${escapeHtml(parentPath(fileState.path))}">../</a>`;
    rows.push(`<li class="file dir up">${up}</li>`);
  }
  if (entries.length === 0 && !fileState.path) {
    renderFileMessage("empty");
    return;
  }
  for (const e of entries) {
    const full = joinPath(fileState.path, e.name);
    if (e.is_dir) {
      // When busy the name is inert (no navigation); otherwise it's a link.
      const nameCell = uiBusy
        ? `<span class="fname dirname">${escapeHtml(e.name)}/</span>`
        : `<a class="fname" href="#" data-dir="${escapeHtml(full)}">${escapeHtml(e.name)}/</a>`;
      // ⚙ on a folder transcodes every video inside it (one job per file).
      const tcBtn = transcodeAvailable
        ? `<button class="btn tc" type="button" data-folder="${escapeHtml(full)}" title="Transcode this folder"${dis}>⚙</button>`
        : "";
      rows.push(
        `<li class="file dir">${nameCell}<span class="fmeta">${tcBtn}</span></li>`
      );
    } else {
      // A download link only while idle; a plain name while busy (the device is
      // held by the running job, so the download would 503).
      let nameCell;
      if (uiBusy) {
        nameCell = `<span class="fname">${escapeHtml(e.name)}</span>`;
      } else {
        const url = `/api/files/download?device=${encodeURIComponent(fileState.device)}&path=${encodeURIComponent(full)}`;
        nameCell = `<a class="fname" href="${url}" download>${escapeHtml(e.name)}</a>`;
      }
      const tcBtn = transcodeAvailable
        ? `<button class="btn tc" type="button" data-file="${escapeHtml(full)}" title="Transcode this file"${dis}>⚙</button>`
        : "";
      rows.push(
        `<li class="file">
           ${nameCell}
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
    fileState.entries = data.entries || [];
    renderBreadcrumb(fileState.device, fileState.path);
    renderFileList(fileState.entries);
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
        if (uiBusy) {
          // Downloads 503 while the device is held by the running job.
          statusRight = `<span class="dl disabled" title="Download paused while busy" aria-label="Download paused">${DL_ICON}</span>`;
        } else {
          const url = `/api/files/download?device=${encodeURIComponent(j.output_device)}&path=${encodeURIComponent(j.output_path)}`;
          statusRight = `<a class="dl" href="${url}" download title="Download" aria-label="Download">${DL_ICON}</a>`;
        }
      } else if (j.status === "error") {
        statusRight = `<span class="role error" title="${escapeHtml(j.error || "")}">error</span>`;
      } else if (j.status === "queued") {
        statusRight = `<span class="role queued">queued</span>`;
      } else if (j.status === "canceled") {
        statusRight = `<span class="role canceled">canceled</span>`;
      }
      const cancelable = j.status === "queued" || j.status === "running";
      const armed = String(armedCancelId) === String(j.id);
      let cancel = "";
      if (cancelable) {
        cancel = armed
          ? `<button class="btn tc-cancel armed" type="button" data-job="${j.id}" title="Click again to cancel">Cancel?</button>`
          : `<button class="btn tc-cancel" type="button" data-job="${j.id}" title="Cancel job (click twice)">✕</button>`;
      }

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

function armCancel(id) {
  armedCancelId = id;
  if (armedCancelTimer) clearTimeout(armedCancelTimer);
  armedCancelTimer = setTimeout(disarmCancel, CANCEL_ARM_MS);
  renderJobs(lastJobs);
}

function disarmCancel() {
  armedCancelId = null;
  if (armedCancelTimer) {
    clearTimeout(armedCancelTimer);
    armedCancelTimer = null;
  }
  renderJobs(lastJobs);
}

async function cancelJob(id) {
  disarmCancel();
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
      lastJobs = data.jobs || [];
      renderJobs(lastJobs);
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
  return ({ hw: "Hardware", "hw+cpu": "Hardware + CPU", cpu: "CPU (software)" })[p] || p || "--";
}
function pathShort(p) {
  return ({ hw: "HW", "hw+cpu": "HW+CPU", cpu: "CPU" })[p] || p || "--";
}
function pathClass(p) {
  return ({ hw: "hw", "hw+cpu": "hwcpu", cpu: "cpu" })[p] || "";
}
// A plain-language explanation of the chosen path (empty for the simple cases,
// where the badge already says it all).
function pathNote(d) {
  if (d.path === "hw+cpu" && d.out_height && d.target_height) {
    return `The hardware decodes and scales to ${d.out_height}p, then the CPU ` +
           `re-encodes down to the exact ${d.target_height}p.`;
  }
  if (d.path === "cpu") return "Encoded entirely on the CPU (no hardware path fits).";
  return "";
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
  document.getElementById("dlg-path-note").textContent = "";
  document.getElementById("dlg-estimate").textContent = "…";
  document.getElementById("dlg-delete").hidden = !deleteAvailable;
  disarmDelete(); // start every dialog with the delete button un-armed
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "");
  refreshDialogPlan();
}

function closeDialog() {
  disarmDelete(); // never leave the button armed for the next open
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
    const note = document.getElementById("dlg-path-note");
    const est = document.getElementById("dlg-estimate");
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      badge.textContent = "--";
      note.textContent = "";
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
    badge.textContent = pathLabel(d.path);
    badge.className = "dlg-badge " + pathClass(d.path);
    note.textContent = pathNote(d);
    est.textContent =
      d.estimate_seconds != null ? `~${fmtDuration(d.estimate_seconds)} (from past jobs)` : "no data yet";
  } catch (e) {
    document.getElementById("dlg-estimate").textContent = "unavailable";
  }
}

// Delete uses the same two-click confirm as a job cancel: the first click arms
// the button ("Delete?"), a second within a few seconds performs the delete.
let deleteArmed = false;
let deleteArmTimer = null;

function disarmDelete() {
  deleteArmed = false;
  if (deleteArmTimer) {
    clearTimeout(deleteArmTimer);
    deleteArmTimer = null;
  }
  const btn = document.getElementById("dlg-delete");
  if (btn) {
    btn.classList.remove("armed");
    btn.textContent = "Delete";
  }
}

function armDelete() {
  deleteArmed = true;
  const btn = document.getElementById("dlg-delete");
  btn.classList.add("armed");
  btn.textContent = "Delete?";
  if (deleteArmTimer) clearTimeout(deleteArmTimer);
  deleteArmTimer = setTimeout(disarmDelete, CANCEL_ARM_MS);
}

async function deleteCurrentFile() {
  if (!dlgState.path) return;
  if (!deleteArmed) {
    armDelete(); // first click arms; a second click confirms the delete
    return;
  }
  disarmDelete();
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

// ---- folder dialog (⚙ on a folder) --------------------------------------

const folderState = { path: null };

function openFolderDialog(path) {
  const dlg = document.getElementById("folder-dialog");
  folderState.path = path;
  document.getElementById("fdlg-title").textContent = (baseName(path) || "root") + "/";
  // Mirror the main transcode controls; preselect the preset chosen there.
  const dlgPreset = document.getElementById("fdlg-preset");
  const mainPreset = document.getElementById("tc-preset");
  dlgPreset.innerHTML = mainPreset.innerHTML;
  dlgPreset.value = mainPreset.value;
  const dlgOut = document.getElementById("fdlg-output");
  dlgOut.innerHTML = document.getElementById("tc-output").innerHTML;
  dlgOut.value = document.getElementById("tc-output").value || fileState.device;
  document.getElementById("fdlg-summary").textContent = "scanning folder…";
  document.getElementById("fdlg-files").innerHTML = "";
  document.getElementById("fdlg-transcode").disabled = true;
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "");
  refreshFolderPlan();
}

function closeFolderDialog() {
  const dlg = document.getElementById("folder-dialog");
  if (typeof dlg.close === "function") dlg.close();
  else dlg.removeAttribute("open");
}

function renderFolderSummary(d) {
  const counts = d.counts || {};
  const order = [["hw", "HW"], ["hw+cpu", "HW+CPU"], ["cpu", "CPU"]];
  const buckets = order.filter(([k]) => (counts[k] || 0) > 0);
  const summary = document.getElementById("fdlg-summary");
  if (!d.count) {
    summary.textContent = "No video files in this folder.";
    return;
  }
  const parts = buckets.map(([k, lbl]) => `${counts[k]} ${lbl}`);
  let text = `${d.count} video${d.count === 1 ? "" : "s"} · ${parts.join(" · ")}`;
  if (d.estimate_seconds) text += ` · ~${fmtDuration(d.estimate_seconds)} total`;
  // Make it explicit when the files are NOT handled uniformly.
  const note =
    buckets.length > 1
      ? " Files are split across encoders — each is transcoded on its best path."
      : " All files use the same path.";
  summary.innerHTML = `${escapeHtml(text)}<span class="muted">${escapeHtml(note)}</span>`;
}

async function refreshFolderPlan() {
  const preset = document.getElementById("fdlg-preset").value;
  if (!fileState.device || folderState.path == null || !preset) return;
  const url = `/api/transcode/folder-plan?device=${encodeURIComponent(fileState.device)}&path=${encodeURIComponent(folderState.path)}&preset=${encodeURIComponent(preset)}`;
  const summary = document.getElementById("fdlg-summary");
  const list = document.getElementById("fdlg-files");
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      summary.textContent = b.detail || `error ${res.status}`;
      list.innerHTML = "";
      return;
    }
    const d = await res.json();
    renderFolderSummary(d);
    list.innerHTML = (d.files || [])
      .map((f) => {
        const res2 = f.width && f.height ? `${f.width}×${f.height}` : "--";
        return `<li class="file">
          <span class="fname">${escapeHtml(f.name)}</span>
          <span class="fmeta">
            <span class="muted fsize">${res2}</span>
            <span class="dlg-badge ${pathClass(f.plan)}" title="${escapeHtml(pathLabel(f.plan))}">${escapeHtml(pathShort(f.plan))}</span>
          </span>
        </li>`;
      })
      .join("");
    document.getElementById("fdlg-transcode").disabled = !d.count;
  } catch (e) {
    summary.textContent = "unavailable";
    list.innerHTML = "";
  }
}

async function submitFolder() {
  if (folderState.path == null || !fileState.device) return;
  const preset = document.getElementById("fdlg-preset").value;
  const output = document.getElementById("fdlg-output").value || fileState.device;
  try {
    const res = await fetch("/api/transcode/folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device: fileState.device, path: folderState.path, preset, output_device: output }),
    });
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      alert(`Transcode failed: ${b.detail || res.status}`);
      return;
    }
    closeFolderDialog();
    loadJobs();
  } catch (e) {
    alert("Transcode request failed");
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
    document.getElementById("file-refresh").addEventListener("click", () => {
      if (uiBusy) return;
      loadVolumes();
    });
    document.getElementById("file-path").addEventListener("click", (e) => {
      if (uiBusy) { e.preventDefault(); return; }
      const a = e.target.closest("a[data-path]");
      if (!a) return;
      e.preventDefault();
      loadDir(a.getAttribute("data-path"));
    });
    document.getElementById("file-list").addEventListener("click", (e) => {
      if (uiBusy) { e.preventDefault(); return; }
      const folder = e.target.closest("button.tc[data-folder]");
      if (folder) {
        openFolderDialog(folder.getAttribute("data-folder"));
        return;
      }
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
      if (!btn) return;
      const id = btn.getAttribute("data-job");
      // First click arms; a second click on the same job confirms the cancel.
      if (String(armedCancelId) === String(id)) cancelJob(id);
      else armCancel(id);
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

    // Folder dialog (opened by the ⚙ button on a folder).
    const fdlg = document.getElementById("folder-dialog");
    document.getElementById("fdlg-preset").addEventListener("change", refreshFolderPlan);
    document.getElementById("fdlg-close").addEventListener("click", closeFolderDialog);
    document.getElementById("fdlg-cancel").addEventListener("click", closeFolderDialog);
    document.getElementById("fdlg-transcode").addEventListener("click", submitFolder);
    fdlg.addEventListener("click", (e) => { if (e.target === fdlg) closeFolderDialog(); });

    await loadPresets();
    loadJobs();
    setInterval(loadJobs, 1500);
  }

  if (features.files) await loadVolumes();
}

poll();
setInterval(poll, POLL_MS);
initFeatures();
