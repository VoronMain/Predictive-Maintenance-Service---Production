/* spa.js: Common interface module (theme, header/navigation, utilities).
   Loaded in <head> without defer, so window.SPA is available to page scripts.
   Header renders on DOMContentLoaded into <header id="app-header">. */
(function () {
  "use strict";
  const SPA = {};

  /* Theme */
  SPA.getTheme = function () {
    try { return localStorage.getItem("spa-theme") === "dark" ? "dark" : "light"; }
    catch (e) { return "light"; }
  };
  SPA.applyTheme = function (theme) {
    document.documentElement.classList.toggle("dark", theme === "dark");
    updateThemeIcon(theme);
    document.dispatchEvent(new CustomEvent("spa-theme", { detail: { theme } }));
  };
  SPA.toggleTheme = function () {
    const next = SPA.getTheme() === "dark" ? "light" : "dark";
    try { localStorage.setItem("spa-theme", next); } catch (e) {}
    SPA.applyTheme(next);
  };

  function updateThemeIcon(theme) {
    const btn = document.querySelector("[data-theme-toggle]");
    if (!btn) return;
    const dark = (theme || SPA.getTheme()) === "dark";
    btn.innerHTML = dark ? ICON.sun : ICON.moon;
    btn.setAttribute("aria-label", dark ? "Светлая тема" : "Тёмная тема");
    btn.setAttribute("title", dark ? "Светлая тема" : "Тёмная тема");
  }

  /* Configuration (threshold t* and sensor limits) */
  let _cfg = null;
  SPA.config = async function () {
    if (_cfg) return _cfg;
    try {
      const r = await fetch("/config");
      _cfg = r.ok ? await r.json() : { threshold: 0.33, limits: {} };
    } catch (e) { _cfg = { threshold: 0.33, limits: {} }; }
    return _cfg;
  };

  /* Number formatters */
  SPA.num = function (v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(digits === undefined ? 1 : digits);
  };
  SPA.fmtTs = function (ts) {
    if (!ts) return "—";
    const d = new Date(ts);
    if (isNaN(d)) return "—";
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()} - ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  };

  /* Three-color classification */
  // Returns 'ok' | 'warn' | 'crit' | '' based on limits [lo, hi].
  // 10% warning band at boundaries.
  SPA.sensorClass = function (key, val, limits) {
    if (val === null || val === undefined || isNaN(val)) return "";
    const lim = limits && limits[key];
    if (!lim) return "";
    const lo = lim[0], hi = lim[1];
    if ((lo !== null && val < lo) || (hi !== null && val > hi)) return "crit";
    const band = (hi - lo) * 0.1;
    if ((lo !== null && val < lo + band) || (hi !== null && val > hi - band)) return "warn";
    return "ok";
  };
  // Failure probability classification: ≥0.75 = critical, ≥0.5 = warning.
  SPA.probClass = function (p) {
    if (p === null || p === undefined || isNaN(p)) return "";
    if (p >= 0.75) return "crit";
    if (p >= 0.5) return "warn";
    return "ok";
  };

  /* Animated count-up for KPI numbers (eased, integer or fixed-decimal). */
  SPA.countUp = function (el, to, opts) {
    if (!el || to === null || to === undefined || isNaN(to)) return;
    opts = opts || {};
    const dec = opts.decimals || 0;
    const fmt = (v) => Number(v).toFixed(dec);
    const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) { el.textContent = fmt(to); return; }
    const cur = parseFloat(String(el.textContent).replace(/[^\d.-]/g, ""));
    const from = opts.from != null ? opts.from : (isNaN(cur) ? 0 : cur);
    if (from === Number(to)) { el.textContent = fmt(to); return; }
    const dur = opts.duration || 650;
    const ease = (t) => 1 - Math.pow(1 - t, 3);
    const t0 = performance.now();
    cancelAnimationFrame(el._cuRaf);
    const step = (now) => {
      const t = Math.min(1, (now - t0) / dur);
      el.textContent = fmt(from + (to - from) * ease(t));
      if (t < 1) el._cuRaf = requestAnimationFrame(step);
      else el.textContent = fmt(to);
    };
    el._cuRaf = requestAnimationFrame(step);
  };

  SPA.fetchJSON = async function (url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error("HTTP " + r.status + " @ " + url);
    return r.json();
  };

  /* Toast notifications */
  SPA.toast = function (message, isError) {
    let el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.toggle("error", !!isError);
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 2600);
  };

  /* Chart.js colors (theme-dependent) */
  SPA.themeColors = function () {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    return {
      text: v("--muted") || "#5B6577",
      strong: v("--text") || "#1A2233",
      surface: v("--surface") || "#FFFFFF",
      grid: v("--border") || "#E4E8F0",
      brand: v("--brand") || "#243F7F",
      crit: v("--crit") || "#C0392B",
      warn: v("--warn") || "#B26A00",
      ok: v("--ok") || "#1A7A40",
    };
  };

  /* Icons (Lucide, 24×24) */
  const ICON = {
    moon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>',
    sun: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>',
    grid: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    list: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>',
    bell: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
    mail: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 5L2 7"/></svg>',
    phone: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L19 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2Z"/></svg>',
    push: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/></svg>',
    eye: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
    x: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  };
  SPA.icon = function (name) { return ICON[name] || ""; };

  /* Header and navigation */
  const NAV = [
    { key: "overview",  href: "/",            label: "Цех",            icon: "grid" },
    { key: "incidents", href: "/incidents-ui", label: "Журнал отказов", icon: "list" },
    { key: "settings",  href: "/settings",    label: "Настройки",      icon: "bell" },
  ];

  function headerHTML(active) {
    const links = NAV.map((n) =>
      `<a class="nav-link${n.key === active ? " active" : ""}" href="${n.href}" aria-label="${n.label}">
         <span class="nav-ico">${ICON[n.icon]}</span><span class="nav-label">${n.label}</span>
       </a>`).join("");
    return `
      <a class="brand-lockup" href="/" aria-label="ЧКПЗ: home">
        <img src="/static/logo.svg" alt="ЧКПЗ">
        <span class="brand-sub">Система предиктивной аналитики оборудования</span>
      </a>
      <nav style="display:flex;gap:6px;margin-left:auto;flex-wrap:wrap">${links}</nav>
      <button class="icon-btn" data-theme-toggle type="button"></button>`;
  }

  function renderHeader() {
    const el = document.getElementById("app-header");
    if (!el) return;
    el.className = "app-header";
    el.innerHTML = headerHTML(el.dataset.active || "");
    const btn = el.querySelector("[data-theme-toggle]");
    if (btn) btn.addEventListener("click", SPA.toggleTheme);
    updateThemeIcon(SPA.getTheme());
  }

  document.addEventListener("DOMContentLoaded", renderHeader);
  window.SPA = SPA;
})();
