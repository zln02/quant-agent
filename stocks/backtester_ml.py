#!/usr/bin/env python3
"""
ML + 리스크 전체 파이프라인 백테스터 v1.0

대상:
- Supabase의 daily_ohlcv (일봉 1년치)
- stocks/ml_model.py 에서 학습된 XGBoost 모델

전략 요약:
- 매수:
  - ML 매수확률 >= 78% (XGBoost)
  - 하루 신규 매수 최대 2건 (전 종목 합산)
- 매도:
  - 손절: -2% (수수료/세금 포함 실수익률 기준)
  - 트레일링 스탑: 수익 1% 이상 구간에서 고점 대비 -1.5% 하락
  - 고정 익절: +10% (수수료/세금 포함 실수익률 기준)

주의:
- 실제 trading_agent의 AI/GPT, 코스피/주봉/수급 필터까지는 포함하지 않고
  "ML 필터 + 손절/트레일링/익절 + 하루 2건 제한" 조합만 시뮬레이션합니다.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from common.env_loader import load_env

load_env()

# ml_model은 같은 디렉토리(stocks/)에 있으므로 sys.path에 추가
_STOCKS_DIR = str(Path(__file__).parent)
if _STOCKS_DIR not in sys.path:
    sys.path.insert(0, _STOCKS_DIR)

from ml_model import _load_model, extract_features  # noqa: E402

from supabase import create_client  # noqa: E402

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_SECRET_KEY', '')
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


# 전략 파라미터 — stock_trading_agent.py와 동일 값 사용
ML_THRESHOLD = 0.78
MAX_TRADES_PER_DAY = 2
STOP_LOSS = -0.02
TAKE_PROFIT = 0.10
TRAILING_STOP = 0.015
TRAILING_ACTIVATE = 0.01
FEE_BUY = 0.00015
FEE_SELL = 0.00015
TAX_SELL = 0.0018
FEE_TOTAL = FEE_BUY + FEE_SELL + TAX_SELL


def run_ml_backtest(days: int = 250) -> dict:
    if not supabase:
        print('Supabase 미연결')
        return {}

    model = _load_model()
    if model is None:
        print('ML 모델(xgb_model.pkl) 없음. 먼저 ml_model.py train 실행 필요')
        return {}

    # 종목 목록
    stocks = (
        supabase.table('top50_stocks')
        .select('stock_code,stock_name')
        .execute()
        .data
        or []
    )
    if not stocks:
        print('top50_stocks 테이블 비어 있음')
        return {}

    # 종목별 시계열 로드
    series = {}
    all_dates = set()

    for s in stocks:
        code = s['stock_code']
        name = s.get('stock_name', code)
        rows = (
            supabase.table('daily_ohlcv')
            .select('date,open_price,high_price,low_price,close_price,volume')
            .eq('stock_code', code)
            .order('date', desc=False)
            .execute()
            .data
            or []
        )
        if len(rows) < 80:
            continue

        dates = [r['date'] for r in rows]
        closes = np.array([float(r['close_price']) for r in rows], dtype=float)
        highs = np.array(
            [float(r.get('high_price', r['close_price'])) for r in rows], dtype=float
        )
        lows = np.array(
            [float(r.get('low_price', r['close_price'])) for r in rows], dtype=float
        )
        vols = np.array([float(r.get('volume', 0)) for r in rows], dtype=float)
        idx_by_date = {d: i for i, d in enumerate(dates)}

        series[code] = {
            'name': name,
            'dates': dates,
            'closes': closes,
            'highs': highs,
            'lows': lows,
            'vols': vols,
            'idx_by_date': idx_by_date,
        }
        all_dates.update(dates)

    if not series:
        print('시계열 데이터 없음 (daily_ohlcv 부족)')
        return {}

    # 공통 날짜 캘린더 (마지막 days일만 사용)
    sorted_dates = sorted(all_dates)
    if len(sorted_dates) > days:
        period_dates = sorted_dates[-days:]
    else:
        period_dates = sorted_dates

    print(f'백테스트 기간: {period_dates[0]} ~ {period_dates[-1]} ({len(period_dates)}일)')
    print(f'대상 종목: {len(series)}개\n')

    # 포지션/트레이드 상태
    positions = {}  # code -> {entry_idx, entry_price, highest, open}
    trades = []  # 완료된 트레이드 목록
    daily_new_trades = {d: 0 for d in period_dates}

    # 날짜 루프
    for d in period_dates:
        # 1) 기존 포지션에 대해 매도 조건 체크
        for code, info in series.items():
            if code not in positions or not positions[code]['open']:
                continue
            idx = info['idx_by_date'].get(d)
            if idx is None:
                continue

            price = info['closes'][idx]
            if price <= 0:
                continue

            pos = positions[code]
            entry_idx = pos['entry_idx']
            entry_price = pos['entry_price']
            highest = pos['highest']

            # 고점 갱신
            if price > highest:
                highest = price
                pos['highest'] = highest

            raw_pnl = (price - entry_price) / entry_price
            net_pnl = raw_pnl - FEE_TOTAL

            sell_reason = None

            # 손절
            if net_pnl <= STOP_LOSS:
                sell_reason = '손절'
            # 트레일링 스탑
            elif net_pnl > TRAILING_ACTIVATE and highest > 0:
                drop = (highest - price) / highest
                if drop >= TRAILING_STOP:
                    sell_reason = f'트레일링(고점 대비 -{drop*100:.1f}%)'
            # 고정 익절
            elif net_pnl >= TAKE_PROFIT:
                sell_reason = '익절'

            if sell_reason:
                trades.append(
                    {
                        'stock_code': code,
                        'stock_name': info['name'],
                        'entry_date': info['dates'][entry_idx],
                        'exit_date': d,
                        'entry_price': entry_price,
                        'exit_price': price,
                        'pnl_pct': round(net_pnl * 100, 2),
                        'reason': sell_reason,
                    }
                )
                pos['open'] = False

        # 2) 신규 매수 한도 체크
        if daily_new_trades[d] >= MAX_TRADES_PER_DAY:
            continue

        # 3) 신규 매수 시도 (종목 순회)
        for code, info in series.items():
            if daily_new_trades[d] >= MAX_TRADES_PER_DAY:
                break
            # 이미 포지션 보유 중이면 스킵
            if code in positions and positions[code]['open']:
                continue

            idx = info['idx_by_date'].get(d)
            if idx is None or idx < 60:
                continue

            price = info['closes'][idx]
            if price <= 0:
                continue

            features = extract_features(
                info['closes'], info['vols'], info['highs'], info['lows'], idx
            )
            if features is None:
                continue

            X = np.array([features], dtype=float)
            prob = float(model.predict_proba(X)[0][1])

            if prob >= ML_THRESHOLD:
                # 매수 체결
                positions[code] = {
                    'entry_idx': idx,
                    'entry_price': price,
                    'highest': price,
                    'open': True,
                }
                daily_new_trades[d] += 1

    # 남은 미청산 포지션은 마지막 날 가격으로 정산 (참고용)
    last_date = period_dates[-1]
    for code, pos in positions.items():
        if not pos['open']:
            continue
        info = series[code]
        idx = info['idx_by_date'].get(last_date)
        if idx is None:
            continue
        price = info['closes'][idx]
        if price <= 0:
            continue
        entry_price = pos['entry_price']
        raw_pnl = (price - entry_price) / entry_price
        net_pnl = raw_pnl - FEE_TOTAL
        trades.append(
            {
                'stock_code': code,
                'stock_name': info['name'],
                'entry_date': info['dates'][pos['entry_idx']],
                'exit_date': last_date,
                'entry_price': entry_price,
                'exit_price': price,
                'pnl_pct': round(net_pnl * 100, 2),
                'reason': '미청산-마감정산',
            }
        )

    if not trades:
        print('완료된 트레이드 없음')
        return {}

    pnls = [t['pnl_pct'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_trades = len(trades)
    win_count = len(wins)
    win_rate = win_count / total_trades * 100 if total_trades > 0 else 0
    avg_pnl = sum(pnls) / total_trades

    print(f'\n{"="*50}')
    print(f'총 트레이드: {total_trades}건')
    print(f'승률: {win_rate:.1f}% (승 {win_count} / 패 {total_trades - win_count})')
    print(f'평균 수익률: {avg_pnl:+.2f}%')
    print(f'누적 수익률 합: {sum(pnls):+.2f}%')
    print(f'최고/최저: {max(pnls):+.2f}% / {min(pnls):.2f}%')

    return {
        'total_trades': total_trades,
        'win_rate': win_rate,
        'avg_pnl': avg_pnl,
        'total_pnl_sum': sum(pnls),
        'wins': win_count,
        'losses': total_trades - win_count,
    }


if __name__ == '__main__':
    days = 250
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        days = int(sys.argv[1])
    result = run_ml_backtest(days)
    if result:
        print('\n요약:', json.dumps(result, indent=2, ensure_ascii=False))
