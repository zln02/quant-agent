#!/usr/bin/env python3
"""
미국 주식 자동매매 에이전트 v2.0 (Top-tier Quant)

v2 변경사항:
- [NEW] 멀티팩터 스코어 (모멘텀+밸류+퀄리티) — 기존 순수 모멘텀에서 확장
- [NEW] 어닝 캘린더 필터 — 발표 5일 전 매수 차단
- [NEW] 변동성 조절 포지션 사이징
- [NEW] 섹터 분산 (동일 섹터 max 2종목)
- [IMPROVE] 복합 스코어에 밸류/퀄리티 반영

실행:
    .venv/bin/python stocks/us_stock_trading_agent.py          # 매매 사이클
    .venv/bin/python stocks/us_stock_trading_agent.py check    # 손절/익절만 체크
    .venv/bin/python stocks/us_stock_trading_agent.py status   # 보유 현황
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo  # v6.2 B2: ET 시간대 통일

import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config import US_TRADING_LOG
from common.env_loader import load_env
from common.equity_loader import (append_equity_snapshot,
                                  get_effective_market_weight,
                                  load_equity_curve, load_recent_trades,
                                  save_drawdown_state)
from common.logger import get_logger
from common.supabase_client import get_supabase
from common.telegram import send_telegram as _tg_send
from common.utils import check_order_idempotency, generate_order_id
from execution.smart_router import SmartRouter
from quant.risk.drawdown_guard import DrawdownGuard
from quant.risk.drawdown_state_store import DrawdownStateStore
from quant.risk.position_sizer import KellyPositionSizer

try:
    from common.sheets_logger import append_trade as _sheets_append
except ImportError:
    _sheets_append = None

try:
    from stocks.us_broker import AlpacaBroker as _AlpacaBroker
except ImportError:
    _AlpacaBroker = None

try:
    from quant.drift_detector import \
        ConceptDriftDetector as _ConceptDriftDetector
except Exception:
    _ConceptDriftDetector = None

# v6: SmartRouter 인스턴스 (US 모의투자용 — 슬리피지 추적 + 주문 크기별 라우팅 로깅)
_smart_router = SmartRouter()

load_env()
_log = get_logger("us_agent", US_TRADING_LOG)

sys.path.insert(0, str(Path(__file__).parent))
from us_momentum_backtest import US_UNIVERSE, scan_today_top_us

supabase = get_supabase()

# ─────────────────────────────────────────────
# 리스크 설정 (미주용)
# ─────────────────────────────────────────────
RISK = {
    "stop_loss": -0.035,
    "take_profit": 0.10,
    "partial_tp_pct": 0.06,
    "partial_tp_ratio": 0.50,
    "trailing_stop": 0.02,
    "trailing_activate": 0.025,
    "trailing_adaptive": True,
    "max_positions": 5,
    "max_trades_per_day": 3,
    "min_score": 50,
    "min_order_usd": 50,
    "fee_rate": 0.001,
    "timecut_days": 12,
    "virtual_capital": 10000,
    "invest_ratio_A": 0.30,
    "invest_ratio_B": 0.20,
    "invest_ratio_C": 0.15,
    "market_regime_filter": True,
    "vix_max": 35,
    "relative_strength": True,
    "multifactor": True,          # 밸류+퀄리티 팩터 반영
    "earnings_filter": True,      # 어닝 5일 전 매수 차단
    "max_sector_positions": 2,    # 동일 섹터 최대 2종목
    "volatility_sizing": True,    # ATR 기반 포지션 사이징
    "max_hold_days": 20,           # v6: 좀비 포지션 하드 컷오프
    "indicator_fail_max": 3,       # v6: 지표 N회 연속 실패 시 강제 매도
    "max_daily_loss": -0.08,       # audit fix: 일일 손실 한도 (-8%)
}

RULES = {
    "buy_composite_min": 50,
    "buy_rsi_hard_max": 80,
    "buy_vol_hard_min": 0.3,
    "sell_rsi_min": 78,
    "momentum_accel_bonus": True,
}

US_TRADE_TABLE = "us_trade_executions"
STOP_FLAG = Path(__file__).parent / "US_STOP_TRADING"

_us_buy_blocked = False
_us_drift_cache: Dict = {}
# audit fix: CrossMarket 리스크 — 모듈 레벨 싱글턴 (매 사이클 재사용)
_cmr_instance = None
# P1-10: ConceptDriftDetector US 연동
_us_drift_detector = _ConceptDriftDetector() if _ConceptDriftDetector is not None else None


def _get_us_ml_signal(symbol: str) -> dict:
    try:
        from us_ml_model import get_ml_signal

        return get_ml_signal(symbol)
    except Exception as e:
        return {"action": "HOLD", "confidence": 0.0, "source": f"US_ML_ERROR: {e}"}


def _load_us_ml_drift_report(force: bool = False) -> dict:
    global _us_drift_cache
    if _us_drift_cache and not force:
        return _us_drift_cache
    path = Path(__file__).resolve().parents[1] / "brain" / "ml" / "us" / "drift_report.json"
    if not path.exists():
        _us_drift_cache = {}
        return _us_drift_cache
    try:
        import json

        _us_drift_cache = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _us_drift_cache = {}
    return _us_drift_cache


def _apply_us_drift_gate(signal: dict) -> dict:
    report = _load_us_ml_drift_report()
    if not report:
        return signal

    status = str(report.get("status", "UNKNOWN")).upper()
    max_psi = float(report.get("max_psi", 0.0) or 0.0)
    high_psi_count = int(report.get("high_psi_count", 0) or 0)
    adjusted = dict(signal)
    base_conf = float(adjusted.get("confidence", 0.0) or 0.0)
    reason = str(adjusted.get("reason", ""))

    if status == "WARNING":
        adjusted["confidence"] = max(0.0, round(base_conf - 6.0, 1))
        adjusted["reason"] = (reason + f" [US_ML_DRIFT:WARNING psi={max_psi:.2f}]").strip()
        adjusted["drift_status"] = status
        adjusted["drift_penalty"] = 6.0
        return adjusted

    if status == "DANGER":
        if max_psi >= 0.75 or high_psi_count >= 8:
            adjusted["action"] = "HOLD"
            adjusted["confidence"] = 0.0
            adjusted["reason"] = (reason + f" [US_ML_DRIFT_BLOCK psi={max_psi:.2f}]").strip()
            adjusted["drift_status"] = status
            adjusted["drift_penalty"] = 100.0
            return adjusted
        adjusted["confidence"] = max(0.0, round(base_conf - 12.0, 1))
        adjusted["reason"] = (reason + f" [US_ML_DRIFT:DANGER psi={max_psi:.2f}]").strip()
        adjusted["drift_status"] = status
        adjusted["drift_penalty"] = 12.0
        return adjusted

    adjusted["drift_status"] = status
    adjusted["drift_penalty"] = 0.0
    return adjusted


# ─────────────────────────────────────────────
# 유틸리티
# ─────────────────────────────────────────────
def log(msg: str, level: str = "INFO"):
    """Backward-compat wrapper routing to structured logger."""
    _dispatch = {
        "INFO": _log.info, "WARN": _log.warning,
        "ERROR": _log.error, "TRADE": _log.trade,
    }
    _dispatch.get(level, _log.info)(msg)


def send_telegram(msg: str):
    _tg_send(msg)


def is_us_market_open() -> bool:
    """미국장 개장 여부 (미국 동부 시간 ET 기준 09:30~16:00, 서머타임 자동 반영).
    # v6.2 B2: KST 기준 하드코딩 → ET(America/New_York) ZoneInfo 기준으로 교체
    """
    _ET = ZoneInfo("America/New_York")
    now_et = datetime.now(_ET)
    # 주말 제외
    if now_et.weekday() >= 5:
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et < market_close


# ─────────────────────────────────────────────
# 시장 레짐 필터 (SPY 200MA + VIX) — common 모듈 위임
# ─────────────────────────────────────────────
from common.market_data import get_market_regime  # noqa: E402


def calc_relative_strength(symbol: str, days: int = 20) -> float:
    """종목의 SPY 대비 상대강도. 1.0 이상이면 아웃퍼폼."""
    try:
        data = yf.download([symbol, "SPY"], period=f"{days + 5}d", progress=False)
        if data.empty:
            return 1.0
        close = data["Close"]
        if symbol not in close.columns or "SPY" not in close.columns:
            return 1.0
        sym_ret = float(close[symbol].iloc[-1] / close[symbol].iloc[-days] - 1)
        spy_ret = float(close["SPY"].iloc[-1] / close["SPY"].iloc[-days] - 1)
        if spy_ret == 0:
            return 1.0
        return round(sym_ret / spy_ret if spy_ret > 0 else sym_ret - spy_ret + 1, 2)
    except Exception:
        return 1.0


# ─────────────────────────────────────────────
# 시장/지표 데이터
# ─────────────────────────────────────────────
_yf_cache: Dict[str, tuple] = {}  # v6.2 B5: {symbol: (data, timestamp)}
_YF_CACHE_TTL = 300  # v6.2 B5: 5분 TTL


def get_us_indicators(symbol: str) -> Optional[dict]:
    """yfinance에서 일봉 기반 RSI/BB/거래량 지표 계산."""
    # v6.2 B5: yf_cache TTL
    now = time.time()
    if symbol in _yf_cache:
        cached_data, cached_ts = _yf_cache[symbol]
        if now - cached_ts < _YF_CACHE_TTL:
            return cached_data

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="90d")
        if hist is None or len(hist) < 30:
            return None

        close = hist["Close"]
        high = hist["High"]
        volume = hist["Volume"]
        price = float(close.iloc[-1])

        rsi_s = RSIIndicator(close=close, window=14).rsi()
        rsi = float(rsi_s.iloc[-1]) if not pd.isna(rsi_s.iloc[-1]) else 50.0

        bb = BollingerBands(close=close, window=20, window_dev=2)
        bb_upper = float(bb.bollinger_hband().iloc[-1])
        bb_lower = float(bb.bollinger_lband().iloc[-1])
        bb_width = bb_upper - bb_lower
        bb_pos = ((price - bb_lower) / bb_width * 100) if bb_width > 0 else 50.0

        vol_20 = float(volume.tail(20).mean())
        vol_5 = float(volume.tail(5).mean())
        vol_ratio = (vol_5 / vol_20) if vol_20 > 0 else 1.0

        high_60d = float(high.tail(60).max())
        near_high = (price / high_60d * 100) if high_60d > 0 else 50.0

        result = {
            "price": price,
            "rsi": round(rsi, 1),
            "bb_pos": round(bb_pos, 1),
            "vol_ratio": round(vol_ratio, 2),
            "near_high": round(near_high, 1),
            "high_60d": high_60d,
        }
        _yf_cache[symbol] = (result, now)  # v6.2 B5: yf_cache TTL
        return result
    except Exception as e:
        log(f"{symbol}: 지표 조회 실패: {e}", "WARN")
        return None


# ─────────────────────────────────────────────
# Supabase DB (포지션 관리)
# ─────────────────────────────────────────────
def get_open_positions() -> List[dict]:
    if not supabase:
        return []
    try:
        res = (
            supabase.table(US_TRADE_TABLE)
            .select("*")
            .eq("result", "OPEN")
            .execute()
        )
        return res.data or []
    except Exception as e:
        log(f"포지션 조회 실패: {e}", "WARN")
        return []


def get_position_for_symbol(symbol: str) -> List[dict]:
    return [p for p in get_open_positions() if p.get("symbol") == symbol]


def count_today_buys() -> int:
    if not supabase:
        return 0
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        res = (
            supabase.table(US_TRADE_TABLE)
            .select("id")
            .eq("trade_type", "BUY")
            .gte("created_at", today)
            .execute()
        )
        return len(res.data or [])
    except Exception:
        return 0


def save_trade(trade_type: str, symbol: str, quantity: float, price: float,
               reason: str = "", score: float = 0, result: str = "OPEN",
               ml_score: float = 0.0, ml_confidence: float = 0.0,
               composite_score: float = 0.0, signal_source: str = "",
               strategy: str = "", drift_status: str = "",
               drift_penalty: float = 0.0, order_id: str = "") -> None:
    if not supabase:
        return
    payload = {
        "trade_type": trade_type,
        "symbol": symbol,
        "quantity": quantity,
        "price": price,
        "reason": reason,
        "score": score,
        "result": result,
        "highest_price": price,
        "ml_score": ml_score,
        "ml_confidence": ml_confidence,
        "composite_score": composite_score,
        "source": signal_source,
        "strategy": strategy,
        "drift_status": drift_status,
        "drift_penalty": drift_penalty,
    }
    if order_id:
        payload["order_id"] = order_id
    try:
        supabase.table(US_TRADE_TABLE).insert(payload).execute()
    except Exception as e:
        try:
            basic_payload = {
                "trade_type": trade_type,
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "reason": reason,
                "score": score,
                "result": result,
                "highest_price": price,
            }
            supabase.table(US_TRADE_TABLE).insert(basic_payload).execute()
            log(f"DB 확장필드 저장 실패, 기본필드로 폴백: {e}", "WARN")
        except Exception as inner_e:
            log(f"DB 저장 실패: {inner_e}", "ERROR")


def close_position(symbol: str, exit_price: float, reason: str, pnl_pct: float | None = None) -> None:
    if not supabase:
        return
    positions = get_position_for_symbol(symbol)
    for p in positions:
        pid = p.get("id")
        if pid:
            try:
                payload = {
                    "result": "CLOSED",
                    "exit_price": exit_price,
                    "exit_reason": reason,
                }
                if pnl_pct is not None:
                    payload["pnl_pct"] = pnl_pct
                supabase.table(US_TRADE_TABLE).update(payload).eq("id", pid).execute()
            except Exception as e:
                log(f"DB 클로즈 실패 (id={pid}): {e}", "ERROR")


def update_highest_price(symbol: str, new_high: float) -> None:
    if not supabase:
        return
    positions = get_position_for_symbol(symbol)
    for p in positions:
        pid = p.get("id")
        current_high = float(p.get("highest_price", 0) or 0)
        if new_high > current_high and pid:
            try:
                supabase.table(US_TRADE_TABLE).update({
                    "highest_price": new_high,
                }).eq("id", pid).execute()
            except Exception:
                pass


# ─────────────────────────────────────────────
# 매매 로직
# ─────────────────────────────────────────────
def should_buy(symbol: str, score: float, indicators: dict) -> dict:
    """매수 판단: 복합 스코어 + 마켓 레짐 + 상대강도 + 멀티팩터 + 어닝."""
    rsi = indicators.get("rsi", 50)
    bb_pos = indicators.get("bb_pos", 50)
    vol_ratio = indicators.get("vol_ratio", 1.0)
    near_high = indicators.get("near_high", 50)

    if score < RISK["min_score"]:
        return {"action": "HOLD", "reason": f"스코어 부족 ({score:.0f} < {RISK['min_score']})"}
    if rsi > RULES["buy_rsi_hard_max"]:
        return {"action": "HOLD", "reason": f"RSI 극과매수 ({rsi:.0f} > {RULES['buy_rsi_hard_max']})"}
    if vol_ratio < RULES["buy_vol_hard_min"]:
        return {"action": "HOLD", "reason": f"거래량 급감 ({vol_ratio:.2f}x)"}

    # 마켓 레짐 (1회 조회 후 재사용)
    _regime = get_market_regime() if RISK.get("market_regime_filter") else {}

    # 마켓 레짐 필터
    if _regime:
        if _regime["regime"] == "BEAR":
            return {"action": "HOLD", "reason": f"BEAR 마켓 (SPY < 200MA, VIX: {_regime['vix']})"}
        if _regime.get("vix", 20) > RISK.get("vix_max", 35):
            return {"action": "HOLD", "reason": f"VIX 과열 ({_regime['vix']:.0f} > {RISK['vix_max']})"}

    # v2: 어닝 캘린더 필터
    if RISK.get("earnings_filter"):
        try:
            from common.market_data import check_earnings_proximity
            earnings = check_earnings_proximity(symbol, days=5)
            if earnings.get("near_earnings"):
                days_to = earnings.get("days_to_earnings", "?")
                return {"action": "HOLD", "reason": f"어닝 {days_to}일 전 — 매수 차단"}
        except Exception:
            pass

    cs = 0
    reasons = []

    # 레짐 적응형 팩터 가중치 (TTL 30분 캐시)
    from agents.regime_classifier import get_regime_cached
    _regime_adj = get_regime_cached(1800)
    _mom_mult = _regime_adj.get("momentum_mult", 1.0)
    _val_mult = _regime_adj.get("value_mult", 1.0)

    # 1) 모멘텀 등급 (35점 × 레짐 배수)
    if score >= 75:
        cs += round(35 * _mom_mult)
        reasons.append(f"모멘텀A({score:.0f})")
    elif score >= 65:
        cs += round(25 * _mom_mult)
        reasons.append(f"모멘텀B({score:.0f})")
    elif score >= 55:
        cs += round(18 * _mom_mult)
        reasons.append(f"모멘텀C({score:.0f})")
    elif score >= 50:
        cs += round(12 * _mom_mult)
        reasons.append(f"모멘텀D({score:.0f})")

    # 2) RSI 구간 (15점)
    if rsi <= 35:
        cs += 15
        reasons.append(f"RSI과매도({rsi:.0f})")
    elif rsi <= 45:
        cs += 12
        reasons.append(f"RSI저점({rsi:.0f})")
    elif rsi <= 55:
        cs += 8
        reasons.append(f"RSI중립({rsi:.0f})")
    elif rsi <= 65:
        cs += 5
        reasons.append(f"RSI적정({rsi:.0f})")
    elif rsi <= 75:
        cs += 2

    # 3) 볼린저밴드 (10점)
    if bb_pos <= 30:
        cs += 10
        reasons.append(f"BB하단({bb_pos:.0f}%)")
    elif bb_pos <= 50:
        cs += 7
        reasons.append(f"BB중간({bb_pos:.0f}%)")
    elif bb_pos <= 70:
        cs += 3

    # 4) 거래량 (10점)
    if vol_ratio >= 2.0:
        cs += 10
        reasons.append(f"거래량폭증({vol_ratio:.1f}x)")
    elif vol_ratio >= 1.2:
        cs += 7
        reasons.append(f"거래량증가({vol_ratio:.1f}x)")
    elif vol_ratio >= 0.8:
        cs += 4
    elif vol_ratio >= 0.5:
        cs += 2

    # 5) 신고가 근접도 (8점)
    if near_high >= 95:
        cs += 8
        reasons.append("신고가근접")
    elif near_high >= 90:
        cs += 5
    elif near_high >= 80:
        cs += 3

    # 6) 상대강도 (5점)
    if RISK.get("relative_strength"):
        rs = calc_relative_strength(symbol)
        if rs >= 1.5:
            cs += 5
            reasons.append(f"RS강({rs:.1f}x)")
        elif rs >= 1.2:
            cs += 3
            reasons.append(f"RS양호({rs:.1f}x)")
        elif rs < 0.8:
            cs -= 3

    # 7) 마켓 레짐 (3점) — 위에서 조회한 _regime 재사용
    if _regime:
        if _regime["regime"] == "BULL":
            cs += 3
        elif _regime["regime"] == "CORRECTION":
            cs -= 2

    # 8) v2: 멀티팩터 보너스 (밸류+퀄리티 — 15점 × 레짐 val 배수)
    if RISK.get("multifactor"):
        try:
            from common.market_data import calc_us_multifactor
            mf = calc_us_multifactor(symbol)
            mf_grade = mf.get("grade", "N/A")
            mf_score = mf.get("score", 0)
            if mf_grade == "A":
                cs += round(15 * _val_mult)
                reasons.append(f"팩터A({mf_score})")
            elif mf_grade == "B":
                cs += round(10 * _val_mult)
                reasons.append(f"팩터B({mf_score})")
            elif mf_grade == "C":
                cs += round(4 * _val_mult)
            elif mf_grade == "D":
                cs -= 5
                reasons.append(f"팩터D({mf_score})")
        except Exception:
            pass

    ml = _get_us_ml_signal(symbol)
    ml_confidence = float(ml.get("confidence", 0) or 0)
    ml_action = ml.get("action", "HOLD")
    ml_source = ml.get("source", "US_ML_UNKNOWN")

    # v6: 뉴스 감정 게이트
    try:
        from agents.news_analyst import get_symbol_sentiment
        from common.config import (NEWS_SENTIMENT_BLOCK_THRESHOLD,
                                   NEWS_SENTIMENT_BONUS_POINTS,
                                   NEWS_SENTIMENT_BONUS_THRESHOLD)
        _sentiment = get_symbol_sentiment(symbol)
        if _sentiment < NEWS_SENTIMENT_BLOCK_THRESHOLD:
            return {"action": "HOLD", "reason": f"뉴스 부정적 ({_sentiment:.2f} < {NEWS_SENTIMENT_BLOCK_THRESHOLD})"}
        if _sentiment > NEWS_SENTIMENT_BONUS_THRESHOLD:
            cs += NEWS_SENTIMENT_BONUS_POINTS
            reasons.append(f"뉴스긍정({_sentiment:.2f})")
    except Exception:
        pass

    if cs >= RULES["buy_composite_min"]:
        blended_conf = float(min(95, cs))
        if ml_confidence > 0:
            # 정확도 기반 동적 블렌딩 (KR과 동일 방식)
            try:
                from stocks.ml_model import load_performance_metrics as _lpm
                _perf = _lpm()
                _acc = float(_perf.get("accuracy", 0) or 0)
            except Exception:
                _acc = 0.0
            if _acc >= 0.65:
                _rule_w, _ml_w = 0.50, 0.50
            elif _acc >= 0.55:
                _rule_w, _ml_w = 0.70, 0.30
            else:
                _rule_w, _ml_w = 0.90, 0.10
            blended_conf = round(blended_conf * _rule_w + ml_confidence * _ml_w, 1)
            reasons.append(f"USML({ml_action}:{ml_confidence:.0f},acc={_acc:.0%})")
        return _apply_us_drift_gate({
            "action": "BUY",
            "confidence": blended_conf,
            "reason": " + ".join(reasons),
            "source": "RULE+US_ML" if ml_confidence > 0 else "RULE",
            "ml_confidence": ml_confidence,
            "ml_score": ml_confidence,
            "ml_source": ml_source,
        })

    top_reasons = reasons[:3] if reasons else ["조건미충족"]
    return _apply_us_drift_gate({
        "action": "HOLD",
        "confidence": cs,
        "reason": f"복합스코어 {cs}/{RULES['buy_composite_min']}: {', '.join(top_reasons)}",
        "source": "RULE+US_ML" if ml_confidence > 0 else "RULE",
        "ml_confidence": ml_confidence,
        "ml_score": ml_confidence,
        "ml_source": ml_source,
    })


def _get_atr_pct(symbol: str, period: int = 14) -> float:
    """14일 ATR / 현재가 비율 계산. 실패 시 0.0."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="60d")
        if hist is None or len(hist) < period + 1:
            return 0.0
        close = hist["Close"]
        high = hist["High"]
        low = hist["Low"]
        tr_list = []
        for i in range(1, len(close)):
            tr = max(
                float(high.iloc[i]) - float(low.iloc[i]),
                abs(float(high.iloc[i]) - float(close.iloc[i - 1])),
                abs(float(low.iloc[i]) - float(close.iloc[i - 1])),
            )
            tr_list.append(tr)
        atr = sum(tr_list[-period:]) / min(len(tr_list), period)
        price = float(close.iloc[-1])
        return atr / price if price > 0 else 0.0
    except Exception:
        return 0.0


def _get_dynamic_sl_tp(symbol: str) -> tuple:
    """ATR 기반 동적 SL/TP 계산. (sl_pct, tp_pct) 반환 (sl은 음수)."""
    from common.config import (ATR_MAX_STOP_LOSS, ATR_MAX_TAKE_PROFIT,
                               ATR_MIN_STOP_LOSS, ATR_MIN_TAKE_PROFIT,
                               ATR_SL_MULTIPLIER, ATR_TP_MULTIPLIER)
    atr_pct = _get_atr_pct(symbol)
    if atr_pct <= 0:
        return RISK["stop_loss"], RISK["take_profit"]
    dynamic_sl = -max(ATR_SL_MULTIPLIER * atr_pct, abs(ATR_MIN_STOP_LOSS))
    dynamic_sl = max(dynamic_sl, ATR_MAX_STOP_LOSS)  # cap at max
    dynamic_tp = max(ATR_TP_MULTIPLIER * atr_pct, ATR_MIN_TAKE_PROFIT)
    dynamic_tp = min(dynamic_tp, ATR_MAX_TAKE_PROFIT)
    return dynamic_sl, dynamic_tp


def check_exit(symbol: str, position: dict, indicators: dict) -> Optional[str]:
    """보유 포지션 청산 조건 체크. 청산 사유 문자열 반환, 없으면 None."""
    entry_price = float(position.get("price", 0))
    highest = float(position.get("highest_price", 0) or entry_price)
    current_price = indicators.get("price", 0)
    if not entry_price or not current_price:
        return None

    pnl = (current_price - entry_price) / entry_price
    pnl_net = pnl - RISK["fee_rate"]

    # v6: ATR 기반 동적 SL/TP
    dynamic_sl, dynamic_tp = _get_dynamic_sl_tp(symbol)

    # 손절 (ATR 동적)
    if pnl_net <= dynamic_sl:
        return f"손절 ({pnl_net*100:.1f}%, ATR_SL={dynamic_sl*100:.1f}%)"

    # 익절 (ATR 동적)
    if pnl_net >= dynamic_tp:
        return f"익절 ({pnl_net*100:.1f}%, ATR_TP={dynamic_tp*100:.1f}%)"

    # 적응형 트레일링 스탑: 수익 구간별 차등
    if highest > 0 and pnl_net >= RISK["trailing_activate"]:
        # 수익이 클수록 트레일링 타이트하게
        if pnl_net >= 0.08:
            ts_pct = 0.015   # 8%+ 수익일 때 1.5% 트레일링
        elif pnl_net >= 0.05:
            ts_pct = 0.02    # 5%+ 수익일 때 2% 트레일링
        else:
            ts_pct = 0.025   # 기본 2.5% 트레일링

        drop = (highest - current_price) / highest
        if drop >= ts_pct:
            return f"트레일링 (고점 {highest:.2f} → {current_price:.2f}, -{drop*100:.1f}%)"

    # v6: max_hold_days 하드 컷오프 (좀비 포지션 방지)
    max_hold = RISK.get("max_hold_days", 20)
    created = position.get("created_at", "")
    if created:
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            hold_days = (datetime.now(timezone.utc) - created_dt).days
            if hold_days >= max_hold:
                return f"최대보유일 초과 ({hold_days}일 ≥ {max_hold}일)"
            if hold_days >= RISK.get("timecut_days", 12):
                return f"타임컷 ({hold_days}일 보유)"
        except Exception as e:
            log(f"  {symbol}: 보유일 계산 실패: {e}", "WARN")

    rsi = indicators.get("rsi", 50)
    if rsi >= RULES["sell_rsi_min"] and pnl_net > 0:
        return f"RSI 과매수 ({rsi:.0f})"

    return None


def execute_buy(symbol: str, score: float, indicators: dict, signal: Optional[dict] = None) -> dict:
    """매수 실행."""
    global _cmr_instance
    price = indicators.get("price", 0)
    if not price:
        return {"result": "NO_PRICE"}

    if _us_buy_blocked:
        return {"result": "BLOCKED_DRAWDOWN"}

    # audit fix: CrossMarket 리스크 체크
    try:
        from quant.risk.cross_market_manager import CrossMarketRiskManager
        if _cmr_instance is None:
            _cmr_instance = CrossMarketRiskManager()
        cm_result = _cmr_instance.evaluate()
        if cm_result.buy_blocked:
            log(f"{symbol}: CrossMarket 리스크 차단: {cm_result.block_reasons}")
            return {"result": "CROSS_MARKET_BLOCKED", "reasons": cm_result.block_reasons}
    except Exception as _e:
        log(f"{symbol}: CrossMarket 체크 실패 (무시): {_e}")

    positions = get_open_positions()
    open_symbols = list(set(p.get("symbol") for p in positions))

    if symbol in open_symbols:
        return {"result": "ALREADY_HOLDING"}

    if len(open_symbols) >= RISK["max_positions"]:
        return {"result": "MAX_POSITIONS"}

    # v6: 섹터 분산 체크 (ETF 매핑 포함 + 상관관계 체크)
    from common.config import ETF_SECTOR_MAP
    max_sector = RISK.get("max_sector_positions", 2)

    def _get_sector(sym: str) -> str:
        """ETF 매핑 우선, 없으면 yfinance info 조회."""
        if sym in ETF_SECTOR_MAP:
            return ETF_SECTOR_MAP[sym]
        try:
            return yf.Ticker(sym).info.get("sector", "") or ""
        except Exception:
            return ""

    try:
        sym_sector = _get_sector(symbol)
        if sym_sector:
            sector_count = sum(1 for os_sym in open_symbols if _get_sector(os_sym) == sym_sector)
            if sector_count >= max_sector:
                return {"result": "MAX_SECTOR", "sector": sym_sector}
    except Exception:
        pass

    # v6: 상관관계 체크 — 기존 포지션과 20일 상관 > 0.8이면 거부
    if open_symbols:
        try:
            _corr_tickers = [symbol] + open_symbols[:4]
            _corr_data = yf.download(_corr_tickers, period="25d", progress=False)
            if _corr_data is not None and not _corr_data.empty and "Close" in _corr_data:
                _close = _corr_data["Close"]
                if symbol in _close.columns:
                    for _os in open_symbols:
                        if _os in _close.columns:
                            _corr = _close[symbol].corr(_close[_os])
                            if _corr is not None and _corr > 0.8:
                                log(f"  {symbol}: {_os}과 상관관계 {_corr:.2f} > 0.8 — 매수 거부")
                                return {"result": "HIGH_CORRELATION", "corr_with": _os, "corr": round(_corr, 2)}
        except Exception:
            pass

    if count_today_buys() >= RISK["max_trades_per_day"]:
        return {"result": "MAX_DAILY_TRADES"}

    target_market_weight = get_effective_market_weight('US')
    if target_market_weight is not None:
        open_value = sum(float(p.get("quantity", 0) or 0) * float(p.get("price", 0) or 0) for p in positions)
        current_weight = open_value / RISK["virtual_capital"] if RISK["virtual_capital"] > 0 else 0.0
        if current_weight >= target_market_weight + 0.02:
            return {"result": "OVERWEIGHT_MARKET", "current_weight": current_weight}

    # 차등 포지션 사이징: 모멘텀 등급별
    if score >= 75:
        ratio = RISK["invest_ratio_A"]
    elif score >= 65:
        ratio = RISK["invest_ratio_B"]
    else:
        ratio = RISK["invest_ratio_C"]
    invest_usd = RISK["virtual_capital"] * ratio

    # v6: Kelly sizer always called (conservative defaults for < 50 trades)
    recent_trades = load_recent_trades('us', limit=100)
    _n_trades = len(recent_trades)
    if _n_trades >= 50:
        wins = [t['pnl_pct'] for t in recent_trades if t.get('pnl_pct', 0) > 0]
        losses = [abs(t['pnl_pct']) for t in recent_trades if t.get('pnl_pct', 0) < 0]
        win_rate = len(wins) / _n_trades if _n_trades else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.02
        avg_loss = sum(losses) / len(losses) if losses else 0.03
    else:
        # v6: 보수적 기본값 (50건 미만)
        win_rate = 0.35
        avg_win = 0.04
        avg_loss = 0.025

    # v6: Kelly sizer always called (regardless of trade count)
    atr_pct = _get_atr_pct(symbol)
    current_exposure = sum(
        float(p.get("quantity", 0) or 0) * float(p.get("price", 0) or 0)
        for p in positions
    ) / max(RISK["virtual_capital"], 1)
    sizer = KellyPositionSizer()
    sizing = sizer.size_position(
        account_equity=RISK["virtual_capital"],
        price=price,
        win_rate=win_rate,
        payoff_ratio=avg_win / max(avg_loss, 0.001),
        current_total_exposure=current_exposure,
        atr_pct=atr_pct,
        conviction=max(0.0, min(1.0, score / 100.0)),
    )
    # v6.2 C1: Half Kelly 포지션 사이징 — min(Kelly, config_ratio) 패턴 적용
    kelly_fraction_val = float(sizing.get("capped_fraction", 0.0))
    kelly_invest = RISK["virtual_capital"] * kelly_fraction_val
    config_invest_ratio = ratio  # 위에서 score 기반으로 결정된 invest_ratio_A/B/C
    config_invest = RISK["virtual_capital"] * config_invest_ratio
    if kelly_invest > 0:
        # Half Kelly가 config 상한 초과하지 않도록 min() 적용
        invest_usd = min(kelly_invest, config_invest)
    log(f"  {symbol}: [C1] Half Kelly: kelly={kelly_fraction_val:.3f} cfg={config_invest_ratio:.3f} eff={invest_usd/max(RISK['virtual_capital'],1):.3f} → ${invest_usd:.0f}")

    # v6: VaR 체크 — 포트폴리오 VaR 3% 초과 시 거부
    new_var = sizer.estimate_position_var(
        position_fraction=sizing.get("capped_fraction", 0.0),
        daily_vol=atr_pct if atr_pct > 0 else 0.02,
    )
    existing_var = sizer.estimate_position_var(
        position_fraction=current_exposure,
        daily_vol=0.02,  # portfolio avg volatility estimate
    )
    if not sizer.check_portfolio_var(existing_var, new_var):
        log(f"  {symbol}: VaR 초과 ({(existing_var + new_var)*100:.1f}% > {sizer.config.portfolio_var_limit*100:.0f}%) — 매수 거부")
        return {"result": "VAR_EXCEEDED"}

    # v2: ATR 기반 변동성 사이징
    if RISK.get("volatility_sizing"):
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="30d")
            if hist is not None and len(hist) >= 14:
                close = hist["Close"]
                diffs = [abs(float(close.iloc[i] - close.iloc[i - 1])) for i in range(1, len(close))]
                atr = sum(diffs[-14:]) / min(len(diffs), 14)
                atr_pct = atr / price if price > 0 else 0.02
                if atr_pct > 0.04:
                    invest_usd *= 0.6
                    log(f"  {symbol}: 고변동성({atr_pct*100:.1f}%) — 40% 축소")
                elif atr_pct > 0.03:
                    invest_usd *= 0.8
                    log(f"  {symbol}: 중변동성({atr_pct*100:.1f}%) — 20% 축소")
        except Exception:
            pass

    qty = invest_usd / price
    if qty < 0.01:
        return {"result": "INSUFFICIENT"}

    qty = round(qty, 4)

    # 멱등성 체크: 동일 분 내 중복 매수 방지
    _order_id = generate_order_id("us", symbol, "buy")
    if check_order_idempotency(supabase, US_TRADE_TABLE, _order_id):
        log(f"  {symbol}: 중복 주문 감지 — order_id={_order_id}", "WARN")
        return {"result": "DUPLICATE_ORDER"}

    log(f"🟢 {symbol} 매수: ${price:.2f} × {qty}주 ≈ ${invest_usd:.0f}", "TRADE")

    # v6: SmartRouter 라우팅 결정 로깅
    try:
        _route_decision = _smart_router.decide(
            symbol=symbol, side="BUY", total_qty=qty,
            market="us", price_hint=price,
        )
        _route_name = getattr(_route_decision, 'route', 'MARKET')
        log(f"  {symbol}: SmartRouter → {_route_name} (${invest_usd:.0f})")
    except Exception:
        pass

    # 팩터 스냅샷 수집 (Phase Level 4: 팩터 로깅)
    _factor_snapshot: str | None = None
    try:
        import sys as _sys
        _WORKSPACE_ROOT = str(Path(__file__).resolve().parents[1])
        if _WORKSPACE_ROOT not in _sys.path:
            _sys.path.insert(0, _WORKSPACE_ROOT)
        import json as _json
        from datetime import datetime as _dt

        from quant.factors.registry import FactorContext, calc_all
        _fctx = FactorContext()
        _today_iso = _dt.now().date().isoformat()
        _all_factors = calc_all(_today_iso, symbol=symbol, market='us', context=_fctx)
        _top5 = dict(
            sorted(_all_factors.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
        )
        _factor_snapshot = _json.dumps(_top5, ensure_ascii=False)
        log(f"  {symbol} 팩터 스냅샷: {list(_top5.keys())}")
    except Exception as _fe:
        log(f"  {symbol} 팩터 스냅샷 건너뜀: {_fe}", "WARN")

    signal = signal or {}
    composite_conf = float(signal.get("confidence", score) or score)
    ml_conf = float(signal.get("ml_confidence", 0.0) or 0.0)
    source = str(signal.get("source", "RULE"))
    strategy = "US_ML_BLEND" if ml_conf > 0 else "US_RULE"
    drift_status = str(signal.get("drift_status", ""))
    drift_penalty = float(signal.get("drift_penalty", 0.0) or 0.0)

    # ── Alpaca 실전/페이퍼 주문 실행 ──────────────────────────────
    import os as _os
    _us_trading_env = _os.environ.get("US_TRADING_ENV", "sim").lower()  # sim / paper / live
    _alpaca_result: dict = {}
    _alpaca_mode_label = "⚠️ 모의투자"
    if _AlpacaBroker is not None and _us_trading_env in ("paper", "live"):
        try:
            _broker = _AlpacaBroker(live=(_us_trading_env == "live"))
            _alpaca_result = _broker.route_and_execute(
                symbol=symbol, side="buy", qty=qty, price_hint=price,
                simulate=False,
            )
            _alpaca_fills = _alpaca_result.get("execution", {}).get("fills") or []
            _alpaca_status = _alpaca_fills[0].get("response", {}).get("status", "UNKNOWN") if _alpaca_fills else "UNKNOWN"
            _is_live = (_us_trading_env == "live")
            _alpaca_mode_label = f"{'💰 실전' if _is_live else '📄 페이퍼'} ({_alpaca_status})"
            if not _alpaca_result.get("ok"):
                log(f"  {symbol} Alpaca 매수 실패: {_alpaca_result}", "ERROR")
                # P1-9: Alpaca 주문 실패 시 DB 저장 건너뜀
                send_telegram(f"🚨 {symbol} Alpaca 매수 실패 — DB 저장 건너뜀")
                return {"result": "ALPACA_BUY_FAILED", "symbol": symbol}
            else:
                log(f"  {symbol} Alpaca 매수 주문: {_alpaca_mode_label}", "TRADE")
        except Exception as _ae:
            log(f"  {symbol} Alpaca 매수 예외: {_ae}", "ERROR")
    # ────────────────────────────────────────────────────────────

    save_trade(
        "BUY",
        symbol,
        qty,
        price,
        reason=signal.get("reason", f"모멘텀 {score:.0f}"),
        score=score,
        ml_score=ml_conf,
        ml_confidence=ml_conf,
        composite_score=composite_conf,
        signal_source=source,
        strategy=strategy,
        drift_status=drift_status,
        drift_penalty=drift_penalty,
        order_id=_order_id,
    )

    # factor_snapshot 컬럼에 별도 저장 — 가장 최근 OPEN BUY 레코드에만 업데이트 (graceful)
    if _factor_snapshot and supabase:
        try:
            _recent = (
                supabase.table(US_TRADE_TABLE)
                .select("id")
                .eq("symbol", symbol)
                .eq("result", "OPEN")
                .eq("trade_type", "BUY")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if _recent.data:
                _rec_id = _recent.data[0]["id"]
                supabase.table(US_TRADE_TABLE).update(
                    {"factor_snapshot": _factor_snapshot}
                ).eq("id", _rec_id).execute()
        except Exception as _ue:
            log(f"  {symbol} 팩터 snapshot upsert 실패 (graceful): {_ue}", "WARN")

    send_telegram(
        f"🇺🇸🟢 <b>{symbol} 매수</b>\n"
        f"💰 ${price:.2f} × {qty}주\n"
        f"💵 투입: ${invest_usd:.0f}\n"
        f"📊 모멘텀: {score:.0f}\n"
        f"{_alpaca_mode_label}"
    )
    if _sheets_append:
        try:
            _sheets_append("us", "매수", symbol, price, qty, None, f"모멘텀 {score:.0f}")
        except Exception:
            pass

    # audit fix: Prometheus 메트릭 연동
    try:
        from common.prometheus_metrics import record_trade, set_signal_score
        record_trade("US", "buy")
        set_signal_score("US", "composite", float(composite_conf))
    except Exception:
        pass

    return {"result": "BUY", "symbol": symbol, "qty": qty, "price": price}


def execute_sell(symbol: str, position: dict, reason: str, indicators: dict) -> dict:
    """매도 실행."""
    price = indicators.get("price", 0)
    entry_price = float(position.get("price", 0))
    qty = float(position.get("quantity", 0))
    if not price or not entry_price:
        return {"result": "NO_PRICE"}

    pnl_pct = ((price - entry_price) / entry_price - RISK["fee_rate"]) * 100
    pnl_usd = (price - entry_price) * qty

    log(f"🔴 {symbol} 매도: ${price:.2f} × {qty}주 | {pnl_pct:+.2f}% (${pnl_usd:+.1f}) | {reason}", "TRADE")

    # ── Alpaca 실전/페이퍼 매도 주문 실행 ────────────────────────
    import os as _os
    _us_trading_env = _os.environ.get("US_TRADING_ENV", "sim").lower()  # sim / paper / live
    _sell_mode_label = "⚠️ 모의투자"
    if _AlpacaBroker is not None and _us_trading_env in ("paper", "live"):
        try:
            _broker = _AlpacaBroker(live=(_us_trading_env == "live"))
            _sell_result = _broker.route_and_execute(
                symbol=symbol, side="sell", qty=qty, price_hint=price,
                simulate=False,
            )
            _sell_fills = _sell_result.get("execution", {}).get("fills") or []
            _sell_status = _sell_fills[0].get("response", {}).get("status", "UNKNOWN") if _sell_fills else "UNKNOWN"
            _is_live = (_us_trading_env == "live")
            _sell_mode_label = f"{'💰 실전' if _is_live else '📄 페이퍼'} ({_sell_status})"
            if not _sell_result.get("ok"):
                log(f"  {symbol} Alpaca 매도 실패: {_sell_result}", "ERROR")
            else:
                log(f"  {symbol} Alpaca 매도 주문: {_sell_mode_label}", "TRADE")
        except Exception as _ae:
            log(f"  {symbol} Alpaca 매도 예외: {_ae}", "ERROR")
    # ────────────────────────────────────────────────────────────

    close_position(symbol, price, reason, pnl_pct=pnl_pct)

    send_telegram(
        f"🇺🇸🔴 <b>{symbol} 매도</b>\n"
        f"💰 ${price:.2f} × {qty}주\n"
        f"📊 수익: {pnl_pct:+.2f}% (${pnl_usd:+.1f})\n"
        f"📝 {reason}\n"
        f"{_sell_mode_label}"
    )
    if _sheets_append:
        try:
            action = "손절" if pnl_pct < -3 else "익절" if pnl_pct > 5 else "매도"
            _sheets_append("us", action, symbol, price, qty, pnl_pct, reason)
        except Exception:
            pass

    # audit fix: Prometheus 메트릭 연동
    try:
        from common.prometheus_metrics import record_trade, set_pnl
        record_trade("US", "sell")
        set_pnl("US", float(pnl_pct))
    except Exception:
        pass

    # P1-10: ConceptDriftDetector US 연동 — 매도 후 예측 결과 업데이트
    try:
        if _us_drift_detector is not None:
            _actual = 1 if pnl_pct > 0 else 0
            _ml_score_raw = float(position.get("ml_score", None) or position.get("ml_confidence", None) or 0.5)
            _predicted = _ml_score_raw if _ml_score_raw != 0.0 else 0.5
            _is_drift = _us_drift_detector.update(_predicted, _actual)
            if _is_drift:
                log(f"{symbol} ML 드리프트 감지!", "WARN")
    except Exception:
        pass

    return {"result": "SELL", "pnl_pct": pnl_pct, "reason": reason}


# ─────────────────────────────────────────────
# 손절/익절 체크 (보유 포지션 순회)
# ─────────────────────────────────────────────
def _get_price_fallback(symbol: str) -> Optional[float]:
    """yf.Ticker fast_info fallback으로 현재가 조회."""
    try:
        ticker = yf.Ticker(symbol)
        fi = ticker.fast_info
        price = fi.get("lastPrice") or fi.get("last_price")
        return float(price) if price else None
    except Exception:
        return None


def check_stop_loss_take_profit():
    """보유 포지션 전체 손절/익절/트레일링 체크 (v6: retry + fallback 강화)."""
    positions = get_open_positions()
    if not positions:
        return

    log(f"보유 {len(positions)}개 포지션 체크 중...")
    indicator_fail_max = RISK.get("indicator_fail_max", 3)

    for pos in positions:
        symbol = pos.get("symbol", "")
        if not symbol:
            continue

        # v6: get_us_indicators 실패 시 3초 후 1회 재시도 + fallback
        indicators = get_us_indicators(symbol)
        if not indicators:
            import time as _time
            _time.sleep(3)
            _yf_cache.pop(symbol, None)  # 캐시 무효화 후 재시도
            indicators = get_us_indicators(symbol)

        if not indicators:
            # fallback: fast_info에서 가격만이라도 가져오기
            fb_price = _get_price_fallback(symbol)
            if fb_price:
                indicators = {"price": fb_price, "rsi": 50, "bb_pos": 50, "vol_ratio": 1.0, "near_high": 50}
                log(f"  {symbol}: 지표 fallback (fast_info price=${fb_price:.2f})", "WARN")
            else:
                # v6: 연속 N회 실패 시 강제 시장가 매도
                _fail_key = f"_indicator_fail_{symbol}"
                _fail_count = getattr(check_stop_loss_take_profit, _fail_key, 0) + 1
                setattr(check_stop_loss_take_profit, _fail_key, _fail_count)
                log(f"  {symbol}: 지표 조회 실패 ({_fail_count}/{indicator_fail_max})", "WARN")
                if _fail_count >= indicator_fail_max:
                    entry_price = float(pos.get("price", 0) or 0)
                    if entry_price > 0:
                        log(f"  {symbol}: 지표 {_fail_count}회 연속 실패 → 강제 시장가 매도", "ERROR")
                        fallback_ind = {"price": entry_price}
                        execute_sell(symbol, pos, f"지표실패 {_fail_count}회 강제매도", fallback_ind)
                        send_telegram(
                            f"🇺🇸🚨 <b>{symbol} 강제매도</b>\n"
                            f"지표 {_fail_count}회 연속 실패\n"
                            f"진입가: ${entry_price:.2f}"
                        )
                        setattr(check_stop_loss_take_profit, _fail_key, 0)
                continue

        # 지표 성공 시 실패 카운터 초기화
        _fail_key = f"_indicator_fail_{symbol}"
        setattr(check_stop_loss_take_profit, _fail_key, 0)

        current_price = indicators["price"]
        update_highest_price(symbol, current_price)

        exit_reason = check_exit(symbol, pos, indicators)
        if exit_reason:
            execute_sell(symbol, pos, exit_reason, indicators)
        else:
            entry = float(pos.get("price", 0))
            pnl = ((current_price - entry) / entry * 100) if entry else 0
            log(f"  {symbol}: ${current_price:.2f} ({pnl:+.2f}%) — HOLD")


# audit fix: US 일일 손실 한도 체크 추가 (BTC/KR과 동일한 패턴)
def check_daily_loss_us() -> bool:
    """당일 US 손실 체크. 한도 초과 시 True 반환."""
    # P0-6: ET 날짜 대신 UTC 기준 (Supabase created_at은 UTC 저장)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        # P0-5: us_trade_executions 테이블, trade_type="SELL" 컬럼 사용
        sells = supabase.table("us_trade_executions").select("pnl_pct").eq("trade_type", "SELL").gte("created_at", today_str).execute()
        if not sells.data:
            return False
        total_loss = sum(
            float(s.get("pnl_pct", 0))
            for s in sells.data
            if float(s.get("pnl_pct", 0)) < 0
        )
        limit = RISK.get("max_daily_loss", -0.08)
        if total_loss / 100 < limit:
            log(f"[WARN] US 일일 손실 한도 초과: {total_loss:.2f}% < {limit * 100:.1f}%")
            from common.telegram import send_telegram
            send_telegram(f"🚨 US 일일 손실 한도 초과\n손실: {total_loss:.2f}%\n한도: {limit * 100:.1f}%\n→ 오늘 US 매매 중단")
            return True
    except Exception as e:
        log(f"[ERROR] US 일일 손실 체크 실패: {e}")
    return False


# ─────────────────────────────────────────────
# 메인 사이클
# ─────────────────────────────────────────────
def run_trading_cycle():
    global _us_buy_blocked, _us_drift_cache
    _us_buy_blocked = False
    _us_drift_cache = {}

    # P1-8: 장 외 시간 체크
    if not is_us_market_open():
        log("US 장 외 시간 — 사이클 건너뜀", "INFO")
        return

    # audit fix: 일일 손실 한도 초과 시 사이클 스킵
    if check_daily_loss_us():
        log("[SKIP] US 일일 손실 한도 초과 — 사이클 종료")
        return

    # 레짐별 팩터 가중치 (TTL 30분 캐시)
    from agents.regime_classifier import get_regime_cached
    _regime_adj = get_regime_cached(1800)

    # v6: 레짐별 리스크 파라미터 런타임 오버라이드
    from common.config import REGIME_RISK_OVERRIDES
    _current_regime = _regime_adj.get("regime", "TRANSITION")
    _regime_override = REGIME_RISK_OVERRIDES.get(_current_regime, {})
    if _regime_override:
        if "max_positions" in _regime_override:
            RISK["max_positions"] = _regime_override["max_positions"]
        if not _regime_override.get("allow_new_buys", True):
            _us_buy_blocked = True
            log(f"레짐 {_current_regime}: 신규매수 차단")
        log(f"레짐 리스크 오버라이드: {_current_regime} → max_pos={RISK['max_positions']}, sl_mult={_regime_override.get('sl_mult', 1.0)}")

    log("=" * 50)
    log("🇺🇸 US 자동매매 사이클 시작")

    # PR #24: sim 모드 명시적 경고. 실거래 진입 절차는 docs/US_TRADING_GUIDE.md
    import os as _os
    _us_env_check = _os.environ.get("US_TRADING_ENV", "sim").lower()
    if _us_env_check == "sim":
        log("⚠️ US_TRADING_ENV=sim — Alpaca 미연결, virtual_capital 시뮬레이션 (실거래 X)", "WARN")
        log("   → 실거래 전환: docs/US_TRADING_GUIDE.md", "WARN")
    elif _us_env_check == "paper":
        log("📄 US_TRADING_ENV=paper — Alpaca paper 모드 (가상 주문, 슬리피지/수수료 실증)", "INFO")
    elif _us_env_check == "live":
        log("💰 US_TRADING_ENV=live — Alpaca 실거래 모드", "INFO")

    try:
        open_value = sum(float(p.get("quantity", 0) or 0) * float(p.get("price", 0) or 0) for p in get_open_positions())
        account_equity = max(RISK["virtual_capital"], open_value)
        # PR #24: source 라벨에 모드 반영
        _eq_source = {"sim": "virtual_capital", "paper": "alpaca_paper", "live": "alpaca_live"}.get(_us_env_check, "virtual_capital")
        append_equity_snapshot('us', account_equity, {"source": _eq_source, "mode": _us_env_check})
        tw = get_effective_market_weight('US')
        if tw is not None:
            log(f"리밸런싱 목표 비중(US): {tw:.1%}")
        drift = _load_us_ml_drift_report(force=True)
        if drift:
            log(
                f"US ML Drift: {drift.get('status', 'UNKNOWN')} "
                f"(max_psi={float(drift.get('max_psi', 0.0) or 0.0):.3f})"
            )
    except Exception as e:
        log(f"US 자산 스냅샷 저장 실패: {e}", "WARN")

    if STOP_FLAG.exists():
        log("⛔ US_STOP_TRADING 플래그 감지 — 사이클 스킵")
        _stop_cd = Path("/tmp/openclaw_stop_us.ts")
        import time as _t
        _last = float(_stop_cd.read_text()) if _stop_cd.exists() else 0.0
        if _t.time() - _last >= 3600:
            send_telegram("🇺🇸⛔ US 자동매매 중지 플래그 감지 — 이번 사이클 스킵")
            _stop_cd.write_text(str(_t.time()))
        return

    equity_curve = load_equity_curve('us')
    if equity_curve:
        _dd_store = DrawdownStateStore()
        guard = DrawdownGuard(store=_dd_store)
        returns = guard.returns_from_equity_curve(equity_curve)
        decision = guard.evaluate(
            daily_return=returns.get('daily_return', 0.0),
            weekly_return=returns.get('weekly_return', 0.0),
            monthly_return=returns.get('monthly_return', 0.0),
            market='us',
        )
        save_drawdown_state('us', decision['state'].__dict__)
        _us_buy_blocked = not decision.get('allow_new_buys', True)
        triggers = set(decision.get('triggered_rules') or [])
        if 'WEEKLY_DELEVERAGE' in triggers:
            ranked = sorted(
                get_open_positions(),
                key=lambda p: float(p.get("quantity", 0) or 0) * float(p.get("price", 0) or 0),
                reverse=True,
            )
            total_value = sum(float(p.get("quantity", 0) or 0) * float(p.get("price", 0) or 0) for p in ranked)
            reduced = 0.0
            for pos in ranked:
                symbol = pos.get("symbol", "")
                if not symbol:
                    continue
                indicators = get_us_indicators(symbol) or {"price": float(pos.get("price", 0) or 0)}
                execute_sell(symbol, pos, "DrawdownGuard DELEVERAGE", indicators)
                reduced += float(pos.get("quantity", 0) or 0) * float(pos.get("price", 0) or 0)
                if total_value > 0 and reduced / total_value >= 0.5:
                    break
        if decision.get('force_liquidate'):
            for pos in get_open_positions():
                symbol = pos.get("symbol", "")
                if symbol:
                    indicators = get_us_indicators(symbol) or {"price": float(pos.get("price", 0) or 0)}
                    execute_sell(symbol, pos, "DrawdownGuard FULL_STOP", indicators)
            return

    # 보유 포지션 손절/익절 먼저
    check_stop_loss_take_profit()

    # 오늘 매수 한도 체크
    today_buys = count_today_buys()
    if today_buys >= RISK["max_trades_per_day"]:
        log(f"오늘 매수 한도 도달 ({today_buys}/{RISK['max_trades_per_day']}) — 신규 매수 스킵")
        log("US 매매 사이클 완료")
        return

    # 시장 레짐 확인
    regime = get_market_regime()
    log(f"시장 레짐: {regime['regime']} | SPY: {regime.get('spy_price',0):.0f} (200MA: {regime.get('spy_ma200',0):.0f}) | VIX: {regime.get('vix',0):.1f}")

    if regime["regime"] == "BEAR" and RISK.get("market_regime_filter"):
        log("🐻 BEAR 마켓 — 신규 매수 전면 차단")
        return

    # 모멘텀 스캔 (상위 10% 대상으로 분석)
    log("모멘텀 스캔 중...")
    top_list = scan_today_top_us(universe=US_UNIVERSE, lookback_days=90, top_percent=10.0)
    if not top_list:
        log("상위 종목 없음 — 종료")
        return

    open_positions = get_open_positions()
    open_symbols = [p.get("symbol") for p in open_positions]

    # 종목별 분석 + 매수 판단
    for ms in top_list:
        symbol = ms.symbol
        score = ms.score

        if symbol in open_symbols:
            continue

        log("")
        log(f"  📊 {symbol} 분석 (스코어: {score:.1f})...")

        indicators = get_us_indicators(symbol)
        if not indicators:
            log(f"  {symbol}: 지표 없음 — 스킵", "WARN")
            continue

        log(f"  RSI: {indicators['rsi']} / BB: {indicators['bb_pos']:.0f}% / "
            f"Vol: {indicators['vol_ratio']:.2f}x / 60dHigh: {indicators['near_high']:.0f}%")

        # v2: 멀티팩터 로깅
        if RISK.get("multifactor"):
            try:
                from common.market_data import calc_us_multifactor
                mf = calc_us_multifactor(symbol)
                if mf.get("grade") != "N/A":
                    log(f"  팩터: {mf['grade']}({mf['score']}) | {mf.get('detail', '')}")
            except Exception:
                pass

        signal = should_buy(symbol, score, indicators)
        if signal.get("ml_confidence", 0) > 0:
            log(
                f"  US ML: {signal.get('ml_confidence', 0):.1f}% "
                f"[{signal.get('ml_source', 'US_ML_UNKNOWN')}]"
            )
        log(f"  신호: {signal['action']} — {signal.get('reason', '')}")

        if signal["action"] == "BUY":
            result = execute_buy(symbol, score, indicators, signal=signal)
            log(f"  결과: {result['result']}")
            if result["result"] == "MAX_DAILY_TRADES":
                log("오늘 매수 한도 도달 — 스캔 종료")
                break

        time.sleep(0.5)

    log("🇺🇸 US 매매 사이클 완료")
    log("=" * 50)

    # audit fix: Prometheus 메트릭 연동
    try:
        from common.prometheus_metrics import record_agent_cycle
        record_agent_cycle("US", "success")
    except Exception:
        pass


# ─────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        log("보유 포지션 손절/익절 체크")
        check_stop_loss_take_profit()
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        positions = get_open_positions()
        if not positions:
            log("열린 포지션 없음")
        else:
            for p in positions:
                sym = p.get("symbol", "?")
                entry = float(p.get("price", 0))
                qty = float(p.get("quantity", 0))
                ind = get_us_indicators(sym)
                cur = ind["price"] if ind else 0
                pnl = ((cur - entry) / entry * 100) if entry and cur else 0
                log(f"  {sym}: {qty}주 × ${entry:.2f} → ${cur:.2f} ({pnl:+.2f}%)")
    else:
        run_trading_cycle()
