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

  window.mfUi = {
    setTone,
    setStatus,
    updateNavScan,
    updateNavWatch,
    escapeHtml,
    getPageData,
  };
})();

