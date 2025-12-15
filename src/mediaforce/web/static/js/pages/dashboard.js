(function () {
  if (window.location.pathname !== "/") return;

  const table = document.getElementById("activeEncodes");
  const workersTable = document.getElementById("workersTable");
  const watchChip = document.getElementById("watchStatusDash");

  const page = window.mfUi?.getPageData() || {};
  const dashboardLibrary = page.library_root || "";
  const hostName = page.host_name || "";
  let watchStatusData = page.watch_status || {};
  let currentWorkers = page.workers || [];

  function setChip(el, text, tone = "muted") {
    window.mfUi?.setStatus(el, text, tone);
  }

  function updateNavWatch(running, message) {
    window.mfUi?.updateNavWatch(running, message);
  }

  function buildWatchRow() {
    const tr = document.createElement("tr");
    const state = watchStatusData.running ? "running" : watchStatusData.paused ? "paused" : "stopped";
    const libs = (watchStatusData.libraries || []).join(", ") || "none";
    tr.innerHTML = `
      <td>${hostName || "local"}</td>
      <td>watcher</td>
      <td>${state}</td>
      <td>${(watchStatusData.libraries || []).length}</td>
      <td>${libs}</td>
      <td class="truncate" title="${watchStatusData.message || ""}">${watchStatusData.message || "-"}</td>
      <td class="text-muted">Use controls above</td>
    `;
    return tr;
  }

  function renderWorkers(list) {
    if (!workersTable) return;
    currentWorkers = list;
    const tbody = workersTable.querySelector("tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    tbody.appendChild(buildWatchRow());
    if (!list || list.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="7" class="text-muted">No encoder workers reporting.</td>';
      tbody.appendChild(tr);
      return;
    }
    list.forEach((w) => {
      const state = w.state || ((w.active || 0) > 0 ? "encoding" : "waiting");
      const progress = w.percent_complete ? `${Math.round(w.percent_complete)}%` : "0%";
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${w.machine || "-"}</td>
        <td>encoder</td>
        <td>${state}</td>
        <td>${w.active || 0}</td>
        <td>${state === "offline" ? "—" : progress}</td>
        <td class="truncate" title="${w.sample_path || ""}">${w.sample_path || "-"}</td>
        <td class="text-muted">Route jobs in Queue</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function updateWatchUI() {
    const state = watchStatusData.running ? "running" : watchStatusData.paused ? "paused" : "stopped";
    const tone = watchStatusData.running ? "success" : watchStatusData.paused ? "warning" : "danger";
    const msg = watchStatusData.message || "idle";
    setChip(watchChip, `Watch: ${state}${msg ? " (" + msg + ")" : ""}`, tone);
    updateNavWatch(!!watchStatusData.running, msg);
    renderWorkers(currentWorkers);
  }

  async function toggleWatch(action) {
    setChip(watchChip, `Watch: ${action}…`, "warning");
    try {
      const resp = await window.mfApi.postJson("/api/watch", { action });
      if (resp.ok && resp.data?.success) {
        watchStatusData = resp.data.status || watchStatusData;
        updateWatchUI();
      } else {
        setChip(watchChip, resp.data?.error || resp.error || "Watch action failed", "danger");
      }
    } catch (e) {
      setChip(watchChip, "Watch error", "danger");
    }
  }

  async function refreshWorkers() {
    try {
      const suffix = dashboardLibrary ? `?library=${encodeURIComponent(dashboardLibrary)}` : "";
      const resp = await window.mfApi.getJson(`/api/workers${suffix}`);
      if (resp.ok && resp.data?.success) {
        renderWorkers(resp.data.workers || []);
      }
    } catch (_) {
      /* silent */
    }
  }

  async function refreshActive() {
    try {
      const resp = await window.mfApi.getJson("/api/active-encodes");
      if (!resp.ok || !resp.data?.success || !table) return;
      const tbody = table.querySelector("tbody");
      if (!tbody) return;
      tbody.innerHTML = "";
      (resp.data.encodes || []).forEach((enc) => {
        const tr = document.createElement("tr");
        const machine = enc.machine || "-";
        tr.innerHTML = `
          <td class="truncate" title="${enc.path || ""}">${enc.filename || "Unknown"}</td>
          <td>${machine}</td>
          <td class="tier-${enc.tier || ""}">${enc.tier || "-"}</td>
          <td>${(enc.percent_complete || 0).toFixed(1)}%</td>
          <td>${enc.speed ? enc.speed.toFixed(2) + "x" : "0x"}</td>
          <td>${enc.eta || "-"}</td>
          <td>${enc.frame || 0} / ${enc.total_frames || "?"}</td>
        `;
        tbody.appendChild(tr);
      });

      // Keep the Workers table in sync with any active encodes so the "Active"
      // and "Scope/Progress" columns don't lag behind when only progress rows
      // update.
      try {
        const workersByMachine = new Map((currentWorkers || []).map((w) => [w.machine, { ...w }]));
        (resp.data.encodes || []).forEach((enc) => {
          const machine = enc.machine;
          if (!machine) return;
          const existing = workersByMachine.get(machine) || { machine };
          const percent = enc.percent_complete || 0;
          workersByMachine.set(machine, {
            ...existing,
            active: Math.max(existing.active || 0, 1),
            percent_complete: Math.max(existing.percent_complete || 0, percent),
            state: "encoding",
            sample_path: enc.path || existing.sample_path,
            updated_at: new Date().toISOString(),
          });
        });
        renderWorkers(Array.from(workersByMachine.values()));
      } catch (_) {
        /* ignore */
      }
    } catch (_) {
      /* silent */
    }
  }

  window.toggleWatch = toggleWatch;
  window.refreshWorkers = refreshWorkers;

  renderWorkers(currentWorkers);
  updateWatchUI();
  refreshActive();
  setInterval(refreshActive, 3000);
  setInterval(refreshWorkers, 8000);

  const dashHover = window.mfCommon?.createHoverPreview({
    thumbId: "hoverThumb",
    videoId: "hoverVideo",
    srcBuilder: (id) => `/video/encoded/${id}`,
    delay: 150,
    maxWidth: 240,
  });

  window.dashShowThumb = function (evt, id) {
    dashHover?.show(evt, id);
  };
  window.dashHideThumb = function () {
    dashHover?.hide();
  };
})();
