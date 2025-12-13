(function () {
  let currentShowName = null;

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

  window.setOverride = async function (select) {
    const showName = select.dataset.show;
    const tier = select.value;

    const resp = await window.mfApi.postJson("/api/show-override", {
      show_name: showName,
      tier: tier || null,
    });
    const data = resp.data;
    if (!(resp.ok && data?.success)) {
      alert("Error: " + (data?.error || resp.error || "unknown"));
      window.location.reload();
      return;
    }

    select.closest("tr").dataset.override = tier;
  };

  window.applyTierToShow = function (showName) {
    currentShowName = showName;
    document.getElementById("modalShowName").textContent = showName;

    const row = document.querySelector(`tr[data-show="${CSS.escape(showName)}"]`);
    const currentTier = row?.dataset.override || row?.dataset.tier || "good";
    document.getElementById("modalTier").value = currentTier;

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

    const resp = await window.mfApi.postJson("/api/apply-tier-to-show", {
      show_name: currentShowName,
      tier,
      set_override: setOverride,
    });
    const data = resp.data;
    if (resp.ok && data?.success) {
      alert(`Updated ${data.updated} episodes to ${tier} tier.`);
      window.location.reload();
    } else {
      alert("Error: " + (data?.error || resp.error || "unknown"));
    }
  };

  document.addEventListener("keydown", (e) => {
    if (e.code === "Escape") window.hideOverrideModal();
  });
})();

