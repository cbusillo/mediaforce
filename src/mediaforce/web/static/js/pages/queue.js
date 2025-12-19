(function () {
  if (window.location.pathname !== "/queue") return;

  const page = window.mfUi?.getPageData?.() || {};
  const libraryRoot = page.library_root || "";
  const currentSort = page.sort || "priority";
  const currentOrder = page.order || "desc";
  const workers = Array.isArray(page.workers) ? page.workers : [];

  const loadedShows = {};
  const loadedSeasons = {};

  const scanStatusEl = document.getElementById("scanStatus");
  const serverShowInput = document.getElementById("serverShow");
  const tierFilter = document.getElementById("tierFilter");
  const sizeMinInput = document.getElementById("sizeMin");
  const sizeMaxInput = document.getElementById("sizeMax");

  const escapeHtml = (text) => window.mfUi?.escapeHtml?.(text) || String(text ?? "");
  const setStatus = (el, message, tone = "muted") => window.mfUi?.setInlineStatus?.(el, message, tone);
  const updateNavScan = (running, summaryText) => window.mfUi?.updateNavScan?.(running, summaryText);

  const tierQuery = page.tier_query || "";
  if (tierFilter && tierQuery) tierFilter.value = tierQuery;

  function applyFilters() {
    const url = new URL(window.location.href);
    const showVal = serverShowInput?.value.trim();
    const tierVal = tierFilter?.value || "";
    const minVal = sizeMinInput?.value.trim();
    const maxVal = sizeMaxInput?.value.trim();

    url.searchParams.delete("page");
    if (showVal) url.searchParams.set("show", showVal);
    else url.searchParams.delete("show");
    if (tierVal) url.searchParams.set("tier", tierVal);
    else url.searchParams.delete("tier");
    if (minVal) url.searchParams.set("size_min", minVal);
    else url.searchParams.delete("size_min");
    if (maxVal) url.searchParams.set("size_max", maxVal);
    else url.searchParams.delete("size_max");
    window.location.href = url.toString();
  }

  function resetFilters() {
    const url = new URL(window.location.href);
    ["show", "tier", "size_min", "size_max", "page"].forEach((k) => url.searchParams.delete(k));
    window.location.href = url.toString();
  }

  function showToast(message, success = true) {
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.className = `toast ${success ? "success" : "error"}`;
    toast.classList.remove("hidden", "opacity-0");
    toast.classList.add("animate-fade-in");
    setTimeout(() => {
      toast.classList.add("opacity-0");
    }, 1800);
    setTimeout(() => {
      toast.classList.add("hidden");
      toast.classList.remove("opacity-0", "animate-fade-in");
    }, 2200);
  }

  async function triggerScan(evt) {
    const btn = evt?.currentTarget || evt?.target;
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = "Scanning...";
    setStatus(scanStatusEl, "Starting scan…", "warning");
    try {
      const resp = await fetch("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: libraryRoot }),
      });
      const data = await resp.json();
      if (data.success) {
        setStatus(scanStatusEl, "Scan running…", "success");
        updateNavScan(true, `Scan running on ${libraryRoot}`);
        showToast("Scan started", true);
      } else {
        setStatus(scanStatusEl, `Scan error: ${data.error || data.output || "unknown"}`, "danger");
      }
    } catch (e) {
      setStatus(scanStatusEl, `Scan error: ${e}`, "danger");
    } finally {
      btn.disabled = false;
      btn.textContent = "Rescan";
    }
  }

  function changeSort(key) {
    const url = new URL(window.location.href);
    const currentKey = url.searchParams.get("sort") || currentSort || "priority";
    const currentDir = url.searchParams.get("order") || currentOrder || "desc";
    let nextDir = "desc";
    if (currentKey === key) nextDir = currentDir === "desc" ? "asc" : "desc";
    url.searchParams.set("sort", key);
    url.searchParams.set("order", nextDir);
    window.location.href = url.toString();
  }

  function switchLibrary(lib) {
    const url = new URL(window.location.href);
    url.searchParams.set("library", lib);
    window.location.href = url.toString();
  }

  function toggleView() {
    const url = new URL(window.location.href);
    const current = url.searchParams.get("view");
    url.searchParams.set("view", current === "compact" ? "table" : "compact");
    window.location.href = url.toString();
  }

  function filterShows() {
    const term = document.getElementById("searchBox")?.value.trim().toLowerCase() || "";
    document.querySelectorAll(".show-row").forEach((row) => {
      const name = (row.dataset.name || "").toLowerCase();
      const match = !term || name.includes(term);
      row.classList.toggle("hidden", !match);
      const seasonsRow = row.nextElementSibling;
      if (seasonsRow && seasonsRow.classList.contains("seasons-container")) {
        seasonsRow.classList.toggle("hidden", !match);
      }
    });
    document.querySelectorAll(".show-card").forEach((card) => {
      const name = (card.dataset.name || "").toLowerCase();
      const match = !term || name.includes(term);
      card.classList.toggle("hidden", !match);
    });
  }

  function expandAll() {
    document.querySelectorAll(".show-row").forEach((row) => {
      const icon = row.querySelector(".expand-icon");
      const seasonsRow = row.nextElementSibling;
      const showName = row.dataset.show;
      if (seasonsRow?.classList.contains("hidden")) {
        seasonsRow.classList.remove("hidden");
        icon?.classList.add("expanded");
        void loadSeasons(showName, seasonsRow);
      }
    });
  }

  function collapseAll() {
    document.querySelectorAll(".seasons-container, .episodes-container, .episode-details-row").forEach((row) => {
      row.classList.add("hidden");
    });
    document.querySelectorAll(".expand-icon").forEach((icon) => icon.classList.remove("expanded"));
  }

  function toggleShow(showName, evt) {
    evt?.stopPropagation?.();
    const row = evt?.target?.closest?.("tr") || evt?.currentTarget?.closest?.("tr");
    if (!row) return;
    const icon = row.querySelector(".expand-icon");
    const seasonsRow = row.nextElementSibling;
    if (!seasonsRow) return;

    if (seasonsRow.classList.contains("hidden")) {
      seasonsRow.classList.remove("hidden");
      icon?.classList.add("expanded");
      void loadSeasons(showName, seasonsRow);
    } else {
      seasonsRow.classList.add("hidden");
      icon?.classList.remove("expanded");
    }
  }

  async function loadSeasons(showName, container) {
    if (!showName || loadedShows[showName]) return;
    try {
      const resp = await fetch(
        `/api/queue/seasons/${encodeURIComponent(showName)}?library=${encodeURIComponent(libraryRoot)}&sort=${encodeURIComponent(currentSort)}&order=${encodeURIComponent(currentOrder)}`,
      );
      const data = await resp.json();
      loadedShows[showName] = data.seasons;

      container.querySelector("td").innerHTML = renderSeasonsTable(showName, data.seasons || []);

      if (Array.isArray(data.seasons) && data.seasons.length === 1 && data.seasons[0].season_name === "Files") {
        const seasonName = data.seasons[0].season_name;
        const seasonRow = container.querySelector(".season-row");
        const episodesRow = container.querySelector(".episodes-container");
        if (seasonRow && episodesRow) {
          seasonRow.querySelector(".expand-icon")?.classList.add("expanded");
          episodesRow.classList.remove("hidden");
          loadEpisodes(showName, seasonName, episodesRow, true);
        }
      }
    } catch (_) {
      container.querySelector("td").innerHTML = '<div class="loading">Error loading seasons</div>';
    }
  }

  function renderSeasonsTable(showName, seasons) {
    if (!seasons.length) return '<div class="loading">No seasons found</div>';

    let html = `<div class="nested-table-wrapper"><table class="nested-table">
        <thead>
            <tr>
                <th class="w-[30px]"></th>
                <th class="sortable" data-sort-key="season" data-sort-type="str">Season</th>
                <th class="sortable" data-sort-key="files" data-sort-type="num">Files</th>
                <th class="sortable" data-sort-key="size" data-sort-type="num">Total Size</th>
                <th class="sortable" data-sort-key="savings" data-sort-type="num">Est. Savings</th>
                <th class="sortable" data-sort-key="priority" data-sort-type="num">Max Priority</th>
            </tr>
        </thead>
        <tbody>`;

    seasons.forEach((season, idx) => {
      html += `
            <tr class="season-row" data-show="${escapeHtml(showName)}" data-season="${escapeHtml(season.season_name)}" data-files="${season.file_count}" data-size="${season.total_savings_bytes || 0}" data-savings="${season.total_savings_bytes || 0}" data-priority="${season.max_priority}" data-name="${escapeHtml(season.season_name)}">
                <td>
                    <span class="expand-icon" onclick="toggleSeason('${escapeHtml(showName)}', '${escapeHtml(season.season_name)}', event)">&#9654;</span>
                </td>
                <td class="indent-1"><strong>${escapeHtml(season.season_name)}</strong></td>
                <td>${season.file_count}</td>
                <td>${season.total_size}</td>
                <td class="text-success">${season.total_savings}</td>
                <td>${season.max_priority.toFixed(3)}</td>
            </tr>
            <tr class="episodes-container hidden" id="episodes-${idx}" data-show="${escapeHtml(showName)}" data-season="${escapeHtml(season.season_name)}">
                <td colspan="6" class="nested-container">
                    <div class="loading">Loading episodes...</div>
                </td>
            </tr>`;
    });

    html += "</tbody></table></div>";
    setTimeout(() => {
      const wrapper = document.querySelector(".seasons-container td");
      attachSortHandlers(wrapper || document);
    }, 0);
    return html;
  }

  function toggleSeason(showName, seasonName, evt) {
    evt?.stopPropagation?.();
    const row = evt?.target?.closest?.("tr");
    if (!row) return;
    const icon = row.querySelector(".expand-icon");
    const episodesRow = row.nextElementSibling;
    if (!episodesRow) return;

    if (episodesRow.classList.contains("hidden")) {
      episodesRow.classList.remove("hidden");
      icon?.classList.add("expanded");
      void loadEpisodes(showName, seasonName, episodesRow);
    } else {
      episodesRow.classList.add("hidden");
      icon?.classList.remove("expanded");
    }
  }

  async function loadEpisodes(showName, seasonName, container, autoExpand = false) {
    const key = `${showName}|${seasonName}`;
    if (loadedSeasons[key]) return;
    try {
      const resp = await fetch(
        `/api/queue/episodes/${encodeURIComponent(showName)}/${encodeURIComponent(seasonName)}?library=${encodeURIComponent(libraryRoot)}&sort=${encodeURIComponent(currentSort)}&order=${encodeURIComponent(currentOrder)}`,
      );
      const data = await resp.json();
      loadedSeasons[key] = data.episodes;
      container.querySelector("td").innerHTML = renderEpisodesTable(data.episodes || []);
      if (autoExpand) {
        container.querySelectorAll(".episode-row").forEach((row) => {
          const id = row.dataset.id;
          toggleEpisodeDetails(id, { stopPropagation: () => {}, target: row });
        });
      }
    } catch (_) {
      container.querySelector("td").innerHTML = '<div class="loading">Error loading episodes</div>';
    }
  }

  function renderEpisodesTable(episodes) {
    if (!episodes.length) return '<div class="loading">No episodes found</div>';

    let html = `<div class="nested-table-wrapper"><table class="nested-table">
        <thead>
            <tr>
                <th class="w-[30px]"></th>
                <th class="sortable" data-sort-key="file" data-sort-type="str">File</th>
                <th class="sortable" data-sort-key="size" data-sort-type="num">Size</th>
                <th class="sortable" data-sort-key="savings" data-sort-type="num">Est. Savings</th>
                <th class="sortable" data-sort-key="codec" data-sort-type="str">Codec</th>
                <th class="sortable" data-sort-key="res" data-sort-type="num">Resolution</th>
                <th class="sortable" data-sort-key="tier" data-sort-type="str">Tier</th>
                <th class="sortable" data-sort-key="priority" data-sort-type="num">Priority</th>
                <th class="sortable" data-sort-key="bitrate" data-sort-type="num">Bitrate</th>
                <th class="sortable" data-sort-key="duration" data-sort-type="num">Duration</th>
                <th>Actions</th>
            </tr>
        </thead>
        <tbody>`;

    episodes.forEach((ep) => {
      html += `
            <tr class="episode-row" data-id="${ep.id}" data-size="${ep.size_bytes || 0}" data-savings="${ep.savings_bytes || 0}" data-priority="${ep.priority_score}" data-bitrate="${ep.bitrate_kbps || 0}" data-duration="${ep.duration_sec || 0}" data-file="${escapeHtml(ep.filename)}" data-tier="${ep.detected_tier}" data-codec="${escapeHtml(ep.video_codec || "")}" data-res="${ep.height || 0}">
                <td>
                    <span class="expand-icon" onclick="toggleEpisodeDetails(${ep.id}, event)">&#9654;</span>
                </td>
                <td class="truncate indent-2" title="${escapeHtml(ep.path)}">${escapeHtml(ep.filename)}</td>
                <td>${ep.size}</td>
                <td class="text-success">${ep.savings}</td>
                <td>${escapeHtml(ep.video_codec)}${ep.video_profile ? " (" + escapeHtml(ep.video_profile) + ")" : ""}</td>
                <td>${ep.resolution}</td>
                <td class="tier-${ep.detected_tier}">${ep.detected_tier}</td>
                <td>${ep.priority_score.toFixed(3)}</td>
                <td>${ep.bitrate}</td>
                <td>${ep.duration}</td>
                <td>
                    <button class="btn btn-sm btn-primary" onclick="bumpEpisode(${ep.id})">Bump</button>
                    <button class="btn btn-sm" onclick="pauseEpisode(${ep.id})">Pause</button>
                    <button class="btn btn-sm" onclick="resumeEpisode(${ep.id})">Resume</button>
                    <select class="form-select" aria-label="Send episode to worker" onchange="sendToWorker(${ep.id}, this.value)">
                        <option value="">Send to…</option>
                        ${workers.map((w) => `<option value="${w.machine}">${w.machine}</option>`).join("")}
                    </select>
                </td>
            </tr>
            <tr class="episode-details-row hidden" id="ep-details-${ep.id}">
                <td colspan="8" class="nested-container">
                    ${renderEpisodeDetails(ep)}
                </td>
            </tr>`;
    });

    html += "</tbody></table></div>";
    setTimeout(() => {
      const wrapper = document.querySelector(".episodes-container td");
      attachSortHandlers(wrapper || document);
    }, 0);
    return html;
  }

  function renderEpisodeDetails(ep) {
    let html = `<div class="episode-details">
        <div class="detail-grid">
            <div class="detail-section">
                <h4>Video</h4>
                <div class="detail-item">
                    <span class="detail-label">Codec:</span>
                    <span>${escapeHtml(ep.video_codec)}${ep.video_profile ? " (" + escapeHtml(ep.video_profile) + ")" : ""}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Resolution:</span>
                    <span>${escapeHtml(ep.resolution)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Bit Depth:</span>
                    <span>${ep.bit_depth || "?"}-bit</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Frame Rate:</span>
                    <span>${escapeHtml(ep.frame_rate)}</span>
            </div>`;

    if (ep.is_hdr) {
      html += `<div class="detail-item">
            <span class="detail-label">HDR:</span>
            <span class="text-warning">${ep.hdr_format || "Yes"}</span>
        </div>`;
    }
    if (ep.is_interlaced) {
      html += `<div class="detail-item">
            <span class="detail-label">Interlaced:</span>
            <span class="text-warning">Yes</span>
        </div>`;
    }

    html += `<div class="detail-item mt-2">
        <button class="btn btn-sm btn-primary" onclick="bumpEpisode(${ep.id})">Bump to Front</button>
    </div>`;

    html += `</div>
            <div class="detail-section">
                <h4>Audio</h4>
                <div class="detail-item">
                    <span class="detail-label">Tracks:</span>
                    <span>${escapeHtml(ep.audio_info)}</span>
                </div>
            </div>
            <div class="detail-section">
                <h4>Subtitles</h4>
                <div class="detail-item">
                    <span class="detail-label">Count:</span>
                    <span>${ep.subtitle_count} track${ep.subtitle_count !== 1 ? "s" : ""}</span>
                </div>
            </div>`;

    if (ep.tier_reasoning) {
      html += `<div class="detail-section detail-full-width">
            <h4>Tier Classification</h4>
            <div class="detail-item">
                <span class="tier-reasoning">${escapeHtml(ep.tier_reasoning)}</span>
            </div>
        </div>`;
    }

    html += `</div>
        <div class="detail-path text-muted">
            <span class="detail-label">Full Path:</span>
            <code>${escapeHtml(ep.path)}</code>
        </div>
    </div>`;
    return html;
  }

  function toggleEpisodeDetails(id, evt) {
    evt?.stopPropagation?.();
    const row = evt?.target?.closest?.("tr");
    const icon = row?.querySelector?.(".expand-icon");
    const detailsRow = document.getElementById(`ep-details-${id}`);
    if (!detailsRow) return;
    if (detailsRow.classList.contains("hidden")) {
      detailsRow.classList.remove("hidden");
      icon?.classList.add("expanded");
    } else {
      detailsRow.classList.add("hidden");
      icon?.classList.remove("expanded");
    }
  }

  async function bumpEpisode(id) {
    const resp = await fetch(`/api/queue/${id}/bump`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delta: 1 }),
    }).catch((e) => {
      alert("Bump failed: " + (e?.message || "Unknown error"));
      return null;
    });
    if (!resp) return;

    const data = await resp.json().catch((e) => {
      alert("Bump failed: " + (e?.message || "Invalid JSON"));
      return null;
    });
    if (!data?.success) {
      alert("Bump failed: " + (data?.error || "Unknown error"));
      return;
    }

    showToast(`Bumped item ${id}`, true);
  }

  async function pauseEpisode(id) {
    const resp = await fetch(`/api/queue/${id}/pause`, { method: "POST" }).catch((e) => {
      alert("Pause failed: " + (e?.message || "Unknown error"));
      return null;
    });
    if (!resp) return;

    const data = await resp.json().catch((e) => {
      alert("Pause failed: " + (e?.message || "Invalid JSON"));
      return null;
    });
    if (!data?.success) {
      alert("Pause failed: " + (data?.error || "Unknown error"));
      return;
    }

    showToast(`Paused ${id}`, true);
  }

  async function resumeEpisode(id) {
    const resp = await fetch(`/api/queue/${id}/resume`, { method: "POST" }).catch((e) => {
      alert("Resume failed: " + (e?.message || "Unknown error"));
      return null;
    });
    if (!resp) return;

    const data = await resp.json().catch((e) => {
      alert("Resume failed: " + (e?.message || "Invalid JSON"));
      return null;
    });
    if (!data?.success) {
      alert("Resume failed: " + (data?.error || "Unknown error"));
      return;
    }

    showToast(`Resumed ${id}`, true);
  }

  async function sendToWorker(id, worker) {
    if (!worker) return;

    const resp = await fetch("/api/send-to-worker", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, worker }),
    }).catch((e) => {
      alert("Send failed: " + (e?.message || "Unknown error"));
      return null;
    });
    if (!resp) return;

    const data = await resp.json().catch((e) => {
      alert("Send failed: " + (e?.message || "Invalid JSON"));
      return null;
    });
    if (!data?.success) {
      alert("Send failed: " + (data?.error || "Unknown error"));
      return;
    }

    showToast(`Queued ${id} for ${worker}`, true);
  }

  async function loadSkipped() {
    const container = document.getElementById("skippedContainer");
    if (!container) return;
    container.textContent = "Loading skipped items...";

    const resp = await fetch("/api/queue/skipped").catch((e) => {
      container.textContent = "Failed to load skipped items: " + (e?.message || "Unknown error");
      return null;
    });
    if (!resp) return;

    const data = await resp.json().catch((e) => {
      container.textContent = "Failed to load skipped items: " + (e?.message || "Invalid JSON");
      return null;
    });
    if (!data?.success) {
      container.textContent = data?.error || "Unknown error";
      return;
    }
    if (!data.items.length) {
      container.textContent = "No skipped items";
      return;
    }

    const rows = data.items
      .map(
        (it) => `
          <tr>
            <td>${it.id}</td>
            <td class="truncate" title="${escapeHtml(it.path)}">${escapeHtml(it.path)}</td>
            <td>${escapeHtml(it.skip_reason || "-")}</td>
            <td>${it.updated_at || "-"}</td>
            <td>
              <button class="btn btn-sm" onclick="forceRescan(${it.id})">Force Rescan</button>
              <button class="btn btn-sm" onclick="resetSkip(${it.id})">Reset</button>
            </td>
          </tr>
        `,
      )
      .join("");
    container.innerHTML = `<div class="nested-table-wrapper"><table class="nested-table"><thead><tr><th>ID</th><th>Path</th><th>Reason</th><th>Updated</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  async function forceRescan(id) {
    const resp = await fetch(`/api/queue/${id}/force-rescan`, { method: "POST" });
    const data = await resp.json();
    showToast(data.success ? "Re-queued" : "Error: " + (data.error || "unknown"), !!data.success);
    loadSkipped();
  }

  async function resetSkip(id) {
    const resp = await fetch(`/api/queue/${id}/reset-skip`, { method: "POST" });
    const data = await resp.json();
    showToast(data.success ? "Skip reset" : "Error: " + (data.error || "unknown"), !!data.success);
    loadSkipped();
  }

  function openSendModal(worker) {
    const targetId = prompt(`Send which item id to ${worker}?`);
    if (!targetId) return;
    void sendToWorker(parseInt(targetId, 10), worker);
  }

  function sortTable(tableEl, key, type, direction) {
    const tbody = tableEl.querySelector("tbody");
    if (!tbody) return;
    const rows = Array.from(tbody.querySelectorAll("tr")).filter((r) => !r.classList.contains("episode-details-row"));
    const dir = direction === "desc" ? -1 : 1;
    rows.sort((a, b) => {
      const va = a.dataset[key] ?? "";
      const vb = b.dataset[key] ?? "";
      if (type === "num") {
        const na = parseFloat(va) || 0;
        const nb = parseFloat(vb) || 0;
        return (na - nb) * dir;
      }
      return String(va).localeCompare(String(vb)) * dir;
    });
    rows.forEach((r) => tbody.appendChild(r));
  }

  function attachSortHandlers(container) {
    container.querySelectorAll(".sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sortKey;
        const type = th.dataset.sortType || "str";
        const table = th.closest("table");
        if (!table) return;
        const current = th.classList.contains("sorted-asc") ? "asc" : th.classList.contains("sorted-desc") ? "desc" : null;
        table.querySelectorAll(".sortable").forEach((el) => el.classList.remove("sorted-asc", "sorted-desc"));
        const nextDir = current === "asc" ? "desc" : "asc";
        th.classList.add(nextDir === "asc" ? "sorted-asc" : "sorted-desc");
        sortTable(table, key, type, nextDir);
      });
    });
  }

  function openCard(showName) {
    const tableView = document.getElementById("tableView");
    if (tableView?.classList.contains("hidden")) {
      const url = new URL(window.location.href);
      url.searchParams.set("view", "table");
      url.searchParams.set("open_show", showName);
      window.location.href = url.toString();
      return;
    }

    const row = document.querySelector(`tr.show-row[data-show="${CSS.escape(showName)}"]`);
    if (!row) return;
    const icon = row.querySelector(".expand-icon");
    if (icon && !icon.classList.contains("expanded")) icon.click();
    window.scrollTo({ top: row.offsetTop - 80, behavior: "smooth" });
  }

  document.querySelectorAll(".show-row").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.classList.contains("expand-icon")) return;
      toggleShow(row.dataset.show, { stopPropagation: () => {}, target: row });
    });
  });

  document.addEventListener("click", (e) => {
    const row = e.target.closest(".season-row");
    if (!row) return;
    if (e.target.classList.contains("expand-icon")) return;
    toggleSeason(row.dataset.show, row.dataset.season, { stopPropagation: () => {}, target: row });
  });

  document.addEventListener("click", (e) => {
    const row = e.target.closest(".episode-row");
    if (!row) return;
    if (e.target.classList.contains("expand-icon")) return;
    toggleEpisodeDetails(row.dataset.id, { stopPropagation: () => {}, target: row });
  });

  attachSortHandlers(document);

  const openShow = new URL(window.location.href).searchParams.get("open_show") || "";
  if (openShow) {
    setTimeout(() => {
      openCard(openShow);
      try {
        const url = new URL(window.location.href);
        url.searchParams.delete("open_show");
        window.history.replaceState({}, "", url.toString());
      } catch (_) {
        /* ignore */
      }
    }, 0);
  }

  window.applyFilters = applyFilters;
  window.resetFilters = resetFilters;
  window.triggerScan = triggerScan;
  window.changeSort = changeSort;
  window.switchLibrary = switchLibrary;
  window.toggleView = toggleView;
  window.filterShows = filterShows;
  window.expandAll = expandAll;
  window.collapseAll = collapseAll;
  window.toggleShow = toggleShow;
  window.toggleSeason = toggleSeason;
  window.toggleEpisodeDetails = toggleEpisodeDetails;
  window.bumpEpisode = bumpEpisode;
  window.pauseEpisode = pauseEpisode;
  window.resumeEpisode = resumeEpisode;
  window.sendToWorker = sendToWorker;
  window.loadSkipped = loadSkipped;
  window.forceRescan = forceRescan;
  window.resetSkip = resetSkip;
  window.openSendModal = openSendModal;
  window.openCard = openCard;
})();
