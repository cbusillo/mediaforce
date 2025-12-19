(function () {
  const form = document.getElementById("settings-form");
  if (!form) return;

  const statusEl = document.getElementById("settings-status");
  const watchStatus = document.getElementById("watch-status");
  const watchStart = document.getElementById("watch-start");
  const watchStop = document.getElementById("watch-stop");
  const addLibraryBtn = document.getElementById("add-library");

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

  addLibraryBtn?.addEventListener("click", () => addLibraryRow());
  wireScanButtons(document);
})();
