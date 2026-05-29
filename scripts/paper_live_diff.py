#!/usr/bin/env python3
"""US paper/live 실거래 결과 분석 (PR #25).

us_trade_executions 의 CLOSED 거래를 모드별 (sim/paper/live)로 분석:
- PnL 분포 (mean, median, std, sharpe)
- 승률 / 평균 손익비
- 슬리피지 추정 (예측 score vs 실제 PnL 상관)
- 모드 구분: equity.jsonl 의 metadata.source / mode 정보 사용

CLI:
    .venv/bin/python scripts/paper_live_diff.py              # 30일
    .venv/bin/python scripts/paper_live_diff.py --days 7
    .venv/bin/python scripts/paper_live_diff.py --mode paper # 모드 필터

PR #24에서 equity.jsonl 에 metadata.mode 가 들어가기 시작했으므로,
created_at >= equity.jsonl 의 paper 시작 시각으로 모드를 식별한다.
sim/paper 시작 시각을 못 찾으면 전체를 한 그룹으로 본다.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.env_loader import load_env  # noqa: E402

load_env()


def _equity_file() -> Path:
    base = Path(os.environ.get("OPENCLAW_BRAIN", "")) if os.environ.get("OPENCLAW_BRAIN") else (
        Path(__file__).resolve().parents[1] / "brain"
    )
    return base / "equity" / "us.jsonl"


def _detect_mode_transitions() -> dict:
    """equity.jsonl 에서 mode 전환 시각 추출. {mode: first_seen_iso}."""
    f = _equity_file()
    if not f.exists():
        return {}
    seen: dict[str, str] = {}
    with f.open("r", encoding="utf-8") as fp:
        for line in fp:
            try:
                row = json.loads(line)
            except Exception:
                continue
            md = row.get("metadata") or {}
            mode = md.get("mode")
            ts = row.get("timestamp")
            if mode and ts and mode not in seen:
                seen[mode] = ts
    return seen


def _trade_mode(created_at: str, transitions: dict) -> str:
    """trade 시각이 paper/live transition 이후면 해당 모드, 아니면 sim."""
    if not transitions:
        return "unknown"
    ordered = sorted(transitions.items(), key=lambda kv: kv[1])
    current = "sim"
    for mode, start in ordered:
        if created_at >= start:
            current = mode
    return current


def _fetch_closed_trades(days: int) -> list[dict]:
    from common.supabase_client import get_supabase
    sb = get_supabase()
    if sb is None:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = (
        sb.table("us_trade_executions")
        .select("symbol,trade_type,quantity,price,exit_price,pnl_pct,exit_reason,"
                "score,ml_confidence,composite_score,signal_source,strategy,created_at,result")
        .gte("created_at", since)
        .in_("result", ["CLOSED", "CLOSED_MANUAL", "CLOSED_SYNC"])
        .order("created_at", desc=False)
        .execute()
    )
    return list(res.data or [])


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    mean = statistics.mean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    sharpe = (mean / std) if std > 0 else 0.0
    return {
        "n": len(values),
        "mean": round(mean, 3),
        "median": round(statistics.median(values), 3),
        "std": round(std, 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "sharpe": round(sharpe, 3),
    }


def analyze(trades: list[dict], transitions: dict) -> dict:
    """모드별 PnL 통계 + 시그널 소스별 분포 + 슬리피지(예측-실제) 상관."""
    by_mode: dict[str, list[float]] = {}
    by_source: dict[str, list[float]] = {}
    score_pnl_pairs: list[tuple[float, float]] = []

    for t in trades:
        pnl = t.get("pnl_pct")
        if pnl is None:
            continue
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            continue
        created = str(t.get("created_at", ""))
        mode = _trade_mode(created, transitions)
        by_mode.setdefault(mode, []).append(pnl)
        src = str(t.get("signal_source") or "UNKNOWN").upper()
        by_source.setdefault(src, []).append(pnl)
        # 슬리피지 추정: composite_score 예측이 실제 pnl과 얼마나 상관
        try:
            cs = float(t.get("composite_score") or t.get("score") or 0)
            if cs > 0:
                score_pnl_pairs.append((cs, pnl))
        except (TypeError, ValueError):
            pass

    # 모드별 승률
    mode_summary = {}
    for mode, vals in by_mode.items():
        wins = sum(1 for v in vals if v > 0)
        wr = round(wins / len(vals) * 100, 1) if vals else 0.0
        mode_summary[mode] = {**_stats(vals), "win_rate": wr}

    # 시그널 소스별
    source_summary = {src: _stats(vals) for src, vals in by_source.items()}

    # composite_score vs pnl 단순 상관
    corr = 0.0
    if len(score_pnl_pairs) >= 5:
        xs = [p[0] for p in score_pnl_pairs]
        ys = [p[1] for p in score_pnl_pairs]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        denom_x = sum((x - mx) ** 2 for x in xs) ** 0.5
        denom_y = sum((y - my) ** 2 for y in ys) ** 0.5
        if denom_x > 0 and denom_y > 0:
            corr = round(num / (denom_x * denom_y), 4)

    return {
        "total_closed": len(trades),
        "mode_transitions": transitions,
        "by_mode": mode_summary,
        "by_signal_source": source_summary,
        "score_vs_pnl_correlation": corr,
        "score_pairs_n": len(score_pnl_pairs),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="US paper/live PnL 분석")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--mode", type=str, default=None,
                        help="필터: sim / paper / live")
    parser.add_argument("--json", action="store_true",
                        help="JSON 출력 (기본은 표 출력)")
    args = parser.parse_args()

    trades = _fetch_closed_trades(args.days)
    transitions = _detect_mode_transitions()
    if args.mode:
        trades = [t for t in trades
                  if _trade_mode(str(t.get("created_at", "")), transitions) == args.mode]
    report = analyze(trades, transitions)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"\n=== US 실거래 분석 (최근 {args.days}일) ===")
    print(f"CLOSED 거래: {report['total_closed']}건")
    print(f"모드 전환: {transitions or '없음 (equity.jsonl 미발견 또는 sim 고정)'}")
    print(f"\n[모드별 PnL]")
    for mode, s in report["by_mode"].items():
        if s.get("n", 0) == 0:
            continue
        print(f"  {mode}: n={s['n']} mean={s['mean']:+.2f}% sharpe={s['sharpe']} "
              f"win_rate={s.get('win_rate')}%")
    print(f"\n[시그널 소스별]")
    for src, s in report["by_signal_source"].items():
        if s.get("n", 0) == 0:
            continue
        print(f"  {src}: n={s['n']} mean={s['mean']:+.2f}% sharpe={s['sharpe']}")
    print(f"\n[score↔PnL 상관]: {report['score_vs_pnl_correlation']} "
          f"(n={report['score_pairs_n']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
