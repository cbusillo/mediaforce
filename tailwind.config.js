module.exports = {
  content: ["./src/mediaforce/web/templates/**/*.html"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      colors: {
        surface: {
          DEFAULT: "#0f172a",
          subtle: "#111827",
          card: "#1f2937",
          muted: "#0b1220",
        },
        foreground: {
          DEFAULT: "#e5e7eb",
          muted: "#9ca3af",
        },
        accent: {
          DEFAULT: "#e94560",
          hover: "#ff6b6b",
        },
        success: "#22c55e",
        warning: "#f59e0b",
        info: "#0ea5e9",
        danger: "#ef4444",
      },
      boxShadow: {
        card: "0 10px 30px -16px rgba(0,0,0,0.6)",
        elevated: "0 18px 50px -24px rgba(0,0,0,0.65)",
      },
      borderRadius: {
        xl: "0.9rem",
      },
      maxWidth: {
        content: "80rem",
      },
      keyframes: {
        fade: {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        slideup: {
          "0%": { opacity: "0", transform: "translateY(12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "fade-in": "fade 200ms ease-out both",
        "slide-up": "slideup 200ms ease-out both",
      },
    },
  },
  plugins: [],
};
