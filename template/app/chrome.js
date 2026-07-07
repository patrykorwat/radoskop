/* Wspólny chrome Radoskop — JEDNO źródło prawdy dla całego portalu:
   motyw (light/dark, cross-subdomain cookie) + pasek logowania (auth bridge
   cross-TLD). Używają go: strony spółek (apex), base.html (eu/legal) oraz SPA
   miast (po wpięciu zamiast wklejonej kopii).

   Endpointy i logika 1:1 z dotychczasowym SPA:
     AUTH_API  = api.radoskop.{pl|eu}/api/auth   (/me, /logout, /bridge-token)
     AUTH_FRONT= radoskop.eu                      (login + bridge sesji .pl)

   Odporny na kontekst: jeśli na stronie są #theme-icon/#theme-label albo
   Chart.js — aktualizuje je; jeśli nie ma — pomija. Rozszerzalny przez
   OPCJONALNE hooki, które definiuje konsument (SPA) PRZED załadowaniem:
     window.onThemeChange(theme)  — np. SPA przerysowuje wykresy (if(K)render())
     window.onAuthUser(user)      — np. SPA odświeża gating Pro
   Eksponuje funkcje pod window pod tymi samymi nazwami co stare SPA, żeby
   istniejące onclicki i kod (submitAuth itd.) działały bez zmian. */
(function () {
  "use strict";
  var H = location.hostname;
  var EU = H === "radoskop.eu" || H.indexOf(".radoskop.eu") !== -1;
  var API_HOST = window.RADOSKOP_API || (EU ? "https://api.radoskop.eu" : "https://api.radoskop.pl");
  var AUTH_API = (window.RADOSKOP_AUTH_API || API_HOST) + "/api/auth";
  var AUTH_FRONT = window.RADOSKOP_AUTH_FRONTEND || "https://radoskop.eu";
  window.AUTH_API = AUTH_API;
  window.AUTH_FRONTEND = AUTH_FRONT;

  function cookieDomain() {
    var h = location.hostname;
    if (h.indexOf("radoskop.pl") !== -1) return "; domain=.radoskop.pl";
    if (h.indexOf("radoskop.eu") !== -1) return "; domain=.radoskop.eu";
    return "";
  }
  function getCookie(name) {
    var m = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]+)"));
    return m ? decodeURIComponent(m[1]) : null;
  }
  function setCookie(name, value, days) {
    var d = new Date(); d.setTime(d.getTime() + (days || 365) * 86400000);
    var secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie = name + "=" + encodeURIComponent(value) + "; expires=" + d.toUTCString() +
      "; path=/; SameSite=Lax" + secure + cookieDomain();
  }
  window.radoskopGetCookie = getCookie;
  window.radoskopSetCookie = setCookie;

  /* Umami Distinct ID. Anonim dostaje losowy ID w cookie radoskop_did na
     domenie nadrzędnej (wspólne dla wszystkich subdomen danej TLD), TTL
     krótki (2 dni), żeby skleić wizytę w obrębie kilku dni bez trwałego
     profilowania (tak deklaruje polityka prywatności). Po zalogowaniu
     nadpisujemy przez "u:" + public_id z /me (hook w setAuthUser).
     identify() czeka aż async script.js trackera się załaduje, ten sam
     wzorzec co bufory eventów w SPA. Kopie tej logiki: SPA
     template/index.html, docs_eu/assets/topbar.js, docs_eu/index.html. */
  var _idPending = null, _idTimer = null;
  function _umamiIdentifiable() {
    return typeof umami !== "undefined" && typeof umami.identify === "function";
  }
  function identifyUmami(id) {
    if (!id) return;
    _idPending = id;
    if (_umamiIdentifiable()) {
      try { umami.identify(_idPending); } catch (e) {}
      _idPending = null;
      return;
    }
    if (_idTimer) return;
    var tries = 0;
    _idTimer = setInterval(function () {
      tries++;
      if (_umamiIdentifiable()) {
        clearInterval(_idTimer); _idTimer = null;
        if (_idPending) { try { umami.identify(_idPending); } catch (e) {} _idPending = null; }
      } else if (tries > 50) {
        clearInterval(_idTimer); _idTimer = null;
      }
    }, 400);
  }
  window._rkIdentify = identifyUmami;
  function anonDistinctId() {
    var id = getCookie("radoskop_did");
    if (!id) {
      id = (window.crypto && typeof crypto.randomUUID === "function")
        ? crypto.randomUUID()
        : Date.now().toString(36) + "." + Math.random().toString(36).slice(2, 12);
      setCookie("radoskop_did", id, 2);
    }
    return id;
  }
  window._rkAnonId = anonDistinctId;

  function getTheme() {
    var c = getCookie("radoskop_theme");
    if (c === "dark" || c === "light") return c;
    try { var s = localStorage.getItem("radoskop_theme"); if (s === "dark" || s === "light") return s; } catch (e) {}
    return (window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  }
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var icon = document.getElementById("theme-icon");
    var label = document.getElementById("theme-label");
    if (icon) icon.textContent = theme === "light" ? "🌙" : "☀️";
    if (label) label.textContent = theme === "light" ? "Ciemny" : "Jasny";
    if (window.Chart && Chart.defaults) {
      var s = getComputedStyle(document.documentElement);
      Chart.defaults.color = s.getPropertyValue("--chart-text").trim() || "#4b5563";
      Chart.defaults.borderColor = s.getPropertyValue("--chart-border").trim() || "rgba(0,0,0,0.08)";
    }
  }
  function toggleTheme() {
    var cur = document.documentElement.getAttribute("data-theme") || "light";
    var next = cur === "dark" ? "light" : "dark";
    applyTheme(next);
    setCookie("radoskop_theme", next, 365);
    try { localStorage.removeItem("radoskop_theme"); } catch (e) {}
    if (typeof window.onThemeChange === "function") { try { window.onThemeChange(next); } catch (e) {} }
  }
  window.getTheme = getTheme;
  window.applyTheme = applyTheme;
  window.toggleTheme = toggleTheme;

  var user = null;
  try { var cached = sessionStorage.getItem("radoskop_user"); if (cached) user = JSON.parse(cached); } catch (e) {}
  window._authUser = user;  // SPA czyta to bezpośrednio (render alertów, gating)

  function renderAuth() {
    var btn = document.getElementById("auth-login-btn");
    var chip = document.getElementById("user-chip");
    var sales = document.querySelectorAll(".topbar-nav .nav-sales");
    var pro = !!(user && user.pro);
    for (var i = 0; i < sales.length; i++) sales[i].style.display = pro ? "none" : "";
    if (!btn || !chip) return;
    if (user) {
      btn.style.display = "none";
      chip.style.display = "inline-flex";
      var av = document.getElementById("user-avatar");
      if (av) { av.textContent = (user.email || "?")[0].toUpperCase(); av.title = user.email || ""; }
      var nm = document.getElementById("user-name");
      if (nm) {
        var dn = (user.display_name && user.display_name.trim())
          ? user.display_name.trim() : (user.email || "").split("@")[0];
        nm.textContent = dn; nm.title = user.email || "";
      }
    } else {
      btn.style.display = "";
      chip.style.display = "none";
    }
  }
  function setAuthUser(u) {
    user = u;
    window._authUser = u;
    try {
      if (u) sessionStorage.setItem("radoskop_user", JSON.stringify(u));
      else sessionStorage.removeItem("radoskop_user");
    } catch (e) {}
    identifyUmami(u && u.id ? "u:" + u.id : anonDistinctId());
    renderAuth();
    if (typeof window.onAuthUser === "function") { try { window.onAuthUser(u); } catch (e) {} }
  }
  window._setAuthUser = setAuthUser;
  window._renderAuthHeader = renderAuth;

  window._bridgeLogin = function () {
    location.href = AUTH_FRONT + "/?bridge_to=" + encodeURIComponent(location.href);
  };

  function maybeAutoBridge() {
    var onPl = H === "radoskop.pl" || (H.indexOf(".radoskop.pl") !== -1);
    if (!onPl) return false;
    try {
      if (sessionStorage.getItem("radoskop_bridge_probed") === "1") return false;
      sessionStorage.setItem("radoskop_bridge_probed", "1");
    } catch (e) { return false; }
    location.replace(AUTH_FRONT + "/?bridge_to=" + encodeURIComponent(location.href) + "&probe=1");
    return true;
  }
  window._maybeAutoBridge = maybeAutoBridge;

  window._refreshAuth = function () {
    return fetch(AUTH_API + "/me", { credentials: "include" }).then(function (r) {
      if (!r.ok) { if (maybeAutoBridge()) return; setAuthUser(null); return; }
      return r.json().then(function (d) { setAuthUser(d.user || null); });
    }).catch(function () { /* sieć: zostaw cache */ });
  };

  window.doLogout = function () {
    fetch(AUTH_API + "/logout", { method: "POST", credentials: "include" })
      .catch(function () {})
      .then(function () {
        setAuthUser(null);
        location.href = AUTH_FRONT + "/?logout_remote=1&return_to=" + encodeURIComponent(location.href);
      });
  };

  window.goToProfile = function () {
    var p = AUTH_FRONT + "/profile/";
    if (EU) { location.href = p; return; }
    fetch(AUTH_API + "/bridge-token", { credentials: "include" }).then(function (r) {
      if (!r.ok) { location.href = p; return; }
      return r.json().then(function (d) {
        if (!d.token) { location.href = p; return; }
        location.href = "https://api.radoskop.eu/api/auth/bridge-exchange?token=" +
          encodeURIComponent(d.token) + "&return_to=" + encodeURIComponent(p);
      });
    }).catch(function () { location.href = p; });
  };

  applyTheme(getTheme());
  renderAuth();
  identifyUmami(user && user.id ? "u:" + user.id : anonDistinctId());
  // Konsument może odroczyć auto /me (window.RADOSKOP_DEFER_CHROME=true) i sam
  // zawołać window._refreshAuth() w swoim init. Domyślnie odświeżamy od razu.
  if (!window.RADOSKOP_DEFER_CHROME) window._refreshAuth();
})();
