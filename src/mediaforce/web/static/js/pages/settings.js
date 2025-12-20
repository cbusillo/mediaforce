(function () {
  const form = document.getElementById("settings-form");
  if (!form) return;

  const statusEl = document.getElementById("settings-status");
  const watchStatus = document.getElementById("watch-status");
  const watchStart = document.getElementById("watch-start");
  const watchStop = document.getElementById("watch-stop");
  const addLibraryBtn = document.getElementById("add-library");
  const transcodeInput = document.getElementById("transcode-root");
  const transcodeStatus = document.getElementById("transcode-root-status");
  let transcodeTimer = null;

  function setTranscodeStatus(text, level) {
    window.mfUi?.setStatus(transcodeStatus, text, level || "muted");
  }

  async function validateTranscodeRoot({ loud = false } = {}) {
    if (!transcodeInput || !transcodeStatus) return;
    const value = transcodeInput.value.trim();
    if (!value) {
      setTranscodeStatus("", "muted");
      return;
    }
    if (loud) setTranscodeStatus("Validating…", "warning");
    try {
      const resp = await window.mfApi.postJson("/api/validate/transcode-root", { transcode_root: value });
      if (resp.ok && resp.data?.success) {
        if (resp.data.valid) setTranscodeStatus(resp.data.message || "Looks good.", "success");
        else setTranscodeStatus(resp.data.message || "Invalid transcode root.", "danger");
      } else {
        setTranscodeStatus(resp.data?.error || resp.error || "Failed to validate.", "danger");
      }
    } catch (_) {
      setTranscodeStatus("Failed to validate.", "danger");
    }
  }

  function scheduleTranscodeValidation({ loud = false } = {}) {
    if (transcodeTimer) clearTimeout(transcodeTimer);
    transcodeTimer = setTimeout(() => {
      void validateTranscodeRoot({ loud });
    }, 400);
  }

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

  function getNextLibraryIndex() {
    const existing = Array.from(form.querySelectorAll("[name^='libraries[']"));
    const indices = existing
      .map((el) => {
        const m = String(el.getAttribute("name") || "").match(/^libraries\[(\d+)\]/);
        return m ? parseInt(m[1], 10) : null;
      })
      .filter((x) => typeof x === "number" && !Number.isNaN(x));
    return indices.length ? Math.max(...indices) + 1 : 0;
  }

  function wireScanButtons(root) {
    (root || document).querySelectorAll(".scan-btn").forEach((btn) => {
      if (btn.__mfBound) return;
      btn.__mfBound = true;
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
  }

  function addLibraryRow() {
    const tmpl = document.getElementById("libraryRowTemplate");
    const tbody = form.querySelector("table tbody");
    if (!tmpl || !tbody) return;

    const idx = getNextLibraryIndex();
    const frag = tmpl.content.cloneNode(true);
    const row = frag.querySelector("tr");
    if (!row) return;

    const replacements = {
      __ID__: `libraries[${idx}][id]`,
      __NAME__: `libraries[${idx}][name]`,
      __MEDIA_TYPE__: `libraries[${idx}][media_type]`,
      __MAC_PATH__: `libraries[${idx}][mac_path]`,
      __LINUX_PATH__: `libraries[${idx}][linux_path]`,
      __WATCH__: `libraries[${idx}][watch]`,
      __MAX_HEIGHT__: `libraries[${idx}][max_height]`,
    };

    row.querySelectorAll("[name]").forEach((el) => {
      const name = el.getAttribute("name");
      if (!name) return;
      const next = replacements[name];
      if (next) el.setAttribute("name", next);
    });

    tbody.appendChild(frag);

    const nameInput = form.querySelector(`input[name='libraries[${idx}][name]']`);
    nameInput?.focus();
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
    const transcodeRoot = document.getElementById("transcode-root")?.value || "";

    window.mfUi?.setStatus(statusEl, "Saving…", "warning");

    try {
      const resp = await window.mfApi.postJson("/api/settings", {
        libraries,
        global_max_height,
        max_concurrency,
        offpeak_enabled,
        offpeak_start,
        offpeak_end,
        transcode_root: transcodeRoot,
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
        const msg = `Watcher: ${st["running"] ? "Running" : "Stopped"}${st["message"] ? " (" + st["message"] + ")" : ""}`;
        window.mfUi?.setStatus(watchStatus, msg, st["running"] ? "success" : "muted");
      } else {
        window.mfUi?.setStatus(watchStatus, resp.data?.error || resp.error || "Failed to toggle watcher", "danger");
      }
    } catch (_) {
      window.mfUi?.setStatus(watchStatus, "Error toggling watcher", "danger");
    }
  }

  watchStart?.addEventListener("click", () => toggleWatch("start"));
  watchStop?.addEventListener("click", () => toggleWatch("stop"));

  addLibraryBtn?.addEventListener("click", () => addLibraryRow());
  wireScanButtons(document);

  if (transcodeInput) {
    transcodeInput.addEventListener("input", () => scheduleTranscodeValidation({ loud: false }));
    transcodeInput.addEventListener("blur", () => scheduleTranscodeValidation({ loud: true }));
    scheduleTranscodeValidation({ loud: false });
  }

  document.querySelectorAll("[data-transcode-suggestion]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const suggestion = btn.getAttribute("data-transcode-suggestion") || "";
      if (transcodeInput && suggestion) {
        transcodeInput.value = suggestion;
        scheduleTranscodeValidation({ loud: true });
      }
    });
  });

  const cleanupStatus = document.getElementById("cleanup-status");
  const previewResult = document.getElementById("cleanup-preview-result");

  async function cleanupResetQueue() {
    const ok = window.confirm(
      'Reset all inventory items to "pending"?\n\nThis clears claims, cooldowns, and skip reasons. Original media is not deleted.',
    );
    if (!ok) return;

    window.mfUi?.setStatus(cleanupStatus, "Resetting queue…", "warning");
    const resp = await window.mfApi.postJson("/api/cleanup/reset-queue", { confirm: true });
    if (resp.ok && resp.data?.success) {
      window.mfUi?.setStatus(cleanupStatus, resp.data.message || "Queue reset.", "success");
    } else {
      window.mfUi?.setStatus(cleanupStatus, resp.data?.error || resp.error || "Failed to reset queue.", "danger");
    }
  }

  async function cleanupClearReview(deleteAll) {
    const msg = deleteAll
      ? "DELETE all encode results?\n\nThis cannot be undone."
      : 'Reset all encode results to "pending" review?';
    const ok = window.confirm(msg);
    if (!ok) return;

    window.mfUi?.setStatus(cleanupStatus, deleteAll ? "Deleting encodes…" : "Resetting review…", "warning");
    const resp = await window.mfApi.postJson("/api/cleanup/clear-review", { confirm: true, delete_all: !!deleteAll });
    if (resp.ok && resp.data?.success) {
      window.mfUi?.setStatus(cleanupStatus, resp.data.message || "Done.", "success");
    } else {
      window.mfUi?.setStatus(cleanupStatus, resp.data?.error || resp.error || "Failed to clear review.", "danger");
    }
  }

  async function cleanupPreviewTranscode() {
    const root = document.getElementById("cleanup-transcode-root")?.value || "";
    window.mfUi?.setStatus(cleanupStatus, "Previewing…", "warning");
    const resp = await window.mfApi.postJson("/api/cleanup/transcode-files", {
      dry_run: true,
      confirm: false,
      transcode_root: root,
    });
    if (resp.ok && resp.data?.success) {
      const n = (resp.data["deleted_files"] || []).length;
      const mb = ((resp.data["bytes_freed"] || 0) / 1024 / 1024).toFixed(1);
      if (previewResult) previewResult.textContent = `Would delete ${n} file(s) (~${mb} MB).`;
      window.mfUi?.setStatus(cleanupStatus, "Preview ready.", "success");
      setTimeout(() => window.mfUi?.setStatus(cleanupStatus, "", "muted"), 1500);
    } else {
      window.mfUi?.setStatus(cleanupStatus, resp.data?.error || resp.error || "Preview failed.", "danger");
    }
  }

  async function cleanupApplyTranscode() {
    const root = document.getElementById("cleanup-transcode-root")?.value || "";
    const ok = window.confirm(`Delete files under:\n\n${root}\n\nThis cannot be undone.`);
    if (!ok) return;

    window.mfUi?.setStatus(cleanupStatus, "Deleting files…", "warning");
    const resp = await window.mfApi.postJson("/api/cleanup/transcode-files", {
      dry_run: false,
      confirm: true,
      transcode_root: root,
    });
    if (resp.ok && resp.data?.success) {
      const n = (resp.data["deleted_files"] || []).length;
      const mb = ((resp.data["bytes_freed"] || 0) / 1024 / 1024).toFixed(1);
      if (previewResult) previewResult.textContent = `Deleted ${n} file(s) (~${mb} MB).`;
      window.mfUi?.setStatus(cleanupStatus, `Deleted ${n} file(s).`, "success");
    } else {
      window.mfUi?.setStatus(cleanupStatus, resp.data?.error || resp.error || "Delete failed.", "danger");
    }
  }

  async function cleanupResetAll() {
    const deleteEncodes = document.getElementById("cleanup-resetall-delete-encodes")?.checked;
    const cleanTranscode = document.getElementById("cleanup-resetall-clean-transcode")?.checked;
    const root = document.getElementById("cleanup-transcode-root")?.value || "";

    const msg =
      `Reset everything?\n\n` +
      `- Queue reset: yes\n` +
      `- Review reset: ${deleteEncodes ? "delete all encodes" : "reset to pending"}\n` +
      `- Clean transcode: ${cleanTranscode ? "yes" : "no"}\n\n` +
      `This cannot be undone.`;

    const ok = window.confirm(msg);
    if (!ok) return;

    window.mfUi?.setStatus(cleanupStatus, "Resetting…", "warning");
    const resp = await window.mfApi.postJson("/api/cleanup/reset-all", {
      confirm: true,
      delete_encode_results: !!deleteEncodes,
      clean_transcode_files: !!cleanTranscode,
      transcode_root: root,
    });
    if (resp.ok && resp.data?.success) {
      window.mfUi?.setStatus(cleanupStatus, "Reset complete.", "success");
      if (previewResult) previewResult.textContent = JSON.stringify(resp.data.results || {});
    } else {
      window.mfUi?.setStatus(cleanupStatus, resp.data?.error || resp.error || "Reset failed.", "danger");
    }
  }

  document.getElementById("cleanup-reset-queue")?.addEventListener("click", cleanupResetQueue);
  document.getElementById("cleanup-reset-review")?.addEventListener("click", () => cleanupClearReview(false));
  document.getElementById("cleanup-delete-review")?.addEventListener("click", () => cleanupClearReview(true));
  document.getElementById("cleanup-preview")?.addEventListener("click", cleanupPreviewTranscode);
  document.getElementById("cleanup-apply")?.addEventListener("click", cleanupApplyTranscode);
  document.getElementById("cleanup-reset-all")?.addEventListener("click", cleanupResetAll);
})();
