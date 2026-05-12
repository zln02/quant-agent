import { Sun, Moon } from "lucide-react";
import { useTheme } from "../../hooks/useTheme";

/**
 * Light/Dark theme toggle button.
 * - Icon switches based on current theme (Sun in dark mode → switch to light)
 * - Accessible: aria-label + title
 * - Compact size for header bar
 *
 * TODO(PR #D2): tailwind config text 토큰 → var(--text-primary) 참조로 SSOT 완성.
 * 현재 text-text-* 는 다크 hex 하드코딩이라 라이트 모드 가독성 미흡.
 */
export function ThemeToggle({ className = "" }) {
  const { theme, toggleTheme } = useTheme();
  const isDark = theme === "dark";

  return (
    <button
      type="button"
      onClick={toggleTheme}
      className={`inline-flex items-center justify-center
        w-9 h-9 rounded-md
        text-text-secondary hover:text-text-primary
        hover:bg-[var(--bg-hover)]
        transition-colors duration-150 ${className}`}
      aria-label={isDark ? "라이트 모드로 전환" : "다크 모드로 전환"}
      title={isDark ? "라이트 모드" : "다크 모드"}
    >
      {isDark ? <Sun size={18} /> : <Moon size={18} />}
    </button>
  );
}
