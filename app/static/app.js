(function () {
  "use strict";

  function storageGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function storageSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* storage unavailable */ }
  }

  // Light/dark theme. With nothing saved the page follows the system setting;
  // the toggle saves an explicit choice (applied early by the inline script in <head>).
  function initTheme() {
    var button = document.getElementById("theme-toggle");
    if (!button) return;
    var root = document.documentElement;
    var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    function current() {
      var chosen = root.getAttribute("data-theme");
      if (chosen === "light" || chosen === "dark") return chosen;
      return media && media.matches ? "dark" : "light";
    }
    function render() {
      var theme = current();
      button.setAttribute("data-current", theme);
      button.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
    }
    button.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      storageSet("theme", next);
      render();
    });
    if (media && media.addEventListener) media.addEventListener("change", render);
    button.hidden = false;
    render();
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

  // Filter buttons and search over the container table. The filter is remembered per browser.
  function initFilter() {
    var tools = document.getElementById("table-tools");
    if (!tools) return;
    var buttons = tools.querySelectorAll("button[data-filter]");
    var search = document.getElementById("container-search");
    var rows = document.querySelectorAll("table.containers tbody tr[data-state]");
    var empty = document.getElementById("no-matches");
    var filter = storageGet("containerFilter") || "all";
    function apply() {
      var query = (search.value || "").trim().toLowerCase();
      var shown = 0;
      for (var i = 0; i < rows.length; i++) {
        var row = rows[i];
        var visible = (filter === "all" || row.getAttribute("data-state") === filter) &&
          (!query || (row.getAttribute("data-search") || "").indexOf(query) !== -1);
        row.hidden = !visible;
        if (visible) shown++;
      }
      for (var j = 0; j < buttons.length; j++) {
        buttons[j].setAttribute("aria-pressed", buttons[j].getAttribute("data-filter") === filter ? "true" : "false");
      }
      if (empty) empty.hidden = shown !== 0;
    }
    for (var k = 0; k < buttons.length; k++) {
      buttons[k].addEventListener("click", function () {
        filter = this.getAttribute("data-filter");
        storageSet("containerFilter", filter);
        apply();
      });
    }
    if (!tools.querySelector('button[data-filter="' + filter + '"]')) filter = "all";
    search.addEventListener("input", apply);
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
        result.textContent = ok ? "Update succeeded" : "Update failed – see the log";
      }
      result.classList.remove("hidden");
      result.classList.add(ok || event.data === "interrupted" ? "notice-ok" : "notice-error");
      back.classList.remove("hidden");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initTheme();
    initAccountMenu();
    initFilter();
    initPreview();
    initJobLog();
  });
})();
