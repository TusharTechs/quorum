(function () {
  try {
    var t = localStorage.getItem("quorum-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) {}
})();
