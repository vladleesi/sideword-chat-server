"use strict";

// Runs in the head, before styles are painted. Only appearance is stored here.
(() => {
  const storageKey = "sideword-client-theme";
  const system = window.matchMedia?.("(prefers-color-scheme: dark)");
  let preference = "system";
  let controls = [];
  const valid = value => ["light", "dark", "system"].includes(value);
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (valid(saved)) preference = saved;
  } catch { /* Appearance remains usable when storage is unavailable. */ }

  function apply() {
    const theme = preference === "system" ? (system?.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = theme;
    for (const control of controls) control.checked = control.value === preference;
  }
  apply();
  system?.addEventListener?.("change", () => {
    if (preference === "system") apply();
  });
  window.addEventListener("storage", event => {
    if (event.key !== storageKey && event.key !== null) return;
    preference = valid(event.newValue) ? event.newValue : "system";
    apply();
  });
  function initialize() {
    controls = [...document.querySelectorAll('input[name="appearance"]')];
    apply();
    for (const control of controls) control.addEventListener("change", () => {
      if (!control.checked || !valid(control.value)) return;
      preference = control.value;
      apply();
      try { window.localStorage.setItem(storageKey, preference); }
      catch { /* A failed save must not undo the visible choice. */ }
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else initialize();
})();
