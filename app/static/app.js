(function () {
  "use strict";

  function storageGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function storageSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* storage unavailable */ }
  }

  // Settings: show one section at a time, chosen from the list on the left. Without JS
  // every section shows and the list links jump down the page.
  function initSettingsNav() {
    var layout = document.querySelector(".settings-layout");
    if (!layout) return;
    var cards = Array.prototype.slice.call(layout.querySelectorAll(".settings-card"));
    var links = Array.prototype.slice.call(layout.querySelectorAll('.settings-nav a[href^="#"]'));
    if (!cards.length) return;
    function sectionFor(hash) {
      var target = hash ? document.getElementById(hash.slice(1)) : null;
      var card = target && target.closest ? target.closest(".settings-card") : null;
      if (card) return card;
      var initial = layout.getAttribute("data-section");
      return (initial && document.getElementById(initial)) || cards[0];
    }
    function show(hash) {
      var card = sectionFor(hash);
      cards.forEach(function (c) { c.hidden = c !== card; });
      links.forEach(function (a) {
        if (a.getAttribute("href") === "#" + card.id) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
      });
    }
    window.addEventListener("hashchange", function () { show(window.location.hash); window.scrollTo(0, 0); });
    layout.classList.add("settings-paged");
    show(window.location.hash);
    // the browser jumped to the section's anchor; stay at the top so any notice shows
    window.requestAnimationFrame(function () { window.scrollTo(0, 0); });
  }

  // Schedule picker: show only the days-and-time or the custom cron part, as chosen.
  // Without JS both show and the chosen mode decides which one is saved.
  function initSchedules() {
    document.querySelectorAll("[data-schedule]").forEach(function (box) {
      var radios = box.querySelectorAll('input[type="radio"]');
      function render() {
        var mode = "days";
        radios.forEach(function (r) { if (r.checked) mode = r.value; });
        box.querySelectorAll("[data-mode]").forEach(function (part) {
          part.hidden = part.getAttribute("data-mode") !== mode;
        });
      }
      radios.forEach(function (r) { r.addEventListener("change", render); });
      box.classList.add("schedule-js");
      render();
    });
  }

  // Close the account menu on Escape or a click elsewhere.
  function initAccountMenu() {
    var menu = document.querySelector("details.account");
    if (!menu) return;
    document.addEventListener("click", function (event) {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
    menu.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && menu.open) {
        menu.open = false;
        menu.querySelector("summary").focus();
      }
    });
  }

  // Filter chips and search over the table rows and the attention cards. Chips are toggles:
  // a row shows when it matches any pressed chip (none pressed = everything). The choice is
  // remembered per browser under "dm-filters". Without JS the chips stay hidden and everything shows.
  function initFilter() {
    var tools = document.getElementById("table-tools");
    if (!tools) return;
    var chips = tools.querySelectorAll("button[data-chip]");
    var search = document.getElementById("container-search");
    var items = document.querySelectorAll("[data-state][data-name]");
    var groups = document.querySelectorAll("[data-group-header]");
    var rows = document.querySelectorAll("table.containers tbody tr[data-name]");
    var empty = document.getElementById("no-matches");
    var active = {};
    var saved = null;
    try { saved = JSON.parse(storageGet("dm-filters") || "null"); } catch (e) { saved = null; }
    if (saved && typeof saved === "object") {
      var names = Array.isArray(saved.chips) ? saved.chips : [];
      for (var n = 0; n < names.length; n++) {
        if (tools.querySelector('button[data-chip="' + String(names[n]).replace(/[^a-z-]/g, "") + '"]')) active[names[n]] = true;
      }
      if (typeof saved.q === "string") search.value = saved.q;
    }
    function matches(item, chip) {
      if (chip === "skipped") return item.getAttribute("data-skipped") === "1";
      if (chip === "layout") return parseInt(item.getAttribute("data-layout") || "0", 10) > 0;
      return (item.getAttribute("data-state") || "").split(" ").indexOf(chip) !== -1;
    }
    function show(item, query, any) {
      if (query) {
        var hay = (item.getAttribute("data-name") || "") + " " + (item.getAttribute("data-image") || "") + " " +
          (item.getAttribute("data-project") || "");
        if (hay.indexOf(query) === -1) return false;
      }
      if (!any) return true;
      for (var c in active) if (active[c] && matches(item, c)) return true;
      return false;
    }
    function apply() {
      var query = (search.value || "").trim().toLowerCase();
      var any = false;
      for (var key in active) if (active[key]) any = true;
      var shown = 0;
      var projects = {};
      for (var i = 0; i < items.length; i++) {
        var ok = show(items[i], query, any);
        items[i].hidden = !ok;
        if (ok && items[i].tagName === "TR") { shown++; projects[items[i].getAttribute("data-project") || ""] = true; }
      }
      for (var g = 0; g < groups.length; g++) {
        groups[g].hidden = !projects[groups[g].getAttribute("data-project") || ""];
      }
      for (var j = 0; j < chips.length; j++) {
        var chip = chips[j].getAttribute("data-chip");
        chips[j].setAttribute("aria-pressed", active[chip] ? "true" : "false");
        var count = 0;
        for (var r = 0; r < rows.length; r++) if (matches(rows[r], chip)) count++;
        var badge = chips[j].querySelector(".count");
        if (badge) badge.textContent = String(count);
      }
      if (empty) empty.hidden = shown !== 0;
      var chosen = [];
      for (var k in active) if (active[k]) chosen.push(k);
      storageSet("dm-filters", JSON.stringify({ chips: chosen, q: query }));
    }
    for (var m = 0; m < chips.length; m++) {
      chips[m].addEventListener("click", function () {
        var chip = this.getAttribute("data-chip");
        active[chip] = !active[chip];
        apply();
      });
    }
    search.addEventListener("input", apply);
    search.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && search.value) {
        search.value = "";
        apply();
      }
    });
    tools.hidden = false;
    apply();
  }

  // Version update preview in a dialog.
  function initPreview() {
    var dialog = document.getElementById("preview-dialog");
    var body = document.getElementById("preview-body");
    if (!dialog || !body || typeof dialog.showModal !== "function") return;
    document.addEventListener("click", function (event) {
      var link = event.target.closest ? event.target.closest("a.preview-link") : null;
      if (!link) return;
      event.preventDefault();
      body.textContent = "Loading…";
      dialog.showModal();
      fetch(link.href, { credentials: "same-origin" })
        .then(function (resp) {
          // A redirect (to sign-in) or an error page is never injected as the preview.
          if (resp.redirected || resp.status === 401) {
            body.textContent = "Your session has expired – reload the page";
            return;
          }
          if (!resp.ok) {
            body.textContent = "Couldn't load the preview – reload the page and try again";
            return;
          }
          return resp.text().then(function (html) { body.innerHTML = html; });
        })
        .catch(function () { body.textContent = "Couldn't load the preview – reload the page and try again"; });
    });
  }

  // Forms with data-confirm ask in a modal dialog before they are sent. Without the
  // dialog (no JS or an old browser) they are sent straight away.
  function initConfirm() {
    var dialog = document.getElementById("confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") return;
    var title = document.getElementById("confirm-title");
    var detail = document.getElementById("confirm-detail");
    var button = document.getElementById("confirm-button");
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form.hasAttribute || !form.hasAttribute("data-confirm")) return;
      event.preventDefault();
      title.textContent = form.getAttribute("data-confirm");
      detail.textContent = form.getAttribute("data-confirm-detail") || "";
      detail.hidden = !detail.textContent;
      button.textContent = form.getAttribute("data-confirm-button") || "Confirm";
      dialog.returnValue = "";
      dialog.onclose = function () {
        if (dialog.returnValue !== "confirm") return;
        form.submit();  // no submit event, so this doesn't ask again
      };
      dialog.showModal();
      button.focus();
    });
  }

  // Live job log over server-sent events.
  function initJobLog() {
    var log = document.getElementById("job-log");
    if (!log || log.dataset.status !== "running" || typeof EventSource === "undefined") return;
    var result = document.getElementById("job-result");
    var back = document.getElementById("job-back");
    var source = new EventSource("/jobs/" + encodeURIComponent(log.dataset.jobId) + "/stream");
    // Each (re)connection replays the full log, so start from empty.
    source.onopen = function () { log.textContent = ""; };
    source.onmessage = function (event) {
      var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
      log.textContent += event.data + "\n";
      if (atBottom) log.scrollTop = log.scrollHeight;
    };
    source.addEventListener("end", function (event) {
      source.close();
      var ok = event.data === "succeeded";
      if (event.data === "interrupted") {
        result.textContent = "The dashboard is restarting – reload the page in a minute to see the result";
      } else {
        var noun = log.dataset.noun || "Update";
        result.textContent = ok ? noun + " succeeded" : noun + " failed – see the log";
      }
      result.classList.remove("hidden");
      result.classList.add(ok || event.data === "interrupted" ? "notice-ok" : "notice-error");
      back.classList.remove("hidden");
    });
  }

  // Copy buttons (data-copy = id of a read-only input). Hidden until the browser can copy.
  function initCopy() {
    var buttons = document.querySelectorAll("button[data-copy]");
    for (var i = 0; i < buttons.length; i++) {
      (function (button) {
        var source = document.getElementById(button.getAttribute("data-copy"));
        if (!source || !navigator.clipboard) return;
        button.hidden = false;
        button.addEventListener("click", function () {
          navigator.clipboard.writeText(source.value).then(function () {
            button.textContent = "Copied";
            setTimeout(function () { button.textContent = "Copy"; }, 2000);
          }, function () { source.select(); });
        });
      })(buttons[i]);
    }
  }

  // SMTP presets on the channel form: fill host, port and security. Hidden without JS.
  // After "Update the dashboard": the container is replaced, so wait for /healthz to go
  // away and come back, then open the job's log (which records how it went). If it never
  // goes down (nothing to update), open the log after a while anyway.
  function initAwaitRestart() {
    var el = document.querySelector("[data-await-restart]");
    if (!el) return;
    var job = el.getAttribute("data-job");
    var before = el.getAttribute("data-started");
    if (!/^[0-9]+$/.test(job || "") || !before) return;
    var log = "/jobs/" + job;
    var started = Date.now();
    var LIMIT_MS = 600000;    // matches the server's own wait for the restart
    el.hidden = false;
    function say(text) { el.textContent = text; }
    function get(url) {
      var controller = window.AbortController ? new AbortController() : null;
      var timer = controller ? setTimeout(function () { controller.abort(); }, 3000) : null;
      return fetch(url, { cache: "no-store", credentials: "same-origin",
                          signal: controller ? controller.signal : undefined })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) { if (timer) clearTimeout(timer); return data; },
              function () { if (timer) clearTimeout(timer); return null; });
    }
    function again() {
      if (Date.now() - started > LIMIT_MS) {
        say("The dashboard hasn't come back yet – check the server, then reload this page.");
        return;
      }
      setTimeout(check, 1500);
    }
    function check() {
      get("/healthz").then(function (health) {
        if (!health || health.status !== "ok") {
          say("Restarting the dashboard…");
          return again();
        }
        if (health.started !== before) {
          // A new dashboard answered: the self-update worked.
          say("The dashboard is back (version " + health.version + ") – opening it…");
          window.location.assign("/?msg=self_updated");
          return;
        }
        // Still the old dashboard: if the job ended, it failed or had nothing to do.
        get(log + "/status").then(function (state) {
          if (state && state.status && state.status !== "running") {
            window.location.assign(log);
            return;
          }
          say("Waiting for the new version to start…");
          again();
        });
      });
    }
    say("Waiting for the new version to start…");
    setTimeout(check, 1500);
  }

  function initPresets() {
    var box = document.querySelector("[data-presets]");
    if (!box) return;
    function set(name, value) {
      var field = document.getElementById("f-" + name);
      if (field) field.value = value;
    }
    box.addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest("button[data-preset]") : null;
      if (!button) return;
      set("host", button.getAttribute("data-host"));
      set("port", button.getAttribute("data-port"));
      set("security", button.getAttribute("data-security"));
    });
    box.hidden = false;
  }

  document.addEventListener("DOMContentLoaded", function () {
    initSettingsNav();
    initSchedules();
    initPresets();
    initAwaitRestart();
    initCopy();
    initAccountMenu();
    initFilter();
    initPreview();
    initConfirm();
    initJobLog();
  });
})();
