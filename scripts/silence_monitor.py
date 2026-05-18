"""24h/4h 매매 침묵 시 Telegram CRITICAL 알람. W0-6 PR #24.

매매 발화원이 죽거나 (cron disable, AI 401, ML drift block 등) 시스템이
조용히 침묵하는 경우 자가 보고. alert_manager.py cooldown 패턴 차용.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import holidays

from common.supabase_client import get_supabase
from common.telegram import Priority, send_telegram

_COOLDOWN_DIR = Path("/tmp/openclaw_alert_cooldown")
_COOLDOWN_DIR.mkdir(parents=True, exist_ok=True)

_US_LOG = Path("/app/logs/us_trading.log")
_KR_HOLIDAYS = holidays.SouthKorea()

THRESHOLDS = {
    "KR": {
        "window_h": 24,
        "min_trades": 1,
        "cooldown_s": 86400,
        "table": "trade_executions",
        "ts_col": "created_at",
        "ticker_col": "stock_code",
        "ticker_re": r"^\d{6}$",
    },
    "BTC": {
        "window_h": 4,
        "min_trades": 1,
        "cooldown_s": 14400,
        "table": "btc_trades",
        "ts_col": "timestamp",
        "ticker_col": None,
        "ticker_re": None,
    },
}


def _cooldown_file(market: str) -> Path:
    return _COOLDOWN_DIR / f"silence_{market.lower()}.ts"


def _in_cooldown(market: str, cooldown_s: int) -> bool:
    f = _cooldown_file(market)
    return f.exists() and (time.time() - f.stat().st_mtime) < cooldown_s


def _mark_alert_sent(market: str) -> None:
    _cooldown_file(market).touch()


def _is_market_open_today(market: str) -> bool:
    if market == "BTC":
        return True
    now = datetime.now(timezone.utc)
    if market == "KR":
        kst = now.astimezone(timezone(timedelta(hours=9)))
        if kst.weekday() >= 5 or kst.date() in _KR_HOLIDAYS:
            return False
        return kst.hour >= 16
    return False


def _count_recent_trades(cfg: dict) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=cfg["window_h"])).isoformat()
    sb = get_supabase()
    ts_col = cfg["ts_col"]
    if cfg["ticker_re"]:
        pat = re.compile(cfg["ticker_re"])
        ticker_col = cfg["ticker_col"]
        rows = sb.table(cfg["table"]).select(f"{ticker_col},{ts_col}").gte(ts_col, since).execute()
        return sum(1 for r in (rows.data or []) if pat.match(str(r.get(ticker_col, ""))))
    res = sb.table(cfg["table"]).select("id", count="exact").gte(ts_col, since).execute()
    return res.count or 0


def _check_us_silence() -> bool:
    if not _US_LOG.exists():
        return False
    return (time.time() - _US_LOG.stat().st_mtime) > 86400


def _fire(market: str, count: int, window_h: int, min_trades: int) -> None:
    kst = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST")
    msg = (
        f"🚨 SILENCE ALERT [{market}]\n"
        f"최근 {window_h}h 매매 {count}건 (임계 {min_trades})\n"
        f"시점: {kst}\n"
        f"확인: docker logs workspace-{market.lower()}-agent-1 --since {window_h}h"
    )
    send_telegram(msg, priority=Priority.URGENT)
    _mark_alert_sent(market)


def check_all() -> list[str]:
    fired: list[str] = []
    for market, cfg in THRESHOLDS.items():
        if not _is_market_open_today(market):
            continue
        if _in_cooldown(market, cfg["cooldown_s"]):
            continue
        count = _count_recent_trades(cfg)
        if count < cfg["min_trades"]:
            _fire(market, count, cfg["window_h"], cfg["min_trades"])
            fired.append(market)
    if _check_us_silence() and not _in_cooldown("US", 86400):
        _fire("US", 0, 24, 1)
        fired.append("US")
    return fired


if __name__ == "__main__":
    fired = check_all()
    print(f"[silence_monitor] fired={fired}" if fired else "[silence_monitor] healthy/cooldown")
