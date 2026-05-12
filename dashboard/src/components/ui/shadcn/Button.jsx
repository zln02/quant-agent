import PropTypes from "prop-types";
import { cn } from "../../../lib/cn";

/**
 * shadcn/ui Button
 * variants: default | outline | ghost | destructive
 * sizes: sm | md | lg
 */

const VARIANT_CLASSES = {
  default: [
    "bg-[var(--accent-purple)] text-white",
    "hover:bg-[#7c3aed]",
    "border border-transparent",
    "shadow-sm",
  ],
  outline: [
    "bg-transparent text-[var(--text-primary)]",
    "border border-white/[0.14]",
    "hover:bg-white/[0.04] hover:border-white/[0.22]",
  ],
  ghost: [
    "bg-transparent text-[var(--text-secondary)]",
    "border border-transparent",
    "hover:bg-white/[0.04] hover:text-[var(--text-primary)]",
  ],
  destructive: [
    "bg-[rgba(var(--color-loss),0.18)] text-[var(--loss)]",
    "border border-[rgba(var(--color-loss),0.35)]",
    "hover:bg-[rgba(var(--color-loss),0.28)]",
  ],
};

const SIZE_CLASSES = {
  sm: "h-8 px-3 text-[12px]",
  md: "h-9 px-4 text-[13px]",
  lg: "h-11 px-6 text-[14px]",
};

export function Button({
  variant = "default",
  size = "md",
  className,
  children,
  disabled = false,
  as: Tag = "button",
  ...props
}) {
  const variantCls = VARIANT_CLASSES[variant] || VARIANT_CLASSES.default;
  const sizeCls = SIZE_CLASSES[size] || SIZE_CLASSES.md;

  return (
    <Tag
      className={cn(
        "inline-flex items-center justify-center gap-2",
        "rounded-[var(--radius-sm)]",
        "font-bold tracking-[0.02em]",
        "transition-colors duration-200",
        "cursor-pointer select-none",
        "focus-visible:outline-2 focus-visible:outline-[var(--accent-purple)] focus-visible:outline-offset-2",
        disabled && "opacity-50 pointer-events-none cursor-not-allowed",
        sizeCls,
        ...variantCls,
        className
      )}
      disabled={Tag === "button" ? disabled : undefined}
      aria-disabled={disabled || undefined}
      {...props}
    >
      {children}
    </Tag>
  );
}

Button.propTypes = {
  variant: PropTypes.oneOf(["default", "outline", "ghost", "destructive"]),
  size: PropTypes.oneOf(["sm", "md", "lg"]),
  className: PropTypes.string,
  children: PropTypes.node,
  disabled: PropTypes.bool,
  as: PropTypes.string,
};
