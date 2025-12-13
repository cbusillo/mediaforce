(function () {
  const page = window.mfUi?.getPageData?.() || {};
  const encodeId = page.encode_id;

  const source = document.getElementById("source");
  const encoded = document.getElementById("encoded");
  const playPause = document.getElementById("playPause");
  const toggleView = document.getElementById("toggleView");
  const seekBar = document.getElementById("seekBar");
  const timeDisplay = document.getElementById("timeDisplay");
  const speedSelect = document.getElementById("speed");

  const toggleEl = (el, show) => {
    if (!el) return;
    el.classList.toggle("hidden", !show);
  };

  let isSyncing = false;
  let viewMode = "both";
  const SEEK_STEP = 5;
  const SPEED_MAP = { 1: 1.0, 2: 1.25, 3: 1.5, 4: 1.75, 5: 2.0 };

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

  toggleView.addEventListener("click", () => {
    const current = Math.max(
      isFinite(source.currentTime) ? source.currentTime : 0,
      isFinite(encoded.currentTime) ? encoded.currentTime : 0,
    );
    source.currentTime = current;
    encoded.currentTime = current;

    if (viewMode === "both") {
      viewMode = "source";
      encoded.pause();
      toggleEl(encoded.parentElement, false);
      toggleEl(source.parentElement, true);
      toggleView.textContent = "View: Source";
    } else if (viewMode === "source") {
      viewMode = "encoded";
      source.pause();
      toggleEl(source.parentElement, false);
      toggleEl(encoded.parentElement, true);
      toggleView.textContent = "View: Encoded";
    } else {
      viewMode = "both";
      toggleEl(source.parentElement, true);
      toggleEl(encoded.parentElement, true);
      toggleView.textContent = "View: Both";
    }
    updateTime();
  });

  seekBar.addEventListener("input", () => {
    const dur = isFinite(source.duration) ? source.duration : 0;
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
      source.currentTime = Math.max(0, source.currentTime - SEEK_STEP);
      encoded.currentTime = source.currentTime;
    } else if (e.code === "ArrowRight") {
      source.currentTime = source.currentTime + SEEK_STEP;
      encoded.currentTime = source.currentTime;
    } else if (SPEED_MAP[e.key]) {
      const speed = SPEED_MAP[e.key];
      speedSelect.value = String(speed);
      source.playbackRate = speed;
      encoded.playbackRate = speed;
    }
  });

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
})();

