"""Prometheus metrics for OpenClaw trading system.

Exposes trading-specific counters and gauges via prometheus_client.
All updates are fire-and-forget; import errors are silently handled
so agents work fine without prometheus_client installed.
"""
from __future__ import annotations

try:
    from prometheus_client import Counter, Gauge, Histogram, Info

    TRADE_COUNT = Counter(
        "openclaw_trade_total",
        "Total trades executed",
        ["market", "side"],
    )
    PNL_TOTAL = Gauge(
        "openclaw_pnl_total",
        "Cumulative realized PnL",
        ["market"],
    )
    POSITION_VALUE = Gauge(
        "openclaw_position_value",
        "Current open position value",
        ["market"],
    )
    SIGNAL_SCORE = Gauge(
        "openclaw_signal_score",
        "Latest composite signal score",
        ["market", "signal"],
    )
    API_LATENCY = Histogram(
        "openclaw_api_latency_seconds",
        "API endpoint latency",
        ["endpoint"],
        buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
    )
    AGENT_CYCLE = Counter(
        "openclaw_agent_cycle_total",
        "Agent trading cycle count",
        ["market", "result"],
    )
    SUPABASE_QUERY = Counter(
        "openclaw_supabase_query_total",
        "Supabase query count",
        ["operation", "status"],
    )
    AI_DECISION_TOTAL = Counter(
        "openclaw_ai_decision_total",
        "AI 결정 카운트 (LLM 호출 기반)",
        ["market", "action"],
    )
    RULE_DECISION_TOTAL = Counter(
        "openclaw_rule_decision_total",
        "RULE 결정 카운트 (algorithmic/heuristic)",
        ["market", "action"],
    )

    _ENABLED = True

except ImportError:
    _ENABLED = False
    AI_DECISION_TOTAL = None  # type: ignore[assignment]
    RULE_DECISION_TOTAL = None  # type: ignore[assignment]


def record_trade(market: str, side: str) -> None:
    """거래 실행 카운터 증가."""
    if _ENABLED:
        TRADE_COUNT.labels(market=market, side=side).inc()


def set_pnl(market: str, value: float) -> None:
    """시장별 누적 PnL 게이지 설정."""
    if _ENABLED:
        PNL_TOTAL.labels(market=market).set(value)


def set_position_value(market: str, value: float) -> None:
    """시장별 포지션 가치 게이지 설정."""
    if _ENABLED:
        POSITION_VALUE.labels(market=market).set(value)


def set_signal_score(market: str, signal: str, value: float) -> None:
    """신호 점수 게이지 설정."""
    if _ENABLED:
        SIGNAL_SCORE.labels(market=market, signal=signal).set(value)


def observe_api_latency(endpoint: str, seconds: float) -> None:
    """API 레이턴시 히스토그램 기록."""
    if _ENABLED:
        API_LATENCY.labels(endpoint=endpoint).observe(seconds)


def record_agent_cycle(market: str, result: str) -> None:
    """에이전트 사이클 카운터 증가."""
    if _ENABLED:
        AGENT_CYCLE.labels(market=market, result=result).inc()


def record_supabase_query(operation: str, status: str = "ok") -> None:
    """Supabase 쿼리 카운터 증가."""
    if _ENABLED:
        SUPABASE_QUERY.labels(operation=operation, status=status).inc()


def record_decision_source(market: str, source: str | None, action: str) -> None:
    """매매 결정 시 AI/RULE 분기 Counter 증가 (PR #29).

    매매 사이클 hot path 에서 호출. source 는 signal_source 또는 decision_source.
    분류: AI/LLM/ML/COMPOSITE/ML_MULTI_HORIZON → AI counter, RULE_*/manual/None → RULE counter.
    """
    if not _ENABLED:
        return
    try:
        s = (source or "RULE").upper()
        is_rule = s.startswith("RULE") or s in ("MANUAL", "")
        if is_rule:
            RULE_DECISION_TOTAL.labels(market=market, action=action).inc()
        else:
            AI_DECISION_TOTAL.labels(market=market, action=action).inc()
    except Exception:
        pass
