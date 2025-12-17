(function () {
  if (window.location.pathname !== "/") return;

  const table = document.getElementById("activeEncodes");
  const workersTable = document.getElementById("workersTable");

  const page = window.mfUi?.getPageData() || {};
  const dashboardLibrary = page.library_root || "";
  const hostName = page.host_name || "";
  let currentWorkers = page.workers || [];

  async function setGlobalMode(mode) {
    await window.mfApi.postJson("/api/worker-control/global", { mode });
  }

  function renderWorkers(list) {
    if (!workersTable) return;
    currentWorkers = list;
    const tbody = workersTable.querySelector("tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!list || list.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="8" class="text-muted">No encoder workers reporting.</td>';
      tbody.appendChild(tr);
      return;
    }
    list.forEach((w) => {
      const state = w.state || ((w.active || 0) > 0 ? "encoding" : "waiting");
      const mode = w.control_mode || "run";
      const modeSource = w.override_mode ? "override" : "global";
      const modeTitle = `global=${w.global_mode || "run"}`;
      const percent = typeof w.percent_complete === "number" ? w.percent_complete : 0;
      const progress =
        state === "encoding" ? (percent > 0 ? `${Math.round(percent)}%` : "Starting…") : "—";
      const machine = w.machine || "-";
      const tr = document.createElement("tr");

      const safeMachine = String(machine).replace(/'/g, "\\'");
      const actions =
        machine && machine !== "-"
          ? `<button class="btn btn-xs btn-success" onclick="resumeWorker('${safeMachine}')">Run</button>` +
            `<button class="btn btn-xs btn-warning" onclick="pauseWorker('${safeMachine}')">Pause</button>` +
            `<button class="btn btn-xs btn-danger" onclick="stopWorker('${safeMachine}')">Stop</button>` +
            `<button class="btn btn-xs btn-danger" onclick="stopWorkerNow('${safeMachine}')">Stop Now</button>` +
            (state === "offline" ? `<button class="btn btn-xs" onclick="deleteWorker('${safeMachine}')">Remove</button>` : "")
          : "";
      const message = w.sample_path || w.samplePath || "-";
      tr.innerHTML = `
        <td>${machine}</td>
        <td>encoder</td>
        <td>${state}</td>
        <td title="${modeTitle}">${mode} <span class="text-foreground-muted">(${modeSource})</span></td>
        <td>${w.active || 0}</td>
        <td>${progress}</td>
        <td class="truncate" title="${message}">${message}</td>
        <td class="flex gap-1 flex-wrap">${actions}</td>
      `;
      tbody.appendChild(tr);
    });
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
        const speed = typeof enc.speed === "number" ? enc.speed : 0;
        const speedLabel = speed > 0 ? speed.toFixed(2) + "x" : "-";
        const progressLabel =
          enc.phase === "starting" && (!enc.percent_complete || enc.percent_complete <= 0) ? "Starting…" : (enc.percent_complete || 0).toFixed(1) + "%";
        tr.innerHTML = `
          <td class="truncate" title="${enc.path || ""}">${enc.filename || "Unknown"}</td>
          <td>${machine}</td>
          <td class="tier-${enc.tier || ""}">${enc.tier || "-"}</td>
          <td>${progressLabel}</td>
          <td>${speedLabel}</td>
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
          const samplePath = enc.path || existing.sample_path;
          workersByMachine.set(machine, {
            ...existing,
            active: Math.max(existing.active || 0, 1),
            percent_complete: Math.max(existing.percent_complete || 0, percent),
            state: "encoding",
            sample_path: samplePath,
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

  window.refreshWorkers = refreshWorkers;
  window.pauseAllWorkers = async function () {
    await setGlobalMode("drain");
    await refreshWorkers();
  };
  window.resumeAllWorkers = async function () {
    await setGlobalMode("run");
    await refreshWorkers();
  };
  window.stopAllWorkers = async function () {
    await setGlobalMode("stop");
    await refreshWorkers();
  };
  window.pauseWorker = async function (machine) {
    await window.mfApi.postJson("/api/worker-control/worker", { machine, mode: "drain" });
    await refreshWorkers();
  };
  window.resumeWorker = async function (machine) {
    await window.mfApi.postJson("/api/worker-control/worker", { machine, mode: "run" });
    await refreshWorkers();
  };
  window.stopWorker = async function (machine) {
    await window.mfApi.postJson("/api/worker-control/worker", { machine, mode: "stop" });
    await refreshWorkers();
  };

  window.stopWorkerNow = async function (machine) {
    const ok = window.confirm(`Stop the current encode on ${machine} right now? This will requeue the job.`);
    if (!ok) return;
    await window.mfApi.postJson("/api/worker-control/stop-now", { machine });
    await refreshWorkers();
  };

  window.deleteWorker = async function (machine) {
    const ok = window.confirm(`Remove worker ${machine} from this dashboard?`);
    if (!ok) return;
    await window.mfApi.postJson("/api/workers/cleanup", { machine });
    await refreshWorkers();
  };

  window.clearOfflineWorkers = async function () {
    const ok = window.confirm("Clear offline workers not seen in 30 days?");
    if (!ok) return;
    await window.mfApi.postJson("/api/workers/cleanup", { older_than_days: 30, offline_only: true });
    await refreshWorkers();
  };

  renderWorkers(currentWorkers);
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
