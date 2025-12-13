// Base behaviors shared across pages (theme toggle).
(function () {
  function initTheme() {
    const stored = localStorage.getItem("mf-theme");
    if (stored === "light") {
      document.documentElement.classList.remove("dark");
    } else {
      document.documentElement.classList.add("dark");
    }
  }

  function bindThemeToggle() {
    const toggleBtn = document.getElementById("themeToggle");
    const label = document.getElementById("themeLabel");
    if (!toggleBtn) return;

    const setLabel = () => {
      if (!label) return;
      label.textContent = document.documentElement.classList.contains("dark") ? "Dark" : "Light";
    };
    setLabel();

    toggleBtn.addEventListener("click", () => {
      document.documentElement.classList.toggle("dark");
      const mode = document.documentElement.classList.contains("dark") ? "dark" : "light";
      localStorage.setItem("mf-theme", mode);
      toggleBtn.setAttribute("aria-pressed", mode === "dark");
      setLabel();
    });
    toggleBtn.setAttribute("aria-pressed", document.documentElement.classList.contains("dark"));
  }

  initTheme();
  window.addEventListener("DOMContentLoaded", bindThemeToggle);
})();

