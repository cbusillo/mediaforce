(function () {
  const form = document.getElementById("settings-form");
  if (!form) return;

  const statusEl = document.getElementById("settings-status");
  const watchStatus = document.getElementById("watch-status");
  const watchStart = document.getElementById("watch-start");
  const watchStop = document.getElementById("watch-stop");

  function buildLibrariesPayload(formData) {
    const libraries = [];
    const indexPattern = /^libraries\[(\d+)\]\[(.+)\]$/;

    for (const [key, value] of formData.entries()) {
      const match = key.match(indexPattern);
      if (!match) continue;

      const idx = parseInt(match[1], 10);
      const field = match[2];
      if (!libraries[idx]) libraries[idx] = {};

      if (field === "watch") libraries[idx][field] = true;
      else libraries[idx][field] = value;
    }

    for (let i = 0; i < libraries.length; i++) {
      if (!libraries[i]) continue;
      if (typeof libraries[i].watch === "undefined") libraries[i].watch = false;

      if (libraries[i].max_height === "" || libraries[i].max_height === undefined) libraries[i].max_height = null;
      else libraries[i].max_height = parseInt(libraries[i].max_height, 10) || null;
    }
    return libraries;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(form);

    const libraries = buildLibrariesPayload(formData);

    const gmhEl = document.getElementById("global-max-height");
    const gmh = gmhEl ? gmhEl.value : "";
    const global_max_height = gmh && gmh !== "" ? parseInt(gmh, 10) : null;

    const maxConcurrencyEl = document.getElementById("max-concurrency");
    const max_concurrency = maxConcurrencyEl ? Math.max(1, parseInt(maxConcurrencyEl.value || "1", 10)) : 1;

    const offpeakEnabledEl = document.getElementById("offpeak-enabled");
    const offpeak_enabled = offpeakEnabledEl ? offpeakEnabledEl.checked : false;
    const offpeak_start = document.querySelector('input[name="offpeak_start"]')?.value || "00:00";
    const offpeak_end = document.querySelector('input[name="offpeak_end"]')?.value || "05:00";

    window.mfUi?.setStatus(statusEl, "Saving…", "warning");

    try {
      const resp = await window.mfApi.postJson("/api/settings", {
        libraries,
        global_max_height,
        max_concurrency,
        offpeak_enabled,
        offpeak_start,
        offpeak_end,
      });
      if (resp.ok && resp.data?.success) {
        window.mfUi?.setStatus(statusEl, "Settings saved.", "success");
      } else {
        window.mfUi?.setStatus(statusEl, resp.data?.error || resp.error || "Failed to save settings.", "danger");
      }
    } catch (err) {
      window.mfUi?.setStatus(statusEl, "Error saving settings.", "danger");
    }
  });

  async function toggleWatch(action) {
    window.mfUi?.setStatus(watchStatus, action === "start" ? "Starting…" : "Stopping…", "warning");
    try {
      const resp = await window.mfApi.postJson("/api/watch", { action });
      if (resp.ok && resp.data?.success) {
        const st = resp.data.status || {};
        const msg = `Watcher: ${st.running ? "Running" : "Stopped"}${st.message ? " (" + st.message + ")" : ""}`;
        window.mfUi?.setStatus(watchStatus, msg, st.running ? "success" : "muted");
      } else {
        window.mfUi?.setStatus(watchStatus, resp.data?.error || resp.error || "Failed to toggle watcher", "danger");
      }
    } catch (_) {
      window.mfUi?.setStatus(watchStatus, "Error toggling watcher", "danger");
    }
  }

  watchStart?.addEventListener("click", () => toggleWatch("start"));
  watchStop?.addEventListener("click", () => toggleWatch("stop"));

  document.querySelectorAll(".scan-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const path = btn.getAttribute("data-path");
      if (!path) return;
      btn.textContent = "Scanning…";
      btn.disabled = true;
      try {
        const resp = await window.mfApi.postJson("/api/scan", { path });
        if (!(resp.ok && resp.data?.success)) {
          alert(`Scan failed: ${resp.data?.error || resp.error || "unknown error"}`);
        }
      } catch (_) {
        alert("Error starting scan");
      } finally {
        btn.textContent = "Scan";
        btn.disabled = false;
      }
    });
  });
})();

