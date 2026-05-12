import PropTypes from "prop-types";
import { cn } from "../../../lib/cn";

/**
 * shadcn/ui Table 컴포넌트 세트
 * Table, TableHeader, TableBody, TableRow, TableCell, TableCaption
 * 기존 .table-shell 스타일을 Tailwind로 재현.
 */

export function Table({ className, children, ...props }) {
  return (
    <div
      className={cn(
        "overflow-auto rounded-[var(--radius-md)]",
        "border border-white/[0.06]",
        "bg-white/[0.015]",
        className
      )}
    >
      <table
        className="w-full border-collapse text-[13px]"
        {...props}
      >
        {children}
      </table>
    </div>
  );
}
Table.propTypes = {
  className: PropTypes.string,
  children: PropTypes.node,
};

export function TableHeader({ className, children, ...props }) {
  return (
    <thead className={cn(className)} {...props}>
      {children}
    </thead>
  );
}
TableHeader.propTypes = {
  className: PropTypes.string,
  children: PropTypes.node,
};

export function TableBody({ className, children, ...props }) {
  return (
    <tbody className={cn(className)} {...props}>
      {children}
    </tbody>
  );
}
TableBody.propTypes = {
  className: PropTypes.string,
  children: PropTypes.node,
};

export function TableRow({ className, isActive = false, children, ...props }) {
  return (
    <tr
      className={cn(
        "border-b border-white/[0.05]",
        "transition-colors duration-150",
        "last:border-b-0",
        isActive
          ? "bg-[rgba(var(--color-neutral),0.08)]"
          : "hover:bg-white/[0.03]",
        className
      )}
      {...props}
    >
      {children}
    </tr>
  );
}
TableRow.propTypes = {
  className: PropTypes.string,
  isActive: PropTypes.bool,
  children: PropTypes.node,
};

export function TableCell({ className, header = false, children, ...props }) {
  const Tag = header ? "th" : "td";
  return (
    <Tag
      className={cn(
        "px-[14px] py-[12px] text-left align-middle",
        header && [
          "text-[12px] font-bold tracking-[0.06em] uppercase",
          "text-[var(--text-secondary)]",
        ],
        className
      )}
      {...props}
    >
      {children}
    </Tag>
  );
}
TableCell.propTypes = {
  className: PropTypes.string,
  header: PropTypes.bool,
  children: PropTypes.node,
};

export function TableCaption({ className, children, ...props }) {
  return (
    <caption
      className={cn(
        "mt-2 text-[12px] text-[var(--text-secondary)] text-left px-[14px] pb-[8px]",
        className
      )}
      {...props}
    >
      {children}
    </caption>
  );
}
TableCaption.propTypes = {
  className: PropTypes.string,
  children: PropTypes.node,
};
