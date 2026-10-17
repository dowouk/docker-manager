(function () {
  "use strict";

  function storageGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function storageSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* storage unavailable */ }
  }

  // "Show only updates" filter, remembered per browser.
  function initFilter() {
    var box = document.getElementById("only-updates");
    if (!box) return;
    var empty = document.getElementById("no-updates");
    var anyUpdates = document.querySelector('tbody[data-has-update="1"]') !== null;
    function apply() {
      document.body.classList.toggle("only-updates", box.checked);
      if (empty) empty.classList.toggle("hidden", !(box.checked && !anyUpdates));
      storageSet("onlyUpdates", box.checked ? "1" : "0");
    }
    box.checked = storageGet("onlyUpdates") === "1";
    box.addEventListener("change", apply);
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
    initFilter();
    initPreview();
    initJobLog();
  });
})();
