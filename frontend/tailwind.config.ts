import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        ink: "#12201c",
        canvas: "#f5f7f4",
        mint: "#b8f1d4",
        lime: "#dfff7f",
        forest: "#143c2c",
        line: "#dce6df",
        panel: "var(--panel-solid)",
        frame: "var(--frame-bg)",
        muted: "var(--muted)",
        warning: "#926b0c",
        "danger-line": "var(--danger-line)",
        "danger-surface": "var(--danger-surface)",
      },
      boxShadow: {
        soft: "0 18px 50px rgba(20, 60, 44, .08)",
      },
      fontFamily: {
        sans: ["var(--font-geist-sans)", "ui-sans-serif", "system-ui"],
        mono: ["var(--font-geist-mono)", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
