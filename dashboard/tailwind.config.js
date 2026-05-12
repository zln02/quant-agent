/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        background: "#0f1117",
        card: "#1a1d29",
        border: "#2a2d3a",
        text: {
          primary: "#e4e4eb",
          secondary: "#8b8d9a",
          muted: "#6b6d7a"
        },
        // KR 시그널 — tokens.css의 RGB-split 토큰을 SSOT로 참조
        profit: "rgb(var(--color-gain) / <alpha-value>)",
        loss: "rgb(var(--color-loss) / <alpha-value>)",
        accent: "rgb(var(--toss-blue-rgb) / <alpha-value>)"
      },
      fontFamily: {
        sans: ["Pretendard Variable", "Pretendard", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"]
      },
      borderRadius: {
        card: "12px",
        lg: "16px"
      },
      boxShadow: {
        card: "0 4px 6px -1px rgba(0, 0, 0, 0.3), 0 2px 4px -1px rgba(0, 0, 0, 0.2)"
      }
    },
  },
  plugins: [],
}
