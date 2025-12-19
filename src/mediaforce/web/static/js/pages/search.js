(function () {
  if (window.location.pathname !== "/search") return;

  function syncQueryIntoHiddenForm(form) {
    const qInput = document.getElementById("searchQ");
    const hiddenQ = form.querySelector("input[name='q']");
    if (!hiddenQ) return;
    hiddenQ.value = qInput ? qInput.value : hiddenQ.value;
  }

  function applyHiddenField(name, value) {
    const form = document.getElementById("searchHiddenForm");
    if (!form) return;
    syncQueryIntoHiddenForm(form);
    const input = form.querySelector(`input[name='${CSS.escape(name)}']`);
    if (!input) return;
    input.value = value || "";
    form.submit();
  }

  function setChipActive(selector, activeText) {
    document.querySelectorAll(selector).forEach((chip) => {
      const label = (chip.textContent || "").trim().toLowerCase();
      const active = label === activeText.toLowerCase();
      chip.classList.toggle("filter-chip--active", active);
      if (chip.hasAttribute("aria-pressed")) chip.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  window.setSearchStatus = function (status) {
    const label = status ? status.charAt(0).toUpperCase() + status.slice(1) : "Any";
    setChipActive(".js-status-chip", label);
    applyHiddenField("status", status);
  };

  window.setSearchTier = function (tier) {
    const label = tier ? tier.charAt(0).toUpperCase() + tier.slice(1) : "Any";
    setChipActive(".js-tier-chip", label);
    applyHiddenField("tier", tier);
  };

  window.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("searchHiddenForm");
    const qInput = document.getElementById("searchQ");
    if (!form || !qInput) return;
    qInput.addEventListener("input", () => syncQueryIntoHiddenForm(form));
  });
})();
