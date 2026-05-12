#!/usr/bin/env python3
"""BTC 대체 데이터 수집: 김치프리미엄 + Fear&Greed Index."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.env_loader import load_env

load_env()

from common.logger import get_logger
from common.supabase_client import get_supabase

log = get_logger("collect_btc_alt_data")


def collect_kimchi_premium() -> dict:
    """현재 김치프리미엄 계산."""
    try:
        import pyupbit
        import requests

        krw_price = pyupbit.get_current_price("KRW-BTC")
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=10,
        )
        usdt_price = float(resp.json()["price"])
        fx_resp = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD",
            timeout=10,
        )
        usd_krw = fx_resp.json()["rates"]["KRW"]
        global_krw = usdt_price * usd_krw
        premium = ((krw_price - global_krw) / global_krw) * 100
        return {
            "kimchi_premium": round(premium, 3),
            "krw_price": krw_price,
            "usd_krw": round(usd_krw, 2),
        }
    except Exception as e:
        log.warning(f"김프 계산 실패: {e}")
        return {}


def collect_fear_greed() -> dict:
    """Alternative.me Fear & Greed Index."""
    try:
        import requests

        resp = requests.get(
            "https://api.alternative.me/fng/?limit=1&format=json",
            timeout=10,
        )
        data = resp.json()["data"][0]
        return {
            "fear_greed": int(data["value"]),
            "fg_label": data["value_classification"],
        }
    except Exception as e:
        log.warning(f"Fear&Greed 조회 실패: {e}")
        return {}


def save_to_supabase(data: dict) -> bool:
    sb = get_supabase()
    if not sb or not data:
        return False
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    try:
        sb.table("btc_alt_data").upsert(row).execute()
        log.info(f"대체 데이터 저장: {data}")
        return True
    except Exception as e:
        log.warning(f"저장 실패: {e}")
        return False


if __name__ == "__main__":
    kimchi = collect_kimchi_premium()
    fg = collect_fear_greed()
    combined = {**kimchi, **fg}
    if combined:
        save_to_supabase(combined)
    else:
        log.warning("수집 데이터 없음")
