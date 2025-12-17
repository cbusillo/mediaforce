(function () {
  if (window.location.pathname !== "/settings") return;

  const table = document.getElementById("settingsWorkersTable");
  const refreshBtn = document.getElementById("workers-refresh");
  const clearOfflineBtn = document.getElementById("workers-clear-offline");
  const normalizeBtn = document.getElementById("workers-normalize");
  const runAllBtn = document.getElementById("workers-run-all");
  const pauseAllBtn = document.getElementById("workers-pause-all");
  const stopAllBtn = document.getElementById("workers-stop-all");
  const statusEl = document.getElementById("workers-status");

  const pendingEl = document.getElementById("queuePending");
  const encodingEl = document.getElementById("queueEncoding");
  const pausedEl = document.getElementById("queuePaused");
  const globalModeEl = document.getElementById("globalWorkerMode");

  function setStatus(text, level) {
    window.mfUi?.setStatus(statusEl, text, level || "muted");
  }

  function renderWorkers(list) {
    if (!table) return;
    const tbody = table.querySelector("tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    if (!list || list.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="8" class="text-foreground-muted">No workers reporting.</td>';
      tbody.appendChild(tr);
      return;
    }

    list.forEach((w) => {
      const tr = document.createElement("tr");
      const machine = w.machine || "-";
      const state = w.state || "-";
      const mode = w.control_mode || "-";
      const active = w.active || 0;
      const percent = typeof w.percent_complete === "number" ? w.percent_complete : 0;
      const progressLabel = state === "encoding" ? (percent > 0 ? `${Math.round(percent)}%` : "Starting…") : "—";
      const lastSeen = w.updated_at || "-";
      const message = w.sample_path || w.samplePath || "-";
      const safeMachine = String(machine).replace(/'/g, "\\'");

      const canControl = machine && machine !== "-";
      const actions = !canControl
        ? ""
        : `<button class="btn btn-xs btn-success" onclick="settingsSetWorkerMode('${safeMachine}','run')">Run</button>` +
          `<button class="btn btn-xs btn-warning" onclick="settingsSetWorkerMode('${safeMachine}','drain')">Pause</button>` +
          `<button class="btn btn-xs btn-danger" onclick="settingsSetWorkerMode('${safeMachine}','stop')">Stop</button>` +
          `<button class="btn btn-xs btn-danger" onclick="settingsStopNow('${safeMachine}')">Stop Now</button>` +
          `<button class="btn btn-xs" onclick="settingsClearWorkerOverride('${safeMachine}')">Clear Override</button>` +
          (state === "offline" ? `<button class="btn btn-xs" onclick="settingsDeleteWorker('${safeMachine}')">Remove</button>` : "");

      tr.innerHTML = `
        <td>${machine}</td>
        <td>${state}</td>
        <td>${mode}</td>
        <td>${active}</td>
        <td>${progressLabel}</td>
        <td>${lastSeen}</td>
        <td class="truncate" title="${message}">${message}</td>
        <td>${actions}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  async function refresh() {
    setStatus("Refreshing…", "warning");
    try {
      const resp = await window.mfApi.getJson("/api/workers");
      if (resp.ok && resp.data?.success) {
        renderWorkers(resp.data.workers || []);
        setStatus("", "muted");
      } else {
        setStatus(resp.data?.error || resp.error || "Failed to load workers.", "danger");
      }
    } catch (_) {
      setStatus("Failed to load workers.", "danger");
    }

    try {
      const statsResp = await window.mfApi.getJson("/api/stats");
      if (statsResp.ok && statsResp.data) {
        const pending = statsResp.data.pending || 0;
        const encoding = statsResp.data.encoding || 0;
        const paused = statsResp.data.paused || 0;
        if (pendingEl) pendingEl.textContent = String(pending);
        if (encodingEl) encodingEl.textContent = String(encoding);
        if (pausedEl) pausedEl.textContent = String(paused);
      }
    } catch (_) {
      /* ignore */
    }

    try {
      const ctrlResp = await window.mfApi.getJson("/api/worker-control");
      if (ctrlResp.ok && ctrlResp.data?.success) {
        if (globalModeEl) globalModeEl.textContent = String(ctrlResp.data.global || "run");
      }
    } catch (_) {
      /* ignore */
    }
  }

  window.settingsDeleteWorker = async function (machine) {
    const ok = window.confirm(`Remove worker ${machine}? This clears stored state/overrides.`);
    if (!ok) return;
    setStatus(`Removing ${machine}…`, "warning");
    const resp = await window.mfApi.postJson("/api/workers/cleanup", { machine });
    if (resp.ok && resp.data?.success) {
      setStatus(`Removed ${machine}.`, "success");
      await refresh();
    } else {
      setStatus(resp.data?.error || resp.error || "Failed to remove worker.", "danger");
    }
  };

  window.settingsSetWorkerMode = async function (machine, mode) {
    setStatus(`Setting ${machine} → ${mode}…`, "warning");
    const resp = await window.mfApi.postJson("/api/worker-control/worker", { machine, mode });
    if (resp.ok && resp.data?.success) {
      setStatus(`Set ${machine} → ${mode}.`, "success");
      await refresh();
    } else {
      setStatus(resp.data?.error || resp.error || "Failed to update worker.", "danger");
    }
  };

  window.settingsClearWorkerOverride = async function (machine) {
    const ok = window.confirm(`Clear per-worker override for ${machine} (fallback to global mode)?`);
    if (!ok) return;
    setStatus(`Clearing override for ${machine}…`, "warning");
    const resp = await window.mfApi.postJson("/api/worker-control/worker", { machine, mode: null });
    if (resp.ok && resp.data?.success) {
      setStatus(`Cleared override for ${machine}.`, "success");
      await refresh();
    } else {
      setStatus(resp.data?.error || resp.error || "Failed to clear override.", "danger");
    }
  };

  window.settingsStopNow = async function (machine) {
    const ok = window.confirm(`Stop the current encode on ${machine} right now? This will requeue the job.`);
    if (!ok) return;
    setStatus(`Stopping ${machine}…`, "warning");
    const resp = await window.mfApi.postJson("/api/worker-control/stop-now", { machine });
    if (resp.ok && resp.data?.success) {
      setStatus(`Stop-now requested for ${machine}.`, "success");
      await refresh();
    } else {
      setStatus(resp.data?.error || resp.error || "Failed to stop now.", "danger");
    }
  };

  async function clearOffline() {
    const ok = window.confirm("Clear offline workers not seen in 30 days?");
    if (!ok) return;
    setStatus("Clearing…", "warning");
    const resp = await window.mfApi.postJson("/api/workers/cleanup", { older_than_days: 30, offline_only: true });
    if (resp.ok && resp.data?.success) {
      const n = (resp.data.deleted || []).length;
      setStatus(`Cleared ${n} worker(s).`, "success");
      await refresh();
    } else {
      setStatus(resp.data?.error || resp.error || "Failed to clear workers.", "danger");
    }
  }

  async function normalizeNames() {
    const ok = window.confirm("Merge old dotted worker names (offline > 7 days) into their base hostname?");
    if (!ok) return;
    setStatus("Normalizing…", "warning");
    const resp = await window.mfApi.postJson("/api/workers/normalize", { older_than_days: 7, offline_only: true });
    if (resp.ok && resp.data?.success) {
      const n = (resp.data.merged || []).length;
      setStatus(`Merged ${n} worker(s).`, "success");
      await refresh();
    } else {
      setStatus(resp.data?.error || resp.error || "Failed to normalize.", "danger");
    }
  }

  refreshBtn?.addEventListener("click", refresh);
  clearOfflineBtn?.addEventListener("click", clearOffline);
  normalizeBtn?.addEventListener("click", normalizeNames);

  runAllBtn?.addEventListener("click", async () => {
    setStatus("Setting global mode: run…", "warning");
    await window.mfApi.postJson("/api/worker-control/global", { mode: "run" });
    await refresh();
  });
  pauseAllBtn?.addEventListener("click", async () => {
    setStatus("Setting global mode: drain…", "warning");
    await window.mfApi.postJson("/api/worker-control/global", { mode: "drain" });
    await refresh();
  });
  stopAllBtn?.addEventListener("click", async () => {
    const ok = window.confirm("Stop all workers after they finish current encodes?");
    if (!ok) return;
    setStatus("Setting global mode: stop…", "warning");
    await window.mfApi.postJson("/api/worker-control/global", { mode: "stop" });
    await refresh();
  });

  refresh();
  setInterval(refresh, 5000);
})();
