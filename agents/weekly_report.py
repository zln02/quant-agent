"""Weekly automatic report generator (Phase 18)."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from common.env_loader import load_env
from common.logger import get_logger
from common.telegram import send_telegram

load_env()
log = get_logger("weekly_report")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


@dataclass
class WeeklyReportContext:
    weekly_pnl_pct: float
    weekly_pnl_abs: float
    trade_count: int
    factor_summary: str
    regime_change: str
    next_week_plan: str


class WeeklyReportGenerator:
    def is_send_day(self, as_of: Optional[date | datetime | str] = None) -> bool:
        if as_of is None:
            d = date.today()
        elif isinstance(as_of, datetime):
            d = as_of.date()
        elif isinstance(as_of, date):
            d = as_of
        else:
            d = datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date()
        return d.weekday() == 6

    def build_markdown(self, ctx: WeeklyReportContext, week_label: Optional[str] = None) -> str:
        label = week_label or datetime.now(timezone.utc).strftime("%Y-W%W")
        return (
            f"## Weekly Trading Report ({label})\n\n"
            f"- **Weekly PnL**: {ctx.weekly_pnl_pct:+.2f}% ({ctx.weekly_pnl_abs:+,.0f})\n"
            f"- **Trades**: {ctx.trade_count}\n"
            f"- **Factor Contribution**: {ctx.factor_summary}\n"
            f"- **Regime Change**: {ctx.regime_change}\n\n"
            f"### Next Week Plan\n"
            f"{ctx.next_week_plan}\n"
        )

    def collect_context(self) -> WeeklyReportContext:
        return WeeklyReportContext(
            weekly_pnl_pct=0.0,
            weekly_pnl_abs=0.0,
            trade_count=0,
            factor_summary="No major positive or negative factor tilt.",
            regime_change="Stable",
            next_week_plan="Keep diversified exposures and enforce VaR limits.",
        )

    def run(self, send: bool = True) -> dict:
        ctx = self.collect_context()
        report = self.build_markdown(ctx)
        sent = send_telegram(report, parse_mode="Markdown") if send else False
        return {
            "ok": True,
            "sent": bool(sent),
            "report": report,
            "context": ctx.__dict__,
        }


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Weekly report generator")
    parser.add_argument("--no-send", action="store_true")
    args = parser.parse_args()

    out = WeeklyReportGenerator().run(send=not args.no_send)
    log.info(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
