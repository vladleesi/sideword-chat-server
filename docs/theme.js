"use strict";

(() => {
  const storageKey = "sideword-site-theme";
  const systemTheme = window.matchMedia?.("(prefers-color-scheme: dark)");
  let preference = null;
  let currentTheme;
  let toggle;

  try {
    const stored = window.localStorage.getItem(storageKey);
    if (stored === "light" || stored === "dark") preference = stored;
  } catch {
    // Storage may be disabled; switching remains available for this page.
  }

  function applyTheme(theme) {
    currentTheme = theme;
    document.documentElement.dataset.theme = theme;
    const color = theme === "dark" ? "#111210" : "#F1F0EA";
    for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
      meta.content = color;
    }
    if (toggle) {
      const nextTheme = theme === "dark" ? "light" : "dark";
      toggle.textContent = nextTheme === "dark" ? "Dark" : "Light";
      toggle.setAttribute("aria-label", `Switch to ${nextTheme} theme`);
      toggle.title = `Switch to ${nextTheme} theme`;
    }
  }

  applyTheme(preference || (systemTheme?.matches ? "dark" : "light"));
  systemTheme?.addEventListener?.("change", (event) => {
    if (!preference) applyTheme(event.matches ? "dark" : "light");
  });

  function initializeToggle() {
    toggle = document.querySelector("#theme-toggle");
    if (!toggle) return;
    applyTheme(currentTheme);
    toggle.addEventListener("click", () => {
      preference = currentTheme === "dark" ? "light" : "dark";
      applyTheme(preference);
      try {
        window.localStorage.setItem(storageKey, preference);
      } catch {
        // A blocked preference save must not undo the visible choice.
      }
    });
    toggle.hidden = false;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeToggle, { once: true });
  } else {
    initializeToggle();
  }
})();
