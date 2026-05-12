import PropTypes from "prop-types";
import { cn } from "../../../lib/cn";

/**
 * shadcn/ui Card 패턴
 * 기존 glass-card CSS 변수를 재활용하면서 shadcn API 방식으로 구성.
 */

export function Card({ className, children, style, ...props }) {
  return (
    <div
      className={cn(
        "card-reveal",
        "rounded-[var(--radius-lg)]",
        "border border-white/[0.07]",
        "bg-[var(--bg-card)]",
        "shadow-[var(--shadow-lg)]",
        "backdrop-blur-[var(--glass-blur)]",
        "relative overflow-hidden",
        "transition-shadow duration-300",
        className
      )}
      style={style}
      {...props}
    >
      {/* 상단 광택 레이어 */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background: "linear-gradient(180deg, rgba(255,255,255,0.025), transparent 30%)",
        }}
        aria-hidden="true"
      />
      {children}
    </div>
  );
}
Card.propTypes = {
  className: PropTypes.string,
  children: PropTypes.node,
  style: PropTypes.object,
};

export function CardHeader({ className, children, ...props }) {
  return (
    <div
      className={cn(
        "flex items-center justify-between gap-3",
        "px-5 pt-5 pb-3",
        "border-b border-white/[0.06]",
        className
      )}
      {...props}
    >
      {children}
    </div>
  );
}
CardHeader.propTypes = {
  className: PropTypes.string,
  children: PropTypes.node,
};

export function CardTitle({ className, children, as: Tag = "h2", ...props }) {
  return (
    <Tag
      className={cn(
        "m-0 text-[14px] font-bold tracking-[0.03em] uppercase",
        "text-[var(--text-primary)]",
        className
      )}
      {...props}
    >
      {children}
    </Tag>
  );
}
CardTitle.propTypes = {
  className: PropTypes.string,
  children: PropTypes.node,
  as: PropTypes.string,
};

export function CardContent({ className, children, ...props }) {
  return (
    <div
      className={cn("px-5 py-4", className)}
      {...props}
    >
      {children}
    </div>
  );
}
CardContent.propTypes = {
  className: PropTypes.string,
  children: PropTypes.node,
};

export function CardFooter({ className, children, ...props }) {
  return (
    <div
      className={cn(
        "flex items-center gap-3",
        "px-5 py-3 pt-3",
        "border-t border-white/[0.06]",
        className
      )}
      {...props}
    >
      {children}
    </div>
  );
}
CardFooter.propTypes = {
  className: PropTypes.string,
  children: PropTypes.node,
};
