"""Daily Reviewer — 장 마감 직후 24h 트레이딩 데이터를 Haiku 4.5로 분석. PR #28.

PR #12 silence_monitor (무엇이 안 일어났는지) 보완 — 매매 결정의 품질·정합·위험을 매일 자가 보고.
Phase 1 (데이터 축적) → Phase 2 (자가 진단) 진입.

실행: docker exec workspace-{market}-agent-1 python /app/scripts/daily_reviewer.py {KR|US|BTC}
cron: KR 17:00 / US 07:00 / BTC 09:00 KST (호스트 등록, PR 외부)
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from common.env_loader import load_env
from common.llm_client import call_haiku
from common.logger import get_logger
from common.supabase_client import get_supabase
from common.telegram import Priority, send_telegram

load_env()
log = get_logger("daily_reviewer")

_WORKSPACE = Path(__file__).resolve().parents[1]
_RISK_SNAPSHOT = _WORKSPACE / "brain" / "risk" / "latest_snapshot.json"
_KR_DRIFT = _WORKSPACE / "brain" / "ml" / "drift_report.json"
_US_DRIFT = _WORKSPACE / "brain" / "ml" / "us" / "drift_report.json"
_EQUITY_DIR = _WORKSPACE / "brain" / "equity"

_MODEL = "claude-haiku-4-5-20251001"
_COMPRESS_THRESHOLD = 20  # 20건 초과 시 summary 압축
_MAX_TOKENS = 1500


def _kst_now_str() -> str:
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST")


def _read_json(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("json read 실패 %s: %s", path, exc)
    return None


def _equity_stale_hours(market: str) -> Optional[float]:
    path = _EQUITY_DIR / f"{market.lower()}.jsonl"
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 3600


def _compress_btc_trades(rows: list[dict]) -> dict:
    """BTC 24h 사이클 > 20건 시 summary 압축."""
    if not rows:
        return {"total_cycles": 0}
    actions = Counter(r.get("action", "") for r in rows if r.get("action"))
    reasons = Counter(r.get("reason", "")[:80] for r in rows if r.get("reason"))
    composites = [r.get("composite_score") for r in rows if r.get("composite_score") is not None]
    return {
        "total_cycles": len(rows),
        "action_dist": dict(actions),
        "avg_composite": round(sum(composites) / len(composites), 1) if composites else None,
        "top3_reasons": [r for r, _ in reasons.most_common(3)],
        "first_ts": rows[0].get("timestamp"),
        "last_ts": rows[-1].get("timestamp"),
    }


class DailyReviewer:
    """24h 윈도우 트레이딩 데이터를 Haiku로 분석해 4섹션 리뷰 생성."""

    def __init__(
        self,
        market: str,
        supabase=None,
        llm_fn: Optional[Callable] = None,
        sender: Optional[Callable] = None,
    ):
        self.market = market.upper()
        if self.market not in ("KR", "US", "BTC"):
            raise ValueError(f"unknown market: {market}")
        self.sb = supabase if supabase is not None else get_supabase()
        self.llm = llm_fn or call_haiku
        self.send = sender or send_telegram

    # ─────────────────────────── context 수집 ───────────────────────────

    def _since_iso(self, hours: int = 24) -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    def _kr_section(self) -> dict:
        since = self._since_iso(24)
        rows: list[dict] = []
        if self.sb is not None:
            try:
                r = self.sb.table("trade_executions").select(
                    "trade_type,strategy,signal_source,stock_code,entry_price,price,quantity,pnl_pct,drift_status,created_at"
                ).gte("created_at", since).execute()
                rows = r.data or []
            except Exception as exc:
                log.warning("KR trade_executions SELECT 실패: %s", exc)
        types = Counter(r.get("trade_type", "") for r in rows)
        strategies = Counter(r.get("strategy", "") for r in rows)
        pnl_pcts = [r.get("pnl_pct") for r in rows if r.get("pnl_pct") is not None]
        return {
            "trade_count": len(rows),
            "type_dist": dict(types),
            "strategy_dist": dict(strategies),
            "avg_pnl_pct": round(sum(pnl_pcts) / len(pnl_pcts), 3) if pnl_pcts else None,
            "samples": rows[:5] if len(rows) <= _COMPRESS_THRESHOLD else None,
        }

    def _btc_section(self) -> dict:
        since = self._since_iso(24)
        trades: list[dict] = []
        positions: list[dict] = []
        if self.sb is not None:
            try:
                r = self.sb.table("btc_trades").select(
                    "action,reason,composite_score,signal_source,timestamp"
                ).gte("timestamp", since).order("timestamp").execute()
                trades = r.data or []
            except Exception as exc:
                log.warning("btc_trades SELECT 실패: %s", exc)
            try:
                r = self.sb.table("btc_position").select(
                    "status,pnl,pnl_pct,entry_time,composite_score"
                ).gte("entry_time", since).execute()
                positions = r.data or []
            except Exception as exc:
                log.warning("btc_position SELECT 실패: %s", exc)
        closed = [p for p in positions if p.get("status") == "CLOSED"]
        cycles_data = (
            _compress_btc_trades(trades)
            if len(trades) > _COMPRESS_THRESHOLD
            else {"total_cycles": len(trades), "samples": trades[:10]}
        )
        return {
            "cycles": cycles_data,
            "closed_positions": len(closed),
            "realized_pnl_pct_avg": (
                round(sum(p.get("pnl_pct") or 0 for p in closed) / len(closed), 3)
                if closed else None
            ),
        }

    def _us_section(self) -> dict:
        since = self._since_iso(24)
        rows: list[dict] = []
        if self.sb is not None:
            try:
                r = self.sb.table("us_trade_executions").select(
                    "trade_type,source,signal_source,symbol,price,quantity,result,created_at"
                ).gte("created_at", since).execute()
                rows = r.data or []
            except Exception as exc:
                log.warning("us_trade_executions SELECT 실패: %s", exc)
        types = Counter(r.get("trade_type", "") for r in rows)
        sources = Counter(r.get("source", "") for r in rows)
        return {
            "trade_count": len(rows),
            "type_dist": dict(types),
            "source_dist": dict(sources),
            "samples": rows[:5] if len(rows) <= _COMPRESS_THRESHOLD else None,
        }

    def _risk_section(self) -> dict:
        snap = _read_json(_RISK_SNAPSHOT) or {}
        if self.market == "KR":
            drift = _read_json(_KR_DRIFT) or {}
        elif self.market == "US":
            drift = _read_json(_US_DRIFT) or {}
        else:
            drift = {"status": "측정 불가", "max_psi": None, "_note": "BTC drift_report 미존재"}
        stale = _equity_stale_hours(self.market)
        return {
            "drawdown": snap.get("drawdown"),
            "var_95": snap.get("var_95"),
            "drift_status": drift.get("status"),
            "drift_max_psi": drift.get("max_psi"),
            "equity_stale_hours": round(stale, 1) if stale is not None else None,
            "equity_stale_flag": stale is not None and stale > 24,
        }

    def collect_context(self) -> dict:
        section1 = (
            self._kr_section() if self.market == "KR"
            else self._btc_section() if self.market == "BTC"
            else self._us_section()
        )
        return {
            "market": self.market,
            "window_hours": 24,
            "section1_trading": section1,
            "section3_risk": self._risk_section(),
            "meta": {
                "kst": _kst_now_str(),
                "model": _MODEL,
            },
        }

    # ─────────────────────────── LLM 호출 ───────────────────────────

    _SYSTEM_PROMPT = (
        "당신은 OpenClaw 트레이딩 시스템의 데일리 리뷰어다. "
        "주어진 24h 데이터를 한국어 평문 4섹션으로 분석한다: "
        "(1) 매매 요약 (BUY/SELL/HOLD 비율, 실현 손익) "
        "(2) 알고리즘 정합성 (룰 vs AI 결정 비율, 신호 다양성, fallback 비율) "
        "(3) 위험 신호 (drawdown, ML drift PSI, equity 적재 정합) "
        "(4) 다음 24h 주의 사항. "
        "각 섹션 3줄 이내. 데이터가 없거나 측정 불가하면 '측정 불가' 명시. "
        "마크다운 X, 평문. 섹션 헤더는 '1. 매매 요약', '2. 알고리즘 정합성' 형식."
    )

    def call_review(self, ctx: dict) -> str:
        prompt = (
            f"마켓: {self.market}\n"
            f"윈도우: 최근 24h\n"
            f"시각: {ctx['meta']['kst']}\n\n"
            f"데이터:\n{json.dumps(ctx, ensure_ascii=False, indent=2)}"
        )
        result = self.llm(
            prompt,
            system=self._SYSTEM_PROMPT,
            max_tokens=_MAX_TOKENS,
            temperature=0.2,
        )
        return (result or "").strip()

    # ─────────────────────────── DB persist + Telegram ───────────────────────────

    def persist(self, ctx: dict, review: str) -> bool:
        if self.sb is None:
            log.warning("Supabase 클라이언트 없음 — review_logs INSERT 스킵")
            return False
        try:
            self.sb.table("review_logs").insert({
                "market": self.market.lower(),
                "window_end": datetime.now(timezone.utc).isoformat(),
                "raw_data": ctx,
                "review_text": review,
                "model": _MODEL,
            }).execute()
            return True
        except Exception as exc:
            log.warning("review_logs INSERT 실패: %s", exc)
            return False

    def deliver(self, review: str) -> bool:
        msg = f"📊 Daily Review [{self.market}] {_kst_now_str()}\n\n{review}"
        try:
            return bool(self.send(msg, priority=Priority.IMPORTANT))
        except Exception as exc:
            log.warning("telegram 발송 실패: %s", exc)
            return False

    # ─────────────────────────── 메인 ───────────────────────────

    def run(self) -> dict:
        ctx = self.collect_context()
        review = self.call_review(ctx)
        if not review:
            log.warning("LLM 응답 비어있음 — 발송/저장 스킵")
            return {"status": "empty_review", "ctx": ctx}
        persisted = self.persist(ctx, review)
        sent = self.deliver(review)
        return {
            "status": "ok",
            "review_chars": len(review),
            "persisted": persisted,
            "sent": sent,
        }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: daily_reviewer.py {KR|US|BTC}")
        return 2
    market = sys.argv[1]
    try:
        result = DailyReviewer(market).run()
    except Exception as exc:
        log.error("daily_reviewer 실행 실패: %s", exc, exc_info=True)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
