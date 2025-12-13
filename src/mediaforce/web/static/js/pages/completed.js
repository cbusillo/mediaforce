(function () {
  const hoverPreview = window.mfCommon?.createHoverPreview({
    thumbId: "hoverThumb",
    videoId: "hoverVideo",
    srcBuilder: (id) => `/video/encoded/${id}`,
    delay: 150,
    maxWidth: 220,
  });

  window.showThumb = function (evt, id) {
    hoverPreview?.show(evt, id);
  };

  window.hideThumb = function () {
    hoverPreview?.hide();
  };
})();

