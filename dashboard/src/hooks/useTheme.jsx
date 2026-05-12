import { useState, useEffect, useCallback } from "react";

const STORAGE_KEY = "openclaw-theme";
const VALID_THEMES = ["light", "dark"];

/**
 * Resolve initial theme: localStorage > prefers-color-scheme > default
 * Exported so main.jsx can reuse the same logic for FOUC-prevention init.
 */
export function getInitialTheme() {
  if (typeof window === "undefined") return "light";

  const stored = localStorage.getItem(STORAGE_KEY);
  if (VALID_THEMES.includes(stored)) return stored;

  if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
    return "dark";
  }
  return "light";
}

/**
 * Apply theme to <html> data-theme attribute.
 * Used both inside hook and pre-render (main.jsx) for FOUC prevention.
 */
export function applyTheme(theme) {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", theme);
}

/**
 * useTheme hook
 * - Reads/writes localStorage
 * - Syncs document.documentElement[data-theme]
 * - Responds to system preference changes (when no explicit user choice)
 */
export function useTheme() {
  const [theme, setThemeState] = useState(getInitialTheme);

  // Apply theme on mount + change
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  // Sync with system preference (only if user hasn't explicitly chosen)
  useEffect(() => {
    if (typeof window === "undefined") return;

    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) return;  // User has explicit preference, ignore system

    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = (e) => setThemeState(e.matches ? "dark" : "light");

    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, []);

  const setTheme = useCallback((next) => {
    if (!VALID_THEMES.includes(next)) return;
    localStorage.setItem(STORAGE_KEY, next);
    setThemeState(next);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === "light" ? "dark" : "light");
  }, [theme, setTheme]);

  return { theme, setTheme, toggleTheme };
}
