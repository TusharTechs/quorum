/* Quorum UI behaviour. No inline scripts anywhere (CSP script-src 'self'). */
(function () {
  "use strict";
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // theme toggle
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-theme-toggle]");
    if (!b) return;
    var cur = document.documentElement.getAttribute("data-theme") ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    var next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("quorum-theme", next); } catch (err) {}
  });

  // copy buttons
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-copy]");
    if (!b) return;
    var text = b.getAttribute("data-copy");
    var target = b.getAttribute("data-copy-target");
    if (target) { var el = $(target); text = el ? (el.value || el.textContent) : text; }
    navigator.clipboard && navigator.clipboard.writeText(text).then(function () {
      var old = b.textContent; b.textContent = "Copied"; setTimeout(function () { b.textContent = old; }, 1200);
    });
  });

  // confirmation for destructive / irreversible actions
  document.addEventListener("submit", function (e) {
    var f = e.target;
    var msg = f.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // live countdowns: <time data-countdown datetime="...">
  function tick() {
    $$("[data-countdown]").forEach(function (el) {
      var t = Date.parse(el.getAttribute("datetime")) - Date.now();
      if (isNaN(t)) return;
      if (t <= 0) { el.textContent = el.getAttribute("data-ended") || "closed"; return; }
      var d = Math.floor(t / 864e5), h = Math.floor(t % 864e5 / 36e5), m = Math.floor(t % 36e5 / 6e4), s = Math.floor(t % 6e4 / 1e3);
      el.textContent = (d ? d + "d " : "") + (d || h ? h + "h " : "") + m + "m " + (d ? "" : s + "s");
    });
  }
  setInterval(tick, 1000); document.addEventListener("DOMContentLoaded", tick);

  // gallery / list filters auto-submit
  document.addEventListener("change", function (e) {
    var f = e.target.closest("form[data-autosubmit]");
    if (f) f.requestSubmit ? f.requestSubmit() : f.submit();
  });

  // ---- judge console -------------------------------------------------------
  function initConsole() {
    var form = $("#review-form");
    if (!form) return;
    var crits = $$(".crit", form);
    var active = 0;
    var dirty = false;
    var status = $("#save-status");
    var started = Date.now();
    var visibleMs = 0, lastVis = Date.now(), visible = !document.hidden;
    document.addEventListener("visibilitychange", function () {
      if (visible) visibleMs += Date.now() - lastVis;
      visible = !document.hidden; lastVis = Date.now();
    });
    function activeSeconds() { return Math.round((visibleMs + (visible ? Date.now() - lastVis : 0)) / 1000); }
    function setActive(i) {
      active = Math.max(0, Math.min(crits.length - 1, i));
      crits.forEach(function (c, k) { c.classList.toggle("active", k === active); });
    }
    function showAnchor(c) {
      var checked = $("input:checked", c);
      var a = $(".anchor", c);
      if (a) a.textContent = checked ? (checked.getAttribute("data-anchor") || "") : "";
    }
    crits.forEach(function (c, i) {
      c.addEventListener("click", function () { setActive(i); });
      $$("input[type=radio]", c).forEach(function (r) {
        r.addEventListener("change", function () { showAnchor(c); dirty = true; save(); });
      });
      showAnchor(c);
    });
    setActive(0);
    var fb = $("#feedback");
    var meter = $("#feedback-meter");
    var min = parseInt((fb && fb.getAttribute("data-min")) || "0", 10);
    function updMeter() {
      if (!fb || !meter) return;
      var n = fb.value.trim().length;
      meter.textContent = n + (min ? " / " + min + " characters minimum" : " characters");
      meter.classList.toggle("ok", n >= min);
    }
    if (fb) { fb.addEventListener("input", function () { updMeter(); dirty = true; debounceSave(); }); updMeter(); }
    $$("textarea, select", form).forEach(function (el) { if (el !== fb) el.addEventListener("input", function () { dirty = true; debounceSave(); }); });
    var timer = null;
    function debounceSave() { clearTimeout(timer); timer = setTimeout(save, 900); }
    function save() {
      if (!dirty) return;
      dirty = false;
      var data = new FormData(form);
      data.set("active_seconds", activeSeconds());
      data.set("action", "draft");
      status && (status.textContent = "Saving…");
      fetch(form.getAttribute("data-autosave"), { method: "POST", body: data, headers: { "X-CSRFToken": data.get("csrfmiddlewaretoken") } })
        .then(function (r) { return r.json(); })
        .then(function (j) { status && (status.textContent = j.ok ? "Draft saved " + new Date().toLocaleTimeString() : (j.detail || "Not saved")); })
        .catch(function () { status && (status.textContent = "Offline, will retry"); dirty = true; });
    }
    form.addEventListener("submit", function () {
      var h = $("input[name=active_seconds]", form);
      if (h) h.value = activeSeconds();
    });
    document.addEventListener("keydown", function (e) {
      var tag = (e.target.tagName || "").toLowerCase();
      var typing = tag === "textarea" || (tag === "input" && e.target.type === "text");
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); var s = $("#submit-review"); s && s.click(); return; }
      if (typing) { if (e.key === "Escape") e.target.blur(); return; }
      if (/^[1-9]$/.test(e.key)) {
        var r = $("input[type=radio][value='" + e.key + "']", crits[active]);
        if (r) { r.checked = true; r.dispatchEvent(new Event("change", { bubbles: true })); if (active < crits.length - 1) setActive(active + 1); }
        e.preventDefault();
      } else if (e.key === "j" || e.key === "ArrowDown") { setActive(active + 1); e.preventDefault(); }
      else if (e.key === "k" || e.key === "ArrowUp") { setActive(active - 1); e.preventDefault(); }
      else if (e.key === "f") { fb && fb.focus(); e.preventDefault(); }
      else if (e.key === "n") { var nx = $("#next-link"); if (nx) window.location = nx.href; }
    });
    window.addEventListener("beforeunload", function () { if (dirty) save(); });
  }

  // ---- pairwise console ------------------------------------------------------
  function initPairwise() {
    var f = $("#pairwise-form");
    if (!f) return;
    document.addEventListener("keydown", function (e) {
      var tag = (e.target.tagName || "").toLowerCase();
      if (tag === "textarea" || tag === "input") return;
      var map = { a: "a", ArrowLeft: "a", b: "b", ArrowRight: "b", t: "tie", "=": "tie" };
      var v = map[e.key];
      if (!v) return;
      var btn = $("button[name=outcome][value='" + v + "']", f);
      if (btn) { e.preventDefault(); btn.click(); }
    });
  }

  document.addEventListener("DOMContentLoaded", function () { initConsole(); initPairwise(); });
})();
