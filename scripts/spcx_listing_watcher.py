#!/usr/bin/env python3
"""SPCX (SpaceX IPO) 상장 일정 추적 — PR #29.

매일 1회 실행. SPCX ticker 가 yfinance에서 가격 데이터를 반환하기 시작하면
상장 확정으로 판단하고 1회성 알림 발송 (자비스 우선 + 텔레그램 fallback).

판단 기준:
- yfinance Ticker("SPCX").history(period="5d") 에 close 가격 1개 이상
- once-fired sentinel 파일로 중복 알림 방지

cron:
    # 매일 23:00 KST (미장 개장 90분 전)
    0 23 * * * /home/wlsdud5035/quant-agent/scripts/run_spcx_watcher.sh

CLI:
    python -m scripts.spcx_listing_watcher          # 체크 + 필요 시 알림
    python -m scripts.spcx_listing_watcher --force  # sentinel 무시 강제 알림
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.env_loader import load_env  # noqa: E402
from common.telegram import Priority, send_telegram  # noqa: E402

load_env()

SPCX_TICKER = "SPCX"
# brain mount 안에 두면 docker/host 양쪽에서 보임
_SENTINEL_DIR = Path("/tmp/openclaw_spcx")
_SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
_SENTINEL_FILE = _SENTINEL_DIR / "spcx_listed.flag"


def _has_listed_data() -> tuple[bool, dict]:
    """yfinance 로 SPCX 가격 데이터 존재 여부 확인.

    Returns: (listed, meta) — meta 에 last_close / source.
    """
    try:
        import yfinance as yf
    except ImportError:
        return False, {"source": "yfinance_missing", "error": True}

    try:
        ticker = yf.Ticker(SPCX_TICKER)
        hist = ticker.history(period="5d", auto_adjust=True)
        if hist is None or len(hist) == 0:
            return False, {"source": "no_data", "bars": 0}
        # 가격 데이터 존재 → 상장 확정
        last_close = float(hist["Close"].iloc[-1])
        last_date = str(hist.index[-1].date())
        return True, {
            "source": "yfinance",
            "bars": int(len(hist)),
            "last_close": round(last_close, 2),
            "last_date": last_date,
        }
    except Exception as exc:
        return False, {"source": "yfinance_error", "error": str(exc)[:200]}


def _fire_listed_alert(meta: dict) -> None:
    kst = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    msg = (
        f"🚀 <b>SPCX 상장 감지!</b>\n"
        f"SpaceX IPO 상장 확정 — yfinance 가격 데이터 발견\n"
        f"마지막 종가: ${meta.get('last_close', '?')} ({meta.get('last_date', '?')})\n"
        f"검출 시점: {kst} KST\n\n"
        f"⚠️ 액션:\n"
        f"1. US_UNIVERSE 에 SPCX 추가 PR 작성\n"
        f"2. Alpaca paper/live 에서 즉시 매수 가능\n"
        f"3. 초기 1주 변동성 큼 — 룰만 적용 (ML 미학습)"
    )
    send_telegram(msg, priority=Priority.URGENT)


def check_and_notify(force: bool = False) -> dict:
    """일일 체크 + 1회성 알림.

    Returns: 결과 dict (테스트/CLI 용).
    """
    already_fired = _SENTINEL_FILE.exists() and not force
    listed, meta = _has_listed_data()

    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "listed": listed,
        "already_fired": already_fired,
        "meta": meta,
        "alert_sent": False,
    }

    if not listed:
        return result

    if already_fired:
        result["reason"] = "sentinel exists — 이미 알림 발송됨"
        return result

    _fire_listed_alert(meta)
    _SENTINEL_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    result["alert_sent"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="SPCX 상장 감지")
    parser.add_argument("--force", action="store_true",
                        help="sentinel 무시 강제 재알림")
    parser.add_argument("--json", action="store_true", help="JSON 출력")
    args = parser.parse_args()

    result = check_and_notify(force=args.force)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        listed = result["listed"]
        if listed and result["alert_sent"]:
            print(f"🚀 SPCX 상장 감지 + 알림 발송 (close=${result['meta'].get('last_close')})")
        elif listed:
            print(f"SPCX 상장 — 알림 이미 발송됨 (sentinel: {_SENTINEL_FILE})")
        else:
            print(f"SPCX 미상장 ({result['meta'].get('source', '?')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
