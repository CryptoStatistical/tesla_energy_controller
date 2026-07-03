(function () {
  "use strict";

  document.documentElement.classList.add("js");

  function storedTheme() {
    try {
      return window.localStorage.getItem("dashboard-theme");
    } catch (_error) {
      return null;
    }
  }

  function preferredTheme() {
    var stored = storedTheme();
    if (stored === "light" || stored === "dark") return stored;
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var button = document.getElementById("themeToggle");
    if (button) {
      button.textContent = theme === "dark" ? "Tema chiaro" : "Tema scuro";
      button.setAttribute(
        "aria-label",
        theme === "dark" ? "Passa al tema chiaro" : "Passa al tema scuro"
      );
    }
  }

  setTheme(preferredTheme());

  var themeToggle = document.getElementById("themeToggle");
  if (themeToggle) {
    themeToggle.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
      var next = current === "dark" ? "light" : "dark";
      try {
        window.localStorage.setItem("dashboard-theme", next);
      } catch (_error) {
        /* localStorage non disponibile: il tema resta valido per questa pagina. */
      }
      setTheme(next);
    });
  }

  function css(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function formatValue(value, unit, decimals) {
    if (value == null || value === "") return "-";
    var number = Number(value);
    if (!Number.isFinite(number)) return "-";
    return number.toFixed(decimals || 0) + (unit || "");
  }

  function formatDuration(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    if (hours > 0) return hours + "h " + String(minutes).padStart(2, "0") + "m";
    if (minutes > 0) return minutes + "m";
    return total + "s";
  }

  function parseDecimal(value) {
    return Number(String(value || "").replace(",", "."));
  }

  function gridWattsPerAmp() {
    var note = document.getElementById("extraGridKw");
    var voltage = note ? (Number(note.getAttribute("data-voltage")) || 230) : 230;
    var phaseField = document.getElementById("expected_phases");
    var phases = phaseField ? Number(phaseField.value) : (note ? Number(note.getAttribute("data-phases")) : 1);
    return voltage * Math.max(phases, 1);
  }

  function updateExtraGridKw() {
    var input = document.getElementById("extra_grid_power_a");
    var note = document.getElementById("extraGridKw");
    if (!input || !note) return;
    var amps = parseDecimal(input.value);
    var kw = amps * gridWattsPerAmp() / 1000;
    if (!Number.isFinite(kw)) {
      note.textContent = "≈ - kW";
      return;
    }
    note.textContent = "≈ " + kw.toLocaleString("it-IT", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    }) + " kW";
  }

  var tabButtons = Array.prototype.slice.call(document.querySelectorAll(".tab-btn"));
  var panels = Array.prototype.slice.call(document.querySelectorAll(".tab-panel"));
  var chartControllers = [];
  var lazyCharts = {};

  function activate(name) {
    tabButtons.forEach(function (button) {
      var on = button.getAttribute("data-tab") === name;
      button.classList.toggle("is-active", on);
      button.setAttribute("aria-selected", on ? "true" : "false");
    });
    panels.forEach(function (panel) {
      panel.classList.toggle("is-active", panel.id === "tab-" + name);
    });
    window.setTimeout(function () {
      if (lazyCharts[name]) lazyCharts[name].load();
      chartControllers.forEach(function (item) { item.resize(); });
    }, 0);
  }

  tabButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      activate(button.getAttribute("data-tab"));
    });
  });

  function setLiveBadge(state, label) {
    var badge = document.getElementById("liveBadge");
    if (!badge) return;
    badge.classList.remove("is-waiting", "is-ok", "is-error");
    badge.classList.add(state);
    badge.textContent = label;
  }

  function showFeedback(message, kind) {
    var box = document.getElementById("dashboardFeedback");
    if (!box) return;
    box.classList.remove("is-ok", "is-error");
    if (kind) box.classList.add(kind === "error" ? "is-error" : "is-ok");
    box.textContent = message;
    box.hidden = false;
    window.clearTimeout(showFeedback.timer);
    if (kind !== "error") {
      showFeedback.timer = window.setTimeout(function () {
        box.hidden = true;
      }, 4500);
    }
  }

  function setupVinCopy() {
    var button = document.querySelector(".vin-copy");
    if (!button) return;

    function legacyCopy(value) {
      var onCopy = function (event) {
        event.clipboardData.setData("text/plain", value);
        event.preventDefault();
      };
      document.addEventListener("copy", onCopy);
      var copied = document.execCommand("copy");
      document.removeEventListener("copy", onCopy);
      if (copied) return true;

      var field = document.createElement("textarea");
      field.value = value;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.appendChild(field);
      field.select();
      copied = document.execCommand("copy");
      field.remove();
      return copied;
    }

    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      var vin = button.getAttribute("data-vin") || "";
      var masked = button.getAttribute("data-masked") || "—";
      if (!vin) return;

      window.clearTimeout(setupVinCopy.maskTimer);
      button.textContent = vin;
      button.classList.add("is-revealed");
      var legacyCopied = legacyCopy(vin);
      var modernCopy = navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(vin).then(function () { return true; }).catch(function () {
          return legacyCopied;
        })
        : Promise.resolve(legacyCopied);
      modernCopy.then(function (copied) {
        if (!copied) throw new Error("clipboard unavailable");
        button.classList.remove("is-copied");
        void button.offsetWidth;
        button.classList.add("is-copied");
        showFeedback("VIN copiato negli appunti", "ok");
      }).catch(function () {
        showFeedback("VIN mostrato, ma copia negli appunti non riuscita", "error");
      });
      setupVinCopy.maskTimer = window.setTimeout(function () {
        button.textContent = masked;
        button.classList.remove("is-revealed", "is-copied");
      }, 3500);
    });
  }

  setupVinCopy();

  function boolLabel(value, trueText, falseText) {
    if (typeof value !== "boolean") return "-";
    return value ? trueText : falseText;
  }

  function bleControlInfo(data, wallMode) {
    var state = data.tesla_ble_control_state;
    if (!state) {
      if (wallMode) {
        state = "not-needed";
      } else if (typeof data.tesla_connected === "boolean") {
        state = data.tesla_connected ? "connected" : "unreachable";
      } else {
        state = "cached";
      }
    }
    if (state === "connected") {
      return {
        className: "is-ok",
        text: "Bluetooth pronto",
        detail: data.tesla_ble_control_message || "Bluetooth pronto per controllo"
      };
    }
    if (state === "unreachable") {
      return {
        className: "is-error",
        text: "Bluetooth offline",
        detail: data.tesla_ble_control_message || "Bluetooth non raggiungibile"
      };
    }
    if (state === "not-needed") {
      return {
        className: "is-waiting",
        text: "Bluetooth standby",
        detail: data.tesla_ble_control_message || "Bluetooth non interrogato"
      };
    }
    return {
      className: "is-waiting",
      text: "Bluetooth -",
      detail: data.tesla_ble_control_message || "Stato Bluetooth non disponibile"
    };
  }

  function updateChargeSource(data) {
    var source = data.tesla_data_source || "vehicle";
    var wallMode = source === "wall-connector";
    var wallUnavailable = data.tesla_power_source === "wall-connector-unavailable";
    var wallVehicle = Boolean(data.wall_connector_vehicle_connected);
    var wallContactor = Boolean(data.wall_connector_contactor_closed);

    var teslaConn = document.getElementById("teslaConn");
    if (teslaConn) {
      teslaConn.classList.remove("is-ok", "is-error", "is-waiting");
      if (wallMode) {
        teslaConn.classList.add(data.tesla_connected ? "is-ok" : "is-waiting");
        teslaConn.textContent = data.tesla_connected ? "Tesla collegata" : "Tesla scollegata";
        teslaConn.title = "Rilevata dal Wall Connector";
      } else if (typeof data.tesla_connected === "boolean") {
        teslaConn.classList.add(data.tesla_connected ? "is-ok" : "is-error");
        teslaConn.textContent = data.tesla_connected ? "Tesla connessa" : "Tesla offline";
        teslaConn.title = "Collegamento Tesla BLE";
      } else {
        teslaConn.classList.add("is-waiting");
        teslaConn.textContent = "Tesla -";
      }
    }

    var charge = document.getElementById("chargeSource");
    if (charge) {
      charge.classList.remove("is-ok", "is-error", "is-waiting");
      if (wallMode) {
        if (wallUnavailable) {
          charge.classList.add("is-error");
          charge.textContent = "Wall Connector offline";
        } else {
          charge.classList.add("is-ok");
          charge.textContent = wallContactor
            ? "Wall Connector attivo"
            : wallVehicle
              ? "Wall Connector collegato"
              : "Wall Connector standby";
        }
      } else {
        charge.classList.add(
          typeof data.tesla_connected !== "boolean"
            ? "is-waiting"
            : data.tesla_connected
              ? "is-ok"
              : "is-error"
        );
        charge.textContent = "Bluetooth Tesla";
      }
    }

    var bleInfo = bleControlInfo(data, wallMode);
    var ble = document.getElementById("bleControl");
    if (ble) {
      ble.classList.remove("is-ok", "is-error", "is-waiting");
      ble.classList.add(bleInfo.className);
      ble.textContent = bleInfo.text;
      ble.title = bleInfo.detail;
    }

    var fields = {
      source: wallMode ? "Wall Connector" : "Bluetooth Tesla",
      vehicle: boolLabel(data.wall_connector_vehicle_connected, "collegato", "scollegato"),
      contactor: boolLabel(data.wall_connector_contactor_closed, "chiuso", "aperto"),
      evse: data.wall_connector_evse_state == null ? "-" : String(data.wall_connector_evse_state),
      ble_status: bleInfo.detail
    };
    Object.keys(fields).forEach(function (key) {
      var node = document.querySelector('[data-charge-field="' + key + '"]');
      if (node) node.textContent = fields[key];
    });
  }

  function updateStatus(data) {
    if (!data) return;
    var dot = document.getElementById("statusDot");
    if (dot) {
      dot.className = "status-dot status-" + (data.state || "waiting");
    }
    var message = document.getElementById("statusMessage");
    if (message) message.textContent = data.message || "In attesa della prima verifica";
    var time = document.getElementById("statusTime");
    if (time) time.textContent = data.updated_at || "mai";
    var source = document.getElementById("solarSourceLabel");
    if (source && data.solar_source) source.textContent = data.solar_source;
    var sourceUptime = document.getElementById("solarSourceUptime");
    if (sourceUptime && data.solar_source_uptime_seconds != null) {
      sourceUptime.textContent = formatDuration(data.solar_source_uptime_seconds);
    }

    updateChargeSource(data);

    var flowEl = document.getElementById("flowMetric");
    if (flowEl) {
      var flowStrong = flowEl.querySelector("strong");
      if (flowStrong) {
        var imp = Number(data.import_power_w) || 0;
        var exp = Number(data.export_power_w) || 0;
        var net = Math.round(imp - exp);
        flowStrong.textContent = "";
        flowStrong.className = "flow-value";
        var num = document.createElement("span");
        if (net < 0) {
          num.className = "flow-export";
          num.textContent = Math.abs(net) + " W";
          flowStrong.appendChild(document.createTextNode("("));
          flowStrong.appendChild(num);
          flowStrong.appendChild(document.createTextNode(")"));
        } else {
          num.className = "flow-import";
          num.textContent = net + " W";
          flowStrong.appendChild(num);
        }
      }
    }

    var windowLabel = document.getElementById("windowLabel");
    if (windowLabel && data.window_label) windowLabel.textContent = data.window_label;
    var windowMode = document.getElementById("windowMode");
    if (windowMode && data.window_mode) {
      windowMode.textContent = data.window_mode === "sun" ? "alba / tramonto" : "orario fisso";
    }
    var windowState = document.getElementById("windowState");
    if (windowState && typeof data.window_active === "boolean") {
      windowState.classList.toggle("active", data.window_active);
      windowState.classList.toggle("inactive", !data.window_active);
      windowState.textContent = data.window_active ? "ATTIVA" : "FUORI ORARIO";
    }

    Array.prototype.slice.call(document.querySelectorAll("[data-metric]")).forEach(function (node) {
      var key = node.getAttribute("data-metric");
      var unit = node.getAttribute("data-unit") || "";
      var decimals = Number(node.getAttribute("data-decimals") || "0");
      var target = node.querySelector("strong");
      if (target) target.textContent = formatValue(data[key], unit, decimals);
      if (key === "target_a") {
        var overrideActive = Boolean(data.manual_override_active);
        var targetActive = !overrideActive && Number(data[key] || 0) > 0;
        var overrideLabel = node.querySelector(".target-override-label");
        node.classList.toggle("metric-override", overrideActive);
        node.classList.toggle("metric-target-active", targetActive);
        if (overrideLabel) overrideLabel.hidden = !overrideActive;
      }
    });
  }

  function updateControllerForm(current) {
    if (!current) return;
    var form = document.querySelector('[data-async-form="controller"]');
    if (!form) return;
    var input = form.querySelector('input[name="enabled"]');
    var button = form.querySelector("button");
    if (!input || !button) return;
    if (current.enabled) {
      input.value = "0";
      button.textContent = "Spegni";
      button.className = "danger-button";
    } else {
      input.value = "on";
      button.textContent = "Accendi";
      button.className = "primary";
    }
  }

  function updateSettingsForm(current) {
    var form = document.querySelector('[data-async-form="settings"]');
    if (!form || !current || form.getAttribute("data-dirty") === "1") return;
    Object.keys(current).forEach(function (key) {
      var field = form.elements[key];
      if (!field) return;
      if (field.type === "checkbox") {
        field.checked = Boolean(current[key]);
      } else {
        field.value = current[key] == null ? "" : current[key];
      }
    });
    var extra = form.elements.extra_grid_power_a;
    if (extra && current.extra_grid_power_w != null) {
      extra.value = (Number(current.extra_grid_power_w) / gridWattsPerAmp()).toFixed(1);
      updateExtraGridKw();
    }
    var anomalyKw = form.elements.anomaly_peak_threshold_kw;
    if (anomalyKw && current.anomaly_peak_threshold_w != null) {
      anomalyKw.value = (Number(current.anomaly_peak_threshold_w) / 1000).toFixed(1);
    }
    var enabled = form.elements.enabled;
    if (enabled) enabled.value = current.enabled ? "on" : "";
  }

  function updateBle(ble) {
    if (!ble) return;
    Array.prototype.slice.call(document.querySelectorAll("[data-ble-field]")).forEach(function (node) {
      var key = node.getAttribute("data-ble-field");
      if (key === "key_configured") {
        node.textContent = ble[key] ? "configurata" : "mancante";
      } else if (key === "recovery_enabled") {
        node.textContent = ble[key] ? "attiva" : "spenta";
      } else {
        node.textContent = ble[key] == null || ble[key] === "" ? "-" : ble[key];
      }
    });
  }

  function cell(text) {
    var td = document.createElement("td");
    td.textContent = text == null || text === "" ? "-" : text;
    return td;
  }

  function renderEvents(events) {
    var body = document.getElementById("eventsTableBody");
    if (!body || !Array.isArray(events)) return;
    body.textContent = "";
    if (!events.length) {
      var emptyRow = document.createElement("tr");
      var emptyCell = cell("Nessun evento registrato.");
      emptyCell.colSpan = 5;
      emptyRow.appendChild(emptyCell);
      body.appendChild(emptyRow);
      return;
    }
    events.forEach(function (event) {
      var row = document.createElement("tr");
      row.appendChild(cell(event.observed_at));
      row.appendChild(cell(event.level));
      row.appendChild(cell(event.kind));
      row.appendChild(cell(event.message));
      var detailsCell = document.createElement("td");
      if (event.details_json && event.details_json !== "{}") {
        var details = document.createElement("details");
        details.className = "event-details";
        var summary = document.createElement("summary");
        summary.textContent = "Apri";
        var pre = document.createElement("pre");
        pre.textContent = event.details_json;
        details.appendChild(summary);
        details.appendChild(pre);
        detailsCell.appendChild(details);
      } else {
        detailsCell.textContent = "-";
      }
      row.appendChild(detailsCell);
      body.appendChild(row);
    });
  }

  function renderErrorLog(events) {
    var body = document.getElementById("errorLogTableBody");
    if (!body || !Array.isArray(events)) return;
    body.textContent = "";
    if (!events.length) {
      var emptyRow = document.createElement("tr");
      var emptyCell = cell("Nessun errore registrato.");
      emptyCell.colSpan = 4;
      emptyCell.className = "muted";
      emptyRow.appendChild(emptyCell);
      body.appendChild(emptyRow);
      return;
    }
    events.forEach(function (event) {
      var row = document.createElement("tr");
      row.appendChild(cell(event.observed_at));
      row.appendChild(cell(event.kind));
      row.appendChild(cell(event.message));
      var detailsCell = document.createElement("td");
      if (event.details_json && event.details_json !== "{}") {
        var details = document.createElement("details");
        details.className = "event-details";
        var summary = document.createElement("summary");
        summary.textContent = "Apri";
        var pre = document.createElement("pre");
        pre.textContent = event.details_json;
        details.appendChild(summary);
        details.appendChild(pre);
        detailsCell.appendChild(details);
      } else {
        detailsCell.textContent = "-";
      }
      row.appendChild(detailsCell);
      body.appendChild(row);
    });
  }

  function updateAccount(account) {
    if (!account) return;
    var username = document.getElementById("accountUsername");
    var email = document.getElementById("accountEmail");
    var role = document.getElementById("accountRole");
    var roleValue = document.getElementById("accountRoleValue");
    var emailInput = document.getElementById("account_email_edit");
    if (username) username.textContent = account.username || "-";
    if (email) email.textContent = account.email || "-";
    if (role) role.textContent = account.role || "viewer";
    if (roleValue) roleValue.textContent = account.role || "viewer";
    if (emailInput && document.activeElement !== emailInput) emailInput.value = account.email || "";
  }

  function renderUsers(users) {
    var body = document.getElementById("usersTableBody");
    if (!body || !Array.isArray(users)) return;
    var root = document.getElementById("dashboardApp");
    var updateUrl = root ? root.getAttribute("data-user-update-url") : "/users/update";
    var deleteUrl = root ? root.getAttribute("data-user-delete-url") : "/users/delete";
    var csrfInput = document.querySelector('input[name="csrf"]');
    var csrf = csrfInput ? csrfInput.value : "";
    var currentUsernameNode = document.getElementById("accountUsername");
    var currentUsername = currentUsernameNode ? currentUsernameNode.textContent : "";
    body.textContent = "";
    if (!users.length) {
      var emptyRow = document.createElement("tr");
      var emptyCell = cell("Nessun utente configurato.");
      emptyCell.colSpan = 4;
      emptyCell.className = "muted";
      emptyRow.appendChild(emptyCell);
      body.appendChild(emptyRow);
      return;
    }
    users.forEach(function (user, index) {
      var formId = "user-update-dynamic-" + index;
      var row = document.createElement("tr");
      row.appendChild(cell(user.username));

      var emailCell = document.createElement("td");
      var updateForm = document.createElement("form");
      updateForm.method = "post";
      updateForm.action = updateUrl || "/users/update";
      updateForm.id = formId;
      updateForm.setAttribute("data-async-form", "update-user");
      updateForm.setAttribute("autocomplete", "off");
      var csrfUpdate = document.createElement("input");
      csrfUpdate.type = "hidden";
      csrfUpdate.name = "csrf";
      csrfUpdate.value = csrf;
      var usernameUpdate = document.createElement("input");
      usernameUpdate.type = "hidden";
      usernameUpdate.name = "username";
      usernameUpdate.value = user.username || "";
      var emailInput = document.createElement("input");
      emailInput.name = "email";
      emailInput.type = "email";
      emailInput.value = user.email || "";
      emailInput.placeholder = "utente@example.com";
      emailInput.setAttribute("autocomplete", "off");
      emailInput.setAttribute("aria-label", "Email " + (user.username || ""));
      updateForm.appendChild(csrfUpdate);
      updateForm.appendChild(usernameUpdate);
      updateForm.appendChild(emailInput);
      emailCell.appendChild(updateForm);
      row.appendChild(emailCell);

      var roleCell = document.createElement("td");
      var roleSelect = document.createElement("select");
      roleSelect.name = "role";
      roleSelect.setAttribute("form", formId);
      roleSelect.setAttribute("aria-label", "Ruolo " + (user.username || ""));
      ["viewer", "admin"].forEach(function (role) {
        var option = document.createElement("option");
        option.value = role;
        option.textContent = role;
        option.selected = user.role === role;
        roleSelect.appendChild(option);
      });
      roleCell.appendChild(roleSelect);
      row.appendChild(roleCell);

      var actionsCell = document.createElement("td");
      actionsCell.className = "user-actions";
      var saveButton = document.createElement("button");
      saveButton.className = "ghost compact-button";
      saveButton.type = "submit";
      saveButton.setAttribute("form", formId);
      saveButton.textContent = "Salva";
      actionsCell.appendChild(saveButton);

      var deleteForm = document.createElement("form");
      deleteForm.method = "post";
      deleteForm.action = deleteUrl || "/users/delete";
      deleteForm.className = "inline-form";
      deleteForm.setAttribute("data-async-form", "delete-user");
      var csrfDelete = document.createElement("input");
      csrfDelete.type = "hidden";
      csrfDelete.name = "csrf";
      csrfDelete.value = csrf;
      var usernameDelete = document.createElement("input");
      usernameDelete.type = "hidden";
      usernameDelete.name = "username";
      usernameDelete.value = user.username || "";
      var deleteButton = document.createElement("button");
      deleteButton.className = "danger-button compact-button";
      deleteButton.type = "submit";
      deleteButton.textContent = "Cancella";
      if (user.username === currentUsername) {
        deleteButton.disabled = true;
        deleteButton.title = "Non puoi cancellare il tuo utente";
      }
      deleteForm.appendChild(csrfDelete);
      deleteForm.appendChild(usernameDelete);
      deleteForm.appendChild(deleteButton);
      actionsCell.appendChild(deleteForm);
      row.appendChild(actionsCell);
      body.appendChild(row);
      bindAsyncForm(updateForm);
      bindAsyncForm(deleteForm);
    });
  }

  function showGeneratedPassword(payload) {
    if (!payload || !payload.generated_password) return;
    var box = document.getElementById("generatedPasswordBox");
    var username = document.getElementById("generatedUsername");
    var password = document.getElementById("generatedPasswordValue");
    if (!box || !username || !password) return;
    username.textContent = payload.generated_username || "-";
    password.textContent = payload.generated_password;
    box.hidden = false;
  }

  function applyPayload(payload) {
    if (!payload) return;
    if (payload.current && appRoot) {
      var renderedAlfaMode = appRoot.getAttribute("data-alfa-grid-reading") === "1";
      if (Boolean(payload.current.alfa_grid_reading_enabled) !== renderedAlfaMode) {
        window.location.reload();
        return;
      }
    }
    if (payload.status) updateStatus(payload.status);
    if (payload.current) {
      updateControllerForm(payload.current);
      updateSettingsForm(payload.current);
    }
    if (payload.tesla_ble) updateBle(payload.tesla_ble);
    if (payload.events) renderEvents(payload.events);
    if (payload.error_events) renderErrorLog(payload.error_events);
    if (payload.account) updateAccount(payload.account);
    if (payload.users) renderUsers(payload.users);
    showGeneratedPassword(payload);
  }

  function pollStatus() {
    var root = document.getElementById("dashboardApp");
    if (!root) return;
    var url = root.getAttribute("data-status-url");
    if (!url) return;
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (response.status === 401) { window.location.href = "/login"; return null; }
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (!data) return;
        updateStatus(data);
        setLiveBadge("is-ok", "live");
      })
      .catch(function () {
        setLiveBadge("is-error", "offline");
      });
  }

  var appRoot = document.getElementById("dashboardApp");
  // data-refresh-ms riflette il campionamento storico/decisionale (tipicamente 300s).
  // La UI interroga la cache ogni 30s: i valori mostrati sono gia' mediati lato
  // backend e non creano nuovi campioni SQLite.
  var backendMs = Math.max(3000, Number(appRoot && appRoot.getAttribute("data-refresh-ms")) || 5000);
  var refreshMs = Math.min(backendMs, 30000);
  var chartRefreshMs = Math.min(backendMs, 30000);
  window.setInterval(pollStatus, refreshMs);

  function pollRuntime() {
    if (!appRoot) return;
    var url = appRoot.getAttribute("data-runtime-url");
    if (!url) return;
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (response.status === 401) { window.location.href = "/login"; return null; }
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (payload) { applyPayload(payload); })
      .catch(function () { setLiveBadge("is-error", "offline"); });
  }

  function setFormBusy(form, busy) {
    Array.prototype.slice.call(form.querySelectorAll("button")).forEach(function (button) {
      button.disabled = busy;
    });
  }

  function postForm(form) {
    if (form.reportValidity && !form.reportValidity()) return;
    if (form.getAttribute("data-async-form") === "delete-user") {
      var username = form.elements.username ? form.elements.username.value : "utente";
      if (!window.confirm("Cancellare l'utente " + username + "?")) return;
    }
    setFormBusy(form, true);
    fetch(form.action, {
      method: form.method || "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "X-Requested-With": "fetch"
      },
      body: new FormData(form)
    })
      .then(function (response) {
        if (response.status === 401) { window.location.href = "/login"; return null; }
        return response.json().then(function (payload) {
          if (!response.ok) {
            var error = payload && payload.error ? payload.error : "Operazione non riuscita";
            throw new Error(error);
          }
          return payload;
        });
      })
      .then(function (payload) {
        if (!payload) return;
        form.setAttribute("data-dirty", "0");
        applyPayload(payload);
        if (form.getAttribute("data-async-form") === "password") {
          form.reset();
        } else if (form.getAttribute("data-async-form") === "create-user") {
          form.reset();
        }
        showFeedback(payload.message || "Operazione completata", "ok");
        chartControllers.forEach(function (item) { item.refresh(); });
      })
      .catch(function (error) {
        showFeedback(error.message || "Operazione non riuscita", "error");
        if (form.getAttribute("data-async-form") === "settings") activate("settings");
      })
      .finally(function () {
        setFormBusy(form, false);
      });
  }

  function bindAsyncForm(form) {
    if (!form || form.getAttribute("data-bound") === "1") return;
    form.setAttribute("data-bound", "1");
    if (form.getAttribute("data-async-form") === "settings") {
      form.addEventListener("input", function () {
        form.setAttribute("data-dirty", "1");
        updateExtraGridKw();
      });
      form.addEventListener("change", function () {
        form.setAttribute("data-dirty", "1");
        updateExtraGridKw();
      });
    }
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      postForm(form);
    });
  }

  Array.prototype.slice.call(document.querySelectorAll("[data-async-form]")).forEach(bindAsyncForm);

  updateExtraGridKw();
  window.setInterval(pollRuntime, Math.max(refreshMs * 3, 15000));

  if (typeof Chart === "undefined") return;

  var PALETTE = ["#2563eb", "#0891b2", "#f59e0b", "#10b981", "#f43f5e", "#7c3aed", "#ea580c", "#0d9488"];

  function withAlpha(hex, alpha) {
    var h = hex.replace("#", "");
    var r = parseInt(h.substring(0, 2), 16);
    var g = parseInt(h.substring(2, 4), 16);
    var b = parseInt(h.substring(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
  }

  function ffillArray(values) {
    var out = [];
    var last = 0;
    (values || []).forEach(function (v) {
      if (v == null) { out.push(last); } else { last = v; out.push(v); }
    });
    return out;
  }

  function dataset(label, data, color) {
    return {
      label: label,
      data: data,
      borderColor: color,
      backgroundColor: color,
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0,
      spanGaps: true
    };
  }

  function chartOptions(yTitle, stacked) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: css("--muted", "#64748b"),
            usePointStyle: true,
            boxWidth: 8,
            boxHeight: 8,
            padding: 16
          }
        },
        tooltip: {
          backgroundColor: css("--tooltip-bg", "rgba(15,23,42,0.96)"),
          borderColor: css("--line", "#e5e7eb"),
          borderWidth: 1,
          titleColor: "#fff",
          bodyColor: "#e2e8f0",
          padding: 10,
          callbacks: {
            label: function (ctx) {
              var value = ctx.parsed.y;
              return ctx.dataset.label + ": " + (value == null ? "-" : Math.round(value) + " W");
            }
          }
        }
      },
      scales: {
        x: {
          ticks: { color: css("--muted", "#64748b"), maxTicksLimit: 8, autoSkip: true },
          grid: { display: false }
        },
        y: {
          stacked: Boolean(stacked),
          beginAtZero: true,
          ticks: { color: css("--muted", "#64748b") },
          grid: { color: css("--grid", "rgba(100,116,139,0.14)") },
          title: { display: true, text: yTitle, color: css("--muted", "#64748b") }
        }
      }
    };
  }

  function controller(opts) {
    var card = document.getElementById(opts.cardId);
    if (!card) return null;
    var url = card.getAttribute("data-series-url");
    var canvas = document.getElementById(opts.canvasId);
    var slider = document.getElementById(opts.sliderId);
    var prevDayButton = card.querySelector('[data-day-step="-1"]');
    var nextDayButton = card.querySelector('[data-day-step="1"]');
    var dayOut = opts.dayOutId ? document.getElementById(opts.dayOutId) : null;
    var dayCap = opts.dayCapId ? document.getElementById(opts.dayCapId) : null;
    var empty = opts.emptyId ? document.getElementById(opts.emptyId) : null;
    var days = [];
    var chart = null;
    var selectedDay = null;
    var hasLoaded = false;
    var activeRequest = null;
    var loadTimer = null;

    function selectedIndex() {
      if (!slider) return -1;
      return parseInt(slider.value, 10) || 0;
    }

    function updateDayButtons() {
      var index = selectedIndex();
      var disabled = !days.length || days.length <= 1;
      if (prevDayButton) prevDayButton.disabled = disabled || index <= 0;
      if (nextDayButton) nextDayButton.disabled = disabled || index >= days.length - 1;
    }

    function paint(data) {
      var built = opts.transform(data);
      if (empty) empty.hidden = !built.isEmpty;
      if (dayCap) dayCap.textContent = data.day || "-";
      if (dayOut) dayOut.textContent = data.day || "-";
      if (chart) {
        chart.data.labels = built.labels;
        chart.data.datasets = built.datasets;
        chart.update("none");
      } else {
        chart = new Chart(canvas.getContext("2d"), {
          type: "line",
          data: { labels: built.labels, datasets: built.datasets },
          options: chartOptions(opts.yTitle, opts.stacked)
        });
      }
      if (opts.after) opts.after(data);
    }

    function load(day) {
      hasLoaded = true;
      selectedDay = day || null;
      var target = url + (day ? ("?day=" + encodeURIComponent(day)) : "");
      if (activeRequest) activeRequest.abort();
      var request = typeof AbortController === "undefined" ? null : new AbortController();
      activeRequest = request;
      var fetchOptions = {
        credentials: "same-origin",
        headers: { Accept: "application/json" }
      };
      if (request) fetchOptions.signal = request.signal;
      fetch(target, fetchOptions)
        .then(function (response) {
          if (response.status === 401) { window.location.href = "/login"; return null; }
          if (!response.ok) throw new Error("status " + response.status);
          return response.json();
        })
        .then(function (data) {
          if (!data) return;
          if (Array.isArray(data.days)) {
            days = data.days;
            if (slider) {
              slider.max = String(Math.max(0, days.length - 1));
              var index = days.indexOf(data.day);
              slider.value = String(index < 0 ? Math.max(0, days.length - 1) : index);
              slider.disabled = days.length <= 1;
            }
            updateDayButtons();
          }
          paint(data);
        })
        .catch(function (error) {
          if (error && error.name === "AbortError") return;
          if (empty) {
            empty.hidden = false;
            empty.textContent = "Dati non raggiungibili dalla dashboard.";
          }
        })
        .finally(function () {
          if (activeRequest === request) activeRequest = null;
        });
    }

    function scheduleLoad(day) {
      if (loadTimer) window.clearTimeout(loadTimer);
      loadTimer = window.setTimeout(function () {
        loadTimer = null;
        load(day);
      }, 160);
    }

    if (slider) {
      slider.addEventListener("input", function () {
        var index = selectedIndex();
        updateDayButtons();
        if (days[index]) scheduleLoad(days[index]);
      });
      slider.addEventListener("change", function () {
        var index = selectedIndex();
        if (days[index]) {
          if (loadTimer) window.clearTimeout(loadTimer);
          load(days[index]);
        }
      });
    }
    [prevDayButton, nextDayButton].forEach(function (button) {
      if (!button) return;
      button.addEventListener("click", function () {
        if (!slider || !days.length) return;
        var nextIndex = selectedIndex() + Number(button.getAttribute("data-day-step"));
        nextIndex = Math.max(0, Math.min(days.length - 1, nextIndex));
        if (!days[nextIndex]) return;
        slider.value = String(nextIndex);
        updateDayButtons();
        if (loadTimer) window.clearTimeout(loadTimer);
        load(days[nextIndex]);
      });
    });
    updateDayButtons();

    if (opts.autoload !== false) load(null);
    return {
      load: function () {
        if (!hasLoaded) load(null);
      },
      refresh: function () {
        if (!hasLoaded) return;
        var latest = days.length ? days[days.length - 1] : null;
        if (!selectedDay || selectedDay === latest) load(null);
      },
      resize: function () {
        if (chart) chart.resize();
      }
    };
  }

  var energyController = controller({
    cardId: "chartCard",
    canvasId: "dailyChart",
    sliderId: "daySlider",
    dayOutId: "dayOut",
    dayCapId: "chartDay",
    emptyId: "chartEmpty",
    yTitle: "Watt",
    stacked: true,
    transform: function (data) {
      var points = data.points || [];
      function series(key) {
        // Forward-fill: i buchi prendono l'ultimo valore noto; il primo punto
        // (senza valore precedente) parte da zero.
        var out = [];
        var last = 0;
        points.forEach(function (point) {
          var v = point[key];
          if (v == null) { out.push(last); } else { last = v; out.push(v); }
        });
        return out;
      }
      function targetSeries() {
        var filled = [];
        var last = null;
        points.forEach(function (point) {
          var v = point.target_w;
          if (v == null) { filled.push(last); return; }
          v = Number(v);
          if (!Number.isFinite(v) || v <= 0) { last = null; filled.push(0); return; }
          last = v;
          filled.push(v);
        });
        return filled.map(function (value, index) {
          if (value == null) return null;
          if (Number(value) > 0) return value;
          var previous = index > 0 ? filled[index - 1] : null;
          var next = index < filled.length - 1 ? filled[index + 1] : null;
          return (previous != null && Number(previous) > 0) ||
            (next != null && Number(next) > 0) ? 0 : null;
        });
      }
      function targetSegmentColor(ctx) {
        var p0 = points[ctx.p0DataIndex] || {};
        var p1 = points[ctx.p1DataIndex] || {};
        if (p0.manual_override || p1.manual_override) return "#8b5cf6";
        var y0 = ctx.p0 && ctx.p0.parsed ? Number(ctx.p0.parsed.y) : null;
        var y1 = ctx.p1 && ctx.p1.parsed ? Number(ctx.p1.parsed.y) : null;
        if (y0 === 0 && y1 === 0) return "#020617";
        return "#ef4444";
      }
      var alfaMode = Boolean(data.alfa_grid_reading_enabled);
      var consumptionDatasets = [
        {
          label: "Casa",
          data: series(alfaMode ? "house" : "vimar"),
          stack: "consumo",
          borderColor: "#2563eb",
          backgroundColor: "rgba(37,99,235,0.40)",
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0,
          fill: true,
          spanGaps: true
        }
      ];
      var teslaDatasetIndex = consumptionDatasets.length;
      consumptionDatasets.push(
        {
          label: "Tesla",
          data: series("tesla"),
          stack: "consumo",
          borderColor: "#0891b2",
          backgroundColor: "rgba(8,145,178,0.26)",
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0,
          fill: true,
          spanGaps: true
        },
        {
          label: "Solare",
          data: series("solar"),
          stack: "produzione",
          borderColor: "#eab308",
          backgroundColor: "rgba(234,179,8,0.12)",
          borderWidth: 2.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0,
          spanGaps: true,
          fill: {
            target: teslaDatasetIndex,
            above: "rgba(244,63,94,0.30)",
            below: "rgba(16,185,129,0.34)"
          }
        },
        {
          label: "Target Tesla",
          data: targetSeries(),
          stack: "target",
          borderColor: "#ef4444",
          backgroundColor: "#ef4444",
          borderWidth: 2.2,
          borderDash: [8, 5],
          borderDashOffset: 0,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0,
          segment: {
            borderColor: targetSegmentColor
          },
          order: -10,
          fill: false,
          spanGaps: false
        }
      );
      return {
        labels: points.map(function (point) { return point.t; }),
        datasets: consumptionDatasets,
        isEmpty: points.length === 0
      };
    }
  });

  function renderApplianceTable(latest) {
    var body = document.getElementById("applianceTableBody");
    if (!body) return;
    body.textContent = "";
    if (!latest || !latest.length) {
      var emptyRow = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = 2;
      cell.className = "muted";
      cell.textContent = "Nessuna lettura.";
      emptyRow.appendChild(cell);
      body.appendChild(emptyRow);
      return;
    }
    latest.forEach(function (appliance) {
      var row = document.createElement("tr");
      var name = document.createElement("td");
      name.textContent = appliance.name == null ? "-" : appliance.name;
      var power = document.createElement("td");
      power.textContent = appliance.power_w == null ? "-" : Math.round(appliance.power_w) + " W";
      row.appendChild(name);
      row.appendChild(power);
      body.appendChild(row);
    });
  }

  function renderAnomalyTable(anomalies) {
    var body = document.getElementById("anomalyTableBody");
    if (!body || !Array.isArray(anomalies)) return;
    body.textContent = "";
    if (!anomalies.length) {
      var emptyRow = document.createElement("tr");
      var emptyCell = cell("Nessun picco anomalo rilevato.");
      emptyCell.colSpan = 5;
      emptyCell.className = "muted";
      emptyRow.appendChild(emptyCell);
      body.appendChild(emptyRow);
      return;
    }
    anomalies.forEach(function (item) {
      var row = document.createElement("tr");
      row.appendChild(cell(item.t || item.observed_at));
      row.appendChild(cell(item.name));
      row.appendChild(cell(item.group));
      row.appendChild(cell(item.power_w == null ? "-" : Math.round(item.power_w) + " W"));
      row.appendChild(cell(item.threshold_w == null ? "-" : Math.round(item.threshold_w) + " W"));
      body.appendChild(row);
    });
  }

  var applianceController = controller({
    cardId: "applianceCard",
    canvasId: "applianceChart",
    sliderId: "applianceSlider",
    dayOutId: "applianceDayOut",
    dayCapId: "applianceDay",
    emptyId: "applianceEmpty",
    yTitle: "Watt",
    stacked: true,
    autoload: false,
    transform: function (data) {
      var series = data.series || [];
      var applianceIndex = 0;
      var visibleSeries = series.filter(function (item) {
        return item.kind !== "house" && item.kind !== "balance-device";
      });
      return {
        labels: data.labels || [],
        datasets: visibleSeries.map(function (item) {
          var values = ffillArray(item.data);
          if (item.kind === "appliance") {
            var color = PALETTE[applianceIndex++ % PALETTE.length];
            return {
              label: item.name,
              data: values,
              stack: "appliances",
              borderColor: color,
              backgroundColor: withAlpha(color, 0.34),
              borderWidth: 1,
              pointRadius: 0,
              pointHoverRadius: 4,
              tension: 0,
              fill: true,
              spanGaps: true
            };
          }
          return {
            label: item.name,
            data: values,
            borderColor: "#94a3b8",
            backgroundColor: "transparent",
            borderWidth: 2,
            borderDash: [5, 4],
            pointRadius: 0,
            pointHoverRadius: 4,
            tension: 0,
            fill: false,
            spanGaps: true
          };
        }),
        isEmpty: visibleSeries.length === 0
      };
    },
    after: function (data) {
      renderApplianceTable(data.latest);
      renderAnomalyTable(data.anomalies);
    }
  });
  if (applianceController) {
    lazyCharts.appliances = applianceController;
    var preloadAppliances = function () { applianceController.load(); };
    if (typeof window.requestIdleCallback === "function") {
      window.requestIdleCallback(preloadAppliances, { timeout: 5000 });
    } else {
      window.setTimeout(preloadAppliances, 2500);
    }
  }

  [energyController, applianceController].forEach(function (item) {
    if (item) chartControllers.push(item);
  });
  window.setInterval(function () {
    chartControllers.forEach(function (item) { item.refresh(); });
  }, chartRefreshMs);
})();
