/* ⌘K: search and ask. Results come from /api/v1/search, which only returns what the caller may
   open (and every link still goes through its own server-side policy). No inline scripts. */
(function () {
  "use strict";
  var dlg, input, list, items = [], sel = -1, timer = null, seq = 0;

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function icon(name) {
    var ns = "http://www.w3.org/2000/svg", s = document.createElementNS(ns, "svg"), u = document.createElementNS(ns, "use");
    s.setAttribute("class", "icon"); s.setAttribute("width", "16"); s.setAttribute("height", "16"); s.setAttribute("aria-hidden", "true");
    u.setAttribute("href", (document.body.getAttribute("data-sprite") || "/static/icons/sprite.svg") + "#i-" + (name || "arrow-right"));
    s.appendChild(u); return s;
  }

  function select(i) {
    if (!items.length) { sel = -1; return; }
    sel = (i + items.length) % items.length;
    items.forEach(function (it, n) { it.setAttribute("aria-selected", n === sel ? "true" : "false"); });
    items[sel].scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", items[sel].id);
  }

  function render(data) {
    list.textContent = ""; items = []; sel = -1;
    if (data.answer) {
      var a = el("div", "answer"); a.setAttribute("role", "note");
      var t = el("div"); t.appendChild(el("span", "assist", "Answer")); a.appendChild(t);
      var h = el("p", null, data.answer.text); h.style.margin = "8px 0 0"; h.style.fontSize = "15px"; a.appendChild(h);
      (data.answer.lines || []).forEach(function (l) { var p = el("div", "small", l); a.appendChild(p); });
      if (data.answer.how) a.appendChild(el("div", "how", "How I know: " + data.answer.how));
      list.appendChild(a);
    }
    (data.groups || []).forEach(function (g) {
      if (!g.items.length) return;
      list.appendChild(el("div", "grp", g.title));
      g.items.forEach(function (r) {
        var a = el("a", "item"); a.href = r.url; a.id = "pal-" + items.length; a.setAttribute("role", "option");
        a.appendChild(icon(r.icon)); a.appendChild(el("span", null, r.title));
        if (r.sub) a.appendChild(el("span", "sub", r.sub));
        a.addEventListener("mousemove", function () { select(items.indexOf(a)); });
        list.appendChild(a); items.push(a);
      });
    });
    if (!items.length && !data.answer) list.appendChild(el("div", "empty-pal", input.value ? "Nothing matches. Try a project name, a person, or a question like “which projects are tied?”" : "Type to search."));
    if (items.length) select(0);
  }

  function query() {
    var q = input.value.trim(), my = ++seq;
    var ev = document.body.getAttribute("data-event") || "";
    fetch("/api/v1/search?q=" + encodeURIComponent(q) + (ev ? "&event=" + encodeURIComponent(ev) : ""), { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : { groups: [] }; })
      .then(function (d) { if (my === seq) render(d); })
      .catch(function () { if (my === seq) render({ groups: [] }); });
  }

  function open() {
    if (!dlg) return;
    if (!dlg.open) { dlg.showModal(); input.value = ""; query(); }
    input.focus();
  }

  document.addEventListener("DOMContentLoaded", function () {
    dlg = document.getElementById("palette"); if (!dlg) return;
    input = document.getElementById("palette-q"); list = document.getElementById("palette-res");
    input.setAttribute("role", "combobox"); input.setAttribute("aria-expanded", "true"); input.setAttribute("aria-autocomplete", "list");
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(query, 110); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { select(sel + 1); e.preventDefault(); }
      else if (e.key === "ArrowUp") { select(sel - 1); e.preventDefault(); }
      else if (e.key === "Enter" && sel >= 0) { e.preventDefault(); window.location = items[sel].href; }
    });
    dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });
    document.addEventListener("click", function (e) { if (e.target.closest("[data-palette-open]")) { e.preventDefault(); open(); } });
    document.addEventListener("keydown", function (e) {
      var tag = (e.target.tagName || "").toLowerCase(), typing = tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable;
      if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) { e.preventDefault(); open(); }
      else if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey) { e.preventDefault(); open(); }
    });
  });
})();
