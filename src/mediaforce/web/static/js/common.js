// Shared UI helpers for Mediaforce web.
(function () {
  function toggleEl(el, show) {
    if (!el) return;
    el.classList.toggle("hidden", !show);
  }

  function createHoverPreview(opts = {}) {
    const thumb = document.getElementById(opts.thumbId || "hoverThumb");
    const video = document.getElementById(opts.videoId || "hoverVideo");
    const delay = opts.delay ?? 150;
    const offset = opts.offset ?? 12;
    const maxWidth = opts.maxWidth ?? 240;
    const srcBuilder = opts.srcBuilder || ((id) => id);
    let timer = null;

    const show = (evt, id) => {
      if (!thumb || !video) return;
      clearTimeout(timer);
      timer = setTimeout(() => {
        const target = evt?.currentTarget || evt?.target;
        if (!target?.getBoundingClientRect) return;
        video.src = srcBuilder(id);
        video.load();
        const rect = target.getBoundingClientRect();
        const left = Math.min(rect.right + offset, window.innerWidth - maxWidth);
        thumb.style.left = `${left}px`;
        thumb.style.top = `${rect.top}px`;
        toggleEl(thumb, true);
      }, delay);
    };

    const hide = () => {
      if (!thumb || !video) return;
      clearTimeout(timer);
      toggleEl(thumb, false);
      video.pause();
      video.src = "";
    };

    return { show, hide };
  }

  window.mfCommon = { toggleEl, createHoverPreview };
})();
