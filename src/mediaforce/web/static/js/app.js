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

  function bindMobileMenu() {
    const btn = document.getElementById("mobileMenuBtn");
    const menu = document.getElementById("mobileMenu");
    if (!btn || !menu) return;

    btn.addEventListener("click", () => {
      menu.classList.toggle("hidden");
    });
  }

  function bindNavScrollHint() {
    const scrollEl = document.getElementById("navScroll");
    const hintLeft = document.querySelector(".nav-scroll-hint--left");
    const hintRight = document.querySelector(".nav-scroll-hint--right");
    if (!scrollEl || (!hintLeft && !hintRight)) return;

    const update = () => {
      const canScroll = scrollEl.scrollWidth > scrollEl.clientWidth + 2;
      const atStart = scrollEl.scrollLeft <= 2;
      const atEnd = scrollEl.scrollLeft >= scrollEl.scrollWidth - scrollEl.clientWidth - 2;

      const apply = (el, show) => {
        if (!el) return;
        el.classList.toggle("nav-scroll-hint--hidden", !show);
      };

      if (!canScroll) {
        apply(hintLeft, false);
        apply(hintRight, false);
        return;
      }

      apply(hintLeft, !atStart);
      apply(hintRight, !atEnd);
    };

    scrollEl.addEventListener("scroll", update);
    window.addEventListener("resize", update);
    update();
  }

  initTheme();
  window.addEventListener("DOMContentLoaded", () => {
    bindThemeToggle();
    bindMobileMenu();
    bindNavScrollHint();
  });
})();
