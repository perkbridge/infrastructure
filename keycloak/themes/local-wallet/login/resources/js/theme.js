(function () {
  const storageKey = "perkbridge.theme";
  const allowedThemes = ["light", "dark"];

  function getSavedTheme() {
    try {
      const savedTheme = window.localStorage.getItem(storageKey);
      if (allowedThemes.includes(savedTheme)) return savedTheme;
    } catch (_) {
      // Storage can be unavailable in privacy-restricted browser contexts.
    }
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }

  function saveTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(storageKey, theme);
    } catch (_) {
      // The selected theme still applies for this page when storage is blocked.
    }
  }

  function updateButton(button) {
    const currentTheme = document.documentElement.dataset.theme;
    const nextTheme = currentTheme === "dark" ? "light" : "dark";
    button.textContent = nextTheme === "light" ? "Light mode" : "Dark mode";
    button.setAttribute("aria-label", `Use ${nextTheme} appearance`);
    button.setAttribute("aria-pressed", String(currentTheme === "light"));
  }

  saveTheme(getSavedTheme());

  document.addEventListener("DOMContentLoaded", function () {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pb-theme-toggle";
    updateButton(button);
    button.addEventListener("click", function () {
      saveTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
      updateButton(button);
    });
    document.body.appendChild(button);
  });
})();
