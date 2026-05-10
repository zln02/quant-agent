"""BTC-related API endpoints."""
import asyncio
import json
import math
import os
import sys as _sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
import requests
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common.config import (BRAIN_PATH, BTC_FX_CACHE_TTL, BTC_LOG,
                           BTC_NEWS_CACHE_TTL, MEMORY_PATH,
                           UPBIT_BALANCE_CACHE_TTL)
from common.logger import get_logger
from common.supabase_client import get_supabase

log = get_logger("btc_api")

supabase = get_supabase()
router = APIRouter()

# ── caches ──────────────────────────────────────────────
_upbit_cache = {
    "time": 0,
    # Legacy: historically used as "KRW available" balance
    "krw": None,
    # New: expose available/locked/total for clarity
    "krw_available": None,
    "krw_locked": None,
    "krw_total": None,
    "ok": False,
}
_news_cache = {"data": [], "ts": 0}
_trend_cache = {"value": "SIDEWAYS", "time": 0}
_fx_cache = {"rate": 1300.0, "time": 0}  # USD to KRW fallback


def _refresh_upbit_cache():
    # Cache TTL: 60s. However, if we have a KRW value but the extended fields
    # are still missing (e.g. after a hot reload/deploy), refresh immediately.
    if time.time() - _upbit_cache["time"] <= UPBIT_BALANCE_CACHE_TTL:
        if _upbit_cache.get("krw") is not None and (
            _upbit_cache.get("krw_locked") is None or _upbit_cache.get("krw_total") is None
        ):
            pass
        else:
            return
    _upbit_cache["time"] = time.time()
    _upbit_cache["krw"] = None
    _upbit_cache["krw_available"] = None
    _upbit_cache["krw_locked"] = None
    _upbit_cache["krw_total"] = None
    _upbit_cache["ok"] = False
    try:
        upbit_key = os.environ.get("UPBIT_ACCESS_KEY", "")
        upbit_secret = os.environ.get("UPBIT_SECRET_KEY", "")
        if upbit_key and upbit_secret:
            import pyupbit
            upbit = pyupbit.Upbit(upbit_key, upbit_secret)
            try:
                balances = upbit.get_balances() or []
                krw = next((b for b in balances if b.get("currency") == "KRW"), None)
                if krw is not None:
                    available = float(krw.get("balance") or 0)
                    locked = float(krw.get("locked") or 0)
                    total = available + locked
                    _upbit_cache["krw_available"] = available
                    _upbit_cache["krw_locked"] = locked
                    _upbit_cache["krw_total"] = total
                    # Keep legacy field as "available" so existing logic stays consistent.
                    _upbit_cache["krw"] = available
                else:
                    bal = upbit.get_balance("KRW")
                    _upbit_cache["krw"] = float(bal) if bal is not None else None
                    _upbit_cache["krw_available"] = _upbit_cache["krw"]
                    _upbit_cache["krw_locked"] = 0.0 if _upbit_cache["krw"] is not None else None
                    _upbit_cache["krw_total"] = _upbit_cache["krw"]
            except Exception:
                bal = upbit.get_balance("KRW")
                _upbit_cache["krw"] = float(bal) if bal is not None else None
                _upbit_cache["krw_available"] = _upbit_cache["krw"]
                _upbit_cache["krw_locked"] = 0.0 if _upbit_cache["krw"] is not None else None
                _upbit_cache["krw_total"] = _upbit_cache["krw"]
            _upbit_cache["ok"] = True
    except Exception as e:
        log.error(f"upbit cache: {e}")


def _get_fx_rate():
    """Fetch real-time USD to KRW exchange rate."""
    if time.time() - _fx_cache["time"] < BTC_FX_CACHE_TTL:
        return _fx_cache["rate"]
    try:
        # Try Upbit first (most reliable for KRW pairs)
        import pyupbit
        price = pyupbit.get_current_price("KRW-USD")
        if price:
            _fx_cache["rate"] = float(price)
            _fx_cache["time"] = time.time()
            return _fx_cache["rate"]
    except Exception:
        pass

    try:
        # Fallback: Get rate from exchange API
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
        if r.status_code == 200:
            rate = r.json().get("rates", {}).get("KRW", 1300.0)
            _fx_cache["rate"] = float(rate)
            _fx_cache["time"] = time.time()
            return _fx_cache["rate"]
    except Exception:
        pass

    return _fx_cache["rate"]  # Return cached or default (1300)


def _get_hourly_trend():
    if time.time() - _trend_cache["time"] < 300:
        return _trend_cache["value"]
    try:
        import pyupbit
        df = pyupbit.get_ohlcv("KRW-BTC", interval="minute60", count=50)
        if df is None or df.empty:
            _trend_cache.update(value="SIDEWAYS", time=time.time())
            return "SIDEWAYS"
        from ta.trend import EMAIndicator
        close = df["close"]
        ema20 = EMAIndicator(close, window=20).ema_indicator().iloc[-1]
        ema50 = EMAIndicator(close, window=50).ema_indicator().iloc[-1]
        price = close.iloc[-1]
        if ema20 > ema50 and price > ema20:
            result = "UPTREND"
        elif ema20 < ema50 and price < ema20:
            result = "DOWNTREND"
        else:
            result = "SIDEWAYS"
        _trend_cache.update(value=result, time=time.time())
        return result
    except Exception as e:
        log.error(f"trend: {e}")
        _trend_cache.update(value="SIDEWAYS", time=time.time())
        return "SIDEWAYS"


def get_upbit_cache():
    """Allow other modules to access upbit cache."""
    _refresh_upbit_cache()
    return _upbit_cache


def _safe_float(val, default: float = 0.0) -> float:
    """Replace NaN/Inf with a safe default."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return default if (math.isnan(f) or math.isinf(f)) else f


def _sanitize_floats(obj):
    if isinstance(obj, float):
        return 0.0 if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_floats(v) for v in obj]
    return obj


# ── BTC page ────────────────────────────────────────────
# @router.get("/", response_class=HTMLResponse)
# async def index():
#     from btc.templates.btc_html import BTC_HTML
#     return BTC_HTML


def _compute_composite_sync():
    """Blocking composite computation — run via asyncio.to_thread."""
    import yfinance as _yf_c
    from ta.momentum import RSIIndicator as _RSI
    from ta.volatility import BollingerBands as _BB

    df = _yf_c.download("BTC-USD", period="90d", interval="1d", progress=False)
    if df.empty:
        return {"error": "데이터 없음"}
    close = df["Close"].squeeze()
    latest_close = _safe_float(close.iloc[-1], 0.0)
    rsi_d = _safe_float(_RSI(close, window=14).rsi().iloc[-1], 50.0)
    bb = _BB(close, window=20)
    bb_h = _safe_float(bb.bollinger_hband().iloc[-1], latest_close)
    bb_l = _safe_float(bb.bollinger_lband().iloc[-1], latest_close)
    bb_pct = ((latest_close - bb_l) / (bb_h - bb_l) * 100) if bb_h > bb_l else 50.0
    vol = df["Volume"].squeeze()
    vol_avg = _safe_float(vol.rolling(20).mean().iloc[-1], 1.0)
    latest_vol = _safe_float(vol.iloc[-1], 0.0)
    vol_ratio_d = latest_vol / vol_avg if vol_avg > 0 else 1.0
    close_7d = _safe_float(close.iloc[-8], 0.0) if len(close) > 8 else 0.0
    close_30d = _safe_float(close.iloc[-31], 0.0) if len(close) > 31 else 0.0
    ret_7d = ((latest_close / close_7d) - 1) * 100 if close_7d > 0 else 0.0
    ret_30d = ((latest_close / close_30d) - 1) * 100 if close_30d > 0 else 0.0

    fg_val = 50
    try:
        fg_r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        fg_val = int(fg_r.json()["data"][0]["value"])
    except Exception:
        pass

    trend = _get_hourly_trend()

    from btc.btc_trading_agent import calc_btc_composite
    comp = calc_btc_composite(fg_val, rsi_d, bb_pct, vol_ratio_d, trend, ret_7d)

    pos = None
    if supabase:
        pr = supabase.table("btc_position").select("*").eq("status", "OPEN").order("entry_time", desc=True).limit(1).execute()
        pos = pr.data[0] if pr.data else None

    cur_price = latest_close
    fx_rate = _get_fx_rate()  # Real-time FX rate instead of hardcoded 1450
    pos_pnl = None
    if pos:
        entry_p = _safe_float(pos.get("entry_price", 0), 0.0)
        if entry_p > 0:
            pos_pnl = {
                "pnl_pct": round(_safe_float((cur_price * fx_rate - entry_p) / entry_p * 100, 0.0), 2),
                "entry_price": entry_p,
                "quantity": pos.get("quantity", 0),
                "entry_krw": pos.get("entry_krw", 0),
                "current_fx_rate": _safe_float(fx_rate, 0.0),
            }

    buy_threshold = 50
    try:
        from btc.btc_trading_agent import RISK as _BTC_RISK
        buy_threshold = _BTC_RISK.get("buy_composite_min", 50)
    except Exception:
        pass

    return _sanitize_floats({
        "composite": comp,
        # 프론트엔드 호환을 위한 top-level 편의 키
        "composite_score": comp.get("total", 0),
        "bb_score": comp.get("bb", 0),
        "volume_score": comp.get("vol", 0),
        "trend_score": comp.get("trend", 0),
        "fg_value": fg_val,
        "rsi_d": round(_safe_float(rsi_d, 50.0), 1),
        "bb_pct": round(_safe_float(bb_pct, 50.0), 1),
        "vol_ratio_d": round(_safe_float(vol_ratio_d, 1.0), 2),
        "trend": trend,
        "ret_7d": round(_safe_float(ret_7d, 0.0), 1),
        "ret_30d": round(_safe_float(ret_30d, 0.0), 1),
        "buy_threshold": buy_threshold,
        "position": pos_pnl,
    })


@router.get("/api/btc/composite")
async def api_btc_composite():
    try:
        return await asyncio.to_thread(_compute_composite_sync)
    except Exception as e:
        return {"error": "Internal server error"}


@router.get("/api/btc/portfolio")
async def api_btc_portfolio():
    if not supabase:
        return {"error": "DB 미연결", "open_positions": [], "closed_positions": [], "summary": {}}
    try:
        # NOTE: status values in DB are historically inconsistent in case (OPEN/open, CLOSED/closed)
        open_rows = (
            supabase.table("btc_position")
            .select("*")
            .in_("status", ["OPEN", "open"])
            .execute()
            .data
            or []
        )
        closed_rows = (
            supabase.table("btc_position")
            .select("*")
            .in_("status", ["CLOSED", "closed"])
            .order("exit_time", desc=True)
            .limit(50)
            .execute()
            .data
            or []
        )

        cur_price_krw = 0
        try:
            import pyupbit
            cur_price_krw = float(pyupbit.get_current_price("KRW-BTC") or 0)
        except Exception:
            pass
        if not cur_price_krw:
            try:
                import yfinance as _yf
                df = _yf.download("BTC-USD", period="1d", interval="1d", progress=False)
                if not df.empty:
                    fx_rate = _get_fx_rate()  # Use dynamic FX rate instead of hardcoded 1450
                    cur_price_krw = float(df["Close"].iloc[-1]) * fx_rate
            except Exception:
                pass

        open_positions = []
        total_invested_open = 0
        total_eval_open = 0
        for p in open_rows:
            entry_price = float(p.get("entry_price") or 0)
            entry_krw = float(p.get("entry_krw") or 0)
            qty = float(p.get("quantity") or 0)
            eval_krw = cur_price_krw * qty if cur_price_krw and qty else entry_krw
            pnl_krw = eval_krw - entry_krw
            pnl_pct = (pnl_krw / entry_krw * 100) if entry_krw > 0 else 0
            total_invested_open += entry_krw
            total_eval_open += eval_krw
            # Compute default stop_loss and take_profit if not set
            sl = p.get("stop_loss") or (entry_price * 0.97)  # 3% stop loss
            tp = p.get("take_profit") or (entry_price * 1.12)  # 12% take profit

            open_positions.append({
                "id": p.get("id"),
                "entry_price": entry_price,
                "entry_krw": entry_krw,
                "quantity": qty,
                "entry_time": (p.get("entry_time") or "")[:19],
                "current_price_krw": round(cur_price_krw),
                "eval_krw": round(eval_krw),
                "pnl_krw": round(pnl_krw),
                "pnl_pct": round(pnl_pct, 2),
                "strategy": p.get("strategy") or "",
                "stop_loss": round(float(sl), 2) if sl else None,
                "take_profit": round(float(tp), 2) if tp else None,
            })

        closed_positions = []
        total_realized_pnl = 0
        wins = 0
        losses = 0
        for p in closed_rows:
            pnl = float(p.get("pnl") or 0)
            entry_krw = float(p.get("entry_krw") or 0)
            pnl_pct = (pnl / entry_krw * 100) if entry_krw > 0 else 0
            total_realized_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            closed_positions.append({
                "id": p.get("id"),
                "entry_price": float(p.get("entry_price") or 0),
                "exit_price": float(p.get("exit_price") or 0),
                "entry_krw": entry_krw,
                "exit_krw": float(p.get("exit_krw") or 0),
                "quantity": float(p.get("quantity") or 0),
                "pnl": round(pnl),
                "pnl_pct": round(pnl_pct, 2),
                "entry_time": (p.get("entry_time") or "")[:19],
                "exit_time": (p.get("exit_time") or "")[:19],
                "strategy": p.get("strategy") or "",
                "exit_reason": p.get("exit_reason") or "",
            })

        await asyncio.to_thread(_refresh_upbit_cache)
        krw_balance = _upbit_cache.get("krw", 0) or 0
        krw_locked = _upbit_cache.get("krw_locked")
        krw_total = _upbit_cache.get("krw_total")

        # Summary's estimated_asset should reflect *live Upbit balances*.
        # Keep DB-derived total_eval_open/total_invested_open for position analytics.
        upbit_btc_balance = None
        upbit_btc_value_krw = None
        try:
            upbit_key = os.environ.get("UPBIT_ACCESS_KEY", "")
            upbit_secret = os.environ.get("UPBIT_SECRET_KEY", "")
            if upbit_key and upbit_secret and _upbit_cache["ok"]:
                import pyupbit

                upbit = pyupbit.Upbit(upbit_key, upbit_secret)
                upbit_btc_balance = float(await asyncio.to_thread(upbit.get_balance, "BTC") or 0)
                upbit_btc_value_krw = float(upbit_btc_balance) * float(cur_price_krw or 0)
        except Exception as e:
            log.error(f"upbit balance(BTC) fetch failed: {e}")

        unrealized_pnl = total_eval_open - total_invested_open
        total_pnl = total_realized_pnl + unrealized_pnl
        winrate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        summary = {
            "krw_balance": krw_balance,
            "krw_locked": krw_locked,
            "krw_total": krw_total,
            "btc_price_krw": round(cur_price_krw),
            "open_count": len(open_positions),
            "closed_count": len(closed_rows),
            "total_invested": round(total_invested_open),
            "total_eval": round(total_eval_open),
            "unrealized_pnl": round(unrealized_pnl),
            "unrealized_pnl_pct": round((unrealized_pnl / total_invested_open * 100) if total_invested_open > 0 else 0, 2),
            "realized_pnl": round(total_realized_pnl),
            "total_pnl": round(total_pnl),
            "wins": wins,
            "losses": losses,
            "winrate": round(winrate, 1),
            "estimated_asset": round(
                krw_balance + (upbit_btc_value_krw if upbit_btc_value_krw is not None else total_eval_open)
            ),
            "upbit_btc_balance": upbit_btc_balance,
        }

        return {
            "open_positions": open_positions,
            "closed_positions": closed_positions,
            "summary": summary,
        }
    except Exception as e:
        log.error(f"btc portfolio: {e}")
        return {"error": "Internal server error", "open_positions": [], "closed_positions": [], "summary": {}}


@router.get("/api/summary")
async def api_summary():
    result = {}
    try:
        if supabase:
            btc_pos = supabase.table("btc_position").select("entry_price,entry_krw,quantity").eq("status", "OPEN").execute().data or []
            result["btc"] = {"positions": len(btc_pos), "invested_krw": sum(float(p.get("entry_krw", 0)) for p in btc_pos)}

            kr_open = supabase.table("trade_executions").select("trade_id").eq("result", "OPEN").execute().data or []
            result["kr"] = {"positions": len(kr_open)}

            us_open = supabase.table("us_trade_executions").select("symbol,price,quantity").eq("result", "OPEN").execute().data or []
            us_invested = sum(float(p.get("price", 0)) * float(p.get("quantity", 0)) for p in us_open)
            result["us"] = {"positions": len(us_open), "invested_usd": round(us_invested, 2),
                            "symbols": [p["symbol"] for p in us_open]}
    except Exception as e:
        log.error(f"summary 조회 실패: {e}", exc_info=True)
        result["error"] = "Internal server error"
    return result


def _empty_stats():
    return {
        "last_price": 0, "last_signal": "HOLD", "last_time": "", "last_rsi": 50.0, "last_macd": 0.0,
        "total_pnl": 0, "total_pnl_pct": 0, "winrate": 0, "wins": 0, "losses": 0, "total_trades": 0,
        "buys": 0, "sells": 0, "avg_confidence": 0, "today_trades": 0, "today_pnl": 0,
        "position": None, "trend": "SIDEWAYS", "krw_balance": None,
    }


@router.get("/api/btc/filters")
async def api_btc_filters():
    """매매 필터 상태 — 김치프리미엄·펀딩비·일일 횟수·손실 한도"""
    try:
        # 1. 김치 프리미엄
        from btc.btc_trading_agent import get_kimchi_premium
        kimchi = await asyncio.to_thread(get_kimchi_premium)

        # 2. 펀딩비 (rate는 이미 % 단위로 반환됨)
        from common.market_data import get_btc_funding_rate
        fr = await asyncio.to_thread(get_btc_funding_rate)
        funding_rate = round(float(fr.get("rate", 0)), 4)
        funding_signal = fr.get("signal", "NEUTRAL")

        # 3. 오늘 매매 횟수 (btc_position open today)
        today = datetime.now(timezone.utc).date().isoformat()
        today_count = 0
        today_pnl_pct = 0.0
        if supabase:
            pos_res = supabase.table("btc_position").select(
                "entry_krw,pnl,pnl_pct,status,entry_time"
            ).gte("entry_time", today).execute()
            rows = pos_res.data or []
            today_count = len(rows)
            closed_today = [r for r in rows if r.get("status") == "CLOSED"]
            total_inv = sum(float(r.get("entry_krw") or 0) for r in closed_today)
            total_pnl = sum(float(r.get("pnl") or 0) for r in closed_today)
            today_pnl_pct = round(total_pnl / total_inv * 100, 2) if total_inv > 0 else 0.0

        from common.config import BTC_RISK_DEFAULTS
        return {
            "kimchi_premium": round(float(kimchi or 0), 2),
            "kimchi_blocked": float(kimchi or 0) >= 5.0,
            "funding_rate": funding_rate,
            "funding_signal": funding_signal,
            "funding_overheated": funding_signal in ("LONG_CROWDED",),
            "today_trades": today_count,
            "max_trades_per_day": int(BTC_RISK_DEFAULTS.get("max_trades_per_day", 3)),
            "today_pnl_pct": today_pnl_pct,
            "max_daily_loss": round(BTC_RISK_DEFAULTS.get("max_daily_loss", -0.08) * 100, 1),
            "max_drawdown": round(BTC_RISK_DEFAULTS.get("max_drawdown", -0.15) * 100, 1),
        }
    except Exception as e:
        log.error(f"btc_filters: {e}")
        return {
            "kimchi_premium": None, "kimchi_blocked": False,
            "funding_rate": None, "funding_signal": "NEUTRAL", "funding_overheated": False,
            "today_trades": 0, "max_trades_per_day": 3,
            "today_pnl_pct": 0, "max_daily_loss": -8.0, "max_drawdown": -15.0,
        }


@router.get("/api/stats")
async def get_stats():
    if not supabase:
        return _empty_stats()
    try:
        res = supabase.table("btc_trades").select("*").order("timestamp", desc=True).limit(200).execute()
        trades = res.data or []

        buys = [t for t in trades if t.get("action") == "BUY"]
        sells = [t for t in trades if t.get("action") == "SELL"]
        closed = supabase.table("btc_position").select("*").eq("status", "CLOSED").execute().data or []

        wins = len([p for p in closed if (p.get("pnl") or 0) > 0])
        losses_cnt = len([p for p in closed if (p.get("pnl") or 0) < 0])
        total_pnl = sum(float(p.get("pnl") or 0) for p in closed)
        total_krw = sum(float(p.get("entry_krw") or 0) for p in closed)

        today = datetime.now(timezone.utc).date().isoformat()
        today_closed = [p for p in closed if (p.get("exit_time") or "")[:10] == today]
        today_trades = len([t for t in trades if (t.get("timestamp") or "")[:10] == today])
        today_pnl = sum(float(p.get("pnl") or 0) for p in today_closed)

        pos_res = supabase.table("btc_position").select("*").eq("status", "OPEN").order("entry_time", desc=True).limit(1).execute()
        position = pos_res.data[0] if pos_res.data else None

        last = trades[0] if trades else {}

        _refresh_upbit_cache()
        krw_balance = _upbit_cache["krw"]
        krw_locked = _upbit_cache.get("krw_locked")
        krw_total = _upbit_cache.get("krw_total")

        trend = _get_hourly_trend()

        return {
            "last_price": last.get("price", 0),
            "last_signal": last.get("action", "HOLD"),
            "last_time": (last.get("timestamp", "") or "")[:19],
            "last_rsi": float(last.get("rsi") or 50),
            "last_macd": float(last.get("macd") or 0),
            "total_pnl": total_pnl,
            "total_pnl_pct": (total_pnl / total_krw * 100) if total_krw else 0,
            "winrate": (wins / (wins + losses_cnt) * 100) if (wins + losses_cnt) else 0,
            "wins": wins,
            "losses": losses_cnt,
            "total_trades": len(trades),
            "buys": len(buys),
            "sells": len(sells),
            "avg_confidence": sum(float(t.get("confidence") or 0) for t in trades) / len(trades) if trades else 0,
            "today_trades": today_trades,
            "today_pnl": today_pnl,
            "position": position,
            "trend": trend,
            "krw_balance": krw_balance,
            "krw_locked": krw_locked,
            "krw_total": krw_total,
        }
    except Exception as e:
        log.error(f"stats: {e}")
        return {"error": "Internal server error"}


@router.get("/api/trades")
async def get_trades(
    limit: int = Query(default=50, le=500),
    action: str = Query(default=None, pattern="^(BUY|SELL|HOLD)$"),
    hours: int = Query(default=None, ge=1, le=168)  # 1시간~7일
):
    """거래 내역 조회 (필터링 지원)"""
    if not supabase:
        return []
    try:
        query = supabase.table("btc_trades").select("*")

        # 액션 필터링
        if action:
            query = query.eq("action", action)

        # 시간 필터링
        if hours:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            query = query.gte("timestamp", cutoff)

        # 정렬 및 제한
        res = query.order("timestamp", desc=True).limit(limit).execute()
        data = res.data or []

        for t in data:
            if t.get("timestamp"):
                t["timestamp"] = t["timestamp"][:19]
        return data
    except Exception as e:
        log.error(f"trades: {e}")
        return []


@router.get("/api/logs")
async def get_logs():
    try:
        if not BTC_LOG.exists():
            return {"lines": ["로그 파일 없음"]}
        raw = BTC_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
        lines = [
            l for l in raw[-80:]
            if not l.startswith("declare -x ") and "CRON" not in l[:20]
        ]
        return {"lines": lines}
    except Exception as e:
        log.error(f"logs: {e}")
        return {"lines": [f"로그 읽기 실패: {e}"]}


def _fetch_news_sync():
    import xml.etree.ElementTree as ET
    res = requests.get(
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        timeout=5,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    root = ET.fromstring(res.content)
    items = root.findall(".//item")[:8]
    return [
        {
            "title": item.findtext("title", ""),
            "url": item.findtext("link", ""),
            "time": (item.findtext("pubDate", "") or "")[:16],
            "source": "CoinDesk",
        }
        for item in items
    ]


@router.get("/api/news")
async def get_news():
    try:
        if time.time() - _news_cache["ts"] < BTC_NEWS_CACHE_TTL and _news_cache["data"]:
            return _news_cache["data"]
        items = await asyncio.to_thread(_fetch_news_sync)
        _news_cache["data"] = items
        _news_cache["ts"] = time.time()
        return items
    except Exception as e:
        log.error(f"news: {e}")
        return []


def _fetch_candles_sync(interval, count=100):
    import pyupbit
    df = pyupbit.get_ohlcv("KRW-BTC", interval=interval, count=min(count, 500))
    if df is None or df.empty:
        return []
    result = []
    for ts, row in df.iterrows():
        result.append({
            "time": int(ts.timestamp()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        })
    return result


@router.get("/api/candles")
async def get_candles(interval: str = Query("minute5"), count: int = Query(100, ge=1, le=500)):
    try:
        return await asyncio.to_thread(_fetch_candles_sync, interval, count)
    except Exception as e:
        log.error(f"candles: {e}")
        return []


@router.get("/api/system")
async def get_system():
    try:
        cpu = psutil.cpu_percent(interval=0)  # non-blocking; uses delta from last call
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        # tail + grep: 리스트 방식으로 파이프 없이 처리
        try:
            log_lines = BTC_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
            cron_lines = [l for l in log_lines[-200:] if "매매 사이클 시작" in l]
        except Exception:
            cron_lines = []
        last_cron = cron_lines[-1][:50] if cron_lines else "기록 없음"

        _refresh_upbit_cache()
        upbit_ok = _upbit_cache["ok"]

        return {
            "cpu": round(cpu, 1),
            "mem_used": round(mem.used / 1024**3, 1),
            "mem_total": round(mem.total / 1024**3, 1),
            "mem_pct": mem.percent,
            "disk_used": round(disk.used / 1024**3, 1),
            "disk_total": round(disk.total / 1024**3, 1),
            "disk_pct": disk.percent,
            "last_cron": last_cron,
            "upbit_ok": upbit_ok,
        }
    except Exception as e:
        log.error(f"system: {e}")
        return {"error": "Internal server error"}


@router.get("/api/realtime/news")
async def get_realtime_news(
    currencies: str = Query("BTC"),
    limit: int = Query(10, ge=1, le=50),
):
    """Phase 9: normalized news snapshot.

    Output records:
    {headline, source, timestamp, symbols[], sentiment_raw, url, id}
    """
    try:
        from common.data import collect_news_once

        rows = await asyncio.to_thread(
            collect_news_once,
            currencies.upper(),
            int(limit),
        )
        return {"items": rows, "count": len(rows)}
    except Exception as e:
        log.error(f"realtime news: {e}")
        return {"items": [], "count": 0}


@router.get("/api/realtime/orderbook")
async def get_realtime_orderbook(
    market: str = Query("binance"),
    symbol: str = Query("BTCUSDT"),
):
    """Phase 9: orderbook snapshot endpoint.

    market=binance -> symbol like BTCUSDT
    market=upbit   -> symbol like KRW-BTC
    """
    try:
        from common.data import fetch_binance_orderbook, fetch_upbit_orderbook

        mk = market.lower().strip()
        if mk == "upbit":
            snap = await asyncio.to_thread(fetch_upbit_orderbook, symbol.upper())
        else:
            snap = await asyncio.to_thread(fetch_binance_orderbook, symbol.upper())
        return snap
    except Exception as e:
        log.error(f"realtime orderbook: {e}")
        return {
            "symbol": symbol,
            "bids": [],
            "asks": [],
            "spread": 0.0,
            "imbalance": 0.0,
            "source": market,
        }


@router.get("/api/realtime/alt/{symbol}")
async def get_realtime_alt_data(symbol: str):
    """Phase 9: alternative-data snapshot endpoint."""
    try:
        from common.data import get_alternative_data

        return await asyncio.to_thread(get_alternative_data, symbol)
    except Exception as e:
        log.error(f"realtime alt_data: {e}")
        return {
            "symbol": symbol.upper(),
            "search_trend_7d": 0.0,
            "social_mentions_24h": 0,
            "sentiment_score": 0.0,
        }


@router.get("/api/realtime/price/{symbol}")
async def get_realtime_price(symbol: str, market: str = Query("auto")):
    """Phase 9: realtime price snapshot endpoint."""
    try:
        from common.data import get_price_snapshot

        return await asyncio.to_thread(get_price_snapshot, symbol, market)
    except Exception as e:
        log.error(f"realtime price: {e}")
        return {
            "symbol": symbol,
            "price": 0.0,
            "volume": 0.0,
            "source": market,
        }


@router.get("/api/brain")
async def get_brain():
    try:
        summary_dir = BRAIN_PATH / "daily-summary"
        files = sorted(summary_dir.glob("*.md")) if summary_dir.exists() else []
        summary = files[-1].read_text(encoding="utf-8")[:500] if files else "요약 없음"

        todos_path = BRAIN_PATH / "todos.md"
        todos = todos_path.read_text(encoding="utf-8")[:300] if todos_path.exists() else "할일 없음"

        watch_path = BRAIN_PATH / "watchlist.md"
        watchlist = watch_path.read_text(encoding="utf-8")[:300] if watch_path.exists() else "없음"

        mem_dir = MEMORY_PATH
        mem_files = sorted(mem_dir.glob("*.md")) if mem_dir.exists() else []
        memory = mem_files[-1].read_text(encoding="utf-8")[:300] if mem_files else "기억 없음"

        return {
            "summary": summary,
            "todos": todos,
            "watchlist": watchlist,
            "memory": memory,
        }
    except Exception as e:
        log.error(f"brain: {e}")
        return {"error": "Internal server error"}


@router.get("/api/risk-metrics")
async def get_risk_metrics():
    try:
        from common.circuit_breaker import build_portfolio_state_sync
        from quant.cross_market_risk import CrossMarketRisk

        btc_state = await asyncio.to_thread(build_portfolio_state_sync, "btc")
        kr_state = await asyncio.to_thread(build_portfolio_state_sync, "kr")
        us_state = await asyncio.to_thread(build_portfolio_state_sync, "us")
        cross_market = await CrossMarketRisk().check_exposure()
        return {
            "circuit_breaker": {
                "btc": btc_state,
                "kr": kr_state,
                "us": us_state,
            },
            "cross_market": cross_market,
        }
    except Exception as e:
        log.error(f"risk_metrics 조회 실패: {e}", exc_info=True)
        return {
            "circuit_breaker": {},
            "cross_market": {"risk_level": "UNKNOWN", "correlations": {}},
            "error": "Internal server error",
        }


@router.get("/api/btc/decision-log")
async def get_decision_log(limit: int = 20):
    """BTC 매매 판단 로그 (지표 스냅샷)."""
    try:
        rows = (
            supabase.table("btc_trades")
            .select("*")
            .order("timestamp", desc=True)
            .limit(limit)
            .execute()
        )
        normalized = []
        for row in rows.data or []:
            normalized.append(
                {
                    "created_at": row.get("created_at") or row.get("timestamp"),
                    "action": row.get("action"),
                    "confidence": row.get("confidence"),
                    "reason": row.get("reason"),
                    "composite_score": row.get("composite_score"),
                    "fear_greed": row.get("fear_greed"),
                    "rsi": row.get("rsi"),
                }
            )
        return {"decisions": normalized}
    except Exception as e:
        log.error(f"decision-log 조회 실패: {e}")
        from common.api_utils import api_error
        return api_error("판단 로그 조회 실패")
