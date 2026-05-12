import PropTypes from "prop-types";
import { cn } from "../../../lib/cn";

/**
 * shadcn/ui Separator
 * orientation: "horizontal" (기본) | "vertical"
 */

export function Separator({ orientation = "horizontal", className, decorative = true, ...props }) {
  return (
    <div
      role={decorative ? "none" : "separator"}
      aria-orientation={decorative ? undefined : orientation}
      className={cn(
        "shrink-0 bg-white/[0.07]",
        orientation === "horizontal"
          ? "h-px w-full"
          : "h-full w-px",
        className
      )}
      {...props}
    />
  );
}

Separator.propTypes = {
  orientation: PropTypes.oneOf(["horizontal", "vertical"]),
  className: PropTypes.string,
  decorative: PropTypes.bool,
};
