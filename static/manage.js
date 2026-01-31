(function() {
  "use strict";

  function el(id) { return document.getElementById(id); }

  function toast(msg, type) {
    const t = document.createElement("div");
    t.className = `toast toast-${type || "success"}`;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }

  async function api(url, opts) {
    try {
      const resp = await fetch(url, opts || {});
      if (resp.status === 401) { location.reload(); return null; }
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        toast(err.detail || "Fehler", "error");
        return null;
      }
      return await resp.json();
    } catch (e) {
      toast("Netzwerkfehler: " + e.message, "error");
      return null;
    }
  }

  function normalizeCommand(command) {
    return command.trim().toLowerCase();
  }

  function isCommandAllowed(command) {
    const normalized = normalizeCommand(command);
    if (!normalized) return false;
    const [head] = normalized.split(/\s+/);
    const blocked = new Set(["op", "deop", "stop", "restart", "update", "whitelist", "ban", "unban"]);
    return !blocked.has(head);
  }

  // Shared state for performance widget
  let cachedOnlineCount = 0;

  // --- Players ---
  async function refreshPlayers() {
    const data = await api("/api/players");
    if (!data) return;
    const tbody = el("playerTable");
    const players = data.players || [];
    const ops = new Set((data.ops || []).map(p => String(p).toLowerCase()));
    const allowControl = document.body.dataset.allowControl === "true";

    // Update cached online count for performance widget
    cachedOnlineCount = players.filter(p => p.online).length;
    const playerCountEl = el("queryPlayers");
    if (playerCountEl) playerCountEl.textContent = cachedOnlineCount;

    if (players.length === 0) {
      const colspan = allowControl ? 6 : 5;
      tbody.innerHTML = `<tr><td colspan="${colspan}" class="muted">Keine Spieler gefunden</td></tr>`;
      return;
    }
    tbody.innerHTML = players.map(p => {
      const badge = p.online
        ? '<span class="badge badge-active">Online</span>'
        : '<span class="badge badge-inactive">Offline</span>';
      const login = p.last_login ? p.last_login.replace("T", " ").substring(0, 19) : "-";
      const isOp = ops.has(String(p.name).toLowerCase());
      const role = isOp ? '<span class="badge badge-update">OP</span>' : '<span class="badge badge-muted">Spieler</span>';
      const action = allowControl
        ? `<button class="btn-toggle-op" data-name="${p.name}" data-op="${isOp}">${isOp ? "OP entfernen" : "OP geben"}</button>`
        : "";
      return `<tr>
        <td><strong>${p.name}</strong></td>
        <td>${badge}</td>
        <td>${login}</td>
        <td>${p.world || "-"}</td>
        <td>${role}</td>
        ${allowControl ? `<td>${action}</td>` : ""}
      </tr>`;
    }).join("");
    if (allowControl) {
      tbody.querySelectorAll(".btn-toggle-op").forEach(btn => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          const name = btn.dataset.name;
          const enable = btn.dataset.op !== "true";
          const result = await api("/api/players/op", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, enable }),
          });
          if (result && result.ok) {
            toast(enable ? `OP vergeben: ${name}` : `OP entfernt: ${name}`);
          }
          await refreshPlayers();
        });
      });
    }
  }

  // --- Console ---
  let consoleLastLine = "";

  async function refreshConsole() {
    const data = await api("/api/console/output");
    if (!data) return;
    const output = el("consoleOutput");
    if (output) {
      output.textContent = (data.lines || []).join("\n");
      output.scrollTop = output.scrollHeight;
    }
  }

  function setupConsole() {
    const input = el("consoleInput");
    const sendBtn = el("consoleSend");
    if (!input || !sendBtn) return;

    async function sendCommand() {
      const cmd = input.value.trim();
      if (!cmd) return;
      if (!isCommandAllowed(cmd)) {
        toast("Befehl in der Konsole gesperrt. Nutze die User-Verwaltung oder Dashboard-Funktionen.", "error");
        return;
      }
      input.value = "";
      sendBtn.disabled = true;
      const result = await api("/api/console/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: cmd }),
      });
      if (result && result.ok) {
        toast("Befehl gesendet: " + cmd);
      }
      sendBtn.disabled = false;
      setTimeout(refreshConsole, 1000);
    }

    sendBtn.addEventListener("click", sendCommand);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendCommand();
    });
  }

  // --- Config Editor ---
  let activeConfigTab = "serverConfig";

  async function loadConfigs() {
    const serverData = await api("/api/config/server");
    if (serverData) {
      el("serverConfigEditor").value = JSON.stringify(JSON.parse(serverData.content), null, 2);
    }
    const worldData = await api("/api/config/world");
    if (worldData) {
      el("worldConfigEditor").value = JSON.stringify(JSON.parse(worldData.content), null, 2);
    }
  }

  function setupConfig() {
    // Tab switching
    document.querySelectorAll(".tab-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
        btn.classList.add("active");
        activeConfigTab = btn.dataset.tab;
        el(activeConfigTab + "Tab").classList.add("active");
      });
    });

    // Save
    const saveBtn = el("saveConfig");
    if (saveBtn) {
      saveBtn.addEventListener("click", async () => {
        const isServer = activeConfigTab === "serverConfig";
        const editor = el(isServer ? "serverConfigEditor" : "worldConfigEditor");
        const endpoint = isServer ? "/api/config/server" : "/api/config/world";

        if (!confirm("Konfiguration speichern?" + (isServer ? " Server-Neustart erforderlich." : ""))) return;

        saveBtn.disabled = true;
        const result = await api(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: editor.value }),
        });
        if (result && result.ok) {
          toast("Konfiguration gespeichert");
          el("configHint").hidden = !isServer;
        }
        saveBtn.disabled = false;
      });
    }
  }

  // --- Backup Management ---
  async function refreshBackups() {
    const data = await api("/api/backups/list");
    if (!data) return;
    const tbody = el("backupMgmtTable");
    const backups = data.backups || [];
    if (backups.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">Keine Backups</td></tr>';
      return;
    }
    tbody.innerHTML = backups.map(b => {
      const typeBadge = b.type === "backup"
        ? '<span class="badge badge-active">Backup</span>'
        : '<span class="badge badge-inactive">Update</span>';
      const actions = `<button class="btn-del" data-name="${b.name}" data-type="${b.type}">Loeschen</button>`;
      return `<tr>
        <td><code>${b.name}</code></td>
        <td>${b.size}</td>
        <td>${b.mtime}</td>
        <td>${typeBadge}</td>
        <td>${actions}</td>
      </tr>`;
    }).join("");

    // Attach delete handlers
    tbody.querySelectorAll(".btn-del").forEach(btn => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.name;
        if (!confirm(`Backup '${name}' wirklich loeschen?`)) return;
        btn.disabled = true;
        const result = await api(`/api/backups/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (result && result.ok) {
          toast("Backup geloescht");
          await refreshBackups();
        } else {
          btn.disabled = false;
        }
      });
    });
  }

  // --- Plugin Store ---
  async function refreshPlugins() {
    const data = await api("/api/plugins");
    if (!data) return;
    const tbody = el("pluginTable");
    const plugins = data.plugins || [];
    if (plugins.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">Keine Plugins verfuegbar</td></tr>';
      return;
    }
    tbody.innerHTML = plugins.map(p => {
      let statusBadge, actionBtn;
      if (p.installed && p.enabled) {
        statusBadge = '<span class="badge badge-active">Installiert</span>';
        actionBtn = '-';
      } else if (p.installed && !p.enabled) {
        statusBadge = '<span class="badge badge-inactive">Deaktiviert</span>';
        actionBtn = '-';
      } else {
        statusBadge = '<span class="badge badge-muted">Nicht installiert</span>';
        actionBtn = `<button class="btn-install" data-id="${p.id}">Installieren</button>`;
      }
      const deps = p.depends ? `<br><small class="muted">Abh.: ${p.depends.join(", ")}</small>` : "";
      return `<tr>
        <td><strong>${p.name}</strong></td>
        <td>${p.description}${deps}</td>
        <td>${p.version}</td>
        <td>${statusBadge}</td>
        <td>${actionBtn}</td>
      </tr>`;
    }).join("");

    // Install handlers
    tbody.querySelectorAll(".btn-install").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        btn.disabled = true;
        btn.textContent = "Installiere...";
        const result = await api(`/api/plugins/${encodeURIComponent(id)}/install`, { method: "POST" });
        if (result && result.ok) {
          toast(`Plugin '${result.plugin}' installiert. Server-Neustart erforderlich.`);
          await refreshPlugins();
          await refreshMods();
          await refreshQuery();
        } else {
          btn.disabled = false;
          btn.textContent = "Installieren";
        }
      });
    });
  }

  // --- Server Performance ---
  async function refreshQuery() {
    const statusDiv = el("queryStatus");
    const dataDiv = el("queryData");
    if (!statusDiv || !dataDiv) return;

    // Use lightweight performance endpoint (cached server-side)
    const perf = await api("/api/performance");
    if (!perf) return;

    const tps = perf.tps;
    const viewRadius = perf.view_radius;
    const cpuPercent = perf.cpu_percent;
    const ramMb = perf.ram_mb;

    statusDiv.hidden = true;
    dataDiv.hidden = false;

    // Display TPS with color coding
    const tpsEl = el("queryTps");
    if (tps !== null && tps !== undefined) {
      tpsEl.textContent = tps;
      if (tps >= 18) {
        tpsEl.style.color = "#2ecc71"; // green
      } else if (tps >= 10) {
        tpsEl.style.color = "#f39c12"; // orange
      } else {
        tpsEl.style.color = "#e74c3c"; // red
      }
    } else {
      tpsEl.textContent = "-";
      tpsEl.style.color = "";
    }

    // Display CPU with color coding
    const cpuEl = el("queryCpu");
    if (cpuEl) {
      if (cpuPercent !== null && cpuPercent !== undefined) {
        cpuEl.textContent = cpuPercent.toFixed(1) + "%";
        if (cpuPercent < 50) {
          cpuEl.style.color = "#2ecc71"; // green
        } else if (cpuPercent < 80) {
          cpuEl.style.color = "#f39c12"; // orange
        } else {
          cpuEl.style.color = "#e74c3c"; // red
        }
      } else {
        cpuEl.textContent = "-";
        cpuEl.style.color = "";
      }
    }

    // Display RAM
    const ramEl = el("queryRam");
    if (ramEl) {
      if (ramMb !== null && ramMb !== undefined) {
        if (ramMb >= 1024) {
          ramEl.textContent = (ramMb / 1024).toFixed(1) + " GB";
        } else {
          ramEl.textContent = Math.round(ramMb) + " MB";
        }
      } else {
        ramEl.textContent = "-";
      }
    }

    const vrEl = el("queryViewRadius");
    if (vrEl) {
      vrEl.textContent = viewRadius !== null && viewRadius !== undefined ? viewRadius : "-";
    }

    // Use cached player count (updated by refreshPlayers)
    el("queryPlayers").textContent = cachedOnlineCount;
  }

  // --- Mod Management ---
  async function refreshMods() {
    const data = await api("/api/mods");
    if (!data) return;
    const tbody = el("modTable");
    const mods = data.mods || [];
    if (mods.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="muted">Keine Mods installiert</td></tr>';
      return;
    }
    tbody.innerHTML = mods.map(m => {
      const badge = m.enabled
        ? '<span class="badge badge-active">Aktiv</span>'
        : '<span class="badge badge-inactive">Inaktiv</span>';
      const toggleLabel = m.enabled ? "Deaktivieren" : "Aktivieren";
      const actions = `<button class="btn-toggle" data-name="${m.name}">${toggleLabel}</button>
        <button class="btn-del" data-name="${m.name}">Loeschen</button>`;
      return `<tr>
        <td><strong>${m.name}</strong></td>
        <td>${badge}</td>
        <td>${m.size}</td>
        <td>${actions}</td>
      </tr>`;
    }).join("");

    // Toggle handlers
    tbody.querySelectorAll(".btn-toggle").forEach(btn => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        const result = await api(`/api/mods/${encodeURIComponent(btn.dataset.name)}/toggle`, { method: "POST" });
        if (result && result.ok) {
          toast(result.enabled ? "Mod aktiviert" : "Mod deaktiviert");
          await refreshMods();
        } else {
          btn.disabled = false;
        }
      });
    });

    // Delete handlers
    tbody.querySelectorAll(".btn-del").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!confirm(`Mod '${btn.dataset.name}' wirklich loeschen?`)) return;
        btn.disabled = true;
        const result = await api(`/api/mods/${encodeURIComponent(btn.dataset.name)}`, { method: "DELETE" });
        if (result && result.ok) {
          toast("Mod geloescht");
          await refreshMods();
        } else {
          btn.disabled = false;
        }
      });
    });
  }

  function setupModUpload() {
    const input = el("modUpload");
    if (!input) return;
    input.addEventListener("change", async () => {
      const file = input.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      toast("Mod wird hochgeladen...");
      const result = await api("/api/mods/upload", { method: "POST", body: form });
      if (result && result.ok) {
        toast("Mod '" + result.mod_name + "' hochgeladen");
        await refreshMods();
      }
      input.value = "";
    });
  }

  // --- CurseForge Browser ---
  let cfPage = 0;
  let cfTotal = 0;
  let cfCurrentSearch = "";

  async function checkCurseForge() {
    const statusDiv = el("cfStatus");
    const contentDiv = el("cfContent");
    if (!statusDiv || !contentDiv) return;

    const data = await api("/api/curseforge/status");
    if (!data || !data.available) {
      statusDiv.textContent = data?.reason || "CurseForge nicht verfuegbar";
      statusDiv.hidden = false;
      contentDiv.hidden = true;
      return;
    }

    statusDiv.hidden = true;
    contentDiv.hidden = false;
    await searchCurseForge("");
  }

  async function searchCurseForge(query) {
    cfCurrentSearch = query;
    cfPage = 0;
    await loadCurseForgeMods();
  }

  async function loadCurseForgeMods() {
    const tbody = el("cfModTable");
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="5" class="muted">Suche...</td></tr>';

    const params = new URLSearchParams({ page: cfPage });
    if (cfCurrentSearch) params.set("q", cfCurrentSearch);

    const data = await api(`/api/curseforge/search?${params}`);
    if (!data) return;

    cfTotal = data.total || 0;
    const mods = data.mods || [];

    if (mods.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">Keine Mods gefunden</td></tr>';
      el("cfPagination").hidden = true;
      return;
    }

    tbody.innerHTML = mods.map(m => {
      const icon = m.icon ? `<img src="${m.icon}" class="cf-icon" alt="" />` : '<div class="cf-icon" style="background:var(--line);"></div>';
      const downloads = m.downloads > 1000 ? `${(m.downloads/1000).toFixed(1)}k` : m.downloads;
      return `<tr>
        <td>${icon}</td>
        <td><span class="cf-mod-name" data-id="${m.id}">${m.name}</span><br><small class="muted">${m.summary.substring(0, 80)}${m.summary.length > 80 ? '...' : ''}</small></td>
        <td>${m.author}</td>
        <td>${downloads}</td>
        <td><button class="btn-install cf-details-btn" data-id="${m.id}">Details</button></td>
      </tr>`;
    }).join("");

    // Pagination
    const pageInfo = el("cfPageInfo");
    const pagination = el("cfPagination");
    const totalPages = Math.ceil(cfTotal / 20);
    if (totalPages > 1) {
      pagination.hidden = false;
      pageInfo.textContent = `Seite ${cfPage + 1} von ${totalPages}`;
      el("cfPrevPage").disabled = cfPage === 0;
      el("cfNextPage").disabled = cfPage >= totalPages - 1;
    } else {
      pagination.hidden = true;
    }

    // Click handlers for mod names and details buttons
    tbody.querySelectorAll(".cf-mod-name, .cf-details-btn").forEach(elem => {
      elem.addEventListener("click", () => openModDetails(parseInt(elem.dataset.id)));
    });
  }

  async function openModDetails(modId) {
    const modal = el("cfModal");
    const title = el("cfModalTitle");
    const summary = el("cfModalSummary");
    const fileTable = el("cfFileTable");

    if (!modal) return;

    modal.classList.add("active");
    title.textContent = "Lade...";
    summary.textContent = "";
    fileTable.innerHTML = '<tr><td colspan="4" class="muted">Lade Versionen...</td></tr>';

    const data = await api(`/api/curseforge/mod/${modId}`);
    if (!data) {
      modal.classList.remove("active");
      return;
    }

    title.textContent = data.name;
    summary.textContent = data.summary;

    const files = data.files || [];
    if (files.length === 0) {
      fileTable.innerHTML = '<tr><td colspan="4" class="muted">Keine Dateien verfuegbar</td></tr>';
      return;
    }

    fileTable.innerHTML = files.slice(0, 15).map(f => {
      const date = f.date ? f.date.substring(0, 10) : "-";
      const size = f.size ? `${(f.size / 1024).toFixed(0)} KB` : "-";
      return `<tr>
        <td><strong>${f.version}</strong></td>
        <td><code>${f.name}</code><br><small class="muted">${size}</small></td>
        <td>${date}</td>
        <td><button class="btn-install cf-install-btn" data-mod="${modId}" data-file="${f.id}" data-name="${f.name}">Installieren</button></td>
      </tr>`;
    }).join("");

    // Install handlers
    fileTable.querySelectorAll(".cf-install-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const modId = btn.dataset.mod;
        const fileId = btn.dataset.file;
        const fileName = btn.dataset.name;

        if (!confirm(`'${fileName}' installieren?`)) return;

        btn.disabled = true;
        btn.textContent = "Installiere...";

        const result = await api(`/api/curseforge/install/${modId}/${fileId}`, { method: "POST" });
        if (result && result.ok) {
          toast(`Mod '${result.file}' installiert. Server-Neustart erforderlich.`);
          modal.classList.remove("active");
          await refreshMods();
        } else {
          btn.disabled = false;
          btn.textContent = "Installieren";
        }
      });
    });
  }

  function setupCurseForge() {
    const searchInput = el("cfSearch");
    const searchBtn = el("cfSearchBtn");
    const prevBtn = el("cfPrevPage");
    const nextBtn = el("cfNextPage");
    const closeBtn = el("cfModalClose");
    const modal = el("cfModal");

    if (searchBtn) {
      searchBtn.addEventListener("click", () => searchCurseForge(searchInput?.value || ""));
    }
    if (searchInput) {
      searchInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") searchCurseForge(searchInput.value);
      });
    }
    if (prevBtn) {
      prevBtn.addEventListener("click", () => { cfPage--; loadCurseForgeMods(); });
    }
    if (nextBtn) {
      nextBtn.addEventListener("click", () => { cfPage++; loadCurseForgeMods(); });
    }
    if (closeBtn) {
      closeBtn.addEventListener("click", () => { modal.classList.remove("active"); });
    }
    if (modal) {
      modal.addEventListener("click", (e) => {
        if (e.target === modal) modal.classList.remove("active");
      });
    }
  }

  // --- Settings ---
  async function loadSettings() {
    const data = await api("/api/settings");
    if (!data) return;
    const cfApiKey = el("cfApiKey");
    if (cfApiKey && data.cf_api_key_set) {
      cfApiKey.value = data.cf_api_key; // Will be "***" if set
    }
  }

  function setupSettings() {
    const saveBtn = el("cfApiKeySave");
    const testBtn = el("cfApiKeyTest");
    const input = el("cfApiKey");
    const status = el("cfApiKeyStatus");

    if (saveBtn) {
      saveBtn.addEventListener("click", async () => {
        const value = input.value.trim();
        if (!value || value === "***") {
          toast("Bitte API-Key eingeben", "error");
          return;
        }
        const result = await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cf_api_key: value }),
        });
        if (result && result.ok) {
          toast("API-Key gespeichert!", "success");
          input.value = "***";
          checkCurseForge(); // Refresh CurseForge status
        }
      });
    }

    if (testBtn) {
      testBtn.addEventListener("click", async () => {
        status.textContent = "Teste...";
        status.className = "muted";
        const result = await api("/api/settings/cf-status");
        if (result) {
          status.textContent = result.message;
          status.className = result.valid ? "status-online" : "status-error";
        } else {
          status.textContent = "Test fehlgeschlagen";
          status.className = "status-error";
        }
      });
    }
  }

  // --- Init ---
  async function init() {
    setupConsole();
    setupConfig();
    setupModUpload();
    setupCurseForge();
    setupSettings();

    await Promise.all([
      refreshPlayers(),
      refreshConsole(),
      loadConfigs(),
      refreshBackups(),
      refreshMods(),
      refreshPlugins(),
      refreshQuery(),
      checkCurseForge(),
      loadSettings(),
    ]);

    // Poll players, console, and query
    setInterval(async () => {
      await refreshPlayers();
      await refreshConsole();
      await refreshQuery();
    }, 10000);
  }

  init().catch(err => {
    console.error("Init error:", err);
  });
})();
