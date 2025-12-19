(function () {
  const page = window.mfUi?.getPageData?.() || {};
  const encodeId = page.encode_id;

  const source = document.getElementById("source");
  const encoded = document.getElementById("encoded");
  const playPause = document.getElementById("playPause");
  const seekBar = document.getElementById("seekBar");
  const timeDisplay = document.getElementById("timeDisplay");
  const speedSelect = document.getElementById("speed");

  if (!source || !encoded || !playPause || !seekBar || !timeDisplay || !speedSelect) return;

  const toggleEl = (el, show) => {
    if (!el) return;
    el.classList.toggle("hidden", !show);
  };

  let isSyncing = false;
  let viewMode = "both";
  let fitMode = "contain";
  const SEEK_STEP = 5;
  const SPEED_MAP = { 1: 1.0, 2: 1.25, 3: 1.5, 4: 1.75, 5: 2.0 };

  function setChipActive(selector, activeText) {
    document.querySelectorAll(selector).forEach((chip) => {
      const label = (chip.textContent || "").trim().toLowerCase();
      const active = label === activeText.toLowerCase();
      chip.classList.toggle("filter-chip--active", active);
      if (chip.hasAttribute("aria-pressed")) chip.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function applyFitMode() {
    const fit = fitMode === "cover" ? "cover" : "contain";
    [source, encoded].forEach((el) => {
      if (!el) return;
      el.style.objectFit = fit;
    });
  }

  function formatTimeHMS(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "0:00:00";
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    return `${hrs}:${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
  }

  function syncVideos(master, slave) {
    if (isSyncing) return;
    isSyncing = true;
    if (isFinite(master.currentTime)) {
      slave.currentTime = master.currentTime;
    }
    isSyncing = false;
  }

  function updateTime() {
    const master = viewMode === "encoded" ? encoded : source;
    const fallback = viewMode === "encoded" ? source : encoded;
    const masterTime = isFinite(master.currentTime) ? master.currentTime : fallback.currentTime;
    const durSource = isFinite(source.duration) ? source.duration : 0;
    const durEncoded = isFinite(encoded.duration) ? encoded.duration : 0;
    const dur = Math.max(durSource, durEncoded, 0);
    const pct = dur > 0 ? (masterTime / dur) * 100 : 0;
    seekBar.value = pct;
    timeDisplay.textContent = `${formatTimeHMS(masterTime)} / ${formatTimeHMS(dur)}`;
  }

  source.addEventListener("timeupdate", () => {
    syncVideos(source, encoded);
    updateTime();
  });
  encoded.addEventListener("timeupdate", () => syncVideos(encoded, source));
  source.addEventListener("loadedmetadata", updateTime);
  encoded.addEventListener("loadedmetadata", updateTime);

  playPause.addEventListener("click", () => {
    if (source.paused) {
      source.play();
      encoded.play();
      playPause.textContent = "Pause";
    } else {
      source.pause();
      encoded.pause();
      playPause.textContent = "Play";
    }
  });

  function applyViewMode(nextMode) {
    const current = Math.max(
      isFinite(source.currentTime) ? source.currentTime : 0,
      isFinite(encoded.currentTime) ? encoded.currentTime : 0,
    );
    source.currentTime = current;
    encoded.currentTime = current;

    viewMode = nextMode;
    if (viewMode === "source") {
      encoded.pause();
      toggleEl(encoded.parentElement, false);
      toggleEl(source.parentElement, true);
    } else if (viewMode === "encoded") {
      source.pause();
      toggleEl(source.parentElement, false);
      toggleEl(encoded.parentElement, true);
    } else {
      toggleEl(source.parentElement, true);
      toggleEl(encoded.parentElement, true);
    }

    setChipActive(".js-view-chip", viewMode === "both" ? "both" : viewMode);
    updateTime();
  }

  function setView(mode) {
    const next = mode === "source" || mode === "encoded" ? mode : "both";
    applyViewMode(next);
  }

  function setFit(mode) {
    fitMode = mode === "cover" ? "cover" : "contain";
    applyFitMode();
    setChipActive(".js-fit-chip", fitMode === "cover" ? "fill" : "fit");
  }

  seekBar.addEventListener("input", () => {
    const durSource = isFinite(source.duration) ? source.duration : 0;
    const durEncoded = isFinite(encoded.duration) ? encoded.duration : 0;
    const dur = Math.max(durSource, durEncoded, 0);
    const time = dur > 0 ? (seekBar.value / 100) * dur : 0;
    source.currentTime = time;
    encoded.currentTime = time;
  });

  speedSelect.addEventListener("change", () => {
    const speed = parseFloat(speedSelect.value);
    source.playbackRate = speed;
    encoded.playbackRate = speed;
  });

  document.addEventListener("keydown", (e) => {
    if (e.code === "Space") {
      e.preventDefault();
      playPause.click();
    } else if (e.code === "ArrowLeft") {
      const next = Math.max(0, Math.min(source.currentTime, encoded.currentTime) - SEEK_STEP);
      source.currentTime = next;
      encoded.currentTime = next;
    } else if (e.code === "ArrowRight") {
      const next = Math.max(source.currentTime, encoded.currentTime) + SEEK_STEP;
      source.currentTime = next;
      encoded.currentTime = next;
    } else if (SPEED_MAP[e.key]) {
      const speed = SPEED_MAP[e.key];
      speedSelect.value = String(speed);
      source.playbackRate = speed;
      encoded.playbackRate = speed;
    }
  });

  applyFitMode();
  applyViewMode("both");

  async function promote() {
    if (!encodeId) return;
    if (!confirm("Promote this encode? This will replace the original file.")) return;
    const resp = await window.mfApi.requestJson(`/api/promote/${encodeId}`, { method: "POST" });
    if (resp.ok && resp.data?.success) {
      window.location.href = "/review";
    } else {
      alert("Error: " + (resp.data?.error || resp.error || "unknown"));
    }
  }

  async function reject() {
    if (!encodeId) return;
    if (!confirm("Reject this encode? The encoded file will be deleted.")) return;
    const resp = await window.mfApi.postJson(`/api/reject/${encodeId}`, {});
    if (resp.ok && resp.data?.success) {
      window.location.href = "/review";
    } else {
      alert("Error: " + (resp.data?.error || resp.error || "unknown"));
    }
  }

  window.promote = promote;
  window.reject = reject;
  window.setView = setView;
  window.setFit = setFit;
})();
