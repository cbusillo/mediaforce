module.exports = {
  content: [
    "./src/mediaforce/web/templates/**/*.html",
    "./src/mediaforce/web/static/js/**/*.js",
  ],
  safelist: [
    { pattern: /^tier-/ },
    { pattern: /^text-(success|warning|danger)$/ },
    { pattern: /^bg-(success|warning|danger)$/ },
    { pattern: /^border-(success|warning|danger)$/ },
  ],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      colors: {
        surface: {
          DEFAULT: "#020617", // darker slate 950
          subtle: "#0f172a", // slate 900
          card: "#1e293b", // slate 800
          muted: "#334155", // slate 700
        },
        foreground: {
          DEFAULT: "#f8fafc", // slate 50
          muted: "#94a3b8", // slate 400
        },
        accent: {
          DEFAULT: "#3b82f6", // sky 500
          hover: "#60a5fa", // sky 400
          light: "#e0f2fe", // sky 100
        },
        success: {
          DEFAULT: "#10b981", // emerald 500
          light: "#d1fae5",
        },
        warning: {
          DEFAULT: "#f59e0b", // amber 500
          light: "#fef3c7",
        },
        danger: {
          DEFAULT: "#ef4444", // red 500
          light: "#fee2e2",
        },
        info: "#06b6d4", // cyan 500
        
        // Tier specific colors
        tier: {
          pristine: "#8b5cf6", // violet 500
          good: "#10b981", // emerald 500
          mediocre: "#f59e0b", // amber 500
          poor: "#ef4444", // red 500
        }
      },
      boxShadow: {
        glow: "0 0 20px -5px rgba(59, 130, 246, 0.5)",
      },
      animation: {
        "pulse-slow": "pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite",
      },
    },
  },
  plugins: [],
};