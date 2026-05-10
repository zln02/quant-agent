#!/usr/bin/env python3
"""
텔레그램 제어 봇.

기존 제어 명령은 유지하고, 자유 텍스트와 요약 명령에 AI 응답을 추가한다.
"""

import asyncio
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from stocks.kiwoom_client import KiwoomClient
except ImportError:
    from kiwoom_client import KiwoomClient
from common.config import API_RETRY_CONFIG, WORKSPACE
from common.env_loader import load_env
from common.logger import get_logger
from common.supabase_client import create_supabase_client_from_env
from common.telegram_ai import ai_respond


def _load_env():
    load_env()


_load_env()

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

log = get_logger("telegram_bot")
supabase = create_supabase_client_from_env()
KST = timezone(timedelta(hours=9))


def send_message(
    text: str,
    chat_id: str | None = None,
    reply_markup: dict | None = None,
    html_mode: bool = True,
):
    if not TG_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN 미설정")
        return
    cid = chat_id or TG_CHAT
    if not cid:
        log.warning("TELEGRAM_CHAT_ID 미설정")
        return
    payload: dict[str, Any] = {"chat_id": cid, "text": text}
    if html_mode:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json=payload, timeout=API_RETRY_CONFIG["telegram_timeout_seconds"])
    except Exception as e:
        log.error(f"Telegram 전송 실패: {type(e).__name__}")


def get_help_text() -> str:
    return (
        "지원 명령:\n"
        "/status - 계좌 및 보유종목 상태\n"
        "/stop - 자동매매 중지 플래그 설정\n"
        "/resume - 자동매매 재개 확인 요청\n"
        "/resume_confirm - 자동매매 중지 플래그 해제\n"
        "/sell_all - 전량 매도 확인\n"
        "/drawdown - 드로우다운 가드 상태\n"
        "/daily_loss - 오늘 시장별 일일 손익\n"
        "/market - 시장 현황 요약\n"
        "/review - 최근 성과 요약\n"
        "/ask <질문> - AI에게 직접 질문\n"
        "/risk - 현재 리스크 상태 요약\n"
        "/help - 도움말\n"
        "\n"
        "자유 텍스트도 바로 질문할 수 있습니다."
    )


def get_status_text() -> str:
    """현재 계좌 상태 + 보유종목 요약을 텍스트로 반환"""
    try:
        client = KiwoomClient()
        summary = client.get_asset_summary()
        s = summary
        lines = []
        lines.append("📊 <b>현재 계좌 상태</b>")
        lines.append(f"환경: {s['environment']}")
        lines.append(f"예수금: {s['deposit']:,}원")
        lines.append(f"추정자산: {s['estimated_asset']:,}원")
        lines.append(
            f"총매입/평가: {s['total_purchase']:,}원 → {s['total_evaluation']:,}원"
        )
        lines.append(
            f"누적 손익: {s['cumulative_pnl']:+,}원 ({s['cumulative_pnl_pct']:+.2f}%)"
        )
        lines.append(f"보유종목 수: {s['holdings_count']}개")
        if s["holdings"]:
            lines.append("")
            lines.append("📌 <b>보유종목</b>")
            for h in s["holdings"][:10]:
                lines.append(
                    f"  {h['name']} ({h['code']}) "
                    f"{h['quantity']}주 / 평단 {h['avg_price']:,}원 / "
                    f"손익 {h['pnl_pct']:+.2f}% ({h['pnl_amount']:+,}원)"
                )
            if s["holdings_count"] > 10:
                lines.append(f"  … 외 {s['holdings_count'] - 10}종목")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 상태 조회 실패: {e}"


def set_stop_flag():
    # stock_trading_agent.py와 동일한 경로 사용 (Path(__file__).parent / 'STOP_TRADING')
    flag = Path(__file__).parent / "STOP_TRADING"
    flag.write_text(datetime.now(timezone.utc).isoformat())


def clear_stop_flag():
    flag = Path(__file__).parent / "STOP_TRADING"
    if flag.exists():
        flag.unlink()


def get_drawdown_guard_text() -> str:
    try:
        path = WORKSPACE / "brain" / "drawdown" / "kr.json"
        if not path.exists():
            return "드로우다운 가드 상태 파일 없음"
        data = json.loads(path.read_text(encoding="utf-8"))
        daily_loss = float(data.get("daily_return", 0.0) or 0.0) * 100
        weekly_loss = float(data.get("weekly_return", 0.0) or 0.0) * 100
        blocked = not bool(data.get("allow_new_buys", True))
        cooldown_until = data.get("cooldown_until")
        remaining = 0
        if cooldown_until:
            try:
                dt = datetime.fromisoformat(str(cooldown_until))
                remaining = max(0, int((dt - datetime.now(KST)).total_seconds() / 60))
            except Exception as e:
                log.error(f"drawdown cooldown 파싱 실패: {e}", exc_info=True)
        return (
            "📊 드로우다운 가드 상태\n"
            f"- 일간 손실: {daily_loss:.1f}% (한도: 2.0%)\n"
            f"- 주간 손실: {weekly_loss:.1f}%\n"
            f"- 매수 차단: {'🚫 YES' if blocked else '✅ NO'}\n"
            f"- 쿨다운: {remaining}분 남음"
        )
    except Exception as e:
        log.error(f"drawdown 상태 조회 실패: {e}", exc_info=True)
        return "드로우다운 가드 상태 조회 실패"


def get_daily_loss_text() -> str:
    if not supabase:
        return "오늘 거래 없음"
    try:
        today = datetime.now(KST).date()
        today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=KST)
        rows = (
            supabase.table("trade_executions")
            .select("market,price,entry_price,quantity,created_at,trade_type,result")
            .gte("created_at", today_start.isoformat())
            .execute()
            .data
            or []
        )
        if not rows:
            return "오늘 거래 없음"

        markets = {
            "BTC": {"pnl": 0.0, "count": 0},
            "KR": {"pnl": 0.0, "count": 0},
            "US": {"pnl": 0.0, "count": 0},
        }
        for row in rows:
            market = str(row.get("market") or "KR").upper()
            if market not in markets:
                continue
            price = float(row.get("price") or 0.0)
            entry_price = float(row.get("entry_price") or price)
            quantity = float(row.get("quantity") or 0.0)
            markets[market]["pnl"] += (price - entry_price) * quantity
            markets[market]["count"] += 1

        total_krw = markets["BTC"]["pnl"] + markets["KR"]["pnl"]
        return (
            "📈 오늘 일일 손익 (KST 기준)\n"
            f"- BTC: {markets['BTC']['pnl']:+,.0f}원 ({markets['BTC']['count']}건)\n"
            f"- KR: {markets['KR']['pnl']:+,.0f}원 ({markets['KR']['count']}건)\n"
            f"- US: ${markets['US']['pnl']:+,.2f} ({markets['US']['count']}건)\n"
            f"- 합계: {total_krw:+,.0f}원"
        )
    except Exception as e:
        log.error(f"daily_loss 조회 실패: {e}", exc_info=True)
        return "오늘 손익 조회 실패"


def get_open_positions() -> list:
    if not supabase:
        return []
    try:
        return (
            supabase.table("trade_executions")
            .select("*")
            .eq("result", "OPEN")
            .execute()
            .data
            or []
        )
    except Exception as e:
        log.error(f"get_open_positions 실패: {e}")
        return []


def group_by_code(positions: list) -> dict:
    by_code: dict[str, list] = defaultdict(list)
    for p in positions:
        code = p.get("stock_code")
        if code:
            by_code[code].append(p)
    return by_code


def build_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "⏹ 자동매매 중지(/stop)", "callback_data": "stop"},
                {"text": "💥 전량 매도(/sell_all)", "callback_data": "sell_all"},
            ],
            [
                {"text": "📊 상태 확인(/status)", "callback_data": "status"},
                {"text": "🧠 AI 질문(/ask)", "callback_data": "help"},
            ],
        ]
    }


def handle_sell_all(chat_id: str):
    positions = get_open_positions()
    if not positions:
        send_message("보유종목이 없습니다.", chat_id, reply_markup=build_keyboard())
        return

    by_code = group_by_code(positions)
    summary_lines = []
    for code, trades in by_code.items():
        qty = sum(int(t.get("quantity", 0)) for t in trades)
        name = trades[0].get("stock_name", code)
        summary_lines.append(f"  {name}: {qty}주")

    msg = (
        "⚠️ <b>전량 매도 확인</b>\n\n"
        "아래 종목을 시장가로 전량 매도합니다:\n"
        + "\n".join(summary_lines)
        + "\n\n정말 실행할까요?"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔴 전량 매도 실행", "callback_data": "CONFIRM_SELL_ALL"},
                {"text": "취소", "callback_data": "CANCEL_SELL_ALL"},
            ]
        ]
    }
    send_message(msg, chat_id, reply_markup=keyboard)


def handle_sell_all_confirm(chat_id: str):
    if not supabase:
        send_message("Supabase 설정이 없습니다. 전량 매도를 실행할 수 없습니다.", chat_id)
        return

    kiwoom = KiwoomClient()
    positions = get_open_positions()
    if not positions:
        send_message("보유종목이 없습니다.", chat_id, reply_markup=build_keyboard())
        return

    results: list[str] = []
    by_code = group_by_code(positions)

    for code, trades in by_code.items():
        qty = sum(int(t.get("quantity", 0)) for t in trades)
        if qty <= 0:
            continue
        name = trades[0].get("stock_name", code)
        try:
            result = kiwoom.place_order(code, "sell", qty, 0)
            if result.get("success"):
                for t in trades:
                    tid = t.get("trade_id")
                    if tid is None:
                        continue
                    try:
                        supabase.table("trade_executions").update(
                            {"result": "CLOSED_MANUAL"}
                        ).eq("trade_id", tid).execute()
                    except Exception as e:
                        results.append(f"❌ {name} DB 업데이트 실패: {e}")
                results.append(f"✅ {name} {qty}주 매도 완료")
            else:
                results.append(f"❌ {name} 매도 실패: {result.get('message', '?')}")
        except Exception as e:
            results.append(f"❌ {name} 매도 오류: {e}")

    if not results:
        results.append("실행 결과가 없습니다.")

    msg = "📊 <b>전량 매도 결과</b>\n\n" + "\n".join(results)
    send_message(msg, chat_id, reply_markup=build_keyboard())


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        log.error(f"JSON 로드 실패({path}): {e}")
    return {}


def get_btc_state() -> dict:
    return _load_json(WORKSPACE / "brain" / "market" / "last_btc_state.json")


def get_recent_trades(limit: int = 5) -> list[dict]:
    if not supabase:
        return []
    try:
        rows = (
            supabase.table("trade_executions")
            .select("created_at, stock_code, stock_name, trade_type, price, quantity, pnl_pct")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return rows.data or []
    except Exception as e:
        log.error(f"get_recent_trades 실패: {e}")
        return []


def format_trades(rows: list[dict]) -> str:
    if not rows:
        return "기록 없음"
    lines = []
    for row in rows[:5]:
        # trade_executions 스키마 기준: trade_type (BUY/SELL).
        action = str(row.get("trade_type") or "HOLD").upper()
        lines.append(
            f"- {row.get('stock_name') or row.get('stock_code') or '-'} "
            f"{action} {int(row.get('quantity') or 0)}주 @ {row.get('price') or 0}"
        )
    return "\n".join(lines)


def get_risk_summary_text() -> str:
    try:
        from common.circuit_breaker import build_portfolio_state_sync
    except Exception as e:
        return f"리스크 모듈 로드 실패: {e}"

    try:
        btc = build_portfolio_state_sync("btc")
        kr = build_portfolio_state_sync("kr")
        us = build_portfolio_state_sync("us")
        cross_market = {"risk_level": "UNKNOWN", "correlations": {}}
        try:
            from quant.cross_market_risk import CrossMarketRisk
            cross_market = asyncio.run(CrossMarketRisk().check_exposure())
        except Exception as e:
            log.error(f"cross_market_risk 실패: {e}")
        return (
            "🛡 현재 리스크 상태\n"
            f"- BTC DD: {float(btc.get('current_drawdown', 0)):.2%}\n"
            f"- KR DD: {float(kr.get('current_drawdown', 0)):.2%}\n"
            f"- US DD: {float(us.get('current_drawdown', 0)):.2%}\n"
            f"- 크로스마켓: {cross_market.get('risk_level', 'UNKNOWN')}"
        )
    except Exception as e:
        return f"리스크 요약 조회 실패: {e}"


def build_system_context() -> str:
    btc = get_btc_state()
    status_text = get_status_text().replace("<b>", "").replace("</b>", "")
    recent_trades = get_recent_trades(5)
    return (
        "[실시간 시스템 컨텍스트]\n"
        f"BTC 가격: {btc.get('price') or btc.get('btc_price') or 'N/A'}\n"
        f"Composite: {btc.get('composite', 'N/A')}\n"
        f"RSI: {btc.get('rsi', 'N/A')} / F&G: {btc.get('fg', 'N/A')} / Regime: {btc.get('trend', 'N/A')}\n\n"
        "[계좌 상태]\n"
        f"{status_text}\n\n"
        "[최근 거래]\n"
        f"{format_trades(recent_trades)}"
    )


def handle_ai_chat(text: str, chat_id: str) -> str:
    try:
        reply = asyncio.run(
            ai_respond(
                user_id=chat_id,
                message=text,
                market_context=build_system_context(),
            )
        )
        if reply.startswith("⚠️ AI 응답을 사용할 수 없습니다"):
            return f"{reply}\n\n{get_help_text()}"
        return reply
    except Exception as e:
        log.error(f"AI 응답 처리 실패: {e}", exc_info=True)
        return f"⚠️ AI 응답을 사용할 수 없습니다.\n\n{get_help_text()}"


def handle_command(cmd: str, chat_id: str):
    cmd = cmd.strip()
    lower = cmd.lower()

    if lower.startswith("/status"):
        text = get_status_text()
        send_message(text, chat_id, reply_markup=build_keyboard())
    elif lower.startswith("/stop"):
        set_stop_flag()
        send_message(
            "⏹ 자동매매 중지 플래그를 설정했습니다.\n"
            "※ 에이전트가 이 플래그를 보고 거래 사이클을 스킵하도록 연동 예정입니다.",
            chat_id,
            reply_markup=build_keyboard(),
        )
    elif lower == "/resume" or lower == "/start":
        send_message(
            "⚠️ 매매를 재개합니다. /resume_confirm 으로 확인해주세요",
            chat_id,
            reply_markup=build_keyboard(),
        )
    elif lower.startswith("/resume_confirm"):
        try:
            clear_stop_flag()
            send_message("✅ 매매 재개됨", chat_id, reply_markup=build_keyboard())
        except Exception as e:
            log.error(f"/resume_confirm 실패: {e}", exc_info=True)
            send_message("매매 재개 처리 실패", chat_id, reply_markup=build_keyboard())
    elif lower.startswith("/sell_all"):
        handle_sell_all(chat_id)
    elif lower.startswith("/confirm_sell_all"):
        handle_sell_all_confirm(chat_id)
    elif lower.startswith("/cancel_sell_all"):
        send_message("전량 매도가 취소되었습니다.", chat_id, reply_markup=build_keyboard())
    elif lower.startswith("/risk"):
        send_message(get_risk_summary_text(), chat_id, reply_markup=build_keyboard(), html_mode=False)
    elif lower.startswith("/drawdown"):
        try:
            send_message(get_drawdown_guard_text(), chat_id, reply_markup=build_keyboard(), html_mode=False)
        except Exception as e:
            log.error(f"/drawdown 실패: {e}", exc_info=True)
            send_message("드로우다운 가드 상태 조회 실패", chat_id, reply_markup=build_keyboard(), html_mode=False)
    elif lower.startswith("/daily_loss"):
        try:
            send_message(get_daily_loss_text(), chat_id, reply_markup=build_keyboard(), html_mode=False)
        except Exception as e:
            log.error(f"/daily_loss 실패: {e}", exc_info=True)
            send_message("오늘 손익 조회 실패", chat_id, reply_markup=build_keyboard(), html_mode=False)
    elif lower.startswith("/ask "):
        question = cmd[5:].strip()
        if not question:
            send_message("사용법: /ask 질문 내용", chat_id, reply_markup=build_keyboard(), html_mode=False)
            return
        send_message("🤔 분석 중...", chat_id, reply_markup=build_keyboard(), html_mode=False)
        from agents.gateway_agent import handle_query

        send_message(handle_query(question), chat_id, reply_markup=build_keyboard(), html_mode=False)
    elif lower.startswith("/ask\n") or lower == "/ask":
        question = cmd[4:].strip()
        if not question:
            send_message("사용법: /ask 질문 내용", chat_id, reply_markup=build_keyboard(), html_mode=False)
            return
        send_message("🤔 분석 중...", chat_id, reply_markup=build_keyboard(), html_mode=False)
        from agents.gateway_agent import handle_query

        send_message(handle_query(question), chat_id, reply_markup=build_keyboard(), html_mode=False)
    elif lower == "/market":
        from agents.gateway_agent import handle_query

        send_message(handle_query("시장 현황"), chat_id, reply_markup=build_keyboard(), html_mode=False)
    elif lower == "/review":
        from agents.gateway_agent import handle_query

        send_message(handle_query("성과 요약"), chat_id, reply_markup=build_keyboard(), html_mode=False)
    elif lower.startswith("/help"):
        send_message(get_help_text(), chat_id, reply_markup=build_keyboard(), html_mode=False)
    elif cmd.startswith("/"):
        send_message(get_help_text(), chat_id, reply_markup=build_keyboard(), html_mode=False)
    else:
        send_message(handle_ai_chat(cmd, chat_id), chat_id, reply_markup=build_keyboard(), html_mode=False)


def _is_authorized(chat_id: str) -> bool:
    if not TG_CHAT:
        log.warning(f"[보안] TELEGRAM_CHAT_ID 미설정 — 발신자({chat_id}) 차단")
        return False
    return str(chat_id) == str(TG_CHAT)


def poll_updates():
    if not TG_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN 미설정. 종료.")
        return
    if not TG_CHAT:
        log.warning("TELEGRAM_CHAT_ID 미설정. 보안상 봇을 시작하지 않습니다.")
        return

    last_update_id = None
    log.info("텔레그램 봇 폴링 시작...")
    while True:
        try:
            params = {"timeout": 30}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1
            try:
                url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
                resp = requests.get(url, params=params, timeout=35)
            except Exception as e:
                log.error(f"Telegram 업데이트 조회 실패: {type(e).__name__}")
                time.sleep(5)
                continue
            data = resp.json()
            for upd in data.get("result", []):
                last_update_id = upd["update_id"]
                msg = upd.get("message") or upd.get("edited_message")
                cb = upd.get("callback_query")

                if cb:
                    cid = str(cb["message"]["chat"]["id"])
                    if not _is_authorized(cid):
                        continue
                    data_cmd = cb.get("data") or ""
                    handle_command(f"/{data_cmd}", cid)
                    continue

                if not msg:
                    continue
                cid = str(msg["chat"]["id"])
                if not _is_authorized(cid):
                    continue
                text = msg.get("text") or ""
                if not text:
                    continue
                handle_command(text, cid)
        except Exception as e:
            log.error(f"poll_updates 오류: {e}")
            time.sleep(5)


if __name__ == "__main__":
    poll_updates()
