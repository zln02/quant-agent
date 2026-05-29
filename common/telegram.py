"""Telegram message sender with priority-based routing.

Priority.URGENT    🔴  즉시 발송: 손절 체결·에이전트 다운·API 에러
Priority.IMPORTANT 🟡  즉시 발송: 매수/매도 체결·스코어 급변
Priority.INFO      🟢  버퍼 저장: 일일 리포트에만 포함 (개별 발송 안 함)
"""
import json
import os
import time
from enum import Enum
from pathlib import Path
from typing import Optional

import requests

# v6.2 B6: 텔레그램 실패 로깅
from common.logger import get_logger

log = get_logger(__name__)

_last_send_ts = 0.0
_MIN_INTERVAL = 1.0  # rate-limit: 1 msg/sec

# INFO 등급 버퍼 — 프로세스 간 공유를 위해 파일 기반
_INFO_BUFFER_FILE = Path(__file__).resolve().parents[1] / ".telegram_info_buffer.json"


class Priority(str, Enum):
    """메시지 전송 우선순위."""
    URGENT    = "urgent"     # 🔴 즉시 발송
    IMPORTANT = "important"  # 🟡 즉시 발송
    INFO      = "info"       # 🟢 일일 리포트 버퍼에만 저장


def append_info_buffer(msg: str) -> None:
    """INFO 등급 메시지를 버퍼에 추가 (즉시 발송하지 않음).

    구조 (역호환 유지):
        {
          "date": "YYYY-MM-DD",
          "msgs": [...],              # 일일 리포트용 전체 메시지
          "hours": { "HH": [...] }    # 시각별 버킷 (매시 브리핑용)
        }
    """
    try:
        today = time.strftime("%Y-%m-%d")
        hour = time.strftime("%H")
        data: dict = {}
        if _INFO_BUFFER_FILE.exists():
            try:
                data = json.loads(_INFO_BUFFER_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        if data.get("date") != today:
            # 날짜가 바뀌면 새로 시작 (이전 데이터는 덮어씀)
            data = {"date": today, "msgs": [], "hours": {}}

        # 일일 리포트용 전체 목록
        msgs = data.setdefault("msgs", [])
        msgs.append(msg)

        # 시각별 버킷 (매시 정각 요약용)
        hours = data.setdefault("hours", {})
        bucket = hours.setdefault(hour, [])
        bucket.append(msg)

        _INFO_BUFFER_FILE.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        # 버퍼 기록 실패는 트레이딩 로직에 영향을 주지 않도록 조용히 무시
        pass


def flush_info_buffer() -> list:
    """버퍼에 쌓인 INFO 메시지를 반환하고 초기화. 일일 리포트 발송 시 호출."""
    try:
        if not _INFO_BUFFER_FILE.exists():
            return []
        data = json.loads(_INFO_BUFFER_FILE.read_text(encoding="utf-8"))
        msgs = data.get("msgs", [])
        _INFO_BUFFER_FILE.write_text(
            json.dumps({"date": time.strftime("%Y-%m-%d"), "msgs": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        return msgs
    except Exception:
        return []


def send_telegram(
    msg: str,
    parse_mode: str = "HTML",
    retries: int = 2,
    priority: Priority = Priority.URGENT,
) -> bool:
    """메시지 전송 — 자비스(jay-agent) 우선, 텔레그램 fallback.

    priority=INFO     → 버퍼에 저장, 즉시 발송 안 함
    priority=IMPORTANT/URGENT → 자비스 우선 시도. 자비스 발송 성공 시 텔레그램 skip.
                              자비스 미설정/다운 시에만 텔레그램 fallback.

    Env: JAY_INTERNAL_TOKEN 설정 → 자비스 활성. 미설정 → 텔레그램 단독.
    """
    if priority == Priority.INFO:
        append_info_buffer(msg)
        return True

    # 자비스(jay-agent) 우선 시도 — 사용자 체감 알림 채널
    try:
        from common import jay_bridge as _jb
        if _jb.post_event(msg, priority=priority.name, source="quant"):
            return True
    except Exception as exc:
        log.debug("jay_bridge primary 송신 실패, 텔레그램 fallback: %s", exc)

    # 텔레그램 fallback — 자비스 다운/미설정 시
    global _last_send_ts
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    elapsed = time.time() - _last_send_ts
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": parse_mode},
                timeout=10,
            )
            _last_send_ts = time.time()
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2))
                time.sleep(retry_after)
                continue
            if resp.ok:
                # 자비스(jay-agent) bridge — fire-and-forget, 텔레그램 흐름 무영향
                try:
                    from common import jay_bridge as _jb
                    _jb.post_event(msg, priority=priority.name, source="telegram")
                except Exception:
                    pass
            return resp.ok
        except Exception as e:
            log.error(f"텔레그램 발송 실패: {e}")
            if attempt < retries:
                time.sleep(1 * (attempt + 1))
    return False


def send_trade_alert(
    market: str,
    action: str,
    symbol: str,
    price: float,
    quantity: float,
    entry_reason: str,
    stop_loss: float,
    take_profit: float,
    portfolio_weight: float,
    pnl_pct: Optional[float] = None,
    symbol_name: str = "",
) -> bool:
    """매수/매도 체결 알림 — 진입근거·손절가·목표가·비중 포함.

    Args:
        market: "btc" | "kr" | "us"
        action: "매수" | "매도" | "손절" | "익절"
        stop_loss: 손절 기준가 (절대 가격)
        take_profit: 목표가 (절대 가격)
        portfolio_weight: 포트폴리오 내 비중 (0~100 %)
        pnl_pct: 수익률 — 매도/손절/익절 시에만 전달
    """
    icon = {"매수": "🟢", "매도": "🔴", "손절": "🛑", "익절": "✅"}.get(action, "📌")
    mkt = market.upper()

    if mkt == "US":
        price_str = f"${price:,.2f}"
        sl_str    = f"${stop_loss:,.2f}"
        tp_str    = f"${take_profit:,.2f}"
        qty_str   = f"{quantity:.2f} shares"
    elif mkt == "BTC":
        price_str = f"{price:,.0f}원"
        sl_str    = f"{stop_loss:,.0f}원"
        tp_str    = f"{take_profit:,.0f}원"
        qty_str   = f"{quantity:.6f} BTC"
    else:  # KR
        price_str = f"{price:,.0f}원"
        sl_str    = f"{stop_loss:,.0f}원"
        tp_str    = f"{take_profit:,.0f}원"
        qty_str   = f"{quantity:.0f}주"

    pnl_line  = f"\n📈 <b>수익률:</b> {pnl_pct:+.2f}%" if pnl_pct is not None else ""
    name_part = f" ({symbol_name})" if symbol_name else ""

    msg = (
        f"{icon} <b>[{mkt}] {action} 체결</b> — {symbol}{name_part}\n"
        f"💰 <b>체결가:</b> {price_str}  |  {qty_str}\n"
        f"📝 <b>진입근거:</b> {entry_reason}\n"
        f"🛑 <b>손절가:</b> {sl_str}\n"
        f"🎯 <b>목표가:</b> {tp_str}\n"
        f"⚖️ <b>포트폴리오 비중:</b> {portfolio_weight:.1f}%"
        f"{pnl_line}"
    )
    return send_telegram(msg, priority=Priority.IMPORTANT)


def send_daily_report(
    date_str: str,
    win_rate: float,
    daily_pnl: float,
    cumulative_pnl: float,
    total_trades: int,
    regime: str = "N/A",
    market_breakdown: Optional[dict] = None,
) -> bool:
    """일일 리포트 — 승률·당일 PnL·누적 PnL 포함."""
    daily_sign = "+" if daily_pnl >= 0 else ""
    cum_sign   = "+" if cumulative_pnl >= 0 else ""

    breakdown_lines = ""
    if market_breakdown:
        for mkt, info in market_breakdown.items():
            pnl    = info.get("pnl", 0)
            trades = info.get("trades", 0)
            sign   = "+" if pnl >= 0 else ""
            breakdown_lines += f"\n  • {mkt.upper()}: {sign}{pnl:,.0f}원  ({trades}건)"

    msg = (
        f"📊 <b>일일 리포트 — {date_str}</b>\n"
        f"─────────────────────\n"
        f"🏆 <b>승률:</b> {win_rate:.1f}%  ({total_trades}건 거래)\n"
        f"💵 <b>당일 PnL:</b> {daily_sign}{daily_pnl:,.0f}원\n"
        f"📈 <b>누적 PnL:</b> {cum_sign}{cumulative_pnl:,.0f}원\n"
        f"🌐 <b>시장 레짐:</b> {regime}"
        f"{breakdown_lines}"
    )
    return send_telegram(msg)


def send_emergency_alert(
    alert_type: str,
    message: str,
    detail: str = "",
) -> bool:
    """이상 상황 긴급 알림 — 연속 손절·API 에러·낙폭 경보 구분."""
    icons = {
        "consecutive_loss": "🚨",
        "api_error":        "⛔",
        "drawdown":         "📉",
    }
    labels = {
        "consecutive_loss": "연속 손절 경보",
        "api_error":        "API 오류 긴급 알림",
        "drawdown":         "낙폭 경보",
    }
    icon  = icons.get(alert_type, "🔴")
    label = labels.get(alert_type, "긴급 알림")
    detail_line = f"\n🔍 <b>상세:</b> {detail}" if detail else ""

    import datetime as _dt
    msg = (
        f"{icon} <b>[긴급] {label}</b>\n"
        f"─────────────────────\n"
        f"{message}"
        f"{detail_line}\n"
        f"⏰ {_dt.datetime.now().strftime('%H:%M:%S')}"
    )
    return send_telegram(msg, priority=Priority.URGENT)
