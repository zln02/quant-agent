#!/usr/bin/env python3
"""
BTC 자동매매 에이전트 v6 — Top-tier Quant
기능: 멀티타임프레임, Fear&Greed, 뉴스감정, 거래량분석,
      펀딩비/OI/롱숏비율(온체인), 김치프리미엄,
      동적 가중치 복합스코어, 적응형 트레일링, 부분익절
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo  # v6.2 B2: KST 시간대 통일

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import (BTC_AI_CACHE_TTL, BTC_DAILY_CACHE_TTL,
                           BTC_DB_RETRY_COUNT, BTC_DB_RETRY_SLEEP,
                           BTC_EXECUTION_SLIPPAGE, BTC_FG_API_TIMEOUT, BTC_LOG,
                           BTC_MARKET_COUNT, BTC_MARKET_INTERVAL)
from common.env_loader import load_env
from common.equity_loader import (append_equity_snapshot,
                                  get_effective_market_weight,
                                  load_equity_curve, load_recent_trades,
                                  save_drawdown_state)
from common.logger import get_logger
from common.retry import retry_call
from common.supabase_client import get_supabase
from common.telegram import Priority as _TgPriority
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
    from common.openclaw_notify import notify_openclaw
except ImportError:
    notify_openclaw = None

load_env()
log = get_logger("btc_agent", BTC_LOG)

import pyupbit
from btc_news_collector import get_news_result as _get_news_result

from common.llm_client import is_quota_exceeded


def _parse_entry_time(value) -> datetime | None:
    """Parse Supabase timestamps defensively.

    Historical rows may contain strings, naive datetimes, or already-parsed datetimes.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _load_ic_weights() -> dict:
    """Load IC-derived weights exported by quant/signal_evaluator.py.

    Returns empty dict on any failure.
    """
    try:
        p = Path(__file__).resolve().parents[1] / "brain" / "signal-ic" / "weights.json"
        if not p.exists():
            return {}
        payload = json.loads(p.read_text(encoding="utf-8"))
        w = payload.get("weights") or {}
        if isinstance(w, dict):
            return {str(k): float(v) for k, v in w.items() if v is not None}
        return {}
    except Exception:
        return {}


def _apply_weighted_score(components: dict, *, weights: dict) -> int:
    """Apply weights to component scores.

    components: dict with keys like fg,rsi,bb,vol,trend,funding,ls,oi,bonus,regime_adj
    weights: dict from signal_evaluator (signal-name -> weight)
    """
    if not weights:
        return int(components.get("total", 0) or 0)

    # Map evaluator signal names -> component keys
    map_sig_to_comp = {
        "fg_index": "fg",
        "rsi_signal": "rsi",
        "funding_rate": "funding",
        "whale_signal": "whale",
        "btc_composite": "total",
        "composite_score": "total",
    }

    # Use weights to scale the main components; keep bonus/regime adjustments as-is.
    base_parts = ["fg", "rsi", "bb", "vol", "trend", "funding", "ls", "oi", "whale"]
    # Default weights fallback (legacy proportions)
    default_w = {
        "fg": 22,
        "rsi": 20,
        "bb": 12,
        "vol": 10,
        "trend": 12,
        "funding": 8,
        "ls": 6,
        "oi": 5,
        "whale": 3,
    }
    denom = float(sum(default_w.values())) or 1.0
    w_comp = {k: default_w[k] / denom for k in base_parts}

    # Override subset based on evaluator weights (only for mapped items)
    for sig_name, comp_key in map_sig_to_comp.items():
        if sig_name in weights and comp_key in w_comp:
            w_comp[comp_key] = float(weights[sig_name])

    s = sum(w_comp.values())
    if s > 0:
        w_comp = {k: v / s for k, v in w_comp.items()}

    raw = 0.0
    for k in base_parts:
        raw += float(components.get(k, 0) or 0) * float(w_comp.get(k, 0.0))

    # Re-scale to legacy 0-95-ish range then add bonus/regime_adj
    legacy_max = float(sum(default_w.values()))
    raw_scaled = raw * legacy_max
    raw_scaled += float(components.get("bonus", 0) or 0)
    raw_scaled += float(components.get("regime_adj", 0) or 0)
    raw_scaled += float(components.get("news", 0) or 0)
    total = max(0, min(int(round(raw_scaled)), 100))
    return total


# ── 환경변수 ──────────────────────────────────────
UPBIT_ACCESS = os.environ.get("UPBIT_ACCESS_KEY", "")
UPBIT_SECRET = os.environ.get("UPBIT_SECRET_KEY", "")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
RUNTIME_ENV_READY = all([UPBIT_ACCESS, UPBIT_SECRET])

if not RUNTIME_ENV_READY:
    log.warning("필수 환경변수 부족: 에이전트 실행은 제한되지만 API helper import는 허용")
upbit = pyupbit.Upbit(UPBIT_ACCESS, UPBIT_SECRET) if UPBIT_ACCESS and UPBIT_SECRET else None
supabase = get_supabase()
_btc_buy_blocked = False
# audit fix: CrossMarket 리스크 — 모듈 레벨 싱글턴 (매 사이클 재사용)
_cmr_instance = None

# ── 리스크 설정 (v6 — Top-tier Quant) ─────────────
RISK = {
    "split_ratios": [0.15, 0.25, 0.40],     # 스코어 높을수록 큰 비중
    "split_rsi": [55, 45, 35],
    "invest_ratio": 0.30,
    "stop_loss": -0.03,
    "take_profit": 0.08,        # audit fix: config BTC_RISK_DEFAULTS 기준으로 통일 (+8%)
    "partial_tp_pct": 0.03,        # audit fix: config BTC_RISK_DEFAULTS 기준으로 통일 (3%)
    "partial_tp_ratio": 0.50,        # 1단계 매도 비율 (50%)
    "partial_tp_2_pct": 0.06,        # 2단계 익절 발동 (6%, 1단계 3% 기준으로 조정)
    "partial_tp_2_ratio": 0.50,       # 2단계 매도 비율 (남은 물량의 50%)
    "atr_multiplier": 2.0,         # ATR 손절 배수 (진입가 - ATR * 2.0)
    "atr_period": 14,          # ATR 계산 기간
    "trailing_stop": 0.02,
    "trailing_activate": 0.015,
    "trailing_adaptive": True,
    "max_daily_loss": -0.08,
    "max_drawdown": -0.15,
    "min_confidence": 65,
    "max_trades_per_day": 2,           # 3 → 2 (수수료 절감)
    "fee_buy": 0.001,
    "fee_sell": 0.001,
    "buy_composite_min": 50,           # 43 → 50 (고확신 진입만)
    "sell_composite_max": 20,
    "timecut_days": 7,
    "cooldown_minutes": 60,           # 30 → 60분 (진입 빈도 감소)
    "volatility_filter": True,
    "funding_filter": True,      # 펀딩비 과열 시 매수 억제
    "oi_filter": True,      # OI 급등 시 경고
    "kimchi_premium_max": 5.0,      # 김치프리미엄 5% 이상 시 매수 차단
    "dynamic_weights": True,      # 시장 상태 기반 스코어 가중치 동적 조절
}

# ── Level 5: 파라미터 자동 로드 (param_optimizer / alpha_researcher) ──
_l5_params: dict = {}   # agent_params.json + alpha best_params.params 통합 뷰
try:
    from quant.param_optimizer import load_best_params as _load_opt_params
    _opt_params = _load_opt_params()  # brain/agent_params.json
    if _opt_params:
        _l5_params.update(_opt_params)
        _risk_overrideable = {"stop_loss", "invest_ratio", "buy_composite_min", "atr_multiplier"}
        applied = {}
        for _k, _v in _opt_params.items():
            if _k in _risk_overrideable and _v is not None:
                RISK[_k] = _v
                applied[_k] = _v
        if applied:
            log.info(f"[Level5] agent_params 적용: {applied}")
except Exception as _e:
    log.debug(f"Level5 agent_params 로드 스킵: {_e}")

# alpha/best_params.json — agent_params.json에 아직 반영 안된 경우 fallback
try:
    _best_p = Path(__file__).resolve().parents[1] / "brain" / "alpha" / "best_params.json"
    if _best_p.exists():
        _bp = json.loads(_best_p.read_text(encoding="utf-8"))
        _bp_params = _bp.get("params", {})
        for _k, _v in _bp_params.items():
            if _k not in _l5_params:        # agent_params에 없는 경우만 병합
                _l5_params[_k] = _v
        if _bp_params:
            log.info(f"[Level5] best_params 로드: {_bp_params}")
        if "atr_multiplier" in _bp_params and "atr_multiplier" not in (_opt_params or {}):
            RISK["atr_multiplier"] = _bp_params["atr_multiplier"]
except Exception as _e:
    log.debug(f"Level5 best_params 로드 스킵: {_e}")

# ── 텔레그램 ──────────────────────────────────────


def send_telegram(msg: str, priority: "_TgPriority" = _TgPriority.URGENT) -> None:
    _tg_send(msg, priority=priority)


# ── 주문 실행 헬퍼 ────────────────────────────────────
def _execute_sell(qty: float, label: str, *, pnl_pct: float | None = None,
                  close: bool = True, price: float = 0.0) -> bool:
    """Upbit 시장가 매도 + 메트릭 기록 + 선택적 포지션 종료."""
    if DRY_RUN:
        return True
    try:
        result = upbit.sell_market_order("KRW-BTC", qty)
        if result is None or (isinstance(result, dict) and result.get("error")):
            log.error(f"Upbit {label} 매도 실패: {result}")
            return False
    except Exception as e:
        log.error(f"Upbit {label} 매도 API 에러: {e}")
        send_telegram(f"🚨 BTC {label} 매도 API 에러: {e}")
        return False
    try:
        from common.prometheus_metrics import record_trade, set_pnl
        record_trade("BTC", "sell")
        if pnl_pct is not None:
            set_pnl("BTC", float(pnl_pct))
    except Exception:
        log.debug("Prometheus 매도 메트릭 기록 실패")
    if close and price > 0:
        close_all_positions(price)
    return True


def _execute_buy(invest_krw: float) -> dict | None:
    """Upbit 시장가 매수. 성공 시 API 응답 dict, 실패 시 None."""
    try:
        result = upbit.buy_market_order("KRW-BTC", invest_krw)
        if result is None or (isinstance(result, dict) and result.get("error")):
            log.error(f"Upbit 매수 실패: {result}")
            send_telegram(f"🚨 BTC 매수 API 에러: {result}")
            return None
        return result
    except Exception as e:
        log.error(f"Upbit 매수 API 에러: {e}")
        send_telegram(f"🚨 BTC 매수 API 에러: {e}")
        return None


# ── 시장 데이터 ───────────────────────────────────
def get_market_data() -> "pd.DataFrame | None":  # noqa: F821 — forward-ref string
    return pyupbit.get_ohlcv("KRW-BTC", interval=BTC_MARKET_INTERVAL, count=BTC_MARKET_COUNT)


def _market_data_ready(df) -> bool:
    return df is not None and not df.empty and {"close", "high", "low", "volume"}.issubset(df.columns)


def _latest_market_price() -> float:
    df = get_market_data()
    if _market_data_ready(df):
        try:
            return float(df["close"].iloc[-1])
        except Exception as e:
            log.debug(f"최신 시장가 파싱 실패: {e}")
            return 0.0
    return 0.0

# ── 기술적 지표 ───────────────────────────────────


def calculate_indicators(df) -> dict:
    from ta.momentum import RSIIndicator
    from ta.trend import MACD, EMAIndicator
    from ta.volatility import AverageTrueRange, BollingerBands

    close = df["close"]
    rsi_w = int(_l5_params.get("rsi_window", 14))
    bb_w = int(_l5_params.get("bb_window", 20))
    ema20 = EMAIndicator(close, window=20).ema_indicator().iloc[-1]
    ema50 = EMAIndicator(close, window=50).ema_indicator().iloc[-1]
    rsi = RSIIndicator(close, window=rsi_w).rsi().iloc[-1]
    macd_obj = MACD(close)
    macd = macd_obj.macd_diff().iloc[-1]
    bb = BollingerBands(close, window=bb_w)
    atr = AverageTrueRange(df["high"], df["low"], close, window=14).average_true_range().iloc[-1]

    return {
        "price": df["close"].iloc[-1],
        "ema20": round(ema20, 0),
        "ema50": round(ema50, 0),
        "rsi": round(rsi, 1),
        "macd": round(macd, 0),
        "bb_upper": round(bb.bollinger_hband().iloc[-1], 0),
        "bb_lower": round(bb.bollinger_lband().iloc[-1], 0),
        "volume": round(df["volume"].iloc[-1], 4),
        "atr": round(atr, 0),
    }

# ── 거래량 분석 ───────────────────────────────────


def get_volume_analysis(df) -> dict:
    try:
        if df is None or df.empty or "volume" not in df.columns:
            return {"ratio": 1.0, "label": "거래량 분석 실패"}
        cur = df["volume"].iloc[-1]
        avg20 = df["volume"].rolling(20).mean().iloc[-1]
        ratio = round(cur / avg20, 2) if avg20 > 0 else 1.0

        # 5분봉 거래량이 비정상적으로 0일 때 1시간봉으로 fallback
        if ratio < 0.01:
            try:
                h_df = pyupbit.get_ohlcv("KRW-BTC", interval="minute60", count=30)
                if h_df is not None and not h_df.empty:
                    h_cur = h_df["volume"].iloc[-1]
                    h_avg = h_df["volume"].rolling(20).mean().iloc[-1]
                    if h_avg > 0:
                        ratio = round(h_cur / h_avg, 2)
            except Exception as e:
                log.debug(f"1시간봉 거래량 fallback 실패: {e}")

        if ratio >= 2.0:
            label = "🔥 거래량 급등 (강한 신호)"
        elif ratio >= 1.5:
            label = "📈 거래량 증가 (신호 강화)"
        elif ratio <= 0.5:
            label = "😴 거래량 급감 (신호 약함)"
        else:
            label = "➡️ 거래량 보통"

        return {"current": round(cur, 4), "avg20": round(avg20, 4),
                "ratio": ratio, "label": label}
    except Exception:
        return {"ratio": 1.0, "label": "거래량 분석 실패"}

# ── Fear & Greed ──────────────────────────────────


def get_fear_greed() -> dict:
    try:
        res = retry_call(requests.get, args=("https://api.alternative.me/fng/?limit=1",),
                         kwargs={"timeout": BTC_FG_API_TIMEOUT}, max_attempts=2, default=None)
        if res is None:
            return {"value": 50, "label": "Unknown", "msg": "⚪ 중립(50)"}
        data = res.json()["data"][0]
        value = int(data["value"])
        label = data["value_classification"]
        if value <= 25:
            msg = f"🔴 극도 공포({value}) — 역발상 매수 기회"
        elif value <= 45:
            msg = f"🟠 공포({value}) — 매수 우호적"
        elif value <= 55:
            msg = f"⚪ 중립({value})"
        elif value <= 75:
            msg = f"🟡 탐욕({value}) — 매수 주의"
        else:
            msg = f"🔴 극도 탐욕({value}) — 매수 금지"
        return {"value": value, "label": label, "msg": msg}
    except Exception:
        return {"value": 50, "label": "Unknown", "msg": "⚪ 중립(50)"}

# ── 1시간봉 추세 ──────────────────────────────────


def get_hourly_trend() -> dict:
    try:
        df = pyupbit.get_ohlcv("KRW-BTC", interval="minute60", count=50)
        from ta.momentum import RSIIndicator
        from ta.trend import EMAIndicator
        close = df["close"]
        ema20 = EMAIndicator(close, window=20).ema_indicator().iloc[-1]
        ema50 = EMAIndicator(close, window=50).ema_indicator().iloc[-1]
        rsi = RSIIndicator(close, window=14).rsi().iloc[-1]
        price = close.iloc[-1]

        if ema20 > ema50 and price > ema20:
            trend = "UPTREND"
        elif ema20 < ema50 and price < ema20:
            trend = "DOWNTREND"
        else:
            trend = "SIDEWAYS"

        return {"trend": trend, "ema20": round(ema20, 0),
                "ema50": round(ema50, 0), "rsi_1h": round(rsi, 1)}
    except Exception as e:
        log.warning(f"1시간봉 조회 실패: {e}")
        return {"trend": "UNKNOWN", "ema20": 0, "ema50": 0, "rsi_1h": 50}


def get_kimchi_premium():
    try:
        binance = retry_call(requests.get,
            args=("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",),
            kwargs={"timeout": 3}, max_attempts=2, default=None)
        if binance is None:
            return None
        binance = binance.json()
        binance_price = float(binance["price"])
        usdt = retry_call(requests.get,
            args=("https://api.upbit.com/v1/ticker?markets=KRW-USDT",),
            kwargs={"timeout": 3}, max_attempts=2, default=None)
        if usdt is None:
            return None
        usdt = usdt.json()
        usd_krw = float(usdt[0]["trade_price"])
        binance_krw = binance_price * usd_krw
        upbit_price = pyupbit.get_current_price("KRW-BTC")
        if upbit_price is None:
            return None
        premium = (float(upbit_price) - binance_krw) / binance_krw * 100
        return round(premium, 2)
    except Exception as e:
        log.warning(f"김치 프리미엄 조회 실패: {e}")
        return None


# ── 일봉 모멘텀 분석 ─────────────────────────────
_daily_momentum_cache: dict = {"data": None, "ts": 0.0}


def get_daily_momentum() -> dict:
    """yfinance BTC-USD 일봉으로 RSI/BB/거래량/수익률 분석. TTL 1시간 캐시."""
    import time as _time
    _ttl = BTC_DAILY_CACHE_TTL
    now_ts = _time.time()
    if _daily_momentum_cache["data"] is not None and now_ts - _daily_momentum_cache["ts"] < _ttl:
        return _daily_momentum_cache["data"]
    try:
        import yfinance as yf
        df = yf.download("BTC-USD", period="90d", interval="1d", progress=False)
        if df.empty:
            return {"rsi_d": 50, "bb_pct": 50, "vol_ratio_d": 1.0,
                    "ret_7d": 0, "ret_30d": 0}
        close = df["Close"].squeeze()
        from ta.momentum import RSIIndicator
        from ta.volatility import BollingerBands
        rsi_w = int(_l5_params.get("rsi_window", 14))
        bb_w = int(_l5_params.get("bb_window", 20))
        rsi_d = RSIIndicator(close, window=rsi_w).rsi().iloc[-1]
        bb = BollingerBands(close, window=bb_w)
        bb_h, bb_l = bb.bollinger_hband().iloc[-1], bb.bollinger_lband().iloc[-1]
        bb_pct = (close.iloc[-1] - bb_l) / (bb_h - bb_l) * 100 if bb_h > bb_l else 50
        vol = df["Volume"].squeeze()
        vol_avg = vol.rolling(20).mean().iloc[-1]
        vol_ratio_d = vol.iloc[-1] / vol_avg if vol_avg > 0 else 1.0
        ret_7d = (close.iloc[-1] / close.iloc[-8] - 1) * 100 if len(close) > 8 else 0
        ret_30d = (close.iloc[-1] / close.iloc[-31] - 1) * 100 if len(close) > 31 else 0
        result = {
            "rsi_d": round(float(rsi_d), 1),
            "bb_pct": round(float(bb_pct), 1),
            "vol_ratio_d": round(float(vol_ratio_d), 2),
            "ret_7d": round(float(ret_7d), 1),
            "ret_30d": round(float(ret_30d), 1),
        }
        _daily_momentum_cache["data"] = result
        _daily_momentum_cache["ts"] = now_ts
        return result
    except Exception as e:
        log.warning(f"일봉 모멘텀 조회 실패: {e}")
        return {"rsi_d": 50, "bb_pct": 50, "vol_ratio_d": 1.0,
                "ret_7d": 0, "ret_30d": 0}


# ── BTC 복합 스코어 (v6 — 온체인 + 동적 가중치) ──
def calc_btc_composite(fg_value, rsi_d, bb_pct, vol_ratio_d, trend, ret_7d=0,
                        funding=None, oi=None, ls_ratio=None, kimchi=None,
                        regime: str = "TRANSITION",
                        news_sentiment: float = 0.0,
                        whale=None):
    """
    BTC 매수 복합 스코어 (0~100).
    v6: 온체인 데이터(펀딩비, OI, 롱숏비율) 추가.
    v6.1: regime 파라미터로 실제 동적 가중치 적용.
    v6.2: news_sentiment (-1.0~+1.0) → ±8점 반영.

    배점 구조:
    - F&G: 22점 (공포 구간 보상)
    - RSI일봉: 20점 (과매도)
    - BB: 12점 (하단 근접)
    - 거래량: 10점 (확신 지표)
    - 추세: 12점 (방향성)
    - 펀딩비: 8점 (숏 크라우딩 = 매수 기회)
    - 뉴스 감정: ±8점 (긍정/부정 뉴스)
    - 롱숏비율: 6점 (역발상)
    - OI/고래: 5점
    - 보너스: ±5점
    - 레짐 조정: RISK_ON +5 / RISK_OFF -10 / CRISIS -20
    """
    # F&G (낮을수록 매수 기회)
    if fg_value <= 10: fg_sc = 22
    elif fg_value <= 20: fg_sc = 18
    elif fg_value <= 30: fg_sc = 13
    elif fg_value <= 45: fg_sc = 7
    elif fg_value <= 55: fg_sc = 3
    else: fg_sc = 0

    # 일봉 RSI
    if rsi_d <= 30: rsi_sc = 20
    elif rsi_d <= 38: rsi_sc = 16
    elif rsi_d <= 45: rsi_sc = 12
    elif rsi_d <= 55: rsi_sc = 6
    elif rsi_d <= 65: rsi_sc = 2
    else: rsi_sc = 0

    # BB 포지션
    if bb_pct <= 10: bb_sc = 12
    elif bb_pct <= 25: bb_sc = 9
    elif bb_pct <= 40: bb_sc = 6
    elif bb_pct <= 55: bb_sc = 2
    else: bb_sc = 0

    # 일봉 거래량
    if vol_ratio_d >= 2.0: vol_sc = 10
    elif vol_ratio_d >= 1.5: vol_sc = 8
    elif vol_ratio_d >= 1.0: vol_sc = 5
    elif vol_ratio_d >= 0.6: vol_sc = 2
    else: vol_sc = 0

    # 추세
    if trend == "UPTREND": tr_sc = 12
    elif trend == "SIDEWAYS": tr_sc = 6
    else: tr_sc = 0

    # ── 온체인 신호 (신규) ──

    # 펀딩비 (숏 크라우딩 = 매수 기회)
    funding_sc = 0
    funding = funding or {}
    fr_signal = funding.get("signal", "NEUTRAL")
    if fr_signal == "SHORT_CROWDED":
        funding_sc = 8  # 숏 과열 = 숏 스퀴즈 기대
    elif fr_signal == "SLIGHTLY_SHORT":
        funding_sc = 5
    elif fr_signal == "NEUTRAL":
        funding_sc = 3
    elif fr_signal == "SLIGHTLY_LONG":
        funding_sc = 1
    elif fr_signal == "LONG_CROWDED":
        funding_sc = -2  # 롱 과열 = 매수 위험

    # 롱/숏 비율 (역발상)
    ls_sc = 0
    ls_ratio = ls_ratio or {}
    ls_signal = ls_ratio.get("signal", "NEUTRAL")
    if ls_signal == "EXTREME_SHORT":
        ls_sc = 6  # 숏 포지션 쏠림 = 반등 기대
    elif ls_signal == "SHORT_BIAS":
        ls_sc = 4
    elif ls_signal == "NEUTRAL":
        ls_sc = 2
    elif ls_signal == "LONG_BIAS":
        ls_sc = 0
    elif ls_signal == "EXTREME_LONG":
        ls_sc = -3  # 롱 극단 = 조정 위험

    # OI
    oi_sc = 0
    oi = oi or {}
    oi_signal = oi.get("signal", "OI_NORMAL")
    if oi_signal == "OI_LOW":
        oi_sc = 3  # 저 OI = 새 포지션 유입 여지
    elif oi_signal == "OI_NORMAL":
        oi_sc = 2
    elif oi_signal == "OI_SURGE":
        oi_sc = -1  # OI 급등 = 변동성 주의

    # 뉴스 감정 (±8점)
    if news_sentiment >= 0.8: news_sc = 8
    elif news_sentiment >= 0.5: news_sc = 5
    elif news_sentiment >= 0.2: news_sc = 2
    elif news_sentiment > -0.2: news_sc = 0
    elif news_sentiment > -0.5: news_sc = -2
    elif news_sentiment > -0.8: news_sc = -5
    else: news_sc = -8

    # 보너스
    bonus = 0
    if ret_7d <= -15: bonus = 5
    elif ret_7d <= -10: bonus = 3

    if ret_7d > 0 and trend == "UPTREND":
        bonus += 2
    elif ret_7d < -5 and trend == "DOWNTREND":
        bonus -= 3

    # 김치프리미엄 보정
    if kimchi is not None:
        if kimchi <= -3.0:
            bonus += 3  # 역프리미엄 = 매수 기회
        elif kimchi <= -1.5:
            bonus += 1
        elif kimchi >= 5.0:
            bonus -= 3  # 과열 프리미엄
        elif kimchi >= 3.0:
            bonus -= 1

    # 고래 신호 (±3점)
    whale_sc = 0
    whale = whale or {}
    whale_sig = whale.get("signal", "NEUTRAL")
    if whale_sig == "HODL_SIGNAL":
        whale_sc = 3   # 거래소 유출 급증 = 장기 보유 신호 → 매수 우호
    elif whale_sig in ("SELL_PRESSURE", "LTH_DISTRIBUTION_RISK"):
        whale_sc = -3  # 거래소 유입 급증 / LTH 분배 → 매도 압력
    elif whale_sig == "LTH_MOVEMENT_ALERT":
        whale_sc = -1

    # ── 레짐 기반 실제 동적 조정 (v6.1) ──────────────
    _regime_bonus_map = {
        "RISK_ON": +5,   # 강세장: 진입 문턱 낮춤
        "TRANSITION": 0,
        "RISK_OFF": -10,   # 약세장: 진입 억제
        "CRISIS": -20,   # 위기: 강력 억제
    }
    regime_adj = _regime_bonus_map.get(str(regime).upper(), 0)

    # v6.2 A1: 극도공포 오버라이드 — F&G<=15 + UPTREND 시 레짐 페널티 무시
    if fg_value is not None and fg_value <= 15 and str(trend).upper() == "UPTREND" and regime_adj < 0:
        regime_adj = 0

    raw = fg_sc + rsi_sc + bb_sc + vol_sc + tr_sc + funding_sc + ls_sc + oi_sc + news_sc + whale_sc + bonus + regime_adj
    legacy_total = max(0, min(raw, 100))

    components = {
        "total": legacy_total,
        "fg": fg_sc, "rsi": rsi_sc, "bb": bb_sc,
        "vol": vol_sc, "trend": tr_sc,
        "funding": funding_sc, "ls": ls_sc, "oi": oi_sc,
        "news": news_sc,
        "whale": whale_sc,
        "bonus": bonus,
        "regime_adj": regime_adj,
        "regime": regime,
        "raw": raw,
    }

    if RISK.get("dynamic_weights"):
        weights = _load_ic_weights()
        if weights:
            components["total"] = _apply_weighted_score(components, weights=weights)

    return components


# ── 포지션 관리 ───────────────────────────────────
def get_open_position():
    # audit fix: Supabase 실패 시 None 대신 예외 전파 → 호출부에서 사이클 스킵
    try:
        res = supabase.table("btc_position")\
                      .select("*").eq("status", "OPEN")\
                      .order("entry_time", desc=True).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        log.error(f"포지션 조회 실패: {e}")
        raise  # None 대신 예외를 그대로 전파 → 사이클 중단


def open_position(entry_price, quantity, entry_krw) -> bool:
    row = {
        "entry_price": entry_price,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "quantity": quantity,
        "entry_krw": entry_krw,
        "status": "OPEN",
    }
    try:
        supabase.table("btc_position").insert({**row, "highest_price": entry_price}).execute()
        return True
    except Exception as e:
        log.debug(f"highest_price 포함 포지션 오픈 실패, fallback 시도: {e}")
    try:
        supabase.table("btc_position").insert(row).execute()
        return True
    except Exception as e:
        log.error(f"포지션 오픈 실패: {e}")
        return False


def open_position_with_context(
    entry_price,
    quantity,
    entry_krw,
    *,
    fg_value=None,
    rsi_d=None,
    bb_pct=None,
    vol_ratio_d=None,
    trend=None,
    funding_rate=None,
    ls_ratio=None,
    oi_ratio=None,
    kimchi=None,
    composite_score=None,
    market_regime=None,
    atr_stop_price=None,
    signal_source=None,
) -> bool:
    """Open position and persist signal context for later IC evaluation.

    This keeps backward compatibility with existing Supabase schemas:
    if the additional columns do not exist, it falls back to the minimal insert.
    """
    base_row = {
        "entry_price": entry_price,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "quantity": quantity,
        "entry_krw": entry_krw,
        "status": "OPEN",
        "highest_price": entry_price,
    }
    ctx_row = {
        **base_row,
        "fg_value": fg_value,
        "rsi_d": rsi_d,
        "bb_pct": bb_pct,
        "vol_ratio_d": vol_ratio_d,
        "trend": trend,
        "funding_rate": funding_rate,
        "ls_ratio": ls_ratio,
        "oi_ratio": oi_ratio,
        "kimchi": kimchi,
        "composite_score": composite_score,
        "market_regime": market_regime,
        "atr_stop_price": atr_stop_price,
        # v6.3: 시그널 소스 추적 (attribution용)
        "signal_source": signal_source,
    }

    try:
        supabase.table("btc_position").insert(ctx_row).execute()
        return True
    except Exception:
        # signal_source 컬럼 부재 시 graceful fallback: 해당 키 제거 후 재시도
        try:
            ctx_row.pop("signal_source", None)
            supabase.table("btc_position").insert(ctx_row).execute()
            return True
        except Exception:
            return open_position(entry_price, quantity, entry_krw)


def close_all_positions(exit_price):
    try:
        res = supabase.table("btc_position")\
                      .select("*").eq("status", "OPEN").execute()
        for pos in res.data:
            # audit fix: entry_price=0 ZeroDivisionError 방지
            _ep = float(pos.get("entry_price") or 0)
            if _ep <= 0:
                log.error(f"close_all_positions: entry_price 이상({_ep}) — pos id={pos.get('id')} 스킵")
                continue
            pnl = (exit_price - _ep) * pos["quantity"]
            pnl_pct = (exit_price - _ep) / _ep * 100
            try:
                supabase.table("btc_position").update({
                    "status": "CLOSED",
                    "exit_price": exit_price,
                    "exit_time": datetime.now(timezone.utc).isoformat(),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                }).eq("id", pos["id"]).execute()
            except Exception:
                # pnl/pnl_pct 컬럼 미존재 시 최소 업데이트 (btc_position_schema.sql 실행 전 graceful fallback)
                supabase.table("btc_position").update({
                    "status": "CLOSED",
                    "exit_price": exit_price,
                    "exit_time": datetime.now(timezone.utc).isoformat(),
                }).eq("id", pos["id"]).execute()
    except Exception as e:
        log.error(f"포지션 종료 실패: {e}")

# ── 일일 손실 한도 ────────────────────────────────


def check_daily_loss() -> bool:
    # v6.2 B2: KST 시간대 통일 — 한국 기준 "오늘" 사용
    # P1-2: 전체 ISO datetime 사용 (날짜 문자열 비교 오차 방지)
    try:
        _KST = ZoneInfo("Asia/Seoul")
        today_start = datetime.now(_KST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        res = supabase.table("btc_position")\
                        .select("pnl, entry_krw")\
                        .eq("status", "CLOSED")\
                        .gte("exit_time", today_start).execute()
        if not res.data:
            return False
        total_pnl = sum(float(r["pnl"] or 0) for r in res.data)
        total_krw = sum(float(r["entry_krw"] or 0) for r in res.data)
        if total_krw > 0 and (total_pnl / total_krw) <= RISK["max_daily_loss"]:
            send_telegram(
                f"🚨 <b>일일 손실 한도 {RISK['max_daily_loss']*100:.0f}% 초과</b>\n"
                f"봇 자동 정지 — 내일 재시작"
            )
            return True
    except Exception as e:
        log.debug(f"일일 손실 한도 체크 실패: {e}")
    return False

# ── 룰 기반 BTC 신호 (LLM 대체, 결정론적) ─────────


def _rule_meta() -> dict:
    """rule_based_btc_signal 모든 return decision_meta 통일 헬퍼 (PR #25 hotfix)."""
    return {
        "decision_source": "RULE",
        "model": None,
        "ai_latency_ms": None,
        "prompt_tokens": None,
        "response_tokens": None,
    }


def _map_signal_source(src):
    """signal["source"] → btc_trades.signal_source check constraint 통과 매핑 (PR #25 hotfix).

    btc_trades.signal_source check: NULL or one of rule/ml/llm/composite/manual.
    """
    if not src:
        return None
    s = str(src).upper()
    if s.startswith("RULE") or s == "AI_FAIL":
        return "rule"
    if s in ("AI", "LLM"):
        return "llm"
    if s == "ML":
        return "ml"
    if s == "MANUAL":
        return "manual"
    if s == "COMPOSITE":
        return "composite"
    return None


def rule_based_btc_signal(*args, **kwargs):
    """Wrapper — 모든 return path에 decision_meta 동봉 보장 (PR #25 hotfix)."""
    result = _rule_based_btc_signal_impl(*args, **kwargs)
    if isinstance(result, dict):
        result.setdefault("decision_meta", _rule_meta())
    return result


def _rule_based_btc_signal_impl(
    indicators,
    fg,
    htf,
    volume,
    *,
    comp: dict | None = None,
    rsi_d: float = 50.0,
    momentum: dict | None = None,
    funding: dict | None = None,
    ls_ratio: dict | None = None,
    regime: str = "TRANSITION",
) -> dict:
    """결정론적 BTC 매매 신호.

    v6.3에서 LLM 기반 `analyze_with_ai()`를 대체한다. 이전 system_prompt의 규칙을
    점수화(100점 만점)하여 BUY/SELL/HOLD를 산출한다. 동일 입력 → 동일 출력.

    입력:
      indicators: 5분봉 지표 (price, rsi, macd, macd_histogram, ...)
      fg: Fear&Greed dict (value, label, msg)
      htf: 1시간봉 추세 dict (trend, rsi_1h)
      volume: 거래량 dict (ratio, label)
      comp: 복합스코어 dict (total, ...)
      rsi_d: 일봉 RSI
      momentum: {bb_pct, ret_7d, ...}
      funding: 펀딩비 dict (rate, signal)
      ls_ratio: 롱숏비율 dict (ls_ratio, signal)
      regime: 시장 레짐 문자열

    반환: {"action": "BUY|SELL|HOLD", "confidence": int, "reason": str, "source": "RULE_BTC"}
    """
    mom = momentum or {}
    fund = funding or {}
    ls = ls_ratio or {}
    comp_total = (comp or {}).get("total", 0)

    fg_val = int((fg or {}).get("value", 50))
    vol_ratio = float((volume or {}).get("ratio", 1.0))
    trend = (htf or {}).get("trend", "UNKNOWN")
    rsi_5m = float(indicators.get("rsi", 50))
    macd_val = float(indicators.get("macd", 0))
    macd_hist = float(indicators.get("macd_histogram", 0))
    bb_pct = float(mom.get("bb_pct", 50))
    ret_7d = float(mom.get("ret_7d", 0))
    fund_rate = float(fund.get("rate", 0))
    ls_val = float(ls.get("ls_ratio", 1))

    # ── SELL 조건 우선 평가 (하나라도 만족) ──
    if trend == "DOWNTREND" and rsi_5m >= 65:
        return {
            "action": "SELL",
            "confidence": 75,
            "reason": f"[룰] DOWNTREND + 5m RSI {rsi_5m:.0f}>=65",
            "source": "RULE_BTC",
            "decision_meta": _rule_meta(),
        }
    if fg_val >= 75:
        return {
            "action": "SELL",
            "confidence": 75,
            "reason": f"[룰] 극도탐욕 F&G={fg_val}>=75",
            "source": "RULE_BTC",
            "decision_meta": _rule_meta(),
        }
    if rsi_d >= 70 and bb_pct >= 80:
        return {
            "action": "SELL",
            "confidence": 75,
            "reason": f"[룰] 과매수 dRSI={rsi_d:.0f} + BB%={bb_pct:.0f}",
            "source": "RULE_BTC",
            "decision_meta": _rule_meta(),
        }

    # ── BUY 필수 조건 ──
    is_extreme_fear = fg_val <= 20
    # 필수 1: 추세
    if trend == "DOWNTREND":
        return {
            "action": "HOLD",
            "confidence": 0,
            "reason": f"[룰] DOWNTREND — BUY 금지",
            "source": "RULE_BTC",
            "decision_meta": _rule_meta(),
        }
    # 필수 2: 공포 구간
    if fg_val > 55:
        return {
            "action": "HOLD",
            "confidence": 0,
            "reason": f"[룰] F&G {fg_val}>55 — BUY 구간 아님",
            "source": "RULE_BTC",
            "decision_meta": _rule_meta(),
        }
    # 필수 3: 거래량 (극도공포 면제)
    min_vol = 0.15 if is_extreme_fear else 0.3
    if vol_ratio <= min_vol:
        return {
            "action": "HOLD",
            "confidence": 0,
            "reason": f"[룰] 거래량 {vol_ratio:.2f}x<={min_vol} — BUY 금지",
            "source": "RULE_BTC",
            "decision_meta": _rule_meta(),
        }

    # ── BUY 점수 합산 ──
    score = 0
    reasons: list[str] = []

    if trend == "UPTREND":
        score += 20
        reasons.append("UPTREND+20")

    if fg_val <= 25:
        score += 25
        reasons.append(f"극도공포F&G{fg_val}+25")
    elif fg_val <= 40:
        score += 15
        reasons.append(f"공포F&G{fg_val}+15")

    if vol_ratio >= 2.0:
        score += 15
        reasons.append(f"거래량{vol_ratio:.1f}x+15")

    if macd_val > 0 and macd_hist > 0:
        score += 10
        reasons.append("MACD양전+10")

    if rsi_5m < 35:
        score += 10
        reasons.append(f"5mRSI{rsi_5m:.0f}과매도+10")

    if rsi_d < 50 and ret_7d > -5:
        score += 10
        reasons.append(f"dRSI{rsi_d:.0f}양호+10")

    if comp_total >= 60:
        score += 10
        reasons.append(f"복합{comp_total}+10")

    if fund_rate < 0:
        score += 5
        reasons.append(f"음수펀딩{fund_rate:+.4f}+5")

    if ls_val < 0.8:
        score += 5
        reasons.append(f"숏과다LS{ls_val:.2f}+5")

    # BUY 최소 신뢰도 65
    if score >= 65:
        return {
            "action": "BUY",
            "confidence": min(score, 95),
            "reason": f"[룰] {' '.join(reasons[:5])}",
            "source": "RULE_BTC",
            "decision_meta": {
                "decision_source": "RULE",
                "model": None,
                "ai_latency_ms": None,
                "prompt_tokens": None,
                "response_tokens": None,
            },
        }

    return {
        "action": "HOLD",
        "confidence": score,
        "reason": f"[룰] BUY 점수 {score}<65 ({' '.join(reasons[:3]) if reasons else 'no signal'})",
        "source": "RULE_BTC",
        "decision_meta": {
            "decision_source": "RULE",
            "model": None,
            "ai_latency_ms": None,
            "prompt_tokens": None,
            "response_tokens": None,
        },
    }


# ── Claude Haiku 호출 정책 (PR #25) ────────────────
# timeout 5s + 1회 retry (timeout만), 401/429/parse/empty 즉시 룰 fallback.
# cooldown 파일 mtime 기반 5분 (alert_manager 차용).
# meta: decision_source / model / ai_latency_ms / prompt_tokens / response_tokens.
_BTC_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_BTC_HAIKU_COOLDOWN_FILE = Path("/tmp/openclaw_btc_haiku_cooldown.ts")
_BTC_HAIKU_COOLDOWN_SEC = 300
_BTC_HAIKU_CLIENT = None


def _btc_haiku_in_cooldown() -> bool:
    if not _BTC_HAIKU_COOLDOWN_FILE.exists():
        return False
    import time as _t
    return (_t.time() - _BTC_HAIKU_COOLDOWN_FILE.stat().st_mtime) < _BTC_HAIKU_COOLDOWN_SEC


def _btc_haiku_set_cooldown(reason: str) -> None:
    try:
        _BTC_HAIKU_COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BTC_HAIKU_COOLDOWN_FILE.write_text(reason)
    except Exception:
        pass


def _btc_haiku_get_client(timeout: float):
    global _BTC_HAIKU_CLIENT
    if _BTC_HAIKU_CLIENT is None:
        import anthropic
        _BTC_HAIKU_CLIENT = anthropic.Anthropic(timeout=timeout)
    return _BTC_HAIKU_CLIENT


def _call_btc_haiku(user_prompt: str, system_prompt: str, timeout: float = 5.0):
    """Claude haiku-4-5 호출. (parsed_dict | None, meta) 반환."""
    import time as _t

    import anthropic
    meta = {
        "decision_source": "AI",
        "model": _BTC_HAIKU_MODEL,
        "ai_latency_ms": None,
        "prompt_tokens": None,
        "response_tokens": None,
    }
    if _btc_haiku_in_cooldown():
        return None, {**meta, "decision_source": "RULE_COOLDOWN", "model": None}

    for attempt in (1, 2):
        try:
            t0 = _t.monotonic()
            client = _btc_haiku_get_client(timeout=timeout)
            msg = client.messages.create(
                model=_BTC_HAIKU_MODEL,
                max_tokens=200,
                temperature=0.1,
                system=[{
                    "type": "text", "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_prompt}],
            )
            meta["ai_latency_ms"] = int((_t.monotonic() - t0) * 1000)
            usage = getattr(msg, "usage", None)
            if usage is not None:
                meta["prompt_tokens"] = getattr(usage, "input_tokens", None)
                meta["response_tokens"] = getattr(usage, "output_tokens", None)
            raw = (msg.content[0].text if msg.content else "").strip()
            if not raw:
                _btc_haiku_set_cooldown("EMPTY")
                return None, meta
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw), meta
        except anthropic.APITimeoutError:
            if attempt == 1:
                continue
            return None, meta
        except anthropic.AuthenticationError:
            _btc_haiku_set_cooldown("AUTH_401")
            return None, meta
        except anthropic.RateLimitError:
            _btc_haiku_set_cooldown("RATE_429")
            return None, meta
        except json.JSONDecodeError:
            return None, meta
        except Exception as e:
            log.warning(f"BTC Claude 호출 실패: {e}")
            return None, meta
    return None, meta


# ── AI 분석 ───────────────────────────────────────
# DEPRECATED: 룰 기반 rule_based_btc_signal()로 대체됨 (v6.3).
# 매매 결정에서 LLM 의존 제거. Phase 4 페이퍼 검증 완료 후 완전 삭제 예정.
# 측정용 보존 중 — 호출 금지.
def analyze_with_ai(
    indicators, news_summary, fg, htf, volume,
    *,
    comp: dict | None = None,
    rsi_d: float = 50.0,
    momentum: dict | None = None,
    funding: dict | None = None,
    ls_ratio: dict | None = None,
    regime: str = "TRANSITION",
    memory_context: str = "",
) -> dict:

    trend_map = {
        "UPTREND": "📈 상승 추세 — 매수 우호적",
        "DOWNTREND": "📉 하락 추세 — 매수 금지",
        "SIDEWAYS": "➡️ 횡보 — 신중 판단",
        "UNKNOWN": "❓ 불명확 — HOLD 우선",
    }

    if volume["ratio"] >= 2.0:
        vol_comment = f"🔥 거래량 급등({volume['ratio']}배) — 신뢰도 높음"
    elif volume["ratio"] >= 1.5:
        vol_comment = f"📈 거래량 증가({volume['ratio']}배)"
    elif volume["ratio"] <= 0.5:
        vol_comment = f"😴 거래량 급감({volume['ratio']}배) — BUY 금지"
    else:
        vol_comment = f"➡️ 거래량 보통({volume['ratio']}배)"

    mom = momentum or {}
    fund = funding or {}
    ls = ls_ratio or {}
    comp_total = (comp or {}).get("total", 0)

    system_prompt = """당신은 비트코인 퀀트 트레이더입니다.
아래 데이터로 매매 신호를 JSON으로만 출력하세요.

[매매 규칙]
- BUY 조건:
  1. 1시간봉 DOWNTREND가 아닐 것
  2. Fear&Greed <= 55 (공포 구간 우선 매수)
  3. 거래량 0.3배 이하면 BUY 금지 (단, F&G<=20이면 면제)
  4. 거래량 2배 이상이면 신뢰도 +10
  5. F&G <= 25 구간은 적극 매수 (역발상)
  6. 복합스코어 < 40이면 BUY 신뢰도 낮게 (< 70)

- SELL 조건 (하나라도):
  1. 1시간봉 DOWNTREND + RSI 65 이상
  2. Fear&Greed >= 75
  3. 일봉 RSI >= 70 AND BB% >= 80 (과매수 + 상단)

- HOLD: 위 미충족 또는 불확실
- 신뢰도 65% 미만 → HOLD

[출력 형식 - JSON만]
{"action":"BUY또는SELL또는HOLD","confidence":0~100,"reason":"한줄근거"}"""

    user_prompt = f"""[복합 스코어] {comp_total}/100 (시장 레짐: {regime})

[5분봉 지표]
{json.dumps(indicators, ensure_ascii=False)}

[일봉 지표]
RSI: {rsi_d:.1f} | BB%: {mom.get('bb_pct', 50):.1f}% | 7일수익: {mom.get('ret_7d', 0):+.1f}%

[온체인 신호]
펀딩비: {fund.get('rate', 0):+.4f}% ({fund.get('signal', 'NEUTRAL')}) | 롱숏비율: {ls.get('ls_ratio', 1):.2f} ({ls.get('signal', 'NEUTRAL')})

[거래량 분석]
{vol_comment}

[1시간봉 추세]
{trend_map.get(htf['trend'], '❓ 불명확')} / RSI: {htf['rsi_1h']}

[시장 심리]
{fg['msg']}

[최근 거래 기억 — 반드시 참고하여 같은 실수 반복 금지]
{memory_context if memory_context else "기억 없음"}

[최근 뉴스]
{news_summary}"""

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key or is_quota_exceeded():
        return {"action": "HOLD", "confidence": 0, "reason": "Anthropic API 미설정 또는 quota 초과"}

    # RSI/FG 버킷팅 캐시 — 변화 미미하면 이전 결과 재사용 (10분 TTL)
    from common.cache import get_cached, set_cached
    rsi_val = indicators.get("rsi", 50)
    fg_val = fg.get("value", 50) if fg else 50
    cache_key = f"btc_ai:{int(rsi_val) // 5}:{int(fg_val) // 10}"
    cached = get_cached(cache_key)
    if cached is not None:
        log.debug(f"BTC AI 캐시 히트: {cache_key}")
        return cached

    parsed, meta = _call_btc_haiku(user_prompt, system_prompt)
    if parsed is not None:
        parsed["decision_meta"] = meta
        set_cached(cache_key, parsed, ttl=BTC_AI_CACHE_TTL)
        return parsed
    # Claude 실패 → HOLD with meta. (DEPRECATED 함수이므로 호출 0건이지만 정합성 유지)
    return {
        "action": "HOLD",
        "confidence": 0,
        "reason": "AI 호출 실패",
        "source": "AI_FAIL",
        "decision_meta": meta,
    }

# ── 분할 매수 단계 (복합 스코어 기반) ─────────────


def get_split_stage(composite_total: float) -> int:
    """복합 스코어가 높을수록 큰 비중으로 매수."""
    if composite_total >= 70: return 3
    if composite_total >= 55: return 2
    return 1

# ── 주문 실행 ─────────────────────────────────────


def execute_trade(
    signal,
    indicators,
    fg=None,
    volume=None,
    comp=None,
    *,
    funding=None,
    oi=None,
    ls_ratio=None,
    kimchi=None,
    market_regime=None,
    rsi_d=None,
    bb_pct=None,
    vol_ratio_d=None,
    trend=None,
) -> dict:
    global _btc_buy_blocked

    # ── 코드 레벨 안전 필터 (복합 스코어 기반) ──
    if signal["action"] == "BUY":
        if _btc_buy_blocked:
            return {"result": "BLOCKED_DRAWDOWN"}
        # audit fix: CrossMarket 리스크 체크
        global _cmr_instance
        try:
            from quant.risk.cross_market_manager import CrossMarketRiskManager
            if _cmr_instance is None:
                _cmr_instance = CrossMarketRiskManager()
            cm_result = _cmr_instance.evaluate()
            if cm_result.buy_blocked:
                log.warning(f"CrossMarket 리스크 차단: {cm_result.block_reasons}")
                return {"result": "CROSS_MARKET_BLOCKED", "reasons": cm_result.block_reasons}
        except Exception as _e:
            log.warning(f"CrossMarket 체크 실패 (무시): {_e}")
        if fg and fg["value"] > 75:
            log.warning(f"F&G {fg['value']} > 75 (극도 탐욕) — BUY 차단")
            return {"result": "BLOCKED_FG"}
        is_extreme_fear = fg and fg["value"] <= 20
        if volume and volume["ratio"] <= 0.15 and not is_extreme_fear:
            log.warning(f"5분봉 거래량 {volume['ratio']}x 거의 0 — BUY 차단")
            return {"result": "BLOCKED_VOLUME"}

    # ── 신뢰도 필터 ──
    if signal["confidence"] < RISK["min_confidence"]:
        return {"result": "SKIP"}

    btc_balance = upbit.get_balance("BTC") or 0
    krw_balance = upbit.get_balance("KRW") or 0
    # audit fix: 포지션 조회 실패 시 사이클 스킵
    try:
        pos = get_open_position()
    except Exception:
        log.error("포지션 조회 실패 — 사이클 스킵")
        return {"result": "DB_ERROR"}
    price = indicators["price"]

    if btc_balance > 0.00001 and not pos:
        log.warning(
            "BTC 잔고는 있으나 DB OPEN 포지션이 없음 — 잔고/DB 상태 불일치 가능 | "
            f"btc_balance={btc_balance:.8f}"
        )

    # ── 손절/익절 + 트레일링 스탑 ──
    if btc_balance > 0.00001 and pos:
        # audit fix: entry_price=0 ZeroDivisionError 방지
        entry_price = float(pos["entry_price"])
        if entry_price <= 0:
            log.error(f"entry_price 이상: {entry_price} — 손절/익절 로직 스킵")
            return {"result": "INVALID_ENTRY_PRICE"}
        change = (price - entry_price) / entry_price
        fee_cost = RISK["fee_buy"] + RISK["fee_sell"]
        net_change = change - fee_cost

        # 고점 추적 (highest_price — 컬럼 없으면 무시)
        highest = float(pos.get("highest_price") or entry_price)
        if price > highest:
            highest = price
            if not DRY_RUN:
                try:
                    supabase.table("btc_position").update(
                        {"highest_price": highest}
                    ).eq("id", pos["id"]).execute()
                except Exception as e:
                    log.debug(f"highest_price DB 업데이트 실패: {e}")

        # 적응형 트레일링 스탑: 수익 구간별 트레일링 % 조절
        if net_change > RISK["trailing_activate"] and highest > 0:
            drop = (highest - price) / highest
            if RISK.get("trailing_adaptive"):
                if net_change >= 0.10:
                    trail_pct = 0.015   # 10%+ 수익 시 1.5% 트레일링 (빡빡하게)
                elif net_change >= 0.06:
                    trail_pct = 0.02    # 6-10% 수익 시 2%
                else:
                    trail_pct = 0.025   # 1.5-6% 수익 시 2.5% (넉넉하게)
            else:
                trail_pct = RISK["trailing_stop"]
            if drop >= trail_pct:
                sell_qty = btc_balance * (1 - BTC_EXECUTION_SLIPPAGE)
                _execute_sell(sell_qty, "트레일링", pnl_pct=net_change * 100, price=price)
                send_telegram(
                    f"📉 <b>트레일링 스탑</b>\n"
                    f"고점: {highest:,.0f}원 → 현재가: {price:,.0f}원\n"
                    f"하락폭: {drop*100:.1f}% (기준: {trail_pct*100:.1f}%) / 수익: {net_change*100:.2f}%"
                )
                return {"result": "TRAILING_STOP"}

        # ATR 동적 손절 (진입 시 계산된 ATR 기반 손절가)
        atr_stop_price = float(pos.get("atr_stop_price") or 0)
        if atr_stop_price and price < atr_stop_price:
            sell_qty = btc_balance * (1 - BTC_EXECUTION_SLIPPAGE)
            _execute_sell(sell_qty, "ATR손절", pnl_pct=net_change * 100, price=price)
            send_telegram(
                f"🛑 <b>ATR 동적 손절</b>\n"
                f"진입가: {entry_price:,}원\n"
                f"ATR 손절가: {atr_stop_price:,.0f}원 → 현재가: {price:,}원\n"
                f"손실(비용 포함): {net_change*100:.2f}%"
            )
            if notify_openclaw:
                try:
                    notify_openclaw("btc_stop_loss", "BTC 손절 실행", urgent=True)
                except Exception:
                    log.debug("notify_openclaw ATR손절 알림 실패")
            return {"result": "ATR_STOP_LOSS"}

        # 고정 % 손절 (fallback)
        if net_change <= RISK["stop_loss"]:
            sell_qty = btc_balance * (1 - BTC_EXECUTION_SLIPPAGE)
            _execute_sell(sell_qty, "고정손절", pnl_pct=net_change * 100, price=price)
            send_telegram(
                f"🛑 <b>손절 실행</b>\n"
                f"진입가: {entry_price:,}원\n"
                f"현재가: {price:,}원\n"
                f"손실(비용 포함): {net_change*100:.2f}%"
            )
            if notify_openclaw:
                try:
                    notify_openclaw("btc_stop_loss", "BTC 손절 실행", urgent=True)
                except Exception:
                    log.debug("notify_openclaw 고정손절 알림 실패")
            return {"result": "STOP_LOSS"}

        # ── 다단계 분할 익절 ──
        partial_1_done = pos.get("partial_1_sold") or pos.get("partial_sold", False)
        partial_2_done = pos.get("partial_2_sold", False)

        # 1단계: 8% → 보유량의 50% 매도
        if net_change >= RISK.get("partial_tp_pct", 0.08) and not partial_1_done and btc_balance > 0.0001:
            ratio = RISK.get("partial_tp_ratio", 0.50)
            sell_qty = btc_balance * ratio * (1 - BTC_EXECUTION_SLIPPAGE)
            if _execute_sell(sell_qty, "분할익절1", pnl_pct=net_change * 100, close=False):
                try:
                    supabase.table("btc_position").update(
                        {"partial_1_sold": True, "partial_sold": True}
                    ).eq("id", pos["id"]).execute()
                except Exception:
                    log.debug("분할익절1 DB 플래그 업데이트 실패")
            send_telegram(
                f"🟡 <b>분할 익절 1단계 ({int(ratio*100)}%)</b>\n"
                f"진입가: {entry_price:,}원 | 현재가: {price:,}원\n"
                f"수익: +{net_change*100:.2f}% | 매도량: {sell_qty:.6f} BTC\n"
                f"잔여분 트레일링 스탑 + 2단계 익절 대기"
            )
            return {"result": "PARTIAL_TP_1"}

        # 2단계: 12% → 남은 물량의 50% 추가 매도
        if (net_change >= RISK.get("partial_tp_2_pct", 0.12)
                and partial_1_done and not partial_2_done and btc_balance > 0.0001):
            ratio2 = RISK.get("partial_tp_2_ratio", 0.50)
            sell_qty = btc_balance * ratio2 * (1 - BTC_EXECUTION_SLIPPAGE)
            if _execute_sell(sell_qty, "분할익절2", pnl_pct=net_change * 100, close=False):
                try:
                    supabase.table("btc_position").update(
                        {"partial_2_sold": True}
                    ).eq("id", pos["id"]).execute()
                except Exception:
                    log.debug("분할익절2 DB 플래그 업데이트 실패")
            send_telegram(
                f"🟢 <b>분할 익절 2단계 ({int(ratio2*100)}%)</b>\n"
                f"진입가: {entry_price:,}원 | 현재가: {price:,}원\n"
                f"수익: +{net_change*100:.2f}% | 매도량: {sell_qty:.6f} BTC\n"
                f"잔여분 트레일링 스탑으로 최종 보호"
            )
            return {"result": "PARTIAL_TP_2"}

        # 최대 익절 전량 (설정값%)
        if net_change >= RISK["take_profit"]:
            sell_qty = btc_balance * (1 - BTC_EXECUTION_SLIPPAGE)
            _execute_sell(sell_qty, "전량익절", pnl_pct=net_change * 100, price=price)
            send_telegram(
                f"✅ <b>전량 익절</b>\n"
                f"진입가: {entry_price:,}원 | 현재가: {price:,}원\n"
                f"수익(비용 포함): +{net_change*100:.2f}%"
            )
            return {"result": "TAKE_PROFIT"}

    # ── 분할 매수 ──
    if signal["action"] == "BUY":
        comp_total = comp["total"] if comp else 50
        stage = get_split_stage(comp_total)
        invest_krw = krw_balance * RISK["split_ratios"][stage - 1]

        target_market_weight = get_effective_market_weight('BTC')
        if target_market_weight is not None:
            current_btc_weight = (btc_balance * price) / max((btc_balance * price) + krw_balance, 1)
            if current_btc_weight >= target_market_weight + 0.02:
                return {"result": "OVERWEIGHT_MARKET"}

        # v6.2 C1: Half Kelly 포지션 사이징 — 50건 미만 시 보수적 기본값 사용
        recent_trades = load_recent_trades('btc', limit=100)
        _n_btc_trades = len(recent_trades)
        account_equity = krw_balance + btc_balance * price
        current_exposure = (btc_balance * price) / max(account_equity, 1)
        atr_pct = float(indicators.get("atr", 0) or 0) / price if price > 0 else 0.0
        if _n_btc_trades >= 50:
            wins = [t['pnl_pct'] for t in recent_trades if t.get('pnl_pct', 0) > 0]
            losses = [abs(t['pnl_pct']) for t in recent_trades if t.get('pnl_pct', 0) < 0]
            win_rate = len(wins) / _n_btc_trades if _n_btc_trades else 0.0
            avg_win = sum(wins) / len(wins) if wins else 0.02
            avg_loss = sum(losses) / len(losses) if losses else 0.03
        else:
            # 거래 이력 부족 — 보수적 기본값 (Half Kelly 최소화)
            win_rate = 0.40
            avg_win = 0.04
            avg_loss = 0.03
        sizing = KellyPositionSizer().size_position(
            account_equity=account_equity,
            price=price,
            win_rate=win_rate,
            payoff_ratio=avg_win / max(avg_loss, 0.001),
            current_total_exposure=current_exposure,
            atr_pct=atr_pct,
            conviction=max(0.0, min(1.0, comp_total / 100.0)),
        )
        kelly_fraction_val = float(sizing.get("capped_fraction", 0.0))
        config_invest_ratio = RISK["invest_ratio"] * RISK["split_ratios"][stage - 1]
        # min(Kelly, config) — Half Kelly가 config 상한을 초과하지 않도록
        effective_ratio = min(kelly_fraction_val, config_invest_ratio) if kelly_fraction_val > 0 else config_invest_ratio
        kelly_invest = account_equity * effective_ratio
        if kelly_invest > 0:
            invest_krw = kelly_invest
            log.info(f"[C1] Half Kelly: kelly={kelly_fraction_val:.3f} cfg={config_invest_ratio:.3f} eff={effective_ratio:.3f} → {invest_krw:,.0f}원")

        if invest_krw < 5000:
            return {"result": "INSUFFICIENT_KRW"}

        # 멱등성 체크: 동일 분 내 중복 BTC 매수 방지
        _order_id = generate_order_id("btc", "BTC", "buy", str(stage))
        if check_order_idempotency(get_supabase(), "btc_position", _order_id):
            log.warning(f"중복 주문 감지 — order_id={_order_id}")
            return {"result": "DUPLICATE_ORDER"}

        # SmartRouter 라우팅 결정 로깅 (BTC는 직접 주문 유지, 로깅만)
        try:
            _btc_router = SmartRouter()
            _btc_qty = invest_krw / price if price > 0 else 0
            _route_dec = _btc_router.decide(
                symbol="KRW-BTC", side="buy", total_qty=_btc_qty,
                market="btc", price_hint=price,
            )
            log.info(f"SmartRouter: {getattr(_route_dec, 'route', 'MARKET')} "
                     f"(spread={getattr(_route_dec, 'spread_bps', 0):.1f}bps)")
        except Exception as e:
            log.debug(f"SmartRouter 라우팅 결정 실패: {e}")

        if not DRY_RUN:
            result = _execute_buy(invest_krw)
            if result is None:
                return {"result": "ORDER_FAILED", "reason": "Upbit API 실패"}
            qty = float(result.get("executed_volume", 0)) or (invest_krw / price)
            # ATR 기반 손절가 계산 (진입 시점 ATR * 배수만큼 하락 시 손절)
            atr_val = indicators.get("atr", 0)
            atr_stop = round(price - atr_val * RISK["atr_multiplier"]) if atr_val else None
            # DB 저장 3회 재시도 (레이스 컨디션 방지)
            ok = False
            for _attempt in range(BTC_DB_RETRY_COUNT):
                ok = open_position_with_context(
                    price,
                    qty,
                    invest_krw,
                    fg_value=(fg or {}).get("value") if fg else None,
                    rsi_d=rsi_d,
                    bb_pct=bb_pct,
                    vol_ratio_d=vol_ratio_d,
                    trend=trend,
                    funding_rate=(funding or {}).get("rate") if funding else None,
                    ls_ratio=(ls_ratio or {}).get("ls_ratio") if ls_ratio else None,
                    oi_ratio=(oi or {}).get("ratio") if oi else None,
                    kimchi=kimchi,
                    composite_score=(comp or {}).get("total") if isinstance(comp, dict) else None,
                    market_regime=market_regime,
                    atr_stop_price=atr_stop,
                    signal_source=signal.get("source", "UNKNOWN"),
                )
                if ok:
                    break
                log.warning(f"포지션 DB 저장 재시도 {_attempt + 1}/{BTC_DB_RETRY_COUNT}")
                import time as _time; _time.sleep(BTC_DB_RETRY_SLEEP)
            if not ok:
                log.error(f"포지션 DB 저장 {BTC_DB_RETRY_COUNT}회 실패. qty={qty:.8f} BTC — 수동 확인 필요")
                send_telegram(
                    f"⚠️ BTC 매수 성공했으나 DB 저장 실패 (3회 재시도).\n"
                    f"수량: {qty:.8f} BTC\n수동 확인 후 처리하세요.",
                )
                return {"result": "POSITION_DB_FAIL"}
        else:
            log.info(f"[DRY_RUN] {stage}차 매수 — {invest_krw:,.0f}원")

        qty_est = qty if not DRY_RUN else invest_krw / price
        atr_val_est = indicators.get("atr", 0)
        atr_stop_est = round(price - atr_val_est * RISK["atr_multiplier"]) if atr_val_est else None
        sl_price = atr_stop_est or int(price * (1 + RISK["stop_loss"]))
        tp1_price = int(price * (1 + RISK.get("partial_tp_pct", 0.08)))
        tp2_price = int(price * (1 + RISK.get("partial_tp_2_pct", 0.12)))
        tp_price = int(price * (1 + RISK["take_profit"]))
        comp_total = comp["total"] if comp else 0
        btc_val = int(price * qty_est)
        krw_remain = max(0, int(krw_balance - invest_krw))
        total_asset = krw_remain + btc_val
        btc_weight = round(btc_val / max(total_asset, 1) * 100)
        atr_line = f"ATR손절: ₩{atr_stop_est:,} (ATR×{RISK['atr_multiplier']})\n" if atr_stop_est else ""
        _dry_prefix = "[DRY_RUN] " if DRY_RUN else ""
        send_telegram(
            f"{_dry_prefix}📈 <b>BTC 매수 체결</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"가격: ₩{price:,.0f} ({qty_est:.8f} BTC)\n"
            f"복합스코어: {comp_total}/100\n"
            f"진입근거: {signal['reason']}\n"
            f"━━━━━━━━━━━━━━\n"
            f"{atr_line}"
            f"익절1: ₩{tp1_price:,} (+{RISK.get('partial_tp_pct', 0.03)*100:.0f}%) / 익절2: ₩{tp2_price:,} (+{RISK.get('partial_tp_2_pct', 0.06)*100:.0f}%) / 전량: ₩{tp_price:,} (+{RISK['take_profit']*100:.0f}%)\n"
            f"━━━━━━━━━━━━━━\n"
            f"총자산: ₩{total_asset:,}\n"
            f"BTC 비중: {btc_weight}%",
            priority=_TgPriority.IMPORTANT,
        )
        if notify_openclaw:
            try:
                notify_openclaw("btc_buy", "BTC 매수 체결", metadata={"price": price})
            except Exception as e:
                log.debug(f"notify_openclaw 매수 알림 실패: {e}")
        if _sheets_append:
            try:
                _sheets_append("btc", "매수", "BTC", price, qty, None, signal.get("reason", ""))
            except Exception as e:
                log.debug(f"Sheets 매수 기록 실패: {e}")
        # audit fix: Prometheus 메트릭 연동
        try:
            from common.prometheus_metrics import (record_trade,
                                                   set_signal_score)
            record_trade("BTC", "buy")
            set_signal_score("BTC", "composite", float((comp or {}).get("total", 0) if isinstance(comp, dict) else 0))
        except Exception as e:
            log.debug(f"Prometheus 매수 메트릭 기록 실패: {e}")
        return {"result": f"BUY_{stage}차"}

    # ── AI SELL ──
    elif signal["action"] == "SELL" and btc_balance > 0.00001:
        pnl_pct = None
        if pos:
            # audit fix: entry_price=0 ZeroDivisionError 방지
            _sell_ep = float(pos.get("entry_price") or 0)
            if _sell_ep > 0:
                pnl_pct = (price - _sell_ep) / _sell_ep * 100
        sell_qty = btc_balance * (1 - BTC_EXECUTION_SLIPPAGE)
        _ai_sell_ok = _execute_sell(sell_qty, "AI매도", pnl_pct=pnl_pct, price=price)
        send_telegram(
            f"🔴 <b>BTC 매도</b>\n"
            f"💰 가격: {price:,}원\n"
            f"📊 RSI: {indicators['rsi']}\n"
            f"🎯 신뢰도: {signal['confidence']}%\n"
            f"📝 {signal['reason']}"
        )
        if notify_openclaw:
            try:
                notify_openclaw("btc_sell", "BTC 매도 체결", metadata={"price": price})
            except Exception as e:
                log.debug(f"notify_openclaw 매도 알림 실패: {e}")
        if _sheets_append:
            try:
                action = "손절" if pnl_pct is not None and pnl_pct < -2 else "익절" if pnl_pct is not None and pnl_pct > 2 else "매도"
                _sheets_append("btc", action, "BTC", price, btc_balance, pnl_pct, signal.get("reason", ""))
            except Exception as e:
                log.debug(f"Sheets 매도 기록 실패: {e}")
        return {"result": "SELL"}

    return {"result": "HOLD"}

# ── Supabase 로그 ─────────────────────────────────


def save_log(indicators, signal, result, *, fg=None, volume=None, comp=None, funding=None, oi=None, ls_ratio=None, kimchi=None, market_regime=None) -> None:
    try:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": signal.get("action", "HOLD"),
            "price": indicators["price"],
            "rsi": indicators["rsi"],
            "macd": indicators["macd"],
            "confidence": signal.get("confidence", 0),
            "reason": signal.get("reason", ""),
            "indicator_snapshot": json.dumps(indicators),
            "order_raw": json.dumps(result),
            # --- Optional signal context (safe if schema supports it) ---
            "fg_value": (fg or {}).get("value") if fg else None,
            "bb_pct": (comp or {}).get("bb_pct") if isinstance(comp, dict) else None,
            "vol_ratio_5m": (volume or {}).get("ratio") if volume else None,
            "trend": (comp or {}).get("trend") if isinstance(comp, dict) else None,
            "funding_rate": (funding or {}).get("rate") if funding else None,
            "oi_ratio": (oi or {}).get("ratio") if oi else None,
            "ls_ratio": (ls_ratio or {}).get("ls_ratio") if ls_ratio else None,
            "kimchi": kimchi,
            "market_regime": market_regime,
            "composite_score": (comp or {}).get("total") if isinstance(comp, dict) else None,
            # v6.3: 시그널 소스 추적 (attribution용) — PR #25 hotfix: check constraint 매핑
            "signal_source": _map_signal_source(signal.get("source")),
            # PR #25: AI 결정 메타데이터 (PR #29 Performance Layer 대비)
            "decision_source": signal.get("decision_meta", {}).get("decision_source"),
            "model": signal.get("decision_meta", {}).get("model"),
            "ai_latency_ms": signal.get("decision_meta", {}).get("ai_latency_ms"),
            "prompt_tokens": signal.get("decision_meta", {}).get("prompt_tokens"),
            "response_tokens": signal.get("decision_meta", {}).get("response_tokens"),
        }

        try:
            supabase.table("btc_trades").insert(row).execute()
        except Exception:
            # Fallback to minimal schema + 메타 컬럼 보존 (PR #25 hotfix)
            minimal = {k: row[k] for k in [
                "timestamp", "action", "price", "rsi", "macd",
                "confidence", "reason", "indicator_snapshot", "order_raw",
                "decision_source", "model", "ai_latency_ms", "prompt_tokens", "response_tokens",
            ]}
            supabase.table("btc_trades").insert(minimal).execute()
        log.debug("Supabase 저장 완료")
    except Exception as e:
        log.error(f"Supabase 저장 실패: {e}")

# ── 메인 사이클 ───────────────────────────────────


def run_trading_cycle() -> dict:
    global _btc_buy_blocked
    # P1-1: DrawdownGuard가 설정한 _btc_buy_blocked 값을 사이클 간 유지해야 함
    # equity_curve가 있는 경우에만 DrawdownGuard가 값을 재설정함

    try:
        krw_balance = upbit.get_balance("KRW") or 0
        btc_balance = upbit.get_balance("BTC") or 0
        spot_price = float(pyupbit.get_current_price("KRW-BTC") or 0)
        account_equity = float(krw_balance) + float(btc_balance) * spot_price
        if account_equity > 0:
            append_equity_snapshot('btc', account_equity, {"source": "upbit_balances", "price": spot_price})
            tw = get_effective_market_weight('BTC')
            if tw is not None:
                log.info(f"리밸런싱 목표 비중(BTC): {tw:.1%}")
    except Exception as e:
        log.warning(f"BTC 자산 스냅샷 저장 실패: {e}")

    equity_curve = load_equity_curve('btc')
    if equity_curve:
        _dd_store = DrawdownStateStore()
        guard = DrawdownGuard(store=_dd_store)
        returns = guard.returns_from_equity_curve(equity_curve)
        decision = guard.evaluate(
            daily_return=returns.get("daily_return", 0.0),
            weekly_return=returns.get("weekly_return", 0.0),
            monthly_return=returns.get("monthly_return", 0.0),
            market='btc',
        )
        save_drawdown_state('btc', decision['state'].__dict__)
        _btc_buy_blocked = not decision.get("allow_new_buys", True)
        triggers = set(decision.get("triggered_rules") or [])
        if "WEEKLY_DELEVERAGE" in triggers:
            # audit fix: 포지션 조회 실패 시 사이클 스킵
            try:
                pos = get_open_position()
            except Exception:
                log.error("WEEKLY_DELEVERAGE 포지션 조회 실패 — 사이클 스킵")
                return {"result": "DB_ERROR"}
            btc_balance = upbit.get_balance("BTC") or 0
            price = _latest_market_price()
            if price <= 0:
                log.warning("시장 데이터 없음 — WEEKLY_DELEVERAGE 가격 조회 실패, 즉시 매도 스킵")
                return {"result": "MARKET_DATA_UNAVAILABLE"}
            if pos and btc_balance > 0.00001:
                sell_qty = btc_balance * 0.5 * (1 - BTC_EXECUTION_SLIPPAGE)
                if _execute_sell(sell_qty, "DELEVERAGE", close=False):
                    try:
                        remaining_qty = max(float(pos.get("quantity", 0) or 0) - sell_qty, 0.0)
                        remaining_krw = max(float(pos.get("entry_krw", 0) or 0) * 0.5, 0.0)
                        supabase.table("btc_position").update({
                            "quantity": remaining_qty,
                            "entry_krw": remaining_krw,
                            "highest_price": price,
                        }).eq("id", pos["id"]).execute()
                    except Exception:
                        log.debug("DELEVERAGE 포지션 DB 업데이트 실패")
        if decision.get("force_liquidate"):
            # audit fix: 포지션 조회 실패 시 사이클 스킵
            try:
                pos = get_open_position()
            except Exception:
                log.error("FULL_STOP 포지션 조회 실패 — 사이클 스킵")
                return {"result": "DB_ERROR"}
            btc_balance = upbit.get_balance("BTC") or 0
            price = _latest_market_price()
            if price <= 0:
                log.warning("시장 데이터 없음 — FULL_STOP 가격 조회 실패, 강제청산 스킵")
                return {"result": "MARKET_DATA_UNAVAILABLE"}
            if pos and btc_balance > 0.00001:
                sell_qty = btc_balance * (1 - BTC_EXECUTION_SLIPPAGE)
                _execute_sell(sell_qty, "FULL_STOP", price=price)
            return {"result": "FULL_STOP"}

    # 일일 손실 한도 체크
    if check_daily_loss():
        log.warning("일일 손실 한도 초과 — 사이클 스킵")
        return {"result": "DAILY_LOSS_LIMIT"}

    # 오늘 신규 매수 건수 한도 체크 (포지션 보유 중이면 매도 시그널 분석을 위해 스킵하지 않음)
    # P1-5: UTC→KST 변환으로 check_daily_loss와 일관성 유지
    _kst_tz = ZoneInfo("Asia/Seoul")
    today = datetime.now(_kst_tz).date().isoformat()
    buy_limit_reached = False
    try:
        res = supabase.table("btc_position")\
                      .select("id")\
                      .gte("entry_time", today).execute()
        today_trades = len(res.data or [])
        if today_trades >= RISK.get("max_trades_per_day", 999):
            pos_check = get_open_position()
            if not pos_check:
                log.info("오늘 BTC 매수 한도 도달 + 포지션 없음 — 사이클 스킵")
                return {"result": "MAX_TRADES_PER_DAY"}
            buy_limit_reached = True
    except Exception as e:
        log.warning(f"오늘 BTC 매수 건수 조회 실패: {e}")

    # 쿨다운 체크 (마지막 매수 후 cooldown_minutes 미경과 시 매수 스킵)
    if not buy_limit_reached:
        try:
            cooldown_min = RISK.get("cooldown_minutes", 60)
            _cd_res = supabase.table("btc_position") \
                .select("entry_time") \
                .order("entry_time", desc=True) \
                .limit(1).execute()
            if _cd_res.data:
                _last_entry_raw = _cd_res.data[0].get("entry_time")
                _last_entry = _parse_entry_time(_last_entry_raw)
                if _last_entry is not None:
                    _elapsed_min = (
                        datetime.now(timezone.utc) - _last_entry
                    ).total_seconds() / 60
                    if _elapsed_min < cooldown_min:
                        pos_check2 = get_open_position()
                        if not pos_check2:
                            log.info(
                                f"쿨다운 중 ({_elapsed_min:.0f}분 / {cooldown_min}분) — 매수 스킵"
                            )
                            buy_limit_reached = True  # 매수 차단 (매도 분석은 계속)
                elif _last_entry_raw:
                    log.warning(
                        f"최근 entry_time 파싱 실패 — cooldown 우회 가능 | raw={_last_entry_raw!r}"
                    )
        except Exception as _cd_e:
            log.debug(f"쿨다운 체크 실패 (무시): {_cd_e}")

    log.info("매매 사이클 시작")

    df = get_market_data()
    if not _market_data_ready(df):
        log.warning("시장 데이터 조회 실패 또는 비정상 응답 — 사이클 스킵 | guard=market_data_unavailable, result=MARKET_DATA_UNAVAILABLE")
        return {"result": "MARKET_DATA_UNAVAILABLE"}
    indicators = calculate_indicators(df)
    volume = get_volume_analysis(df)
    fg = get_fear_greed()
    htf = get_hourly_trend()
    momentum = get_daily_momentum()
    news = _get_news_result()
    pos = get_open_position()
    kimchi = get_kimchi_premium()

    # ── 온체인 데이터 (v6 신규) ──
    from common.market_data import (get_btc_funding_rate,
                                    get_btc_long_short_ratio,
                                    get_btc_open_interest,
                                    get_btc_whale_activity, get_market_regime)
    funding = get_btc_funding_rate()
    oi = get_btc_open_interest()
    ls_ratio = get_btc_long_short_ratio()
    whale = get_btc_whale_activity()

    # ── 고래 시그널 분류 (기존 whale 데이터 재사용, 추가 API 호출 없음) ──
    whale_signal: dict = {}
    try:
        from btc.signals.whale_tracker import \
            classify_whale_activity as _classify_whale
        _unc = float((whale or {}).get("unconfirmed_tx") or 0)
        if _unc > 0:
            _bl = max(_unc * 0.010, 1.0)
            whale_signal = _classify_whale(
                inflow_btc=_unc * 0.015,
                outflow_btc=_unc * 0.013,
                inflow_avg_btc=_bl,
                outflow_avg_btc=_bl,
            )
    except Exception as e:
        log.debug(f"고래 시그널 분류 실패: {e}")

    # ── 시장 레짐 (v6.1: 동적 가중치 실제 연동) ──
    try:
        _mr = get_market_regime()
        market_regime = _mr.get("regime", "TRANSITION")
    except Exception:
        market_regime = "TRANSITION"

    fg_value = fg["value"]
    rsi_5m = indicators["rsi"]
    rsi_d = momentum["rsi_d"]

    comp = calc_btc_composite(
        fg_value, rsi_d, momentum["bb_pct"],
        momentum["vol_ratio_d"], htf["trend"], momentum["ret_7d"],
        funding=funding, oi=oi, ls_ratio=ls_ratio, kimchi=kimchi,
        regime=market_regime,
        news_sentiment=news.get("score", 0.0),
        whale=whale_signal,
    )

    # Backfill context columns for existing OPEN positions (schema may have been added later)
    try:
        if supabase and pos and pos.get("id"):
            patch = {}
            if pos.get("composite_score") is None:
                patch["composite_score"] = (comp or {}).get("total") if isinstance(comp, dict) else None
            if pos.get("fg_value") is None:
                patch["fg_value"] = fg_value
            if pos.get("funding_rate") is None:
                patch["funding_rate"] = (funding or {}).get("rate") if funding else None
            if pos.get("rsi_d") is None:
                patch["rsi_d"] = rsi_d
            if pos.get("bb_pct") is None:
                patch["bb_pct"] = momentum.get("bb_pct")
            if pos.get("vol_ratio_d") is None:
                patch["vol_ratio_d"] = momentum.get("vol_ratio_d")
            if pos.get("trend") is None:
                patch["trend"] = htf.get("trend")
            if pos.get("ls_ratio") is None:
                patch["ls_ratio"] = (ls_ratio or {}).get("ls_ratio") if ls_ratio else None
            if pos.get("oi_ratio") is None:
                patch["oi_ratio"] = (oi or {}).get("ratio") if oi else None
            if pos.get("kimchi") is None:
                patch["kimchi"] = kimchi
            if pos.get("market_regime") is None:
                patch["market_regime"] = market_regime

            if patch:
                supabase.table("btc_position").update(patch).eq("id", pos["id"]).execute()
    except Exception as e:
        log.debug(f"포지션 컨텍스트 백필 실패: {e}")

    # 일일 리포트용 상태 캐시 저장
    try:
        _state_file = Path(__file__).resolve().parents[1] / "brain" / "market" / "last_btc_state.json"
        _state_file.parent.mkdir(parents=True, exist_ok=True)
        _state_file.write_text(json.dumps({
            "composite": comp.get("total", 0) if comp else 0,
            "trend": htf.get("trend", "UNKNOWN"),
            "fg": fg_value,
            "fg_label": fg.get("label", "중립"),
            "rsi": rsi_d,
            "updated": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.debug(f"BTC 상태 캐시 저장 실패: {e}")

    log.info(f"F&G: {fg['label']}({fg_value}) | 1h: {htf['trend']} | dRSI: {rsi_d} | 5mRSI: {rsi_5m}")
    log.info(f"BB: {momentum['bb_pct']:.0f}% | dVol: {momentum['vol_ratio_d']}x | 7d: {momentum['ret_7d']:+.1f}% | 30d: {momentum['ret_30d']:+.1f}%")
    log.info(f"Fund: {funding.get('rate', 0):+.4f}%({funding.get('signal', '?')}) | "
             f"LS: {ls_ratio.get('ls_ratio', 1):.2f}({ls_ratio.get('signal', '?')}) | "
             f"OI: {oi.get('ratio', 1):.3f}x({oi.get('signal', '?')}) | "
             f"Whale: {whale.get('unconfirmed_tx', 0):,}tx({whale.get('signal', '?')})")
    log.info(f"Score: {comp['total']}/100 (F&G:{comp['fg']} RSI:{comp['rsi']} BB:{comp['bb']} "
             f"Vol:{comp['vol']} Trend:{comp['trend']} Fund:{comp.get('funding',0)} "
             f"LS:{comp.get('ls',0)} OI:{comp.get('oi',0)} News:{comp.get('news',0):+d} "
             f"Bonus:{comp['bonus']} Regime:{market_regime}[{comp.get('regime_adj',0):+d}])")
    log.info(f"Vol(5m): {volume['label']}({volume['ratio']}x) | "
             f"Pos: {'@ {:,}원'.format(int(pos['entry_price'])) if pos else 'None'}")
    if kimchi is not None:
        log.info(f"김치 프리미엄: {kimchi:+.2f}%")

    # ── 복합 스코어 기반 매매 결정 ──
    signal = None
    # v6.2 A1: 극도공포 보정 — F&G<=15 시 buy_composite_min 15 감소
    if fg_value <= 15:
        effective_min = RISK["buy_composite_min"] - 15
    else:
        effective_min = RISK["buy_composite_min"]
    buy_min = effective_min

    # v6: 온체인 안전장치
    funding_blocked = False
    if RISK.get("funding_filter") and funding.get("signal") == "LONG_CROWDED":
        log.warning(f"펀딩비 롱 과열 ({funding.get('rate', 0):+.4f}%) — 매수 신중")
        funding_blocked = True

    kimchi_blocked = False
    if kimchi is not None and kimchi >= RISK.get("kimchi_premium_max", 5.0):
        log.warning(f"김치 프리미엄 과열 ({kimchi:+.2f}%) — 매수 차단")
        kimchi_blocked = True

    # 1) 복합 스코어 매수 (핵심 로직) — 일일 한도 도달 시 매수 차단
    if buy_limit_reached and not pos:
        log.info("오늘 BTC 매수 한도 도달 — 추가 매수 차단")
    elif funding_blocked:
        log.info(f"펀딩비 롱 과열 — 매수 차단")
    elif kimchi_blocked:
        log.info(f"김치 프리미엄 {kimchi:+.2f}% 과열 — 매수 차단")
    elif comp["total"] >= buy_min and not pos and htf["trend"] != "DOWNTREND":
        conf = min(60 + comp["total"] - buy_min, 90)
        signal = {
            "action": "BUY", "confidence": int(conf),
            "reason": f"복합스코어 {comp['total']}/{buy_min} (F&G={fg_value}, dRSI={rsi_d}) [룰기반]",
            "source": "RULE_COMPOSITE",
        }
        log.trade(f"복합스코어 매수 발동: {comp['total']}점 >= {buy_min}")

    # 2) 극단 공포 오버라이드: F&G<=15 + UPTREND, 또는 F&G<=12 극단공포 시 DOWNTREND도 허용(소량, confidence↓)
    elif fg_value <= 15 and rsi_d <= 55 and not pos:
        if htf["trend"] != "DOWNTREND":
            signal = {
                "action": "BUY", "confidence": 78,
                "reason": f"극도공포 오버라이드 F&G={fg_value}, dRSI={rsi_d} [룰기반]",
                "source": "RULE_EXTREME_FEAR",
            }
            log.trade(f"극도공포 오버라이드: F&G={fg_value}, dRSI={rsi_d}")
        elif fg_value <= 12:
            # F&G 12 이하 극단 공포 — DOWNTREND에도 역발상 소량 매수 허용 (confidence 낮게)
            signal = {
                "action": "BUY", "confidence": 66,
                "reason": f"극단공포 역발상(DOWNTREND) F&G={fg_value}, dRSI={rsi_d} [룰기반]",
                "source": "RULE_EXTREME_FEAR",
            }
            log.trade(f"극단공포 역발상 오버라이드(DOWNTREND): F&G={fg_value}, dRSI={rsi_d}")

    # 3) 기술적 과매수 매도: 일봉 RSI>=75 + 하락 추세
    elif rsi_d >= 75 and htf["trend"] == "DOWNTREND" and pos:
        signal = {
            "action": "SELL", "confidence": 78,
            "reason": f"과매수+하락추세 dRSI={rsi_d:.0f} [룰기반]",
            "source": "RULE_OVERBOUGHT",
        }

    # 4) 타임컷: 보유 기간 초과 + 수익 미미
    if pos and not signal:
        entry_str = str(pos["entry_time"])
        if "Z" in entry_str:
            entry_dt = datetime.fromisoformat(entry_str.replace("Z", "+00:00"))
        else:
            # Supabase는 UTC 저장 — timezone-naive면 UTC로 간주
            entry_dt = datetime.fromisoformat(entry_str).replace(tzinfo=timezone.utc)
        held_days = (datetime.now(timezone.utc) - entry_dt).days
        if held_days >= RISK["timecut_days"]:
            entry_p = float(pos["entry_price"])
            cur_p = indicators["price"]
            pnl_pct = (cur_p - entry_p) / entry_p
            if pnl_pct < 0.02:
                signal = {
                    "action": "SELL", "confidence": 70,
                    "reason": f"타임컷 {held_days}일 보유, 수익 {pnl_pct*100:+.1f}% [룰기반]",
                    "source": "RULE_TIMECUT",
                }
                log.trade(f"타임컷 발동: {held_days}일, 수익 {pnl_pct*100:+.1f}%")

    # 5) 극도 탐욕 매도 (수익 보존)
    if pos and not signal and fg_value >= 75:
        entry_p = float(pos["entry_price"])
        pnl_pct = (indicators["price"] - entry_p) / entry_p
        if pnl_pct > 0:
            signal = {
                "action": "SELL", "confidence": 72,
                "reason": f"극도탐욕 F&G={fg_value} + 수익 {pnl_pct*100:+.1f}% 보존 [룰기반]",
                "source": "RULE_EXTREME_GREED",
            }
            log.trade(f"극도탐욕 매도: F&G={fg_value}, 수익={pnl_pct*100:+.1f}%")

    # 6) BB 상단 과매수 매도
    if pos and not signal and momentum["bb_pct"] >= 85 and rsi_d >= 65:
        signal = {
            "action": "SELL", "confidence": 70,
            "reason": f"BB상단({momentum['bb_pct']:.0f}%) + 일봉과매수 RSI={rsi_d:.0f} [룰기반]",
            "source": "RULE_BB_TOP",
        }
        log.trade(f"BB상단 매도: bb_pct={momentum['bb_pct']:.0f}%, rsi_d={rsi_d:.0f}")

    # 7) 추세 하락전환 + 수익 보존 (2% 이상)
    if pos and not signal and htf["trend"] == "DOWNTREND":
        entry_p = float(pos["entry_price"])
        pnl_pct = (indicators["price"] - entry_p) / entry_p
        if pnl_pct >= 0.02:
            signal = {
                "action": "SELL", "confidence": 68,
                "reason": f"추세 하락전환(DOWNTREND) + 수익 {pnl_pct*100:+.1f}% 보존 [룰기반]",
                "source": "RULE_TREND_REVERSAL",
            }
            log.trade(f"추세전환 매도: DOWNTREND, 수익={pnl_pct*100:+.1f}%")

    # 8) 룰기반 복합스코어 미발동 → 룰 기반 결정론적 신호 (v6.3: LLM 의존 제거)
    if not signal:
        signal = rule_based_btc_signal(
            indicators, fg, htf, volume,
            comp=comp, rsi_d=rsi_d, momentum=momentum,
            funding=funding, ls_ratio=ls_ratio, regime=market_regime,
        )

    # ── 보조 보정 ──

    # 거래량 폭발
    vol_r = volume["ratio"]
    if vol_r >= 3.0:
        log.info(f"거래량 폭발 감지 ({vol_r:.1f}x)")
        if signal["action"] == "BUY":
            signal["confidence"] = max(signal["confidence"], 78)
        elif signal["action"] == "HOLD" and indicators["macd"] > 0 and rsi_d < 60:
            prev_reason = signal.get("reason", "")
            signal["action"] = "BUY"
            signal["confidence"] = 72
            signal["reason"] += " [거래량 폭발]"
            log.warning(
                "HOLD 신호가 거래량 폭발 규칙으로 BUY 승격됨 | "
                f"macd={indicators['macd']}, rsi_d={rsi_d}, vol_ratio_5m={vol_r:.2f}, "
                f"prev_reason={prev_reason}"
            )

    # 김치 프리미엄 저평가
    if kimchi is not None and kimchi <= -2.0 and signal["action"] == "HOLD" and rsi_d < 55:
        prev_reason = signal.get("reason", "")
        signal["action"] = "BUY"
        signal["confidence"] = max(signal.get("confidence", 0), 72)
        signal["reason"] += f" [김치 저평가 {kimchi:+.2f}%]"
        log.warning(
            "HOLD 신호가 김치 저평가 규칙으로 BUY 승격됨 | "
            f"kimchi={kimchi:+.2f}%, rsi_d={rsi_d}, prev_reason={prev_reason}"
        )

    result = execute_trade(
        signal,
        indicators,
        fg,
        volume,
        comp,
        funding=funding,
        oi=oi,
        ls_ratio=ls_ratio,
        kimchi=kimchi,
        market_regime=market_regime,
        rsi_d=rsi_d,
        bb_pct=momentum.get("bb_pct"),
        vol_ratio_d=momentum.get("vol_ratio_d"),
        trend=htf.get("trend"),
    )
    save_log(
        indicators,
        signal,
        result,
        fg=fg,
        volume=volume,
        comp={**(comp or {}), "bb_pct": momentum.get("bb_pct"), "trend": htf.get("trend")},
        funding=funding,
        oi=oi,
        ls_ratio=ls_ratio,
        kimchi=kimchi,
        market_regime=market_regime,
    )
    log.trade(f"신호: {signal['action']} (신뢰도: {signal['confidence']}%) → {result['result']}")

    # audit fix: Prometheus 메트릭 연동
    try:
        from common.prometheus_metrics import (record_agent_cycle,
                                               set_signal_score)
        record_agent_cycle("BTC", "success")
        set_signal_score("BTC", "composite", float((comp or {}).get("total", 0) if isinstance(comp, dict) else 0))
    except Exception as e:
        log.debug(f"Prometheus 사이클 메트릭 기록 실패: {e}")

    return result


def build_hourly_summary() -> str:
    """매시 요약 텍스트 생성 (가격·포지션·오늘 손익·F&G·1시간봉 추세)."""
    try:
        df = get_market_data()
        if not _market_data_ready(df):
            return "⏰ BTC 매시 요약 생성 실패: 시장 데이터 조회 실패"
        ind = calculate_indicators(df)
        price = int(ind["price"])
        rsi = ind["rsi"]
        fg = get_fear_greed()
        htf = get_hourly_trend()
        pos = get_open_position()

        today = datetime.now(timezone.utc).date().isoformat()
        try:
            res = supabase.table("btc_position").select("pnl").eq("status", "CLOSED").gte("exit_time", today).execute()
            today_pnl = sum(float(r["pnl"] or 0) for r in (res.data or []))
        except Exception:
            today_pnl = 0

        pos_line = "포지션 없음"
        if pos:
            entry = int(float(pos["entry_price"]))
            pos_line = f"포지션 있음 @ {entry:,}원"

        msg = (
            f"⏰ <b>BTC 매시 요약</b> {datetime.now().strftime('%m/%d %H:%M')}\n"
            f"💰 가격: {price:,}원 | RSI: {rsi}\n"
            f"📊 {pos_line}\n"
            f"📈 1시간봉: {htf['trend']} | F&G: {fg['label']}({fg['value']})\n"
            f"📉 오늘 손익: {today_pnl:+,.0f}원"
        )
        return msg
    except Exception as e:
        return f"⏰ BTC 매시 요약 생성 실패: {e}"


def send_hourly_report():
    """매시 정각 요약 — INFO 버퍼에 저장 (일일 리포트에 병합됨)."""
    msg = build_hourly_summary()
    send_telegram(msg, priority=_TgPriority.INFO)
    log.info("매시 요약 INFO 버퍼 저장 완료")


if __name__ == "__main__":
    import sys
    if not RUNTIME_ENV_READY:
        log.critical("필수 환경변수 없음: UPBIT keys 필요")
        sys.exit(1)
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        pos = get_open_position()
        if pos:
            df = get_market_data()
            if not _market_data_ready(df):
                log.warning("시장 데이터 조회 실패 또는 비정상 응답 — BTC check 스킵 | guard=market_data_unavailable")
                sys.exit(0)
            ind = calculate_indicators(df)
            fg = get_fear_greed()
            vol = get_volume_analysis(df)
            execute_trade({"action": "HOLD", "confidence": 0, "reason": "1분 체크"}, ind, fg, vol, None)
            log.info("BTC 1분 손절/익절 체크 완료")
        else:
            log.info("BTC 포지션 없음 — 스킵")
    elif len(sys.argv) > 1 and sys.argv[1] == "report":
        send_hourly_report()
    else:
        run_trading_cycle()
