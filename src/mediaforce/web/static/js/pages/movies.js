(function () {
  function setChipActive(selector, activeText) {
    document.querySelectorAll(selector).forEach((chip) => {
      const label = (chip.textContent || "").trim().toLowerCase();
      const active = label === activeText.toLowerCase();
      chip.classList.toggle("filter-chip--active", active);
      if (chip.hasAttribute("aria-pressed")) chip.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  window.setMovieTierFilter = function (tier) {
    const el = document.getElementById("movieTierFilter");
    if (!el) return;
    el.value = tier || "";

    const labelMap = {
      "": "All",
      pristine: "Pristine",
      good: "Good",
      mediocre: "Mediocre",
      poor: "Poor",
    };
    setChipActive(".js-tier-chip", labelMap[el.value] || "All");
    window.filterMovies();
  };

  window.filterMovies = function () {
    const search = document.getElementById("movieSearch")?.value.toLowerCase() || "";
    const tierFilter = document.getElementById("movieTierFilter")?.value || "";
    const libFilter = document.getElementById("movieLibraryFilter")?.value || "";
    const groupFilter = document.getElementById("movieGroupFilter")?.value || "";
    const rows = document.querySelectorAll("#moviesTable tbody tr");
    let visible = 0;

    rows.forEach((row) => {
      const title = (row.dataset.title || "").toLowerCase();
      if (!title) return;
      const tier = row.dataset.tier;
      const library = row.dataset.library || "";
      const group = row.dataset.group || "";

      let show = true;
      if (search && !title.includes(search)) show = false;
      if (tierFilter && tier !== tierFilter) show = false;
      if (libFilter && library !== libFilter) show = false;
      if (groupFilter && group !== groupFilter) show = false;

      row.style.display = show ? "" : "none";
      if (show) visible++;
    });

    const count = document.getElementById("movieCount");
    if (count) count.textContent = String(visible);
  };

  window.addEventListener("DOMContentLoaded", () => {
    window.setMovieTierFilter(document.getElementById("movieTierFilter")?.value || "");
  });
})();
