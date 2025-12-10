module.exports = {
  content: ['./src/mediaforce/web/templates/**/*.html'],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
      },
      colors: {
        surface: {
          DEFAULT: '#0f172a',
          subtle: '#111827',
          card: '#1f2937',
        },
        foreground: {
          DEFAULT: '#e5e7eb',
          muted: '#9ca3af',
        },
        accent: {
          DEFAULT: '#e94560',
          hover: '#ff6b6b',
        },
        success: '#22c55e',
        warning: '#f59e0b',
        info: '#0ea5e9',
      },
    },
  },
  plugins: [],
};
