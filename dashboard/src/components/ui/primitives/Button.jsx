import PropTypes from "prop-types";
import { forwardRef } from "react";
import { cva } from "class-variance-authority";
import { cn } from "../../../lib/cn";

const buttonVariants = cva(
  [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl font-medium",
    "transition-[background,color,border-color,box-shadow,transform] duration-150",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--accent-us)] focus-visible:ring-offset-0",
    "disabled:opacity-50 disabled:pointer-events-none",
    "active:scale-[0.98]",
  ].join(" "),
  {
    variants: {
      variant: {
        primary:
          "bg-[color:var(--accent-us)] text-white shadow-[var(--elev-1)] hover:brightness-[1.08]",
        secondary:
          "bg-[color:var(--bg-overlay)] text-[color:var(--text-primary)] shadow-[var(--elev-1)] hover:bg-white/[0.07]",
        outline:
          "border border-[color:var(--border-default)] text-[color:var(--text-primary)] hover:bg-white/[0.04]",
        ghost:
          "text-[color:var(--text-secondary)] hover:text-[color:var(--text-primary)] hover:bg-white/[0.05]",
        danger:
          "bg-[color:var(--color-loss)] text-white shadow-[var(--elev-1)] hover:brightness-[1.08]",
        link:
          "text-[color:var(--accent-us)] underline-offset-4 hover:underline",
      },
      size: {
        sm:   "h-7 px-2.5 text-xs",
        md:   "h-9 px-3.5 text-sm",
        lg:   "h-11 px-4 text-sm",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

const Button = forwardRef(function Button(
  { className, variant, size, type = "button", ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  );
});

Button.propTypes = {
  className: PropTypes.string,
  variant: PropTypes.oneOf(["primary", "secondary", "outline", "ghost", "danger", "link"]),
  size: PropTypes.oneOf(["sm", "md", "lg", "icon"]),
  type: PropTypes.oneOf(["button", "submit", "reset"]),
};

export { Button, buttonVariants };
