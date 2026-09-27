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
        .then(function (j) { if (status) { status.textContent = j.ok ? "✓ Draft saved " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : (j.detail || "Not saved"); status.className = "save-chip " + (j.ok ? "ok" : "err"); } })
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
    var t0 = Date.now(), secs = $("input[name=active_seconds]", f);
    if (secs) f.addEventListener("submit", function () { secs.value = Math.min(1800, Math.round((Date.now() - t0) / 1000)); });
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

  // ---- submission editor autosave ----------------------------------------------
  function initProjectAutosave() {
    var f = $("form[data-autosave-form]");
    if (!f) return;
    var url = f.getAttribute("data-autosave-form"), st = $("#autosave-status"), t = null;
    f.addEventListener("input", function () {
      clearTimeout(t);
      t = setTimeout(function () {
        var d = new FormData(f); d.set("action", "autosave");
        st && (st.textContent = "saving…");
        fetch(url, { method: "POST", body: d, headers: { "X-CSRFToken": d.get("csrfmiddlewaretoken") } })
          .then(function (r) { return r.json(); })
          .then(function (j) { st && (st.textContent = j.ok ? "saved v" + j.version : (j.detail || "not saved")); })
          .catch(function () { st && (st.textContent = "offline: not saved"); });
      }, 1200);
    });
  }

  document.addEventListener("DOMContentLoaded", function () { initConsole(); initPairwise(); initProjectAutosave(); });
})();

/* toasts and plain-language glossary popovers */
(function () {
  "use strict";
  function dismiss(t) { if (!t || t.classList.contains("out")) return; t.classList.add("out"); setTimeout(function () { t.remove(); }, 220); }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-toast-close]"); if (b) dismiss(b.closest(".toast"));
  });
  document.addEventListener("DOMContentLoaded", function () {
    // successes and info fade after a while; errors stay until dismissed (WCAG 2.2.1: no surprise timeouts for problems)
    Array.prototype.forEach.call(document.querySelectorAll(".toast:not(.error)"), function (t, i) {
      var h = setTimeout(function () { dismiss(t); }, 6500 + i * 600);
      t.addEventListener("mouseenter", function () { clearTimeout(h); });
      t.addEventListener("focusin", function () { clearTimeout(h); });
    });
  });
  window.quorumToast = function (text, kind) {
    var box = document.getElementById("toasts"); if (!box) return;
    var t = document.createElement("div"); t.className = "toast " + (kind || "success");
    var s = document.createElement("span"); s.textContent = text; t.appendChild(s);
    var c = document.createElement("button"); c.type = "button"; c.setAttribute("data-toast-close", ""); c.setAttribute("aria-label", "Dismiss"); c.textContent = "×";
    t.appendChild(c); box.appendChild(t);
    if (kind !== "error") setTimeout(function () { dismiss(t); }, 6500);
  };

  // glossary: <button class="gl" data-gl="...plain words..." data-src="...">?</button>
  var pop = null, owner = null;
  function close() { if (pop) { pop.remove(); pop = null; } if (owner) { owner.setAttribute("aria-expanded", "false"); owner = null; } }
  function show(b) {
    close();
    pop = document.createElement("div"); pop.className = "pop"; pop.id = "gl-pop"; pop.setAttribute("role", "tooltip");
    var t = document.createElement("div"); t.textContent = b.getAttribute("data-gl"); pop.appendChild(t);
    if (b.getAttribute("data-src")) { var s = document.createElement("span"); s.className = "src"; s.textContent = b.getAttribute("data-src"); pop.appendChild(s); }
    document.body.appendChild(pop);
    var r = b.getBoundingClientRect(), w = pop.offsetWidth, x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), document.documentElement.clientWidth - w - 8);
    var y = r.bottom + 8 + window.scrollY;
    if (r.bottom + pop.offsetHeight + 16 > window.innerHeight) y = r.top + window.scrollY - pop.offsetHeight - 8;
    pop.style.left = (x + window.scrollX) + "px"; pop.style.top = y + "px";
    b.setAttribute("aria-expanded", "true"); b.setAttribute("aria-describedby", "gl-pop"); owner = b;
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest(".gl");
    if (b) { e.preventDefault(); if (owner === b) close(); else show(b); return; }
    if (pop && !e.target.closest(".pop")) close();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  window.addEventListener("resize", close);
})();

/* judge console: live weighted total (the judge's own view of the rubric they are filling in) */
(function () {
  "use strict";
  function update(form) {
    var out = form.querySelector("#weighted"); if (!out) return;
    var sw = 0, sv = 0, n = 0, total = 0;
    Array.prototype.forEach.call(form.querySelectorAll("fieldset.crit"), function (fs) {
      var w = parseFloat(fs.getAttribute("data-weight")) || 0, c = fs.querySelector("input:checked");
      total++; if (c) { sw += w; sv += w * parseFloat(c.value); n++; }
    });
    out.textContent = n ? "Weighted " + (sv / sw).toFixed(2) + (n < total ? " · " + n + "/" + total : "") : "";
  }
  document.addEventListener("change", function (e) { var f = e.target.closest("#review-form"); if (f) update(f); });
  document.addEventListener("DOMContentLoaded", function () { var f = document.getElementById("review-form"); if (f) update(f); });
})();
