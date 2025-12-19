// Shared UI helpers for Mediaforce web.
(function () {
  function toggleEl(el, show) {
    if (!el) return;
    el.classList.toggle("hidden", !show);
  }

  window.mfCommon = { toggleEl };
})();
