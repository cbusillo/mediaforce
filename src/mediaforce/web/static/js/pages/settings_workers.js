(function () {
  if (window.location.pathname !== "/settings") return;

  const table = document.getElementById("settingsWorkersTable");
  const normalizeBtn = document.getElementById("workers-normalize");
  const reconcileBtn = document.getElementById("workers-reconcile");
  const statusEl = document.getElementById("workers-status");

  const pendingEl = document.getElementById("queuePending");
  const encodingEl = document.getElementById("queueEncoding");
  const pausedEl = document.getElementById("queuePaused");
  const globalModeEl = document.getElementById("globalWorkerMode");

  function setStatus(text, level) {
    window.mfUi?.setStatus(statusEl, text, level || "muted");
  }

  function renderWorkers(list, opts = {}) {
    if (!table) return;
    const tbody = table.querySelector("tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    if (opts.error) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="9" class="text-danger">${opts.error}</td>`;
      tbody.appendChild(tr);
      return;
    }

    if (!list || list.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="9" class="text-foreground-muted">No workers reporting.</td>';
      tbody.appendChild(tr);
      return;
    }

    function labelize(value) {
      if (!value) return "";
      return String(value)
        .split("_")
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(" ");
    }

    function formatMode(value) {
      if (!value) return "—";
      const norm = String(value).toLowerCase();
      if (norm === "run") return "Run";
      if (norm === "drain") return "Pause";
      if (norm === "stop") return "Stop";
      return labelize(norm);
    }

    function stateBadge(state) {
      const norm = String(state || "").toLowerCase();
      if (norm === "encoding") return { label: "Encoding", cls: "pill pill-on animate-pulse" };
      if (norm === "starting") return { label: "Starting", cls: "pill pill-on" };
      if (norm === "paused") return { label: "Paused", cls: "pill pill-off" };
      if (norm === "waiting") return { label: "Waiting", cls: "pill" };
      if (norm === "unavailable") return { label: "Unavailable", cls: "pill" };
      if (norm === "stopping") return { label: "Stopping", cls: "pill pill-off" };
      if (norm === "stopped") return { label: "Stopped", cls: "pill pill-off" };
      if (norm === "offline") return { label: "Offline", cls: "pill pill-off" };
      if (norm === "idle") return { label: "Idle", cls: "pill pill-off" };
      if (!norm) return { label: "—", cls: "pill" };
      return { label: labelize(norm), cls: "pill" };
    }

    list.forEach((w) => {
      const tr = document.createElement("tr");
      const machine = w.machine || "-";
      const state = w.state || "";
      const mode = w.control_mode || "";
      const override = w.override_mode || "";
      const active = w.active || 0;
      const percent = typeof w.percent_complete === "number" ? w.percent_complete : 0;
      const progressLabel = state === "encoding" ? (percent > 0 ? `${Math.round(percent)}%` : "Starting…") : "—";
      const lastSeen = w.updated_at || "-";
      const message = w.sample_path || w["samplePath"] || "-";
      const safeMachine = String(machine).replace(/'/g, "\\'");
      const badge = stateBadge(state);
      const modeLabel = formatMode(mode);
      const overrideLabel = override ? formatMode(override) : "—";

      const canControl = machine && machine !== "-";
      const actions = !canControl
        ? ""
        : `<div class="flex items-center gap-1 flex-nowrap whitespace-nowrap">` +
          `<button class="btn btn-sm btn-success" onclick="settingsSetWorkerMode('${safeMachine}','run')">Run</button>` +
          `<button class="btn btn-sm btn-warning" onclick="settingsSetWorkerMode('${safeMachine}','drain')">Pause</button>` +
          `<button class="btn btn-sm btn-danger" title="Stop now (kills ffmpeg and requeues)" onclick="settingsStopNow('${safeMachine}')">Stop</button>` +
          (w.override_mode ? `<button class="btn btn-sm" onclick="settingsClearWorkerOverride('${safeMachine}')">Clear</button>` : "") +
          (state === "offline" ? `<button class="btn btn-sm" onclick="settingsDeleteWorker('${safeMachine}')">Remove</button>` : "") +
          `</div>`;

      tr.innerHTML = `
        <td>${machine}</td>
        <td><span class="${badge.cls}" title="${labelize(state)}">${badge.label}</span></td>
        <td>${modeLabel}</td>
        <td>${overrideLabel}</td>
        <td>${active}</td>
        <td>${progressLabel}</td>
        <td>${lastSeen}</td>
        <td class="truncate" title="${message}">${message}</td>
        <td>${actions}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function normalizeWorkersForSignature(list) {
    if (!Array.isArray(list)) return [];
    return list
      .map((w) => ({
        machine: w.machine || "",
        state: w.state || "",
        control_mode: w.control_mode || "",
        override_mode: w.override_mode || "",
        active: w.active || 0,
        percent_complete: typeof w.percent_complete === "number" ? Math.round(w.percent_complete) : 0,
        // `updated_at` can change on every poll (causing a visible flicker). Only
        // track at minute precision so the UI remains stable.
        updated_at: (w.updated_at || "").slice(0, 16),
        sample_path: w.sample_path || w["samplePath"] || "",
      }))
      .sort((a, b) => a.machine.localeCompare(b.machine));
  }

  let inFlight = false;
  let lastSig = "";
  let refreshTimer = null;
  let lastWorkers = null;
  let lastStats = { pending: 0, encoding: 0, paused: 0 };
  let lastGlobalMode = null;

  function scheduleRefresh(delayMs) {
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      void refresh({ loud: false });
    }, delayMs);
  }

  async function refresh(opts = {}) {
    const loud = !!opts.loud;
    if (document.hidden) {
      scheduleRefresh(5000);
      return;
    }
    if (inFlight) return;
    inFlight = true;
    if (loud) setStatus("Refreshing…", "warning");

    let workers = null;
    let pending = null;
    let encoding = null;
    let paused = null;
    let globalMode = null;
    let workersError = null;
    try {
      const resp = await window.mfApi.getJson("/api/workers");
      if (resp.ok && resp.data?.success) {
        workers = resp.data.workers || [];
        lastWorkers = workers;
      } else {
        workersError = resp.data?.error || resp.error || "Failed to load workers.";
        setStatus(workersError, "danger");
      }
    } catch (_) {
      workersError = "Failed to load workers.";
      setStatus(workersError, "danger");
    }

    try {
      const statsResp = await window.mfApi.getJson("/api/stats");
      if (statsResp.ok && statsResp.data) {
        pending = statsResp.data.pending || 0;
        encoding = statsResp.data.encoding || 0;
        paused = statsResp.data.paused || 0;
        lastStats = { pending, encoding, paused };
      }
    } catch (_) {
      /* ignore */
    }

    try {
      const ctrlResp = await window.mfApi.getJson("/api/worker-control");
      if (ctrlResp.ok && ctrlResp.data?.success) {
        globalMode = ctrlResp.data.global || "run";
        lastGlobalMode = globalMode;
      }
    } catch (_) {
      /* ignore */
    }

    if (workers === null) {
      if (lastWorkers !== null) {
        workers = lastWorkers;
      } else {
        workers = [];
      }
    }

    if (pending === null || encoding === null || paused === null) {
      pending = lastStats.pending;
      encoding = lastStats.encoding;
      paused = lastStats.paused;
    }

    if (!globalMode) {
      globalMode = lastGlobalMode;
    }

    const sigObj = {
      workers: normalizeWorkersForSignature(workers),
      stats: { pending, encoding, paused },
      globalMode: String(globalMode || ""),
    };

    const sig = JSON.stringify(sigObj);
    if (sig !== lastSig) {
      if (workersError && lastWorkers === null) renderWorkers([], { error: workersError });
      else renderWorkers(workers);
      if (pendingEl) pendingEl.textContent = String(pending);
      if (encodingEl) encodingEl.textContent = String(encoding);
      if (pausedEl) pausedEl.textContent = String(paused);
      if (globalModeEl && globalMode) globalModeEl.textContent = String(globalMode);
      lastSig = sig;
    }

    if (loud) {
      setStatus("Updated.", "success");
      setTimeout(() => setStatus("", "muted"), 2000);
    } else if (!workersError) {
      setStatus("", "muted");
    }

    inFlight = false;
    scheduleRefresh(5000);
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
    if (window.mfWorkers?.cleanupOffline) {
      await window.mfWorkers.cleanupOffline(statusEl);
      await refresh();
      return;
    }

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
      const n = (resp.data["merged"] || []).length;
      setStatus(`Merged ${n} worker(s).`, "success");
      await refresh();
    } else {
      setStatus(resp.data?.error || resp.error || "Failed to normalize.", "danger");
    }
  }

  normalizeBtn?.addEventListener("click", normalizeNames);

  reconcileBtn?.addEventListener("click", async () => {
    setStatus("Reconciling…", "warning");
    const resp = await window.mfApi.postJson("/api/reconcile/run", {});
    if (resp.ok && resp.data?.success) {
      const r = resp.data.result || {};
      const reset = r["reset_to_pending"] || 0;
      const force = r["force_to_encoding"] || 0;
      setStatus(`Reconciled: reset=${reset}, forced=${force}.`, reset || force ? "warning" : "success");
    } else {
      setStatus(resp.data?.error || resp.error || "Reconcile failed.", "danger");
    }
    await refresh();
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void refresh({ loud: false });
  });

  document.body.addEventListener("mfWorkersRefresh", () => {
    void refresh({ loud: false });
  });

  void refresh({ loud: false });
})();
