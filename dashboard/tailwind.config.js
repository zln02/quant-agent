/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  // darkMode removed: theme uses CSS custom properties, not Tailwind dark: utilities
  theme: {
    extend: {
      colors: {
        shell: "var(--bg-primary)",
        panel: "var(--bg-secondary)",
        elevated: "var(--bg-elevated)",
        border: "var(--border-default)",
        text: {
          primary: "var(--text-primary)",
          secondary: "var(--text-secondary)",
          muted: "var(--text-muted)",
        },
        profit: "var(--color-profit)",
        loss: "var(--color-loss)",
        warning: "var(--color-warning)",
        info: "var(--color-info)",
        btc: "var(--accent-btc)",
        kr: "var(--accent-kr)",
        us: "var(--accent-us)",
        agents: "var(--accent-agents)",
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
      },
      fontSize: {
        "2xs": ["var(--text-2xs)", { lineHeight: "var(--leading-snug)" }],
        xs:   ["var(--text-xs)",   { lineHeight: "var(--leading-snug)" }],
        sm:   ["var(--text-sm)",   { lineHeight: "var(--leading-normal)" }],
        base: ["var(--text-base)", { lineHeight: "var(--leading-normal)" }],
        md:   ["var(--text-md)",   { lineHeight: "var(--leading-normal)" }],
        lg:   ["var(--text-lg)",   { lineHeight: "var(--leading-snug)" }],
        xl:   ["var(--text-xl)",   { lineHeight: "var(--leading-snug)" }],
        "2xl":["var(--text-2xl)",  { lineHeight: "var(--leading-tight)" }],
        "3xl":["var(--text-3xl)",  { lineHeight: "var(--leading-tight)" }],
        "4xl":["var(--text-4xl)",  { lineHeight: "var(--leading-tight)" }],
      },
      letterSpacing: {
        tighter: "var(--tracking-tighter)",
        tight:   "var(--tracking-tight)",
        normal:  "var(--tracking-normal)",
        wide:    "var(--tracking-wide)",
        widest:  "var(--tracking-widest)",
      },
      spacing: {
        // Token-bound spacing; coexists with Tailwind defaults (1=0.25rem, 4=1rem, etc.).
        // Keys prefixed `t-` to avoid collision with default numeric scale.
        "t-1":  "var(--space-1)",
        "t-2":  "var(--space-2)",
        "t-3":  "var(--space-3)",
        "t-4":  "var(--space-4)",
        "t-5":  "var(--space-5)",
        "t-6":  "var(--space-6)",
        "t-8":  "var(--space-8)",
        "t-10": "var(--space-10)",
        "t-12": "var(--space-12)",
        "t-16": "var(--space-16)",
        "t-20": "var(--space-20)",
      },
      borderRadius: {
        card: "var(--panel-radius)",
      },
      boxShadow: {
        panel: "var(--shadow-panel)",
        "elev-1": "var(--elev-1)",
        "elev-2": "var(--elev-2)",
        "elev-3": "var(--elev-3)",
        "elev-pop": "var(--elev-pop)",
      },
    },
  },
  plugins: [],
};
