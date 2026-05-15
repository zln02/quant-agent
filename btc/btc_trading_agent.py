#!/usr/bin/env python3
"""
BTC 자동매매 에이전트 v6 — Top-tier Quant
기능: 멀티타임프레임, Fear&Greed, 뉴스감정, 거래량분석,
      펀딩비/OI/롱숏비율(온체인), 김치프리미엄,
      동적 가중치 복합스코어, 적응형 트레일링, 부분익절
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import API_RETRY_CONFIG, BTC_LOG
from common.env_loader import load_env
from common.logger import get_logger
from common.retry import retry, retry_call
from common.supabase_client import _reset_client, get_supabase
from common.telegram import Priority as _TgPriority
from common.telegram import send_telegram as _tg_send

try:
    from common.sheets_logger import append_trade as _sheets_append
except ImportError:
    _sheets_append = None

try:
    from common.circuit_breaker import \
        check_trade_allowed_sync as _check_circuit_breaker
except ImportError:
    _check_circuit_breaker = None

try:
    from agents.regime_classifier import RegimeClassifier
except ImportError:
    RegimeClassifier = None

try:
    from execution.smart_router import SmartRouter
    _smart_router = SmartRouter()
except ImportError:
    _smart_router = None

load_env()
log = get_logger("btc_agent", BTC_LOG)
KST = timezone(timedelta(hours=9))
_btc_regime_cache = {"regime": None, "multipliers": None, "ts": 0}

import pyupbit
from btc_news_collector import get_news_result as _get_news_result
from btc_news_collector import get_news_summary
from openai import OpenAI


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
    except Exception as e:
        log.debug(f"가중치 로드 실패: {e}")
        return {}


def _apply_weighted_score(components: dict, *, weights: dict) -> int:
    """Apply weights to component scores.

    components: dict with keys like fg,rsi,bb,vol,trend,funding,ls,oi,bonus,regime_adj
    weights: dict from signal_evaluator (signal-name -> weight)
    """
    if not weights:
        return int(components.get("total", 0) or 0)
    weights = {
        k: v for k, v in weights.items()
        if isinstance(v, (int, float)) and math.isfinite(v)
    }
    if not weights:
        return int(components.get("total", 0) or 0)

    # Map evaluator signal names -> component keys
    map_sig_to_comp = {
        "fg_index": "fg",
        "rsi_signal": "rsi",
        "funding_rate": "funding",
        "btc_composite": "total",
        "composite_score": "total",
    }

    # Use weights to scale the main components; keep bonus/regime adjustments as-is.
    base_parts = ["fg", "rsi", "bb", "vol", "trend", "funding", "ls", "oi"]
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
    if not math.isfinite(raw_scaled):
        raw_scaled = float(sum(components.get(k, 0) or 0 for k in base_parts))
    raw_scaled += float(components.get("bonus", 0) or 0)
    raw_scaled += float(components.get("regime_adj", 0) or 0)
    raw_scaled += float(components.get("news", 0) or 0)
    total = max(0, min(int(round(raw_scaled)), 100))
    return total

# ── 환경변수 ──────────────────────────────────────
UPBIT_ACCESS  = os.environ.get("UPBIT_ACCESS_KEY", "")
UPBIT_SECRET  = os.environ.get("UPBIT_SECRET_KEY", "")
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY", "")
DRY_RUN       = os.environ.get("DRY_RUN", "0") == "1"

if not all([UPBIT_ACCESS, UPBIT_SECRET]):
    log.critical("필수 환경변수 없음: UPBIT keys 필요")
    sys.exit(1)
upbit   = pyupbit.Upbit(UPBIT_ACCESS, UPBIT_SECRET)
supabase = get_supabase()
client  = OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None

# ── 프로세스 내 매수 잠금 (Race condition 방지) ──────
# Supabase 쓰기 딜레이로 get_open_position()이 None 반환 시에도 중복매수 차단
import threading as _threading

_buy_lock = _threading.Lock()
_LAST_BUY_FILE = Path(__file__).resolve().parents[1] / "brain" / "btc_last_buy.json"

def _read_last_buy_ts() -> float:
    """마지막 매수 시각을 파일에서 읽어 UTC epoch으로 반환 (없으면 0.0)"""
    try:
        import json as _json
        data = _json.loads(_LAST_BUY_FILE.read_text())
        return float(data.get("ts", 0.0))
    except Exception as e:
        log.debug(f"마지막 매수 시각 로드 실패: {e}")
        return 0.0

def _write_last_buy_ts() -> None:
    """현재 UTC epoch을 파일에 저장"""
    import json as _json
    import time as _t
    _LAST_BUY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LAST_BUY_FILE.write_text(_json.dumps({"ts": _t.time()}))
if client is None:
    log.warning("OPENAI_API_KEY 없음 — 룰 기반 fallback 판단으로 동작")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _get_btc_regime_adj() -> dict:
    """BTC 레짐 기반 가중치 조정 (사이클당 1회 조회)."""
    now = time.time()
    if _btc_regime_cache["ts"] and now - _btc_regime_cache["ts"] < 300:
        return _btc_regime_cache["multipliers"] or {}
    try:
        if RegimeClassifier is None:
            return {}
        rc = RegimeClassifier()
        regime_result = rc.classify()
        regime = regime_result.get("regime") if isinstance(regime_result, dict) else regime_result
        mults = {
            "BULL": {"momentum_mult": 1.3, "volume_mult": 1.0, "mean_reversion_mult": 0.7},
            "RISK_ON": {"momentum_mult": 1.3, "volume_mult": 1.0, "mean_reversion_mult": 0.7},
            "BEAR": {"momentum_mult": 0.7, "volume_mult": 1.2, "mean_reversion_mult": 1.3},
            "RISK_OFF": {"momentum_mult": 0.7, "volume_mult": 1.2, "mean_reversion_mult": 1.3},
            "CRISIS": {"momentum_mult": 0.3, "volume_mult": 0.5, "mean_reversion_mult": 0.5},
        }.get(str(regime).upper(), {"momentum_mult": 1.0, "volume_mult": 1.0, "mean_reversion_mult": 1.0})
        _btc_regime_cache.update({"regime": regime, "multipliers": mults, "ts": now})
        log.info(f"BTC regime: {regime}, multipliers: {mults}")
        return mults
    except Exception as e:
        log.warning(f"BTC regime classification failed: {e}")
        return {}


def _upbit_call(func, *args, max_retries: int = 3, **kwargs):
    """Upbit API 호출 래퍼 (429 대응)."""
    retries = max_retries if max_retries is not None else API_RETRY_CONFIG["max_retries"]
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_str = str(e).lower()
            if "429" in error_str or "too many" in error_str:
                wait = min(
                    API_RETRY_CONFIG["base_wait_seconds"]
                    * (API_RETRY_CONFIG["backoff_multiplier"] ** attempt),
                    API_RETRY_CONFIG["max_wait_seconds"],
                )
                log.warning(f"Upbit 429 Rate Limit, {wait}초 대기 (시도 {attempt + 1}/{retries})")
                time.sleep(wait)
                continue
            raise
    return func(*args, **kwargs)


def _execute_sell_order(quantity: float, *, context: str) -> tuple[bool, str]:
    """Execute an Upbit market sell with consistent validation/error handling."""
    try:
        try:
            current_price = float(pyupbit.get_current_price("KRW-BTC") or 0)
            est_notional = current_price * float(quantity)
            if _smart_router and est_notional >= 5_000_000:
                route = _smart_router.decide(
                    symbol="KRW-BTC",
                    side="sell",
                    total_qty=quantity,
                    market="btc",
                    price_hint=current_price,
                )
                log.info(f"SmartRouter decision: {route.route} for {est_notional:,.0f} KRW")
                if route.route == "TWAP":
                    log.info("TWAP suggested but using MARKET for BTC liquidity")
        except Exception as e:
            log.warning(f"SmartRouter failed, fallback to MARKET: {e}")
        result = _upbit_call(upbit.sell_market_order, "KRW-BTC", quantity, max_retries=1)
        if result is None or not isinstance(result, dict) or "error" in result:
            log.error(f"Upbit {context} 매도 실패: {result}")
            send_telegram(f"❌ BTC 매도 주문 실패 ({context})\n응답: {result}")
            return False, "upbit_sell_error"
        return True, ""
    except Exception as exc:
        log.error(f"Upbit {context} 매도 예외: {exc}")
        send_telegram(f"❌ BTC 매도 예외 발생 ({context}): {exc}")
        return False, str(exc)

# ── 리스크 설정 (v6 — Top-tier Quant) ─────────────
RISK = {
    "split_ratios":     [0.12, 0.18, 0.25],     # 스코어 높을수록 큰 비중
    "split_rsi":        [55,   45,   35  ],
    "invest_ratio":      0.30,
    "stop_loss":        -0.03,
    "take_profit":       0.08,        # 전량 익절 (+8%)
    "partial_tp_pct":    0.03,        # 1단계 익절 발동 (+3%)
    "partial_tp_ratio":  0.50,        # 1단계 매도 비율 (50%)
    "partial_tp_2_pct":  0.05,        # 2단계 익절 발동 (+5%)
    "partial_tp_2_ratio": 0.50,       # 2단계 매도 비율 (남은 물량의 50%)
    "atr_multiplier":    2.0,         # ATR 손절 배수 (진입가 - ATR * 2.0)
    "atr_period":        14,          # ATR 계산 기간
    "trailing_stop":     0.02,
    "trailing_activate": 0.015,
    "trailing_adaptive": True,
    "max_daily_loss":   -0.08,
    "max_drawdown":     -0.15,
    "min_confidence":    65,
    "max_trades_per_day": 2,           # 3 → 2 (수수료 절감)
    "fee_buy":           0.001,
    "fee_sell":          0.001,
    "buy_composite_min": 50,           # 43 → 50 (고확신 진입만)
    "sell_composite_max": 20,
    "timecut_days":      7,
    "cooldown_minutes":  60,           # 30 → 60분 (진입 빈도 감소)
    "volatility_filter": True,
    "funding_filter":    True,      # 펀딩비 과열 시 매수 억제
    "oi_filter":         True,      # OI 급등 시 경고
    "kimchi_premium_max": 5.0,      # 김치프리미엄 5% 이상 시 매수 차단
    "dynamic_weights":   True,      # 시장 상태 기반 스코어 가중치 동적 조절
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

# ── 시장 데이터 ───────────────────────────────────
def get_market_data() -> Any | None:
    return pyupbit.get_ohlcv("KRW-BTC", interval="minute5", count=200)


def has_valid_market_data(df) -> bool:
    try:
        required = {"open", "high", "low", "close", "volume"}
        return df is not None and not df.empty and required.issubset(set(df.columns))
    except Exception as e:
        log.debug(f"시장 데이터 유효성 검사 실패: {e}")
        return False

# ── 기술적 지표 ───────────────────────────────────
def calculate_indicators(df) -> dict:
    from ta.momentum import RSIIndicator
    from ta.trend import MACD, EMAIndicator
    from ta.volatility import AverageTrueRange, BollingerBands

    close   = df["close"]
    rsi_w   = int(_l5_params.get("rsi_window", 14))
    bb_w    = int(_l5_params.get("bb_window", 20))
    ema20 = EMAIndicator(close, window=20).ema_indicator().iloc[-1]
    ema50 = EMAIndicator(close, window=50).ema_indicator().iloc[-1]
    rsi   = RSIIndicator(close, window=rsi_w).rsi().iloc[-1]
    macd_obj = MACD(close)
    macd  = macd_obj.macd_diff().iloc[-1]
    bb    = BollingerBands(close, window=bb_w)
    atr   = AverageTrueRange(df["high"], df["low"], close, window=14).average_true_range().iloc[-1]

    return {
        "price":    df["close"].iloc[-1],
        "ema20":    round(ema20, 0),
        "ema50":    round(ema50, 0),
        "rsi":      round(rsi, 1),
        "macd":     round(macd, 0),
        "bb_upper": round(bb.bollinger_hband().iloc[-1], 0),
        "bb_lower": round(bb.bollinger_lband().iloc[-1], 0),
        "volume":   round(df["volume"].iloc[-1], 4),
        "atr":      round(atr, 0),
    }

# ── 거래량 분석 ───────────────────────────────────
def get_volume_analysis(df) -> dict:
    try:
        if df is None or df.empty or "volume" not in df.columns:
            return {"ratio": 1.0, "label": "거래량 분석 실패"}
        cur   = df["volume"].iloc[-1]
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
                log.debug(f"시간봉 거래량 계산 실패: {e}")

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
    except Exception as e:
        log.debug(f"거래량 분석 실패: {e}")
        return {"ratio": 1.0, "label": "거래량 분석 실패"}


def get_candle_confirmation(df) -> dict:
    """Use the latest closed 5m candle to confirm breakout-style buying.

    A confirmed breakout requires:
    - bullish close
    - close near the candle high
    - close above the previous candle high
    """
    try:
        if df is None or len(df) < 3:
            return {
                "confirmed_breakout": False,
                "bullish_close": False,
                "close_near_high": False,
                "broke_prev_high": False,
                "label": "캔들 데이터 부족",
            }

        closed = df.iloc[-2]
        prev = df.iloc[-3]
        candle_range = float(closed["high"] - closed["low"])
        bullish_close = float(closed["close"]) > float(closed["open"])
        close_near_high = (
            candle_range > 0
            and (float(closed["high"]) - float(closed["close"])) / candle_range <= 0.25
        )
        broke_prev_high = float(closed["close"]) > float(prev["high"])
        confirmed = bullish_close and close_near_high and broke_prev_high

        if confirmed:
            label = "최근 5분봉 돌파 마감 확인"
        elif bullish_close and close_near_high:
            label = "양봉 마감이나 이전 고점 돌파 미확인"
        else:
            label = "돌파 마감 미확인"

        return {
            "confirmed_breakout": confirmed,
            "bullish_close": bullish_close,
            "close_near_high": close_near_high,
            "broke_prev_high": broke_prev_high,
            "label": label,
        }
    except Exception:
        return {
            "confirmed_breakout": False,
            "bullish_close": False,
            "close_near_high": False,
            "broke_prev_high": False,
            "label": "캔들 확인 실패",
        }

# ── Fear & Greed ──────────────────────────────────
def get_fear_greed() -> dict:
    try:
        res = retry_call(requests.get, args=("https://api.alternative.me/fng/?limit=1",),
                         kwargs={"timeout": 5}, max_attempts=2, default=None)
        if res is None:
            return {"value": 50, "label": "Unknown", "msg": "⚪ 중립(50)"}
        data  = res.json()["data"][0]
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
    except Exception as e:
        log.debug(f"공포탐욕지수 조회 실패: {e}")
        return {"value": 50, "label": "Unknown", "msg": "⚪ 중립(50)"}

# ── 1시간봉 추세 ──────────────────────────────────
def get_hourly_trend() -> dict:
    try:
        df    = pyupbit.get_ohlcv("KRW-BTC", interval="minute60", count=50)
        from ta.momentum import RSIIndicator
        from ta.trend import EMAIndicator
        close = df["close"]
        ema20 = EMAIndicator(close, window=20).ema_indicator().iloc[-1]
        ema50 = EMAIndicator(close, window=50).ema_indicator().iloc[-1]
        rsi   = RSIIndicator(close, window=14).rsi().iloc[-1]
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

def get_kimchi_premium() -> float | None:
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
    _ttl = 3600  # 일봉 데이터는 1시간에 한 번만 갱신
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
        bb_w  = int(_l5_params.get("bb_window", 20))
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
    mults = _get_btc_regime_adj()
    momentum_mult = float(mults.get("momentum_mult", 1.0) or 1.0)
    volume_mult = float(mults.get("volume_mult", 1.0) or 1.0)
    mean_reversion_mult = float(mults.get("mean_reversion_mult", 1.0) or 1.0)

    # F&G (낮을수록 매수 기회)
    if fg_value <= 10:   fg_sc = 22
    elif fg_value <= 20: fg_sc = 18
    elif fg_value <= 30: fg_sc = 13
    elif fg_value <= 45: fg_sc = 7
    elif fg_value <= 55: fg_sc = 3
    else:                fg_sc = 0

    # 일봉 RSI
    if rsi_d <= 30:   rsi_sc = 20
    elif rsi_d <= 38:  rsi_sc = 16
    elif rsi_d <= 45:  rsi_sc = 12
    elif rsi_d <= 55:  rsi_sc = 6
    elif rsi_d <= 65:  rsi_sc = 2
    else:              rsi_sc = 0

    # BB 포지션
    if bb_pct <= 10:   bb_sc = 12
    elif bb_pct <= 25: bb_sc = 9
    elif bb_pct <= 40: bb_sc = 6
    elif bb_pct <= 55: bb_sc = 2
    else:              bb_sc = 0
    bb_sc = round(bb_sc * mean_reversion_mult)

    # 일봉 거래량
    if vol_ratio_d >= 2.0:   vol_sc = 10
    elif vol_ratio_d >= 1.5: vol_sc = 8
    elif vol_ratio_d >= 1.0: vol_sc = 5
    elif vol_ratio_d >= 0.6: vol_sc = 2
    else:                    vol_sc = 0
    vol_sc = round(vol_sc * volume_mult)

    # 추세
    if trend == "UPTREND":    tr_sc = 12
    elif trend == "SIDEWAYS": tr_sc = 6
    else:                     tr_sc = 0
    tr_sc = round(tr_sc * momentum_mult)

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
    if news_sentiment >= 0.8:    news_sc = 8
    elif news_sentiment >= 0.5:  news_sc = 5
    elif news_sentiment >= 0.2:  news_sc = 2
    elif news_sentiment > -0.2:  news_sc = 0
    elif news_sentiment > -0.5:  news_sc = -2
    elif news_sentiment > -0.8:  news_sc = -5
    else:                        news_sc = -8

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
        "RISK_ON":    +5,   # 강세장: 진입 문턱 낮춤
        "TRANSITION":  0,
        "RISK_OFF":  -10,   # 약세장: 진입 억제
        "CRISIS":    -20,   # 위기: 강력 억제
    }
    regime_adj = _regime_bonus_map.get(str(regime).upper(), 0)

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
    components["total"] = max(0, min(int(round(float(components.get("total", 0) or 0))), 100))

    return components


# ── 포지션 관리 ───────────────────────────────────
def get_open_position() -> dict | None:
    try:
        res = supabase.table("btc_position")\
                      .select("*").eq("status", "OPEN")\
                      .order("entry_time", desc=True).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None

def open_position(entry_price, quantity, entry_krw) -> bool:
    row = {
        "entry_price": entry_price,
        "entry_time":  _utc_now_iso(),
        "quantity":    quantity,
        "entry_krw":   entry_krw,
        "status":      "OPEN",
    }
    try:
        supabase.table("btc_position").insert({**row, "highest_price": entry_price}).execute()
        return True
    except Exception as e:
        log.debug(f"highest_price 포함 포지션 저장 실패: {e}")
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
) -> bool:
    """Open position and persist signal context for later IC evaluation.

    This keeps backward compatibility with existing Supabase schemas:
    if the additional columns do not exist, it falls back to the minimal insert.
    """
    base_row = {
        "entry_price": entry_price,
        "entry_time": _utc_now_iso(),
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
    }

    try:
        supabase.table("btc_position").insert(ctx_row).execute()
        return True
    except Exception as e:
        log.debug(f"컨텍스트 포함 포지션 저장 실패: {e}")
        return open_position(entry_price, quantity, entry_krw)

def close_all_positions(exit_price, *, exit_reason=None) -> bool:
    try:
        res = supabase.table("btc_position")\
                      .select("*").eq("status", "OPEN").execute()
        for pos in res.data:
            pnl     = (exit_price - pos["entry_price"]) * pos["quantity"]
            pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
            update_row = {
                "status":     "CLOSED",
                "exit_price": exit_price,
                "exit_time":  _utc_now_iso(),
                "pnl":        round(pnl, 2),
                "pnl_pct":    round(pnl_pct, 2),
            }
            if exit_reason:
                update_row["exit_reason"] = exit_reason
            try:
                supabase.table("btc_position").update(update_row).eq("id", pos["id"]).execute()
            except Exception as e:
                log.debug(f"포지션 종료 상세 업데이트 실패: {e}")
                # pnl/pnl_pct 컬럼 미존재 시 최소 업데이트 (btc_position_schema.sql 실행 전 graceful fallback)
                fallback_row = {
                    "status":     "CLOSED",
                    "exit_price": exit_price,
                    "exit_time":  _utc_now_iso(),
                }
                if exit_reason:
                    fallback_row["exit_reason"] = exit_reason
                try:
                    supabase.table("btc_position").update(fallback_row).eq("id", pos["id"]).execute()
                except Exception as fallback_e:
                    log.error(f"포지션 종료 fallback 업데이트 실패: {fallback_e}")
    except Exception as e:
        log.error(f"포지션 종료 실패: {e}")
        return False
    return True

# ── 일일 손실 한도 ────────────────────────────────
def check_daily_loss() -> bool:
    try:
        today = datetime.now(KST).date()
        today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=KST)
        res   = supabase.table("btc_position")\
                        .select("pnl, entry_krw")\
                        .eq("status", "CLOSED")\
                        .gte("exit_time", today_start.isoformat()).execute()
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
        log.debug(f"일일 손실 체크 실패: {e}")
    return False

# ── AI 분석 ───────────────────────────────────────
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
        "UPTREND":   "📈 상승 추세 — 매수 우호적",
        "DOWNTREND": "📉 하락 추세 — 매수 금지",
        "SIDEWAYS":  "➡️ 횡보 — 신중 판단",
        "UNKNOWN":   "❓ 불명확 — HOLD 우선",
    }

    if volume["ratio"] >= 2.0:
        vol_comment = f"🔥 거래량 급등({volume['ratio']}배) — 신뢰도 높음"
    elif volume["ratio"] >= 1.5:
        vol_comment = f"📈 거래량 증가({volume['ratio']}배)"
    elif volume["ratio"] <= 0.5:
        vol_comment = f"😴 거래량 급감({volume['ratio']}배) — BUY 금지"
    else:
        vol_comment = f"➡️ 거래량 보통({volume['ratio']}배)"

    mom  = momentum or {}
    fund = funding or {}
    ls   = ls_ratio or {}
    comp_total = (comp or {}).get("total", 0)

    prompt = f"""당신은 비트코인 퀀트 트레이더입니다.
아래 데이터로 매매 신호를 JSON으로만 출력하세요.

[복합 스코어] {comp_total}/100 (시장 레짐: {regime})

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

[최근 거래 기억 — 반드시 참고하여 같은 실수 반복 금지]
{memory_context if memory_context else "기억 없음"}

[최근 뉴스]
{news_summary}

[출력 형식 - JSON만]
{{"action":"BUY또는SELL또는HOLD","confidence":0~100,"reason":"한줄근거"}}"""

    if client is None or not getattr(client, "chat", None):
        action = "HOLD"
        confidence = 55
        reason = "LLM 비활성화 - 룰 기반 중립 유지"

        if (
            comp_total >= max(RISK["buy_composite_min"], 60)
            and htf["trend"] != "DOWNTREND"
            and fg["value"] <= 55
        ):
            action = "BUY"
            confidence = 68
            reason = "LLM 없음 - 복합스코어/심리/추세 기준 BUY"
        elif (
            comp_total <= max(RISK["sell_composite_max"], 20)
            or fg["value"] >= 75
            or (htf["trend"] == "DOWNTREND" and indicators.get("rsi", 50) >= 65)
        ):
            action = "SELL"
            confidence = 72
            reason = "LLM 없음 - 과열/하락 추세 기준 SELL"

        return {"action": action, "confidence": confidence, "reason": reason}

    try:
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )
        raw  = res.choices[0].message.content.strip()
        raw  = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        log.warning(f"AI 분석 실패: {e}")
        return {"action": "HOLD", "confidence": 0, "reason": "AI 오류"}

# ── 분할 매수 단계 (복합 스코어 기반) ─────────────
def get_split_stage(composite_total: float) -> int:
    """복합 스코어가 높을수록 큰 비중으로 매수."""
    if composite_total >= 75: return 3
    if composite_total >= 65: return 2
    return 1


def is_defensive_regime(regime: str | None) -> bool:
    return str(regime or "").upper() in {"BEAR", "RISK_OFF", "CRISIS", "UNKNOWN"}


def get_required_buy_score(regime: str | None) -> int:
    reg = str(regime or "").upper()
    required = int(RISK["buy_composite_min"])
    if reg == "CORRECTION":
        return max(required, 55)
    if reg in {"RISK_OFF", "UNKNOWN"}:
        return max(required, 60)
    if reg in {"BEAR", "CRISIS"}:
        return max(required, 65)
    return required

# ── 주문 실행 ─────────────────────────────────────
def execute_trade(
    signal,
    indicators,
    fg=None,
    volume=None,
    comp=None,
    candle_confirmation=None,
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
    def _result(code: str, **extra) -> dict:
        payload = {"result": code}
        payload.update(extra)
        return payload

    if signal["action"] == "BUY":
        if _check_circuit_breaker is not None:
            try:
                breaker = _check_circuit_breaker("btc")
                if not breaker.get("allowed", True):
                    log.warning(
                        "circuit breaker blocked BTC buy",
                        guard="circuit_breaker",
                        reason=breaker.get("reason"),
                        cb_level=breaker.get("level"),
                    )
                    return _result("BLOCKED_CIRCUIT_BREAKER", guard="circuit_breaker", reason=breaker.get("reason"))
            except Exception as exc:
                log.warning(f"circuit breaker check failed: {exc}")
        else:
            log.warning("circuit_breaker 모듈 없음 — 서킷브레이커 체크 스킵")

    # ── 코드 레벨 안전 필터 (복합 스코어 기반) ──
    if signal["action"] == "BUY":
        comp_total = comp["total"] if isinstance(comp, dict) else 0
        required_buy_score = get_required_buy_score(market_regime)
        if fg and fg["value"] > 75:
            log.warning(
                f"F&G {fg['value']} > 75 (극도 탐욕) — BUY 차단",
                guard="fg_greed_block",
                result="BLOCKED_FG",
                fg_value=fg["value"],
            )
            return _result("BLOCKED_FG", guard="fg_greed_block")
        if comp_total < required_buy_score:
            reg = str(market_regime or "").upper()
            if reg in {"CORRECTION", "BEAR", "RISK_OFF", "CRISIS", "UNKNOWN"}:
                log.warning(
                    f"레짐({market_regime}) 최소 진입점수 {required_buy_score} 미달 — BUY 차단 "
                    f"(현재 {comp_total})",
                    guard="defensive_regime_score_block",
                    result="BLOCKED_DEFENSIVE_REGIME",
                    market_regime=market_regime,
                    composite_score=comp_total,
                    required_buy_score=required_buy_score,
                )
                return _result("BLOCKED_DEFENSIVE_REGIME", guard="defensive_regime_score_block")
        is_extreme_fear = fg and fg["value"] <= 20
        if volume and volume["ratio"] <= 0.15 and not is_extreme_fear:
            log.warning(
                f"5분봉 거래량 {volume['ratio']}x 거의 0 — BUY 차단",
                guard="low_volume_block",
                result="BLOCKED_VOLUME",
                vol_ratio_5m=volume["ratio"],
            )
            return _result("BLOCKED_VOLUME", guard="low_volume_block")
        if volume and volume["ratio"] >= 3.0:
            confirmed = bool((candle_confirmation or {}).get("confirmed_breakout"))
            if not confirmed:
                log.warning(
                    "거래량 급등 구간이지만 최근 5분봉 돌파 마감이 확인되지 않아 BUY 차단",
                    guard="breakout_confirmation_block",
                    result="BLOCKED_BREAKOUT_CONFIRMATION",
                    vol_ratio_5m=volume["ratio"],
                    breakout_confirmed=False,
                )
                return _result("BLOCKED_BREAKOUT_CONFIRMATION", guard="breakout_confirmation_block")

    # ── 신뢰도 필터 ──
    if signal["confidence"] < RISK["min_confidence"]:
        return _result("SKIP", guard="min_confidence")

    btc_balance = _upbit_call(upbit.get_balance, "BTC", max_retries=3) or 0
    krw_balance = _upbit_call(upbit.get_balance, "KRW", max_retries=3) or 0
    pos         = get_open_position()
    price       = indicators["price"]
    log.info(f"잔고 스냅샷: KRW={float(krw_balance):,.0f} | BTC={float(btc_balance):.8f}")

    # 프로세스 내 잠금 체크 (Supabase 딜레이로 get_open_position()이 None인 경우 대비)
    # 파일 기반 저장으로 프로세스 재시작 후에도 잠금 유지
    import time as _t
    if signal["action"] == "BUY":
        with _buy_lock:
            _elapsed = _t.time() - _read_last_buy_ts()
            if _elapsed < 3600:
                log.warning(
                    "프로세스 잠금: 최근 1시간 내 매수 완료 — 중복 매수 차단",
                    guard="in_process_lock",
                    result="ALREADY_LONG",
                    elapsed_sec=int(_elapsed),
                )
                return _result("ALREADY_LONG", guard="in_process_lock")

    if signal["action"] == "BUY" and (btc_balance > 0.00001 or pos):
        log.warning(
            "실제 BTC 잔고 또는 OPEN 포지션이 존재해 중복 BUY 차단 "
            f"(btc_balance={float(btc_balance):.8f}, has_pos={bool(pos)})",
            guard="already_long_block",
            result="ALREADY_LONG",
            btc_balance=float(btc_balance),
            has_open_position=bool(pos),
        )
        return _result("ALREADY_LONG", guard="already_long_block")

    # ── 손절/익절 + 트레일링 스탑 ──
    if btc_balance > 0.00001 and pos:
        entry_price = float(pos["entry_price"])
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
                    log.debug(f"highest_price 업데이트 실패: {e}")

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
                if not DRY_RUN:
                    sold, reason = _execute_sell_order(btc_balance * 0.9995, context="TRAILING_STOP")
                    if not sold:
                        return _result("SELL_ORDER_FAILED", reason=reason)
                    close_all_positions(price, exit_reason="TRAILING_STOP")
                send_telegram(
                    f"📉 <b>트레일링 스탑</b>\n"
                    f"고점: {highest:,.0f}원 → 현재가: {price:,.0f}원\n"
                    f"하락폭: {drop*100:.1f}% (기준: {trail_pct*100:.1f}%) / 수익: {net_change*100:.2f}%"
                )
                return _result("TRAILING_STOP")

        # ATR 동적 손절 (진입 시 계산된 ATR 기반 손절가)
        atr_stop_price = float(pos.get("atr_stop_price") or 0)
        if atr_stop_price and price < atr_stop_price:
            if not DRY_RUN:
                sold, reason = _execute_sell_order(btc_balance * 0.9995, context="ATR_STOP_LOSS")
                if not sold:
                    return _result("SELL_ORDER_FAILED", reason=reason)
                close_all_positions(price, exit_reason="ATR_STOP_LOSS")
            send_telegram(
                f"🛑 <b>ATR 동적 손절</b>\n"
                f"진입가: {entry_price:,}원\n"
                f"ATR 손절가: {atr_stop_price:,.0f}원 → 현재가: {price:,}원\n"
                f"손실(비용 포함): {net_change*100:.2f}%"
            )
            return _result("ATR_STOP_LOSS")

        # 고정 % 손절 (fallback)
        if net_change <= RISK["stop_loss"]:
            if not DRY_RUN:
                sold, reason = _execute_sell_order(btc_balance * 0.9995, context="STOP_LOSS")
                if not sold:
                    return _result("SELL_ORDER_FAILED", reason=reason)
                close_all_positions(price, exit_reason="STOP_LOSS")
            send_telegram(
                f"🛑 <b>손절 실행</b>\n"
                f"진입가: {entry_price:,}원\n"
                f"현재가: {price:,}원\n"
                f"손실(비용 포함): {net_change*100:.2f}%"
            )
            return _result("STOP_LOSS")

        # ── 다단계 분할 익절 ──
        partial_1_done = pos.get("partial_1_sold") or pos.get("partial_sold", False)
        partial_2_done = pos.get("partial_2_sold", False)

        # 1단계: 설정 수익률 도달 시 보유량의 일부 매도
        if net_change >= RISK.get("partial_tp_pct", 0.08) and not partial_1_done and btc_balance > 0.0001:
            ratio = RISK.get("partial_tp_ratio", 0.50)
            sell_qty = btc_balance * ratio * 0.9995
            if not DRY_RUN:
                sold, reason = _execute_sell_order(sell_qty, context="PARTIAL_TP_1")
                if not sold:
                    return _result("SELL_ORDER_FAILED", reason=reason)
                try:
                    supabase.table("btc_position").update(
                        {"partial_1_sold": True, "partial_sold": True}
                    ).eq("id", pos["id"]).execute()
                except Exception as e:
                    log.debug(f"partial_1_sold 업데이트 실패: {e}")
            send_telegram(
                f"🟡 <b>분할 익절 1단계 ({int(ratio*100)}%)</b>\n"
                f"진입가: {entry_price:,}원 | 현재가: {price:,}원\n"
                f"수익: +{net_change*100:.2f}% (목표 {RISK.get('partial_tp_pct', 0.08)*100:.1f}%) | "
                f"매도량: {sell_qty:.6f} BTC\n"
                f"잔여분 트레일링 스탑 + 2단계 익절 대기"
            )
            return _result("PARTIAL_TP_1")

        # 2단계: 설정 수익률 도달 시 남은 물량의 일부 추가 매도
        if (net_change >= RISK.get("partial_tp_2_pct", 0.12)
                and partial_1_done and not partial_2_done and btc_balance > 0.0001):
            ratio2 = RISK.get("partial_tp_2_ratio", 0.50)
            sell_qty = btc_balance * ratio2 * 0.9995
            if not DRY_RUN:
                sold, reason = _execute_sell_order(sell_qty, context="PARTIAL_TP_2")
                if not sold:
                    return _result("SELL_ORDER_FAILED", reason=reason)
                try:
                    supabase.table("btc_position").update(
                        {"partial_2_sold": True}
                    ).eq("id", pos["id"]).execute()
                except Exception as e:
                    log.debug(f"partial_2_sold 업데이트 실패: {e}")
            send_telegram(
                f"🟢 <b>분할 익절 2단계 ({int(ratio2*100)}%)</b>\n"
                f"진입가: {entry_price:,}원 | 현재가: {price:,}원\n"
                f"수익: +{net_change*100:.2f}% (목표 {RISK.get('partial_tp_2_pct', 0.12)*100:.1f}%) | "
                f"매도량: {sell_qty:.6f} BTC\n"
                f"잔여분 트레일링 스탑으로 최종 보호"
            )
            return _result("PARTIAL_TP_2")

        # 최대 익절 전량
        if net_change >= RISK["take_profit"]:
            if not DRY_RUN:
                sold, reason = _execute_sell_order(btc_balance * 0.9995, context="TAKE_PROFIT")
                if not sold:
                    return _result("SELL_ORDER_FAILED", reason=reason)
                close_all_positions(price, exit_reason="TAKE_PROFIT")
            send_telegram(
                f"✅ <b>전량 익절</b>\n"
                f"진입가: {entry_price:,}원 | 현재가: {price:,}원\n"
                f"수익(비용 포함): +{net_change*100:.2f}%"
            )
            return _result("TAKE_PROFIT")

    # ── 분할 매수 ──
    if signal["action"] == "BUY":
        comp_total = comp["total"] if comp else 50
        stage      = get_split_stage(comp_total)
        invest_krw = krw_balance * RISK["split_ratios"][stage - 1]
        log.info(
            f"매수 가능 금액 계산: stage={stage} | split_ratio={RISK['split_ratios'][stage - 1]:.2f} "
            f"| invest_krw={float(invest_krw):,.0f}"
        )

        if invest_krw < 5000:
            log.warning(
                f"매수 실패: INSUFFICIENT_KRW | KRW={float(krw_balance):,.0f} | "
                f"invest_krw={float(invest_krw):,.0f}"
            )
            return _result("INSUFFICIENT_KRW")

        if not DRY_RUN:
            if _smart_router and invest_krw >= 5_000_000:
                try:
                    route = _smart_router.decide(
                        symbol="KRW-BTC",
                        side="buy",
                        total_qty=invest_krw / max(price, 1),
                        market="btc",
                        price_hint=price,
                    )
                    log.info(f"SmartRouter decision: {route.route} for {invest_krw:,.0f} KRW")
                    if route.route == "TWAP":
                        log.info("TWAP suggested but using MARKET for BTC liquidity")
                except Exception as e:
                    log.warning(f"SmartRouter failed, fallback to MARKET: {e}")
            result = _upbit_call(upbit.buy_market_order, "KRW-BTC", invest_krw, max_retries=1)
            if result is None or not isinstance(result, dict) or "error" in result:
                log.error(f"Upbit 매수 주문 실패: {result}")
                send_telegram(f"❌ BTC 매수 주문 실패\n응답: {result}")
                return _result("ORDER_FAILED", reason="upbit_buy_error")
            qty = float(result.get("executed_volume", 0)) or (invest_krw / price)
            # ATR 기반 손절가 계산 (진입 시점 ATR * 배수만큼 하락 시 손절)
            atr_val = indicators.get("atr", 0)
            atr_stop = round(price - atr_val * RISK["atr_multiplier"]) if atr_val else None
            # DB 저장 3회 재시도 (레이스 컨디션 방지)
            ok = False
            for _attempt in range(3):
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
                )
                if ok:
                    with _buy_lock:
                        _write_last_buy_ts()  # 파일 기반 잠금 갱신 (재시작 후에도 유지)
                    break
                log.warning(f"포지션 DB 저장 재시도 {_attempt + 1}/3")
                import time as _time; _time.sleep(2)
            if not ok:
                log.error(f"포지션 DB 저장 3회 실패. qty={qty:.8f} BTC — 수동 확인 필요")
                send_telegram(
                    f"⚠️ BTC 매수 성공했으나 DB 저장 실패 (3회 재시도).\n"
                    f"수량: {qty:.8f} BTC\n수동 확인 후 처리하세요.",
                )
                return _result("POSITION_DB_FAIL")
        else:
            log.info(f"[DRY_RUN] {stage}차 매수 — {invest_krw:,.0f}원")

        qty_est = qty if not DRY_RUN else invest_krw / price
        atr_val_est = indicators.get("atr", 0)
        atr_stop_est = round(price - atr_val_est * RISK["atr_multiplier"]) if atr_val_est else None
        sl_price = atr_stop_est or int(price * (1 + RISK["stop_loss"]))
        tp1_price = int(price * (1 + RISK.get("partial_tp_pct", 0.08)))
        tp2_price = int(price * (1 + RISK.get("partial_tp_2_pct", 0.12)))
        tp_price  = int(price * (1 + RISK["take_profit"]))
        comp_total = comp["total"] if comp else 0
        btc_val = int(price * qty_est)
        krw_remain = max(0, int(krw_balance - invest_krw))
        total_asset = krw_remain + btc_val
        btc_weight = round(btc_val / max(total_asset, 1) * 100)
        atr_line = f"ATR손절: ₩{atr_stop_est:,} (ATR×{RISK['atr_multiplier']})\n" if atr_stop_est else ""
        send_telegram(
            f"📈 <b>BTC 매수 체결</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"가격: ₩{price:,.0f} ({qty_est:.8f} BTC)\n"
            f"복합스코어: {comp_total}/100\n"
            f"진입근거: {signal['reason']}\n"
            f"━━━━━━━━━━━━━━\n"
            f"{atr_line}"
            f"익절1: ₩{tp1_price:,} (+{RISK.get('partial_tp_pct', 0.08)*100:.0f}%) / 익절2: ₩{tp2_price:,} (+{RISK.get('partial_tp_2_pct', 0.12)*100:.0f}%) / 전량: ₩{tp_price:,} (+{RISK['take_profit']*100:.0f}%)\n"
            f"━━━━━━━━━━━━━━\n"
            f"총자산: ₩{total_asset:,}\n"
            f"BTC 비중: {btc_weight}%",
            priority=_TgPriority.IMPORTANT,
        )
        if _sheets_append:
            try:
                _sheets_append("btc", "매수", "BTC", price, qty, None, signal.get("reason", ""))
            except Exception as e:
                log.debug(f"BTC sheets 매수 기록 실패: {e}")
        return _result(f"BUY_{stage}차", stage=stage)

    # ── AI SELL ──
    elif signal["action"] == "SELL" and btc_balance > 0.00001:
        pnl_pct = None
        if pos:
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        if not DRY_RUN:
            sold, reason = _execute_sell_order(btc_balance * 0.9995, context="SELL_SIGNAL")
            if not sold:
                return _result("SELL_ORDER_FAILED", reason=reason)
            close_all_positions(price, exit_reason="SELL_SIGNAL")
        send_telegram(
            f"🔴 <b>BTC 매도</b>\n"
            f"💰 가격: {price:,}원\n"
            f"📊 RSI: {indicators['rsi']}\n"
            f"🎯 신뢰도: {signal['confidence']}%\n"
            f"📝 {signal['reason']}"
        )
        if _sheets_append:
            try:
                action = "손절" if pnl_pct is not None and pnl_pct < -2 else "익절" if pnl_pct is not None and pnl_pct > 2 else "매도"
                _sheets_append("btc", action, "BTC", price, btc_balance, pnl_pct, signal.get("reason", ""))
            except Exception as e:
                log.debug(f"BTC sheets 매도 기록 실패: {e}")
        return _result("SELL")

    return _result("HOLD")

# ── Supabase 로그 ─────────────────────────────────
def save_log(
    indicators,
    signal,
    result,
    *,
    fg=None,
    volume=None,
    comp=None,
    funding=None,
    oi=None,
    ls_ratio=None,
    kimchi=None,
    market_regime=None,
) -> None:
    try:
        result_code = result.get("result", "UNKNOWN") if isinstance(result, dict) else str(result)
        guard = result.get("guard") if isinstance(result, dict) else None
        row = {
            "timestamp":          _utc_now_iso(),
            "action":             signal.get("action", "HOLD"),
            "price":              indicators["price"],
            "rsi":                indicators["rsi"],
            "macd":               indicators["macd"],
            "confidence":         signal.get("confidence", 0),
            "reason":             signal.get("reason", ""),
            "indicator_snapshot": json.dumps(indicators),
            "order_raw":          json.dumps(result),
            # --- Optional signal context (safe if schema supports it) ---
            "fg_value":           (fg or {}).get("value") if fg else None,
            "bb_pct":             (comp or {}).get("bb_pct") if isinstance(comp, dict) else None,
            "vol_ratio_5m":       (volume or {}).get("ratio") if volume else None,
            "trend":              (comp or {}).get("trend") if isinstance(comp, dict) else None,
            "funding_rate":       (funding or {}).get("rate") if funding else None,
            "oi_ratio":           (oi or {}).get("ratio") if oi else None,
            "ls_ratio":           (ls_ratio or {}).get("ls_ratio") if ls_ratio else None,
            "kimchi":             kimchi,
            "market_regime":      market_regime,
            "composite_score":    (comp or {}).get("total") if isinstance(comp, dict) else None,
        }

        # 팩터 스냅샷 수집 — 모든 사이클(HOLD 포함). ML sequence 학습용.
        try:
            import sys as _sys
            _WORKSPACE_PATH = str(Path(__file__).resolve().parents[1])
            if _WORKSPACE_PATH not in _sys.path:
                _sys.path.insert(0, _WORKSPACE_PATH)
            from quant.factors.registry import FactorContext, calc_all
            _fctx = FactorContext()
            _today_iso = datetime.now(timezone.utc).date().isoformat()
            _all_factors = calc_all(_today_iso, symbol="BTC", market="btc", context=_fctx)
            _top5 = dict(
                sorted(_all_factors.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
            )
            row["factor_snapshot"] = json.dumps(_top5, ensure_ascii=False)
            log.info(f"BTC 팩터 스냅샷 수집: {list(_top5.keys())}")
        except Exception as _fe:
            log.warning(f"BTC 팩터 스냅샷 건너뜀: {_fe}")

        try:
            supabase.table("btc_trades").insert(row).execute()
        except Exception:
            # Step 1: factor_snapshot 컬럼 미적용 환경 대응 — 키 제거 후 재시도
            if "factor_snapshot" in row:
                del row["factor_snapshot"]
                try:
                    supabase.table("btc_trades").insert(row).execute()
                except Exception:
                    # Step 2: Fallback to minimal schema
                    minimal = {k: row[k] for k in [
                        "timestamp", "action", "price", "rsi", "macd",
                        "confidence", "reason", "indicator_snapshot", "order_raw",
                    ]}
                    supabase.table("btc_trades").insert(minimal).execute()
            else:
                # factor_snapshot 없는 경우 기존 minimal fallback
                minimal = {k: row[k] for k in [
                    "timestamp", "action", "price", "rsi", "macd",
                    "confidence", "reason", "indicator_snapshot", "order_raw",
                ]}
                supabase.table("btc_trades").insert(minimal).execute()
        log.debug("Supabase 저장 완료", result_code=result_code, guard=guard)
    except Exception as e:
        log.error(f"Supabase 저장 실패: {e}")

# ── 메인 사이클 ───────────────────────────────────
def run_trading_cycle() -> dict:
    global supabase

    try:
        supabase = get_supabase()
        if not supabase:
            log.warning("Supabase 미연결 — 이번 사이클 스킵")
            return {"result": "SUPABASE_UNAVAILABLE"}
        supabase.table("btc_position").select("id").limit(1).execute()
    except Exception as e:
        log.warning(f"Supabase 쿼리 실패, 재연결 시도: {e}")
        _reset_client()
        return {"result": "SUPABASE_RECONNECT"}

    # 일일 손실 한도 체크
    if check_daily_loss():
        log.warning("일일 손실 한도 초과 — 사이클 스킵")
        return {"result": "DAILY_LOSS_LIMIT"}

    # 오늘 신규 매수 건수 한도 체크 (포지션 보유 중이면 매도 시그널 분석을 위해 스킵하지 않음)
    today = _utc_today_iso()
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
                _last_entry_str = _cd_res.data[0].get("entry_time", "")
                if _last_entry_str:
                    _last_entry = datetime.fromisoformat(
                        _last_entry_str.replace("Z", "+00:00")
                    )
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
        except Exception as _cd_e:
            log.debug(f"쿨다운 체크 실패 (무시): {_cd_e}")

    log.info("매매 사이클 시작")

    df         = get_market_data()
    if not has_valid_market_data(df):
        log.warning(
            "시장 데이터 조회 실패 또는 비정상 응답 — 사이클 스킵",
            guard="market_data_unavailable",
            result="MARKET_DATA_UNAVAILABLE",
        )
        return {"result": "MARKET_DATA_UNAVAILABLE"}
    indicators = calculate_indicators(df)
    volume     = get_volume_analysis(df)
    candle_confirmation = get_candle_confirmation(df)
    fg         = get_fear_greed()
    htf        = get_hourly_trend()
    momentum   = get_daily_momentum()
    news       = _get_news_result()
    pos        = get_open_position()
    try:
        kimchi = get_kimchi_premium()
    except Exception as e:
        log.warning(f"signal fetch failed: kimchi: {e}")
        kimchi = 0.0

    # ── 온체인 데이터 (v6 신규) ──
    from common.market_data import (get_btc_funding_rate,
                                    get_btc_long_short_ratio,
                                    get_btc_open_interest,
                                    get_btc_whale_activity, get_market_regime)
    try:
        funding = get_btc_funding_rate()
    except Exception as e:
        log.warning(f"signal fetch failed: funding: {e}")
        funding = {"rate": 0.0, "signal": "NEUTRAL"}
    oi       = get_btc_open_interest()
    ls_ratio = get_btc_long_short_ratio()
    try:
        whale = get_btc_whale_activity()
    except Exception as e:
        log.warning(f"signal fetch failed: whale: {e}")
        whale = {"unconfirmed_tx": 0.0, "signal": "NEUTRAL"}

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
    rsi_5m   = indicators["rsi"]
    rsi_d    = momentum["rsi_d"]

    comp = calc_btc_composite(
        fg_value, rsi_d, momentum["bb_pct"],
        momentum["vol_ratio_d"], htf["trend"], momentum["ret_7d"],
        funding=funding, oi=oi, ls_ratio=ls_ratio, kimchi=kimchi,
        regime=market_regime,
        news_sentiment=news.get("score", 0.0),
        whale=whale_signal if whale_signal else None,
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
        log.debug(f"OPEN 포지션 컨텍스트 backfill 실패: {e}")

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
            "updated": _utc_now_iso(),
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
    log.info(
        f"5분봉 확인: {candle_confirmation['label']}",
        breakout_confirmed=bool(candle_confirmation.get("confirmed_breakout")),
        breakout_label=candle_confirmation["label"],
    )
    if kimchi is not None:
        log.info(f"김치 프리미엄: {kimchi:+.2f}%")

    # ── 복합 스코어 기반 매매 결정 ──
    signal = None
    buy_min = get_required_buy_score(market_regime)

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
    elif kimchi_blocked:
        log.info(f"김치 프리미엄 {kimchi:+.2f}% 과열 — 매수 차단")
    elif funding_blocked and not pos:
        log.info(f"펀딩비 롱 과열 — 매수 차단 (funding_blocked=True)")
    elif comp["total"] >= buy_min and not pos and htf["trend"] != "DOWNTREND":
        conf = min(60 + comp["total"] - buy_min, 90)
        signal = {
            "action": "BUY", "confidence": int(conf),
            "reason": f"복합스코어 {comp['total']}/{buy_min} (F&G={fg_value}, dRSI={rsi_d}) [룰기반]"
        }
        log.trade(f"복합스코어 매수 발동: {comp['total']}점 >= {buy_min}")

    # 2) 극단 공포 오버라이드: F&G<=15 + UPTREND, 또는 F&G<=12 극단공포 시 DOWNTREND도 허용(소량, confidence↓)
    elif fg_value <= 15 and rsi_d <= 55 and not pos:
        if htf["trend"] != "DOWNTREND":
            signal = {
                "action": "BUY", "confidence": 78,
                "reason": f"극도공포 오버라이드 F&G={fg_value}, dRSI={rsi_d} [룰기반]"
            }
            log.trade(f"극도공포 오버라이드: F&G={fg_value}, dRSI={rsi_d}")
        elif fg_value <= 12:
            # F&G 12 이하 극단 공포 — DOWNTREND에도 역발상 소량 매수 허용 (confidence 낮게)
            signal = {
                "action": "BUY", "confidence": 66,
                "reason": f"극단공포 역발상(DOWNTREND) F&G={fg_value}, dRSI={rsi_d} [룰기반]"
            }
            log.trade(f"극단공포 역발상 오버라이드(DOWNTREND): F&G={fg_value}, dRSI={rsi_d}")

    # 3) 기술적 과매수 매도: 일봉 RSI>=75 + 하락 추세
    elif rsi_d >= 75 and htf["trend"] == "DOWNTREND" and pos:
        signal = {
            "action": "SELL", "confidence": 78,
            "reason": f"과매수+하락추세 dRSI={rsi_d:.0f} [룰기반]"
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
                    "reason": f"타임컷 {held_days}일 보유, 수익 {pnl_pct*100:+.1f}% [룰기반]"
                }
                log.trade(f"타임컷 발동: {held_days}일, 수익 {pnl_pct*100:+.1f}%")

    # 5) 극도 탐욕 매도 (수익 보존)
    if pos and not signal and fg_value >= 75:
        entry_p = float(pos["entry_price"])
        pnl_pct = (indicators["price"] - entry_p) / entry_p
        if pnl_pct > 0:
            signal = {
                "action": "SELL", "confidence": 72,
                "reason": f"극도탐욕 F&G={fg_value} + 수익 {pnl_pct*100:+.1f}% 보존 [룰기반]"
            }
            log.trade(f"극도탐욕 매도: F&G={fg_value}, 수익={pnl_pct*100:+.1f}%")

    # 6) BB 상단 과매수 매도
    if pos and not signal and momentum["bb_pct"] >= 85 and rsi_d >= 65:
        signal = {
            "action": "SELL", "confidence": 70,
            "reason": f"BB상단({momentum['bb_pct']:.0f}%) + 일봉과매수 RSI={rsi_d:.0f} [룰기반]"
        }
        log.trade(f"BB상단 매도: bb_pct={momentum['bb_pct']:.0f}%, rsi_d={rsi_d:.0f}")

    # 7) 추세 하락전환 + 수익 보존 (2% 이상)
    if pos and not signal and htf["trend"] == "DOWNTREND":
        entry_p = float(pos["entry_price"])
        pnl_pct = (indicators["price"] - entry_p) / entry_p
        if pnl_pct >= 0.02:
            signal = {
                "action": "SELL", "confidence": 68,
                "reason": f"추세 하락전환(DOWNTREND) + 수익 {pnl_pct*100:+.1f}% 보존 [룰기반]"
            }
            log.trade(f"추세전환 매도: DOWNTREND, 수익={pnl_pct*100:+.1f}%")

    # 8) 룰기반 미발동 → AI 분석
    if not signal:
        # 최근 거래 기억 주입 (같은 실수 반복 방지)
        _mem_ctx = ""
        try:
            from memory.trade_memory import TradeMemory
            _mem_ctx = TradeMemory(supabase).get_recent_context("btc", limit=10)
        except Exception as _me:
            log.debug("trade_memory 로드 실패 (무시): %s", _me)

        signal = analyze_with_ai(
            indicators, news.get("summary", ""), fg, htf, volume,
            comp=comp, rsi_d=rsi_d, momentum=momentum,
            funding=funding, ls_ratio=ls_ratio, regime=market_regime,
            memory_context=_mem_ctx,
        )

    # ── 보조 보정 ──

    # 거래량 폭발
    vol_r = volume["ratio"]
    if vol_r >= 3.0:
        log.info(f"거래량 폭발 감지 ({vol_r:.1f}x)")
        if signal["action"] == "BUY" and candle_confirmation["confirmed_breakout"]:
            signal["confidence"] = max(signal["confidence"], 78)
        elif signal["action"] == "BUY":
            log.info("거래량 급등은 확인됐지만 돌파 마감이 없어 BUY 신뢰도 보강 생략")

    # 김치 프리미엄 저평가
    if kimchi is not None and kimchi <= -2.0 and signal["action"] == "HOLD" and rsi_d < 55:
        signal["action"] = "BUY"
        signal["confidence"] = max(signal.get("confidence", 0), 72)
        signal["reason"] += f" [김치 저평가 {kimchi:+.2f}%]"

    result = execute_trade(
        signal,
        indicators,
        fg,
        volume,
        comp,
        candle_confirmation=candle_confirmation,
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
    log.trade(
        f"신호: {signal['action']} (신뢰도: {signal['confidence']}%) → {result['result']}",
        action=signal.get("action"),
        confidence=signal.get("confidence", 0),
        result=result.get("result"),
        guard=result.get("guard"),
        composite_score=(comp or {}).get("total") if isinstance(comp, dict) else None,
        vol_ratio_5m=(volume or {}).get("ratio") if volume else None,
    )

    return result

def build_hourly_summary() -> str:
    """매시 요약 텍스트 생성 (가격·포지션·오늘 손익·F&G·1시간봉 추세)."""
    try:
        df = get_market_data()
        ind = calculate_indicators(df)
        price = int(ind["price"])
        rsi = ind["rsi"]
        fg = get_fear_greed()
        htf = get_hourly_trend()
        pos = get_open_position()

        today = _utc_today_iso()
        try:
            res = supabase.table("btc_position").select("pnl").eq("status", "CLOSED").gte("exit_time", today).execute()
            today_pnl = sum(float(r["pnl"] or 0) for r in (res.data or []))
        except Exception as e:
            log.debug(f"매시 요약 손익 계산 실패: {e}")
            today_pnl = 0

        pos_line = "포지션 없음"
        if pos:
            entry = int(float(pos["entry_price"]))
            pos_line = f"포지션 있음 @ {entry:,}원"

        msg = (
            f"⏰ <b>BTC 매시 요약</b> {datetime.now(timezone.utc).strftime('%m/%d %H:%M')}\n"
            f"💰 가격: {price:,}원 | RSI: {rsi}\n"
            f"📊 {pos_line}\n"
            f"📈 1시간봉: {htf['trend']} | F&G: {fg['label']}({fg['value']})\n"
            f"📉 오늘 손익: {today_pnl:+,.0f}원"
        )
        return msg
    except Exception as e:
        return f"⏰ BTC 매시 요약 생성 실패: {e}"

def send_hourly_report() -> None:
    """매시 정각 요약 — INFO 버퍼에 저장 (일일 리포트에 병합됨)."""
    msg = build_hourly_summary()
    send_telegram(msg, priority=_TgPriority.INFO)
    log.info("매시 요약 INFO 버퍼 저장 완료")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        pos = get_open_position()
        if pos:
            df = get_market_data()
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
