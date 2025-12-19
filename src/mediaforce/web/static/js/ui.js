// Shared UI helpers for Mediaforce web.
(function () {
  function setTone(el, tone) {
    if (!el) return;
    el.classList.remove("text-success", "text-danger", "text-warning", "text-muted");
    const cls =
      tone === "success"
        ? "text-success"
        : tone === "danger"
          ? "text-danger"
          : tone === "warning"
            ? "text-warning"
            : "text-muted";
    el.classList.add(cls);
  }

  function setStatus(el, text, tone = "muted") {
    if (!el) return;
    el.textContent = text;
    setTone(el, tone);
  }

  function updateNavPill(selector, running, onText, offText) {
    const pill = document.querySelector(selector);
    if (!pill) return;
    pill.classList.toggle("pill-on", !!running);
    pill.classList.toggle("pill-off", !running);
    pill.textContent = running ? onText : offText;
  }

  function updateNavScan(running, summaryText) {
    updateNavPill(".pill-scan", running, "Scan running", "Scan idle");
    if (summaryText) {
      const summary = document.getElementById("navScanSummary");
      if (summary) summary.textContent = summaryText;
    }
  }

  function updateNavWatch(running, message) {
    updateNavPill(".pill-watch", running, "Watch on", "Watch off");
    const navMsg = document.getElementById("navWatchStatus");
    if (navMsg && message) navMsg.textContent = message;
  }

  function setInlineStatus(el, message, tone = "muted") {
    if (!el) return;
    setStatus(el, message, tone);
  }

  function escapeHtml(text) {
    const s = String(text ?? "");
    return s
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function getPageData() {
    return window.__MF_PAGE__ || {};
  }

  function formatMaybeNumber(value, digits = 1) {
    if (value === null || value === undefined || value === "") return "-";
    const n = Number(value);
    if (Number.isNaN(n)) return String(value);
    return n.toFixed(digits);
  }

  function formatTrack(track) {
    const codec = track?.codec || track?.codec_name || "?";
    const channels = track?.channels;
    const bitrate = track?.bitrate_kbps || track?.bit_rate_kbps;
    const rate = track?.sample_rate_hz || track?.sample_rate || track?.sampleRate;
    const lang = track?.language || track?.tags?.language;
    const parts = [codec.toString().toUpperCase()];
    if (channels) parts.push(`${channels}ch`);
    if (bitrate) parts.push(`${bitrate}k`);
    if (rate) parts.push(`${rate}Hz`);
    if (lang) parts.push(lang);
    return parts.join(" ");
  }

  function trackSignature(track) {
    const codec = (track?.codec || track?.codec_name || "?").toString().toUpperCase();
    const channels = track?.channels ? `${track.channels}ch` : "";
    const lang = track?.language || track?.tags?.language || "";
    return [codec, channels, lang].filter(Boolean).join(" ") || "?";
  }

  function kv(label, value) {
    return `
      <div class="flex items-center justify-between gap-3 py-1 border-b border-surface-subtle">
        <div class="text-xs text-foreground-muted">${escapeHtml(label)}</div>
        <div class="text-sm text-foreground truncate">${escapeHtml(value ?? "-")}</div>
      </div>
    `;
  }

  function renderInspection(payload) {
    const src = payload?.source || {};
    const enc = payload?.encoded || {};
    const evalObj = payload?.evaluation || null;
    const probe = payload?.probe || {};

    const reduction = payload?.reduction_pct;
    const reductionText =
      reduction === null || reduction === undefined
        ? "-"
        : `${(reduction >= 0 ? "-" : "+")}${Math.abs(reduction).toFixed(0)}%`;

    const vmaf = enc?.quality?.vmaf;
    const ssim = enc?.quality?.ssim;
    const psnr = enc?.quality?.psnr;

    const srcProbe = probe?.source;
    const encProbe = probe?.encoded;

    const renderStreamCard = (title, base, probeInfo) => {
      const v = base?.video || {};
      const pvid = probeInfo?.video || {};
      const aud = base?.audio_tracks || [];
      const sub = base?.subtitle_tracks || [];
      const paud = probeInfo?.audio_tracks || [];
      const psub = probeInfo?.subtitle_tracks || [];

      const videoLines = [
        kv("Path", base?.path || "-"),
        kv("Exists", base?.exists ? "yes" : "no"),
        kv("Size", base?.size || "-"),
        kv("Duration", base?.duration || "-"),
        kv("Video codec", v?.codec || pvid?.codec || "-"),
        kv("Resolution", v?.resolution || (v?.width && v?.height ? `${v.width}x${v.height}` : pvid?.width && pvid?.height ? `${pvid.width}x${pvid.height}` : "-")),
        kv("Bit depth", v?.bit_depth || pvid?.bit_depth || "-"),
        kv("Frame rate", v?.frame_rate || (pvid?.framerate ? formatMaybeNumber(pvid?.framerate, 3) : "-")),
        kv("Bitrate", v?.bitrate_kbps ? `${v.bitrate_kbps}k` : pvid?.bitrate_kbps ? `${pvid.bitrate_kbps}k` : "-"),
        kv("HDR", v?.is_hdr ? (v?.hdr_format || "yes") : "no"),
        kv("Interlaced", v?.is_interlaced ? "yes" : "no"),
      ].join("");

      const trackList = (paud.length ? paud : aud).slice(0, 16);
      const audioList = trackList
        .map((t, idx) => `<li><span class='text-foreground-muted'>#${idx + 1}</span> ${escapeHtml(formatTrack(t))}</li>`)
        .join("");
      const subtitleCount = (psub.length ? psub : sub).length;

      return `
        <div class="card">
          <div class="section-heading">
            <div class="section-title">${escapeHtml(title)}</div>
          </div>
          <div class="space-y-2">
            ${videoLines}
            <div class="pt-2">
              <div class="text-xs text-foreground-muted mb-1">Audio tracks</div>
              <ul class="text-sm space-y-1">${audioList || "<li class='text-foreground-muted'>-</li>"}</ul>
            </div>
            <div class="pt-2">
              <div class="text-xs text-foreground-muted mb-1">Subtitles</div>
              <div class="text-sm">${subtitleCount ? `${subtitleCount} track(s)` : "-"}</div>
            </div>
          </div>
        </div>
      `;
    };

    const alerts = [];
    const pushAlert = (text, tone = "warning", short = null) => {
      if (!text) return;
      alerts.push({ text, tone, short });
    };

    const severityRank = (tone) => {
      if (tone === "danger") return 0;
      if (tone === "warning") return 1;
      if (tone === "info") return 2;
      return 3;
    };

    const serverAlerts = Array.isArray(payload?.attention_alerts) ? payload.attention_alerts : null;
    if (serverAlerts && serverAlerts.length) {
      serverAlerts.forEach((a) => {
        pushAlert(a.message || a.text || "", a.severity || a.tone || "warning", a.short || null);
      });
    } else {
    const sourceTracks = (srcProbe?.audio_tracks || src?.audio_tracks || []).filter(Boolean);
    const encodedTracks = (encProbe?.audio_tracks || enc?.audio_tracks || []).filter(Boolean);
    if (sourceTracks.length && encodedTracks.length && sourceTracks.length !== encodedTracks.length) {
      pushAlert(`Audio tracks changed: ${sourceTracks.length} → ${encodedTracks.length}`, "warning");
    } else if (sourceTracks.length && encodedTracks.length) {
      const max = Math.min(sourceTracks.length, encodedTracks.length, 8);
      for (let i = 0; i < max; i++) {
        const sSig = trackSignature(sourceTracks[i]);
        const eSig = trackSignature(encodedTracks[i]);
        if (sSig !== eSig) {
          pushAlert(`Audio #${i + 1}: ${sSig} → ${eSig}`, "warning");
        }
      }
    }
    const sourceSubs = (srcProbe?.subtitle_tracks || src?.subtitle_tracks || []).filter(Boolean);
    const encodedSubs = (encProbe?.subtitle_tracks || enc?.subtitle_tracks || []).filter(Boolean);
    if (sourceSubs.length !== encodedSubs.length) {
      pushAlert(`Subtitle tracks changed: ${sourceSubs.length} → ${encodedSubs.length}`, "warning");
    } else if (sourceSubs.length && encodedSubs.length) {
      const max = Math.min(sourceSubs.length, encodedSubs.length, 8);
      for (let i = 0; i < max; i++) {
        const sLang = sourceSubs[i]?.language || sourceSubs[i]?.tags?.language || "";
        const eLang = encodedSubs[i]?.language || encodedSubs[i]?.tags?.language || "";
        if (sLang && eLang && sLang !== eLang) {
          pushAlert(`Subtitle #${i + 1} language: ${sLang} → ${eLang}`, "warning");
        }
      }
    }
    const sVid = srcProbe?.video || src?.video || {};
    const eVid = encProbe?.video || enc?.video || {};
    if (sVid.codec && eVid.codec && sVid.codec !== eVid.codec) {
      pushAlert(`Video codec: ${sVid.codec} → ${eVid.codec}`, "info");
    }
    if (sVid.width && sVid.height && eVid.width && eVid.height) {
      const sRes = `${sVid.width}x${sVid.height}`;
      const eRes = `${eVid.width}x${eVid.height}`;
      if (sRes !== eRes) pushAlert(`Resolution: ${sRes} → ${eRes}`, "warning");
    }
    if (sVid.is_hdr !== eVid.is_hdr) {
      pushAlert(`HDR changed: ${(sVid.is_hdr ? "yes" : "no")} → ${(eVid.is_hdr ? "yes" : "no")}`, "warning");
    }
    if (sVid.bit_depth && eVid.bit_depth && sVid.bit_depth !== eVid.bit_depth) {
      pushAlert(`Bit depth: ${sVid.bit_depth} → ${eVid.bit_depth}`, "warning");
    }
    if (sVid.is_interlaced !== eVid.is_interlaced) {
      pushAlert(
        `Interlaced: ${(sVid.is_interlaced ? "yes" : "no")} → ${(eVid.is_interlaced ? "yes" : "no")}`,
        "warning",
      );
    }
    if (sVid.bitrate_kbps && eVid.bitrate_kbps) {
      const ratio = Number(eVid.bitrate_kbps) / Number(sVid.bitrate_kbps);
      if (isFinite(ratio) && ratio > 1.3) {
        pushAlert(`Video bitrate up: ${sVid.bitrate_kbps}k → ${eVid.bitrate_kbps}k`, "warning");
      }
    }
    if (enc?.quality?.vmaf !== null && enc?.quality?.vmaf !== undefined) {
      const v = Number(enc.quality.vmaf);
      if (!Number.isNaN(v) && v < 85) pushAlert(`Low VMAF: ${v.toFixed(1)}`, "warning");
    }
    if (reduction !== null && reduction !== undefined && reduction < 0) {
      pushAlert(`Size increased: +${Math.abs(reduction).toFixed(0)}%`, "danger");
    }
    }

    const qualityCard = `
      <div class="card">
        <div class="section-heading">
          <div class="section-title">Quality</div>
        </div>
        ${kv("Reduction", reductionText)}
        ${kv("VMAF", formatMaybeNumber(vmaf, 1))}
        ${kv("SSIM", formatMaybeNumber(ssim, 4))}
        ${kv("PSNR", formatMaybeNumber(psnr, 2))}
        ${kv("Compression", enc?.quality?.compression_ratio ? formatMaybeNumber(enc.quality.compression_ratio, 3) : "-")}
        ${kv("CRF", enc?.settings?.crf ?? "-")}
        ${kv("Preset", enc?.settings?.preset ?? "-")}
        ${kv("Denoise", enc?.settings?.denoise ?? "-")}
        ${kv("Film grain", enc?.settings?.film_grain ?? "-")}
        ${kv("Machine", enc?.machine ?? "-")}
        ${kv("Completed", enc?.timestamps?.completed_at ?? "-")}
      </div>
    `;

    const evalCard = evalObj
      ? `
        <div class="card">
          <div class="section-heading">
            <div class="section-title">Profile Evaluation</div>
          </div>
          ${kv("Selected profile", evalObj.selected_profile)}
          ${kv("Decision", evalObj.decision)}
          ${kv("Status", evalObj.status)}
          ${kv("VMAF summary", evalObj.median_vmaf ? `med ${formatMaybeNumber(evalObj.median_vmaf, 1)} · min ${formatMaybeNumber(evalObj.min_vmaf, 1)}` : "-")}
          ${kv("Thresholds", evalObj.threshold_median ? `${evalObj.threshold_min ?? "-"}/${evalObj.threshold_median ?? "-"}/${evalObj.threshold_max ?? "-"}` : "-")}
        </div>
      `
      : "";

    const sortedAlerts = alerts
      .slice()
      .sort((a, b) => severityRank(String(a.tone || "warning")) - severityRank(String(b.tone || "warning")));

    const alertBar = sortedAlerts.length
      ? `
        <div class="card">
          <div class="section-heading">
            <div class="section-title">Watchlist</div>
            <div class="section-subtle">Things worth checking</div>
          </div>
          <div class="flex flex-wrap gap-2">
            ${sortedAlerts
              .slice(0, 8)
              .map((a) => {
                const tone = String(a.tone || "warning");
                const cls = tone === "danger" ? "chip-danger" : tone === "info" ? "chip-info" : "chip-warning";
                return `<span class="chip chip-sm ${cls}">${escapeHtml(a.text)}</span>`;
              })
              .join("")}
          </div>
        </div>
      `
      : `
        <div class="card">
          <div class="section-heading">
            <div class="section-title">Watchlist</div>
            <div class="section-subtle">No issues detected</div>
          </div>
        </div>
      `;

    return `
      ${alertBar}
      <div class="grid gap-4 md:grid-cols-2">
        ${renderStreamCard("Source", src, srcProbe)}
        ${renderStreamCard("Encoded", enc, encProbe)}
      </div>
      <div class="grid gap-4 md:grid-cols-2">
        ${qualityCard}
        ${evalCard || "<div></div>"}
      </div>
    `;
  }

  async function fetchInspection(encodeId, opts = {}) {
    const probeEncoded = opts.probeEncoded !== false;
    const probeSource = opts.probeSource === true;
    const qs = new URLSearchParams();
    qs.set("probe_encoded", probeEncoded ? "1" : "0");
    qs.set("probe_source", probeSource ? "1" : "0");
    const url = `/api/review/encode/${encodeId}?${qs.toString()}`;
    const resp = await fetch(url);
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      return { ok: false, error: String(e) };
    }
    if (!resp.ok || !data?.success) {
      return { ok: false, error: data?.error || resp.statusText || "failed to load" };
    }
    return { ok: true, data };
  }

  async function loadInspectionInto(containerEl, encodeId, opts = {}) {
    if (!containerEl) return;
    containerEl.innerHTML = `<div class="text-foreground-muted">Loading…</div>`;
    const res = await fetchInspection(encodeId, opts);
    if (!res.ok) {
      containerEl.textContent = res.error;
      return;
    }
    containerEl.innerHTML = renderInspection(res.data);
  }

  window.mfUi = {
    setStatus,
    setInlineStatus,
    updateNavScan,
    escapeHtml,
    getPageData,
  };

  window.mfInspect = {
    fetch: fetchInspection,
    renderHtml: renderInspection,
    loadInto: loadInspectionInto,
  };

  function dispatchWorkersRefresh() {
    document.body.dispatchEvent(new CustomEvent("mfWorkersRefresh"));
  }

  async function postGlobalWorkerMode(mode, statusEl) {
    if (!mode) return;
    setStatus(statusEl, `Setting global mode: ${mode}…`, "warning");
    try {
      const resp = await window.mfApi.postJson("/api/worker-control/global", { mode });
      if (resp.ok && resp.data?.success) {
        setStatus(statusEl, "Updated.", "success");
        setTimeout(() => setStatus(statusEl, "", "muted"), 1500);
        dispatchWorkersRefresh();
      } else {
        setStatus(statusEl, resp.data?.error || resp.error || "Failed to update worker mode.", "danger");
      }
    } catch (_) {
      setStatus(statusEl, "Failed to update worker mode.", "danger");
    }
  }

  async function cleanupOfflineWorkers(statusEl) {
    const ok = window.confirm("Clear offline workers not seen in 30 days?");
    if (!ok) return;
    setStatus(statusEl, "Clearing offline workers…", "warning");
    try {
      const resp = await window.mfApi.postJson("/api/workers/cleanup", { older_than_days: 30, offline_only: true });
      if (resp.ok && resp.data?.success) {
        const n = (resp.data.deleted || resp.data.removed || []).length;
        setStatus(statusEl, n ? `Cleared ${n} offline worker(s).` : "Cleared.", "success");
        setTimeout(() => setStatus(statusEl, "", "muted"), 1500);
        dispatchWorkersRefresh();
      } else {
        setStatus(statusEl, resp.data?.error || resp.error || "Failed to clear offline workers.", "danger");
      }
    } catch (_) {
      setStatus(statusEl, "Failed to clear offline workers.", "danger");
    }
  }

  function resolveStatusEl(el) {
    const selector = el?.getAttribute?.("data-mf-status") || "";
    if (!selector) return null;
    try {
      return document.querySelector(selector);
    } catch (_) {
      return null;
    }
  }

  function bindWorkerControls() {
    document.addEventListener("click", (e) => {
      const target = e.target?.closest?.("[data-mf-worker-action]");
      if (!target) return;
      const action = target.getAttribute("data-mf-worker-action") || "";
      const statusEl = resolveStatusEl(target);

      if (action === "global-mode") {
        const mode = target.getAttribute("data-mode") || "";
        void postGlobalWorkerMode(mode, statusEl);
      } else if (action === "cleanup-offline") {
        void cleanupOfflineWorkers(statusEl);
      } else if (action === "refresh") {
        dispatchWorkersRefresh();
      }
    });
  }

  bindWorkerControls();

  window.mfWorkers = {
    dispatchRefresh: dispatchWorkersRefresh,
    setGlobalMode: postGlobalWorkerMode,
    cleanupOffline: cleanupOfflineWorkers,
  };
})();
