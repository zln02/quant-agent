import PropTypes from "prop-types";
import { cn } from "../../../lib/cn";

/**
 * shadcn/ui Badge
 * variants: default | profit | loss | warning | neutral
 * 기존 CSS 변수 (--profit, --loss, --warning, --color-*) 재사용.
 */

const VARIANT_CLASSES = {
  default: [
    "bg-white/[0.06] text-[var(--text-primary)]",
    "border border-white/[0.1]",
  ],
  profit: [
    "bg-[rgba(var(--color-gain),0.16)] text-[var(--profit)]",
    "border border-[rgba(var(--color-gain),0.35)]",
  ],
  loss: [
    "bg-[rgba(var(--color-loss),0.16)] text-[var(--loss)]",
    "border border-[rgba(var(--color-loss),0.35)]",
  ],
  warning: [
    "bg-[rgba(var(--color-warning),0.16)] text-[var(--warning)]",
    "border border-[rgba(var(--color-warning),0.35)]",
  ],
  neutral: [
    "bg-[rgba(var(--color-neutral),0.14)] text-[var(--accent-purple)]",
    "border border-[rgba(var(--color-neutral),0.3)]",
  ],
};

export function Badge({ variant = "default", className, children, dot = false, pulse = false, ...props }) {
  const variantCls = VARIANT_CLASSES[variant] || VARIANT_CLASSES.default;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-[6px]",
        "px-[10px] py-[4px]",
        "rounded-[var(--radius-sm)]",
        "text-[12px] font-bold tracking-[0.04em] uppercase",
        "leading-none select-none",
        ...variantCls,
        className
      )}
      {...props}
    >
      {dot && (
        <span
          className={cn(
            "w-[7px] h-[7px] rounded-full shrink-0",
            variant === "profit" && "bg-[var(--profit)]",
            variant === "loss" && "bg-[var(--loss)]",
            variant === "warning" && "bg-[var(--warning)]",
            variant === "neutral" && "bg-[var(--accent-purple)]",
            (variant === "default" || !VARIANT_CLASSES[variant]) && "bg-[var(--text-secondary)]",
            pulse && "animate-[livePulse_1.5s_infinite]"
          )}
          aria-hidden="true"
        />
      )}
      {children}
    </span>
  );
}

Badge.propTypes = {
  variant: PropTypes.oneOf(["default", "profit", "loss", "warning", "neutral"]),
  className: PropTypes.string,
  children: PropTypes.node,
  dot: PropTypes.bool,
  pulse: PropTypes.bool,
};
