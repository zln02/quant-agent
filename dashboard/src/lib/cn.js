import clsx from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Conditional className merger with Tailwind conflict resolution.
 * Standard shadcn/ui helper — accepts clsx-style inputs.
 *   cn("p-4", isActive && "bg-blue-500", className)
 */
export function cn(...inputs) {
  return twMerge(clsx(inputs));
}
