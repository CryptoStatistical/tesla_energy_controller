// Applica il tema salvato (login + dashboard) prima del primo paint, così tutte
// le pagine ereditano la scelta chiaro/scuro fatta nella dashboard, senza flash.
// File esterno perché la CSP è `script-src 'self'` e blocca gli script inline.
(function () {
  "use strict";
  try {
    var stored = window.localStorage.getItem("dashboard-theme");
    var theme = stored === "light" || stored === "dark"
      ? stored
      : (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
  } catch (_error) {
    /* localStorage non disponibile: resta il tema di default del CSS. */
  }
})();
