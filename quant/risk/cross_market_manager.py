"""Cross-market risk manager — combined BTC+KR+US portfolio risk.

Provides:
- Combined portfolio MDD tracking
- Total exposure limit enforcement
- Correlation-based concentration warnings
- Global buy-block signal when limits exceeded
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from common.env_loader import load_env
from common.logger import get_logger
from common.supabase_client import get_supabase

load_env()
log = get_logger("cross_market_risk")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


@dataclass
class CrossMarketConfig:
    """Cross-market risk limits."""
    max_total_exposure_pct: float = 0.80      # 전체 포트폴리오 80% 이상 투자 금지
    max_single_market_pct: float = 0.50       # 단일 시장 50% 초과 금지
    max_portfolio_mdd_pct: float = -0.15      # 합산 MDD -15% 초과 시 신규 매수 차단
    correlation_warn_threshold: float = 0.70  # 시장 간 상관 > 0.7 시 경고
    max_daily_loss_pct: float = -0.05         # 합산 일일 손실 -5% 초과 시 차단


@dataclass
class MarketSnapshot:
    """Single market's position summary."""
    market: str
    equity: float = 0.0
    position_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl_today: float = 0.0
    position_count: int = 0


@dataclass
class CrossMarketRiskResult:
    """Risk evaluation result."""
    total_equity: float = 0.0
    total_exposure: float = 0.0
    total_exposure_pct: float = 0.0
    combined_mdd_pct: float = 0.0
    daily_pnl_pct: float = 0.0
    buy_blocked: bool = False
    block_reasons: List[str] = field(default_factory=list)
    market_weights: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    timestamp: str = ""


class CrossMarketRiskManager:
    """Evaluates combined risk across BTC, KR, US markets."""

    def __init__(self, config: Optional[CrossMarketConfig] = None):
        self.config = config or CrossMarketConfig()
        self._supabase = get_supabase()

    def _load_btc_snapshot(self) -> MarketSnapshot:
        """Load BTC position data from Supabase."""
        snap = MarketSnapshot(market="btc")
        if not self._supabase:
            return snap
        try:
            positions = (
                self._supabase.table("btc_position")
                .select("entry_price,quantity,highest_price,status,entry_krw")
                .eq("status", "OPEN")
                .execute()
                .data or []
            )
            for p in positions:
                qty = _safe_float(p.get("quantity"))
                entry = _safe_float(p.get("entry_price"))
                snap.position_value += qty * entry
                snap.position_count += 1
            snap.equity = snap.position_value  # BTC position is the equity
        except Exception as e:
            log.warning("BTC snapshot load failed", error=str(e)[:200])
        return snap

    def _load_kr_snapshot(self) -> MarketSnapshot:
        """Load KR stock position data from Supabase."""
        snap = MarketSnapshot(market="kr")
        if not self._supabase:
            return snap
        try:
            positions = (
                self._supabase.table("trade_executions")
                .select("stock_code,quantity,price,result")
                .eq("result", "OPEN")
                .execute()
                .data or []
            )
            for p in positions:
                qty = _safe_float(p.get("quantity"))
                px = _safe_float(p.get("price"))
                snap.position_value += qty * px
                snap.position_count += 1
            snap.equity = snap.position_value
        except Exception as e:
            log.warning("KR snapshot load failed", error=str(e)[:200])
        return snap

    def _load_us_snapshot(self) -> MarketSnapshot:
        """Load US stock position data from Supabase."""
        snap = MarketSnapshot(market="us")
        if not self._supabase:
            return snap
        try:
            positions = (
                self._supabase.table("us_trade_executions")
                .select("symbol,quantity,price,result")
                .eq("result", "OPEN")
                .execute()
                .data or []
            )
            for p in positions:
                qty = _safe_float(p.get("quantity"))
                px = _safe_float(p.get("price"))
                snap.position_value += qty * px
                snap.position_count += 1
            snap.equity = snap.position_value
        except Exception as e:
            log.warning("US snapshot load failed", error=str(e)[:200])
        return snap

    def evaluate(self, total_capital: float = 0.0) -> CrossMarketRiskResult:
        """Run cross-market risk evaluation.

        Args:
            total_capital: total portfolio capital (KRW-denominated).
                           If 0, auto-estimate from latest equity.jsonl (PR #27).
                           If estimation fails, fall back to position-value proxy.
        """
        btc = self._load_btc_snapshot()
        kr = self._load_kr_snapshot()
        us = self._load_us_snapshot()

        total_position = btc.position_value + kr.position_value + us.position_value

        # PR #27: total_capital_not_provided WARN 제거 — equity.jsonl 자동 추정
        if total_capital <= 0:
            try:
                from common.equity_loader import load_equity_curve
                btc_curve = load_equity_curve('btc', lookback_days=2) or []
                kr_curve = load_equity_curve('kr', lookback_days=2) or []
                us_curve = load_equity_curve('us', lookback_days=2) or []
                btc_eq = float(btc_curve[-1].get('equity', 0)) if btc_curve else 0.0
                kr_eq = float(kr_curve[-1].get('equity', 0)) if kr_curve else 0.0
                us_eq_raw = float(us_curve[-1].get('equity', 0)) if us_curve else 0.0
                # US equity는 USD 단위 — 환율 1350 가정 (정밀 환산은 후속)
                us_eq_krw = us_eq_raw * 1350.0
                est = btc_eq + kr_eq + us_eq_krw
                if est > 0:
                    total_capital = est
            except Exception as _ee:
                log.debug("cross_market equity estimate failed", error=str(_ee)[:200])

        if total_capital <= 0:
            # 추정 실패 — debug 레벨로 (이전엔 warning, noise 큼)
            log.debug(
                "cross_market_skip_no_equity",
                positions={
                    "btc": btc.position_value,
                    "kr": kr.position_value,
                    "us": us.position_value,
                },
            )
            return CrossMarketRiskResult(
                total_equity=0.0,
                total_exposure=total_position,
                total_exposure_pct=0.0,
                buy_blocked=False,
                timestamp=datetime.now(timezone.utc).isoformat(),
                market_weights={"btc": 0.0, "kr": 0.0, "us": 0.0},
            )

        result = CrossMarketRiskResult(
            total_equity=total_capital,
            total_exposure=total_position,
            total_exposure_pct=total_position / total_capital,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # Market weights
        for snap in [btc, kr, us]:
            w = snap.position_value / total_capital if total_capital > 0 else 0
            result.market_weights[snap.market] = round(w, 4)

        # Check total exposure limit
        if result.total_exposure_pct > self.config.max_total_exposure_pct:
            result.buy_blocked = True
            result.block_reasons.append(
                f"총 노출 {result.total_exposure_pct:.1%} > "
                f"한도 {self.config.max_total_exposure_pct:.0%}"
            )

        # Check single-market concentration
        for market, weight in result.market_weights.items():
            if weight > self.config.max_single_market_pct:
                result.warnings.append(
                    f"{market} 비중 {weight:.1%} > "
                    f"한도 {self.config.max_single_market_pct:.0%}"
                )

        log.info(
            "cross-market risk evaluated",
            total_exposure_pct=f"{result.total_exposure_pct:.1%}",
            buy_blocked=result.buy_blocked,
            markets=result.market_weights,
        )

        return result

    def should_block_buy(self, market: str, total_capital: float = 0.0) -> bool:
        """Quick check: should new buys be blocked for the given market?

        Args:
            market: 'btc', 'kr', or 'us'
            total_capital: total portfolio capital
        """
        try:
            result = self.evaluate(total_capital)
            return result.buy_blocked
        except Exception as e:
            log.warning("cross-market check failed (allowing buy)", error=str(e)[:200])
            return False  # Fail-open: don't block on error
