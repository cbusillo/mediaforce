(function () {
  let currentShowName = null;

  function setChipActive(selector, activeText) {
    document.querySelectorAll(selector).forEach((chip) => {
      const label = (chip.textContent || "").trim().toLowerCase();
      const active = label === activeText.toLowerCase();
      chip.classList.toggle("filter-chip--active", active);
      if (chip.hasAttribute("aria-pressed")) chip.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  window.setTierFilter = function (tier) {
    const el = document.getElementById("tierFilter");
    if (!el) return;
    el.value = tier || "";

    const labelMap = {
      "": "All",
      pristine: "Pristine",
      good: "Good",
      mediocre: "Mediocre",
      poor: "Poor",
      override: "Has Override",
    };
    setChipActive(".js-tier-chip", labelMap[el.value] || "All");
    window.filterShows();
  };

  window.filterShows = function () {
    const search = document.getElementById("showSearch")?.value.toLowerCase() || "";
    const tierFilter = document.getElementById("tierFilter")?.value || "";
    const rows = document.querySelectorAll("#showsTable tbody tr");
    let visible = 0;

    rows.forEach((row) => {
      const showName = (row.dataset.show || "").toLowerCase();
      const tier = row.dataset.tier;
      const override = row.dataset.override;

      let show = true;
      if (search && !showName.includes(search)) show = false;
      if (tierFilter === "override" && !override) show = false;
      else if (tierFilter && tierFilter !== "override" && tier !== tierFilter) show = false;

      row.style.display = show ? "" : "none";
      if (show) visible++;
    });

    const count = document.getElementById("showCount");
    if (count) count.textContent = String(visible);
  };

  window.addEventListener("DOMContentLoaded", () => {
    window.setTierFilter(document.getElementById("tierFilter")?.value || "");
  });

  window.setOverride = async function (select) {
    const showName = select.dataset.show;
    const tier = select.value;

    const status = document.getElementById("showsStatus");
    window.mfUi?.setStatus?.(status, `Saving override for ${showName}…`, "warning");

    const resp = await window.mfApi.postJson("/api/show-override", {
      show_name: showName,
      tier: tier || null,
    });
    const data = resp.data;
    if (!(resp.ok && data?.success)) {
      window.mfUi?.setStatus?.(status, data?.error || resp.error || "Failed to save override.", "danger");
      return;
    }

    select.closest("tr").dataset.override = tier;
    window.mfUi?.setStatus?.(status, "Saved.", "success");
    setTimeout(() => window.mfUi?.setStatus?.(status, "", "muted"), 1200);
  };

  window.applyTierToShow = function (showName) {
    currentShowName = showName;
    document.getElementById("modalShowName").textContent = showName;

    const row = document.querySelector(`tr[data-show="${CSS.escape(showName)}"]`);
    document.getElementById("modalTier").value = row?.dataset.override || row?.dataset.tier || "good";

    document.getElementById("overrideModal").style.display = "block";
  };

  window.hideOverrideModal = function () {
    document.getElementById("overrideModal").style.display = "none";
    currentShowName = null;
  };

  window.confirmApplyTier = async function () {
    if (!currentShowName) return;
    const tier = document.getElementById("modalTier").value;
    const setOverride = document.getElementById("modalSetOverride").checked;

    const status = document.getElementById("showsStatus");
    window.mfUi?.setStatus?.(status, `Applying ${tier} to pending…`, "warning");

    const resp = await window.mfApi.postJson("/api/apply-tier-to-show", {
      show_name: currentShowName,
      tier,
      set_override: setOverride,
    });
    const data = resp.data;
    if (resp.ok && data?.success) {
      window.mfUi?.setStatus?.(status, `Updated ${data["updated"]} episode(s).`, "success");
      window.location.reload();
    } else {
      window.mfUi?.setStatus?.(status, data?.error || resp.error || "Failed to apply tier.", "danger");
    }
  };

  document.addEventListener("keydown", (e) => {
    if (e.code === "Escape") window.hideOverrideModal();
  });
})();
