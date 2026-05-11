import PropTypes from "prop-types";
import { useEffect, useRef, useState } from "react";

export default function AnimatedNumber({
  value = 0,
  prefix = "",
  suffix = "",
  decimals = 0,
  className = "",
}) {
  const [displayValue, setDisplayValue] = useState(0);
  const previousRef = useRef(0);

  useEffect(() => {
    const from = previousRef.current;
    const to = Number(value) || 0;
    const startedAt = performance.now();
    const duration = 900;
    let frame = 0;

    const tick = (now) => {
      const progress = Math.min((now - startedAt) / duration, 1);
      const eased = 1 - (1 - progress) ** 3;
      const next = from + (to - from) * eased;
      setDisplayValue(next);
      if (progress < 1) {
        frame = requestAnimationFrame(tick);
      } else {
        previousRef.current = to;
      }
    };

    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [value]);

  return (
    <span className={`num-tabular ${className}`.trim()}>
      {`${prefix}${Number(displayValue).toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })}${suffix}`}
    </span>
  );
}

AnimatedNumber.propTypes = {
  value: PropTypes.number,
  prefix: PropTypes.string,
  suffix: PropTypes.string,
  decimals: PropTypes.number,
  className: PropTypes.string,
};
