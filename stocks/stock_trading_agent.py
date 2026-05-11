#!/usr/bin/env python3
"""
주식 자동매매 에이전트 v3.0 (Top-tier Quant)

v3 변경사항:
- [NEW] DART 재무 스코어를 매매 판단에 반영 (ROE/영업이익률/부채/성장률)
- [NEW] 동적 유니버스: TOP50 + DART 퀄리티 필터
- [NEW] ATR 기반 변동성 포지션 사이징
- [NEW] 섹터 분산 강제 (max_sector_positions)
- [IMPROVE] 복합 스코어에 재무 품질 15점 추가
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TypedDict

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config import (BRAIN_PATH, ML_BLEND_CONFIG, STOCK_TRADING_LOG,
                           WORKSPACE_DIR)
from common.env_loader import load_env
from common.equity_loader import (append_equity_snapshot,
                                  get_effective_market_weight,
                                  load_drawdown_state, load_equity_curve,
                                  load_recent_trades, save_drawdown_state)
from common.logger import get_logger
from common.retry import retry, retry_call
from common.supabase_client import _reset_client, get_supabase
from common.telegram import send_telegram as _tg_send
from execution.smart_router import SmartRouter
from quant.risk.drawdown_guard import DrawdownGuard, DrawdownGuardState
from quant.risk.position_sizer import KellyPositionSizer

try:
    from common.sheets_logger import append_trade as _sheets_append
except ImportError:
    _sheets_append = None

load_env()
_log = get_logger("stock_agent", STOCK_TRADING_LOG)
KST = timezone(timedelta(hours=9))

sys.path.insert(0, str(Path(__file__).parent))
from kiwoom_client import KiwoomClient

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
OPENAI_KEY = os.environ.get('OPENAI_API_KEY', '')

supabase = get_supabase()
kiwoom = KiwoomClient()
_kr_drift_cache: dict = {}
_last_known_state: dict = {
    "open_positions": [],
    "positions_by_code": {},
}


class KospiSentiment(TypedDict):
    rsi: float
    msg: str


class WeeklyTrend(TypedDict, total=False):
    trend: str
    ema5: float
    ema10: float

RISK = {
    "invest_ratio": 0.25,
    "stop_loss": -0.025,
    "take_profit": 0.08,
    "partial_tp_pct": 0.05,              # 5% 부분 익절 진입
    "partial_tp_ratio": 0.50,            # 50% 수량 매도
    "trailing_stop": 0.015,
    "trailing_activate": 0.015,
    "trailing_adaptive": True,           # 수익구간별 트레일링 조절
    "min_confidence": 65,
    "max_positions": 5,
    "max_daily_loss": -0.08,
    "max_drawdown": -0.12,               # 포트폴리오 최대 낙폭 제한
    "max_trades_per_day": 3,
    "split_ratios": [0.50, 0.30, 0.20],
    "split_rsi_thresholds": [50, 42, 35],
    "min_order_krw": 30000,
    "cooldown_minutes": 10,
    "min_hours_between_splits": 3,
    "max_sector_positions": 2,           # 동일 섹터 최대 2종목 (count)
    "max_sector_weight": 0.30,           # 동일 섹터 최대 비중 30% (weight)
    "fee_buy": 0.00015,
    "fee_sell": 0.00015,
    "tax_sell": 0.0018,
    "round_trip_cost": 0.0021,
    "volatility_sizing": True,           # ATR 기반 포지션 사이징
}

# --- Level 5: 자동 파라미터 반영 ---
try:
    from quant.param_optimizer import load_best_params as _load_opt_params
    _opt_params = _load_opt_params()
    if _opt_params:
        _risk_overrideable = {"stop_loss", "invest_ratio", "max_positions", "cooldown_minutes"}
        _applied = {}
        for _k, _v in _opt_params.items():
            if _k in _risk_overrideable and _v is not None:
                RISK[_k] = _v
                _applied[_k] = _v
        if _applied:
            _log.info(f"[Level5] agent_params 적용: {_applied}")
except Exception as _e:
    _log.debug(f"Level5 agent_params 로드 스킵: {_e}")

RULES = {
    "buy_rsi_max": 45,
    "buy_bb_max": 50,
    "buy_vol_min": 0.7,
    "buy_momentum_min": 50,
    "sell_rsi_min": 70,
    "sell_bb_min": 80,
    "block_vol_below": 0.3,
    "block_bb_above": 90,
    "block_kospi_above": 80,
    "trend_confirmation": True,          # KOSPI + 종목 추세 동시 확인
}

# ─────────────────────────────────────────────
# 유틸리티
# ─────────────────────────────────────────────
def log(msg: str, level: str = "INFO") -> None:
    """Backward-compat wrapper routing to structured logger."""
    _dispatch = {
        "INFO": _log.info, "WARN": _log.warning, "WARNING": _log.warning,
        "ERROR": _log.error, "TRADE": _log.trade,
    }
    _dispatch.get(level, _log.info)(msg)


def send_telegram(msg: str):
    _tg_send(msg)


def _load_kr_ml_drift_report(force: bool = False) -> dict:
    global _kr_drift_cache
    if _kr_drift_cache and not force:
        return _kr_drift_cache
    path = BRAIN_PATH / 'ml' / 'drift_report.json'
    if not path.exists():
        _kr_drift_cache = {}
        return _kr_drift_cache
    try:
        _kr_drift_cache = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        _log.debug(f'KR ML drift 리포트 로드 실패: {e}')
        _kr_drift_cache = {}
    return _kr_drift_cache


def _apply_kr_drift_gate(signal: dict) -> dict:
    report = _load_kr_ml_drift_report()
    if not report:
        return signal

    status = str(report.get('status', 'UNKNOWN')).upper()
    max_psi = float(report.get('max_psi', 0.0) or 0.0)
    high_psi_count = int(report.get('high_psi_count', 0) or 0)
    adjusted = dict(signal)
    base_conf = float(adjusted.get('confidence', 0.0) or 0.0)
    reason = str(adjusted.get('reason', ''))

    if status == 'WARNING':
        adjusted['confidence'] = max(0.0, round(base_conf - 8.0, 1))
        adjusted['reason'] = (reason + f' [KR_ML_DRIFT:WARNING psi={max_psi:.2f}]').strip()
        adjusted['drift_status'] = status
        adjusted['drift_penalty'] = 8.0
        return adjusted

    if status == 'DANGER':
        if max_psi >= 1.0 or high_psi_count >= 12:
            adjusted['action'] = 'HOLD'
            adjusted['confidence'] = 0.0
            adjusted['reason'] = (reason + f' [KR_ML_DRIFT_BLOCK psi={max_psi:.2f}]').strip()
            adjusted['drift_status'] = status
            adjusted['drift_penalty'] = 100.0
            return adjusted
        adjusted['confidence'] = max(0.0, round(base_conf - 15.0, 1))
        adjusted['reason'] = (reason + f' [KR_ML_DRIFT:DANGER psi={max_psi:.2f}]').strip()
        adjusted['drift_status'] = status
        adjusted['drift_penalty'] = 15.0
        return adjusted

    adjusted['drift_status'] = status
    adjusted['drift_penalty'] = 0.0
    return adjusted


def is_market_open() -> bool:
    now = datetime.now(KST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 900 <= t <= 1530


# ─────────────────────────────────────────────
# 시장/지표 데이터
# ─────────────────────────────────────────────
_cache = {}  # 간단한 메모리 캐시 (사이클 단위 리셋)
_kr_buy_blocked = False


def _calc_rsi(closes: list, period: int = 14) -> float:
    """RSI 계산 (공통 함수)"""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def _calc_ema(data: list, period: int) -> float:
    """EMA 계산 (공통 함수)"""
    if not data:
        return 0.0
    k = 2 / (period + 1)
    e = data[0]
    for d in data[1:]:
        e = d * k + e * (1 - k)
    return e


def get_kospi_sentiment() -> KospiSentiment:
    """코스피 시장 심리 (RSI 기반)"""
    cache_key = 'kospi_sentiment'
    if cache_key in _cache:
        return _cache[cache_key]
    try:
        import yfinance as yf
        kospi = yf.Ticker('^KS11')
        hist = kospi.history(period='30d')
        if hist.empty:
            return {'rsi': 50, 'msg': '⚪ 코스피 데이터 없음 — 중립 처리'}

        closes = list(hist['Close'])
        rsi = _calc_rsi(closes)

        if rsi <= 30:
            msg = f'🔴 코스피 극도공포({rsi}) — 역발상 매수 기회'
        elif rsi <= 45:
            msg = f'🟠 코스피 공포({rsi}) — 매수 우호적'
        elif rsi <= 55:
            msg = f'⚪ 코스피 중립({rsi})'
        elif rsi <= 70:
            msg = f'🟡 코스피 과열({rsi}) — 매수 주의'
        else:
            msg = f'🔴 코스피 극도과열({rsi}) — 매수 금지'

        result = {'rsi': rsi, 'msg': msg}
        _cache[cache_key] = result
        return result
    except Exception as e:
        log(f'코스피 심리 조회 실패: {e}', 'WARN')
        return {'rsi': 50, 'msg': '⚪ 코스피 조회 실패 — 중립 처리'}


def get_weekly_trend(code: str) -> WeeklyTrend:
    """주봉 EMA 5/10 기반 추세 (캐싱)"""
    cache_key = f'weekly_{code}'
    if cache_key in _cache:
        return _cache[cache_key]
    try:
        import yfinance as yf
        ticker = yf.Ticker(code + '.KS')
        hist = ticker.history(period='6mo', interval='1wk')
        if hist.empty or len(hist) < 10:
            return {'trend': 'UNKNOWN'}

        closes = list(hist['Close'])
        ema5 = _calc_ema(closes, 5)
        ema10 = _calc_ema(closes, 10)
        price = closes[-1]

        if ema5 > ema10 and price > ema5:
            trend = 'UPTREND'
        elif ema5 < ema10 and price < ema5:
            trend = 'DOWNTREND'
        else:
            trend = 'SIDEWAYS'

        result = {'trend': trend, 'ema5': round(ema5, 0), 'ema10': round(ema10, 0)}
        _cache[cache_key] = result
        return result
    except Exception as e:
        log(f'주봉 추세 조회 실패 {code}: {e}', 'WARN')
        return {'trend': 'UNKNOWN'}


def get_stock_news(stock_name: str) -> str:
    """종목 관련 뉴스 헤드라인"""
    try:
        import defusedxml.ElementTree as ET
        sources = [
            'https://www.yna.co.kr/rss/economy.xml',
            'https://rss.hankyung.com/economy.xml',
        ]
        headlines = []
        keywords = [stock_name, '반도체', '코스피', '외국인', '기관']

        for url in sources:
            try:
                res = requests.get(url, timeout=4, headers={'User-Agent': 'Mozilla/5.0'})
                root = ET.fromstring(res.content)
                for item in root.findall('.//item'):
                    title = item.findtext('title', '')
                    if any(k in title for k in keywords):
                        headlines.append(title.strip())
                if headlines:
                    break
            except Exception as e:
                _log.debug(f'뉴스 RSS 조회 실패: {e}')
                continue

        return '\n'.join(headlines[:3]) if headlines else '관련 뉴스 없음'
    except Exception as e:
        _log.debug(f'뉴스 조회 실패: {e}')
        return '뉴스 조회 실패'


def get_investor_trend_krx(stock_code: str) -> dict:
    """
    투자자별 매매동향 — Kiwoom ka10007 실패 시 fallback.

    기존 `data.krx.co.kr` 직접 POST는 User-Agent 검증 강화로 400 반환 →
    pykrx 라이브러리로 교체(2026-04-19). pykrx도 KRX_ID/KRX_PW 환경변수를
    요구할 수 있으며, 실패 시 빈 dict 반환 후 수급 없이 진행 (치명적 경로 아님).
    """
    try:
        from pykrx import stock as _pykrx_stock
    except ImportError:
        log('pykrx 미설치 — 수급 fallback 불가 (requirements.txt 확인)', 'WARN')
        return {}
    code = str(stock_code or '').lstrip('A').strip()
    if not code:
        return {}
    try:
        today = datetime.now(KST).date()
        start = (today - timedelta(days=14)).strftime('%Y%m%d')
        end = today.strftime('%Y%m%d')
        df = _pykrx_stock.get_market_trading_volume_by_date(start, end, code)
        if df is None or df.empty:
            return {}
        row = df.iloc[-1]

        def _to_int(val) -> int:
            try:
                return int(float(val or 0))
            except (ValueError, TypeError):
                return 0

        return {
            'foreign_net': _to_int(row.get('외국인합계', 0)),
            'inst_net': _to_int(row.get('기관합계', 0)),
            'individual_net': _to_int(row.get('개인', 0)),
        }
    except Exception as e:
        log(f'수급(pykrx) 조회 실패 {code}: {e}', 'DEBUG')
        return {}


def calc_momentum_score(code: str) -> dict:
    """
    모멘텀 스코어 — 최근 수익률 + 거래량 증가 + 신고가 근접도
    """
    try:
        rows = (
            supabase.table('daily_ohlcv')
            .select('close_price,high_price,volume,date')
            .eq('stock_code', code)
            .order('date', desc=True)
            .limit(60)
            .execute()
            .data
            or []
        )
        if len(rows) < 20:
            return {'score': 0, 'grade': 'F'}

        rows.reverse()
        closes = [float(r['close_price']) for r in rows]
        highs = [float(r['high_price']) for r in rows]
        volumes = [float(r.get('volume', 0)) for r in rows]
        price = closes[-1]

        # 1. 수익률 모멘텀 (가중치 40%)
        ret_5d = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
        ret_20d = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else 0
        momentum_raw = ret_5d * 0.6 + ret_20d * 0.4
        momentum_score = max(0, min(100, 50 + momentum_raw * 5))

        # 2. 거래량 모멘텀 (가중치 30%)
        vol_5 = sum(volumes[-5:]) / 5
        vol_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else vol_5
        vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1
        vol_score = max(0, min(100, vol_ratio * 50))

        # 3. 신고가 근접도 (가중치 30%)
        high_60d = max(highs) if highs else price
        nearness = (price / high_60d) * 100 if high_60d > 0 else 50
        high_score = max(0, min(100, (nearness - 80) * 5))

        total = momentum_score * 0.4 + vol_score * 0.3 + high_score * 0.3

        if total >= 75:
            grade = 'A'
        elif total >= 60:
            grade = 'B'
        elif total >= 40:
            grade = 'C'
        else:
            grade = 'D'

        return {
            'score': round(total, 1),
            'grade': grade,
            'ret_5d': round(ret_5d, 2),
            'ret_20d': round(ret_20d, 2),
            'vol_ratio': round(vol_ratio, 2),
            'near_high': round(nearness, 1),
        }
    except Exception as e:
        log(f"score calculation failed: {e}", 'WARN')
        return {'score': 0, 'grade': 'F'}


def get_current_price(code: str) -> float:
    """키움 API로 현재가 조회 (안정적 파싱)"""
    try:
        info = kiwoom.get_stock_info(code)
        if not info:
            return 0.0

        # 키움 API 응답 구조에 따라 파싱
        if isinstance(info, dict):
            # output 래핑된 경우
            output = info.get('output', info)
            price_str = (
                output.get('stck_prpr')
                or output.get('cur_prc')
                or '0'
            )
            price = abs(float(str(price_str).replace(',', '')))
            return price
        return 0.0
    except Exception as e:
        log(f'현재가 조회 실패 {code}: {e}', 'WARN')
        return 0.0


def _fetch_live_candles(code: str, period: str = '5d', interval: str = '5m') -> dict:
    cache_key = f'live_{code}_{interval}'
    if cache_key in _cache:
        return _cache[cache_key]
    try:
        import yfinance as yf
        ticker = yf.Ticker(code + '.KS')
        hist = ticker.history(period=period, interval=interval)
        if hist.empty or len(hist) < 14:
            return {}
        result = {
            'closes': [float(c) for c in hist['Close']],
            'volumes': [float(v) for v in hist['Volume']],
            'source': f'{interval}_live',
            'last_time': str(hist.index[-1]),
        }
        _cache[cache_key] = result
        return result
    except Exception as e:
        log(f'실시간 분봉 조회 실패 {code}: {e}', 'WARN')
        return {}


def _fetch_daily_from_db(code: str) -> dict:
    try:
        rows = (
            supabase.table('daily_ohlcv')
            .select('close_price,volume,date')
            .eq('stock_code', code)
            .order('date', desc=False)
            .limit(30)
            .execute()
            .data or []
        )
        if len(rows) < 14:
            return {}
        return {
            'closes': [float(r['close_price']) for r in rows],
            'volumes': [float(r.get('volume', 0)) for r in rows],
            'source': 'daily_db',
            'last_date': rows[-1].get('date', 'unknown'),
        }
    except Exception as e:
        log(f'일봉 DB 조회 실패 {code}: {e}', 'WARN')
        return {}


def _calc_indicators_from_data(closes: list, volumes: list) -> dict:
    rsi = _calc_rsi(closes)
    ema12 = _calc_ema(closes, 12)
    ema26 = _calc_ema(closes, 26)
    macd = round(ema12 - ema26, 0)
    if len(closes) >= 26:
        macd_line = []
        for i in range(26, len(closes) + 1):
            e12 = _calc_ema(closes[:i], 12)
            e26 = _calc_ema(closes[:i], 26)
            macd_line.append(e12 - e26)
        macd_signal = _calc_ema(macd_line, 9) if len(macd_line) >= 9 else macd
        macd_histogram = round(macd - macd_signal, 0)
    else:
        macd_signal = macd
        macd_histogram = 0
    # 마지막 봉은 yfinance 미완성 봉으로 거래량이 0일 수 있으므로 0이면 이전 봉 사용
    cur_vol = volumes[-1] if volumes else 0
    if cur_vol == 0 and len(volumes) >= 2:
        cur_vol = volumes[-2]
    avg_vol = sum(v for v in volumes[-20:] if v > 0) / max(sum(1 for v in volumes[-20:] if v > 0), 1) if volumes else 1
    vol_ratio = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1.0
    vol_labels = [(3.0, '💥 거래량 폭발'), (2.0, '🔥 거래량 급등'), (1.5, '📈 거래량 증가'), (0.5, '➡️ 거래량 보통')]
    vol_label = f'😴 거래량 급감 ({vol_ratio}배)'
    for threshold, label in vol_labels:
        if vol_ratio >= threshold:
            vol_label = f'{label} ({vol_ratio}배)'
            break
    bb_upper = bb_lower = bb_pos = 0
    if len(closes) >= 20:
        ma20 = sum(closes[-20:]) / 20
        std20 = (sum((c - ma20) ** 2 for c in closes[-20:]) / 20) ** 0.5
        bb_upper = round(ma20 + 2 * std20, 0)
        bb_lower = round(ma20 - 2 * std20, 0)
        bb_width = bb_upper - bb_lower
        if bb_width > 0:
            bb_pos = round((closes[-1] - bb_lower) / bb_width * 100, 1)
    return {
        'rsi': rsi, 'macd': macd, 'macd_signal': round(macd_signal, 0),
        'macd_histogram': macd_histogram, 'close': closes[-1],
        'vol_ratio': vol_ratio, 'vol_label': vol_label,
        'bb_upper': bb_upper, 'bb_lower': bb_lower, 'bb_pos': bb_pos,
    }


def get_indicators(code: str) -> dict:
    """장 중: yfinance 5분봉 실시간 / 장 외: DB 일봉"""
    try:
        data = {}
        if is_market_open():
            data = _fetch_live_candles(code, period='5d', interval='5m')
            if data:
                log(f'  {code}: 실시간 5분봉 사용 (마지막: {data.get("last_time", "?")})')
        if not data:
            data = _fetch_daily_from_db(code)
        if not data or len(data.get('closes', [])) < 14:
            log(f'{code}: 데이터 부족', 'WARN')
            return {}
        indicators = _calc_indicators_from_data(data['closes'], data['volumes'])
        price = get_current_price(code)
        if price == 0:
            price = data['closes'][-1]
        if indicators['bb_upper'] > indicators['bb_lower']:
            bb_width = indicators['bb_upper'] - indicators['bb_lower']
            indicators['bb_pos'] = round((price - indicators['bb_lower']) / bb_width * 100, 1)
        indicators['price'] = price
        indicators['data_source'] = data.get('source', 'unknown')
        indicators['data_points'] = len(data['closes'])
        return indicators
    except Exception as e:
        log(f'지표 계산 실패 {code}: {e}', 'ERROR')
        return {}


# ─────────────────────────────────────────────
# 포지션 관리
# ─────────────────────────────────────────────
def get_open_positions() -> list:
    """현재 열린 포지션 목록"""
    try:
        positions = (
            supabase.table('trade_executions')
            .select('*')
            .eq('result', 'OPEN')
            .execute()
            .data or []
        )
        _last_known_state["open_positions"] = positions
        positions_by_code = {}
        for pos in positions:
            code = pos.get("stock_code")
            if code:
                positions_by_code.setdefault(code, []).append(pos)
        _last_known_state["positions_by_code"] = positions_by_code
        return positions
    except Exception as e:
        log(f'포지션 조회 실패, last known state 사용: {e}', 'ERROR')
        return list(_last_known_state.get("open_positions", []))


def _get_kr_market_weight(account_equity: float) -> float:
    if account_equity <= 0:
        return 0.0
    total_value = 0.0
    for pos in get_open_positions():
        total_value += float(pos.get('quantity', 0) or 0) * float(pos.get('price', 0) or 0)
    return total_value / account_equity if account_equity > 0 else 0.0


def _estimate_atr_pct_kr(code: str, price: float) -> float:
    try:
        data = _fetch_live_candles(code, period='1mo', interval='1d') or _fetch_daily_from_db(code)
        closes = data.get('closes', []) if data else []
        if len(closes) < 14 or price <= 0:
            return 0.0
        diffs = [abs(float(closes[i] - closes[i - 1])) for i in range(1, len(closes))]
        atr = sum(diffs[-14:]) / min(len(diffs), 14) if diffs else 0.0
        return atr / price if price > 0 else 0.0
    except Exception as e:
        log(f"value extraction failed: {e}", 'WARN')
        return 0.0


def _apply_drawdown_guard_kr() -> bool:
    global _kr_buy_blocked
    equity_curve = load_equity_curve('kr')
    if not equity_curve:
        _kr_buy_blocked = False
        return False

    guard = DrawdownGuard()
    returns = guard.returns_from_equity_curve(equity_curve)
    prev_state = DrawdownGuardState(**load_drawdown_state('kr'))
    decision = guard.evaluate(
        daily_return=returns.get('daily_return', 0.0),
        weekly_return=returns.get('weekly_return', 0.0),
        monthly_return=returns.get('monthly_return', 0.0),
        state=prev_state,
    )
    save_drawdown_state('kr', decision['state'].__dict__)
    _kr_buy_blocked = not decision.get('allow_new_buys', True)

    triggers = set(decision.get('triggered_rules') or [])
    if 'MONTHLY_STOP' in triggers:
        log('DrawdownGuard: 월간 손실 한도 초과 — 전량 청산 + 쿨다운', 'WARN')
        positions = get_open_positions()
        seen_codes = []
        for pos in positions:
            code = pos.get('stock_code')
            if code and code not in seen_codes:
                seen_codes.append(code)
                execute_sell({'code': code, 'name': pos.get('stock_name', code)}, {'reason': 'DrawdownGuard FULL_STOP'}, {'price': get_current_price(code)}, reason_prefix='DrawdownGuard FULL_STOP ')
        return True

    if 'WEEKLY_DELEVERAGE' in triggers:
        log('DrawdownGuard: 주간 손실 한도 초과 — 신규 매수 차단 + 디레버리징', 'WARN')
        positions = get_open_positions()
        ranked = sorted(
            positions,
            key=lambda p: float(p.get('quantity', 0) or 0) * float(p.get('price', 0) or 0),
            reverse=True,
        )
        total_value = sum(float(p.get('quantity', 0) or 0) * float(p.get('price', 0) or 0) for p in ranked)
        reduced = 0.0
        seen_codes = set()
        for pos in ranked:
            code = pos.get('stock_code')
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            position_value = float(pos.get('quantity', 0) or 0) * float(pos.get('price', 0) or 0)
            execute_sell(
                {'code': code, 'name': pos.get('stock_name', code)},
                {'reason': 'DrawdownGuard DELEVERAGE'},
                {'price': get_current_price(code)},
                reason_prefix='DrawdownGuard DELEVERAGE ',
            )
            reduced += position_value
            if total_value > 0 and reduced / total_value >= 0.5:
                break

    if 'DAILY_BUY_BLOCK' in triggers or 'COOLDOWN_ACTIVE' in triggers:
        log('DrawdownGuard: 신규 매수 차단', 'WARN')

    return False


def get_position_for_stock(code: str) -> list:
    """특정 종목의 열린 포지션"""
    try:
        positions = (
            supabase.table('trade_executions')
            .select('*')
            .eq('stock_code', code)
            .eq('result', 'OPEN')
            .execute()
            .data or []
        )
        if positions:
            _last_known_state.setdefault("positions_by_code", {})[code] = positions
        return positions
    except Exception as e:
        log(f'종목 포지션 조회 실패 {code}, last known state 사용: {e}', 'ERROR')
        return list(_last_known_state.get("positions_by_code", {}).get(code, []))


def calc_avg_entry_price(positions: list) -> float:
    """분할매수 평균 진입가 계산 (가중평균)"""
    total_cost = 0.0
    total_qty = 0
    for p in positions:
        qty = int(p.get('quantity', 0))
        price = float(p.get('price', 0))
        total_cost += price * qty
        total_qty += qty
    return round(total_cost / total_qty, 0) if total_qty > 0 else 0.0


def get_split_stage_for_stock(code: str) -> int:
    """해당 종목의 현재 분할매수 차수 (기존 포지션 수 기반)"""
    positions = get_position_for_stock(code)
    return len(positions) + 1  # 0개면 1차, 1개면 2차, 2개면 3차


def check_cooldown(code: str) -> bool:
    """최근 매도 후 쿨다운 시간 체크 (True = 쿨다운 중)"""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=RISK['cooldown_minutes'])).isoformat()
        recent = (
            supabase.table('trade_executions')
            .select('created_at')
            .eq('stock_code', code)
            .eq('trade_type', 'SELL')
            .gte('created_at', cutoff)
            .limit(1)
            .execute()
            .data or []
        )
        return len(recent) > 0
    except (TypeError, ValueError, KeyError):
        return False


# ─────────────────────────────────────────────
# 리스크 관리
# ─────────────────────────────────────────────
def check_daily_loss() -> bool:
    """오늘 일일 손실 한도 도달 시 True (거래 중단)"""
    try:
        today = datetime.now(KST).date()
        today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=KST)
        closed_today = (
            supabase.table('trade_executions')
            .select('*')
            .eq('result', 'CLOSED')
            .eq('trade_type', 'SELL')
            .gte('created_at', today_start.isoformat())
            .execute()
            .data or []
        )
        if not closed_today:
            return False

        total_pnl = 0.0
        total_invested = 0.0

        for r in closed_today:
            sell_price = float(r.get('price', 0))
            entry_price = float(r.get('entry_price', sell_price))
            qty = int(r.get('quantity', 0))
            total_pnl += (sell_price - entry_price) * qty
            total_invested += entry_price * qty

        if total_invested > 0:
            pnl_ratio = total_pnl / total_invested
            if pnl_ratio <= RISK['max_daily_loss']:
                send_telegram(
                    f'🚨 <b>주식 일일 손실 한도 초과</b>\n'
                    f'손실률: {pnl_ratio*100:.2f}%\n'
                    f'오늘 거래 중단'
                )
                return True
    except Exception as e:
        log(f'일일 손실 체크 실패: {e}', 'ERROR')
    return False


# ─────────────────────────────────────────────
# 매매 판단
# ─────────────────────────────────────────────
def rule_based_signal(
    indicators: dict,
    kospi: dict = None,
    weekly: dict = None,
    has_position: bool = False,
    supply: dict = None,
    momentum: dict = None,
    dart_score: dict = None,
) -> dict:
    """복합 스코어 룰 기반 매매 판단 (모멘텀+기술+수급+재무)."""
    rsi = indicators.get('rsi', 50)
    macd = indicators.get('macd', 0)
    macd_hist = indicators.get('macd_histogram', 0)
    vol_ratio = indicators.get('vol_ratio', 1.0)
    bb_pos = indicators.get('bb_pos', 50)
    kospi_rsi = (kospi or {}).get('rsi', 50)
    trend = (weekly or {}).get('trend', 'UNKNOWN')
    m_score = (momentum or {}).get('score', 0)
    m_grade = (momentum or {}).get('grade', 'F')
    dart = dart_score or {}
    dart_grade = dart.get('grade', 'N/A')
    dart_val = dart.get('score', 0)

    foreign_net = (supply or {}).get('foreign_net', 0)
    inst_net = (supply or {}).get('inst_net', 0)
    supply_signal = 'NEUTRAL'
    if foreign_net > 0 and inst_net > 0:
        supply_signal = 'STRONG_BUY'
    elif foreign_net > 0 or inst_net > 0:
        supply_signal = 'BUY'
    elif foreign_net < 0 and inst_net < 0:
        supply_signal = 'SELL'

    # ── SELL 조건 ──
    if has_position:
        sell_reasons = []
        if rsi >= RULES['sell_rsi_min']:
            sell_reasons.append(f'RSI 과매수({rsi})')
        if bb_pos >= RULES['sell_bb_min']:
            sell_reasons.append(f'BB 상단({bb_pos}%)')
        if macd < 0 and macd_hist < 0:
            sell_reasons.append('MACD 음수 전환')
        if m_grade in ('D', 'F') and m_score < 30:
            sell_reasons.append(f'모멘텀 급락({m_grade}:{m_score:.0f})')

        if len(sell_reasons) >= 2:
            return {
                'action': 'SELL',
                'confidence': 75,
                'reason': f'[룰] {" + ".join(sell_reasons)}',
            }

    # ── BUY 차단 조건 ──
    blocks = []
    if vol_ratio <= RULES['block_vol_below']:
        blocks.append(f'거래량 급감({vol_ratio}배)')
    if bb_pos >= RULES['block_bb_above']:
        blocks.append(f'BB 상단({bb_pos}%)')
    if kospi_rsi >= RULES['block_kospi_above']:
        blocks.append(f'코스피 과열({kospi_rsi})')
    if trend == 'DOWNTREND' and rsi > 35:
        blocks.append('주봉 하락추세')
    if not has_position and supply_signal == 'SELL':
        blocks.append('수급 동시 순매도')
    if dart_grade == 'D' and dart_val < 20:
        blocks.append(f'재무부실({dart_grade}:{dart_val})')

    if blocks:
        return {
            'action': 'HOLD',
            'confidence': 0,
            'reason': f'[룰] 매수 차단: {", ".join(blocks)}',
        }

    # ── 복합 BUY 스코어 (115점 → 정규화 100점) ──
    cs = 0
    buy_reasons = []

    # 레짐 가중치 적용 (사이클에서 로드된 경우 반영)
    _regime_adj = globals().get('_regime_adj_cache', {})
    _mom_mult = _regime_adj.get('momentum_mult', 1.0)
    _val_mult = _regime_adj.get('value_mult', 1.0)
    _qual_mult = _regime_adj.get('quality_mult', 1.0)

    # 1) 모멘텀 (30점 × 레짐 배수)
    if m_grade == 'A':
        _pts = round(30 * _mom_mult); cs += _pts; buy_reasons.append(f'모멘텀A({m_score:.0f})')
    elif m_grade == 'B':
        _pts = round(22 * _mom_mult); cs += _pts; buy_reasons.append(f'모멘텀B({m_score:.0f})')
    elif m_grade == 'C':
        _pts = round(12 * _mom_mult); cs += _pts; buy_reasons.append(f'모멘텀C({m_score:.0f})')

    # 2) RSI (18점)
    if rsi <= 30:
        cs += 18; buy_reasons.append(f'RSI과매도({rsi:.0f})')
    elif rsi <= 40:
        cs += 13; buy_reasons.append(f'RSI저점({rsi:.0f})')
    elif rsi <= 50:
        cs += 8; buy_reasons.append(f'RSI중립({rsi:.0f})')

    # 3) BB (12점)
    if bb_pos <= 25:
        cs += 12; buy_reasons.append(f'BB하단({bb_pos:.0f}%)')
    elif bb_pos <= 45:
        cs += 8; buy_reasons.append(f'BB중간({bb_pos:.0f}%)')

    # 4) 거래량 (10점)
    if vol_ratio >= 2.0:
        cs += 10; buy_reasons.append(f'거래량급증({vol_ratio:.1f}x)')
    elif vol_ratio >= 1.2:
        cs += 7; buy_reasons.append(f'거래량증가({vol_ratio:.1f}x)')

    # 5) 추세 (8점)
    if trend == 'UPTREND':
        cs += 8; buy_reasons.append('상승추세')
    elif trend == 'SIDEWAYS':
        cs += 4

    # 6) 수급 (8점)
    if supply_signal == 'STRONG_BUY':
        cs += 8; buy_reasons.append('수급 동시매수')
    elif supply_signal == 'BUY':
        cs += 4; buy_reasons.append('수급 우호')

    # 7) DART 재무 품질 (15점 — 레짐 quality 배수)
    if dart_grade == 'A':
        _pts = round(15 * _qual_mult); cs += _pts; buy_reasons.append(f'재무A({dart_val})')
    elif dart_grade == 'B':
        _pts = round(10 * _qual_mult); cs += _pts; buy_reasons.append(f'재무B({dart_val})')
    elif dart_grade == 'C':
        _pts = round(5 * _qual_mult); cs += _pts; buy_reasons.append(f'재무C({dart_val})')
    elif dart_grade == 'D':
        cs -= 3

    if cs >= 50:
        return {
            'action': 'BUY',
            'confidence': min(cs + 15, 95),
            'reason': f'[룰] 복합{cs}점: {" + ".join(buy_reasons[:5])}',
        }

    return {'action': 'HOLD', 'confidence': 0, 'reason': f'[룰] 복합{cs}점 미달'}


def analyze_with_ai(
    stock: dict,
    indicators: dict,
    strategy: dict,
    news: str = '',
    weekly: dict = None,
    kospi: dict = None,
    has_position: bool = False,
    supply: dict = None,
) -> dict:
    """AI 분석 (실패 시 룰 기반 fallback)"""
    def _parse_ai_response(raw: str) -> dict:
        raw = raw.replace('```json', '').replace('```', '').strip()
        if raw.startswith('{'):
            return json.loads(raw)
        start = raw.find('{')
        end = raw.rfind('}') + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
        raise ValueError(f'JSON 파싱 불가: {raw[:100]}')

    if not OPENAI_KEY:
        log('OpenAI 키 없음 → 룰 기반 판단', 'WARN')
        momentum = calc_momentum_score(stock['code'])
        dart = _get_dart_score(stock['code'])
        return rule_based_signal(indicators, kospi, weekly, has_position, supply, momentum, dart)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_KEY)

        picks = strategy.get('top_picks', [])
        pick = next((p for p in picks if p.get('code') == stock['code']), None)
        pick_info = f"AI 장 전 전략: {pick['action']} — {pick['reason']}" if pick else "장 전 전략 없음"
        kospi_msg = (kospi or {}).get('msg', '중립')
        weekly_trend = (weekly or {}).get('trend', 'UNKNOWN')

        # 수급 정보
        foreign_net = (supply or {}).get('foreign_net', 0)
        inst_net = (supply or {}).get('inst_net', 0)
        supply_signal = 'NEUTRAL'
        if foreign_net > 0 and inst_net > 0:
            supply_signal = 'STRONG_BUY'
        elif foreign_net > 0 or inst_net > 0:
            supply_signal = 'BUY'
        elif foreign_net < 0 and inst_net < 0:
            supply_signal = 'SELL'

        # 모멘텀 스코어
        momentum = calc_momentum_score(stock['code'])
        m_grade = momentum.get('grade', 'F')
        m_score = momentum.get('score', 0)
        m_ret5 = momentum.get('ret_5d', 0)
        m_ret20 = momentum.get('ret_20d', 0)
        m_vol = momentum.get('vol_ratio', 1)

        prompt = f"""당신은 연평균 수익률 50% 이상의 한국 주식 상위 1% 퀀트 트레이더입니다.
현재 모의투자 환경이므로 공격적으로 수익을 추구합니다.

[종목] {stock['name']} ({stock['code']})
[현재가] {indicators.get('price', 0):,.0f}원
[RSI] {indicators.get('rsi', 50)} — 45 이하면 매수 적극 고려
[MACD] {indicators.get('macd', 0)} (히스토그램: {indicators.get('macd_histogram', 0)})
[거래량] {indicators.get('vol_label', '정보없음')}
[볼린저밴드] 위치: {indicators.get('bb_pos', 50)}% — 40% 이하면 매수 구간
[보유 여부] {'보유 중' if has_position else '미보유'}
[장 전 전략] {pick_info}
[코스피] {kospi_msg}
[주봉 추세] {weekly_trend}
[수급] 외국인: {'+' if foreign_net > 0 else ''}{foreign_net:,}주 / 기관: {'+' if inst_net > 0 else ''}{inst_net:,}주
수급 시그널: {supply_signal}
[모멘텀] 등급: {m_grade}({m_score}) | 5일수익: {m_ret5:+.1f}% | 20일수익: {m_ret20:+.1f}% | 거래량추세: {m_vol:.1f}배
[뉴스] {news if news else '없음'}
[데이터 소스] {indicators.get('data_source', '?')} ({indicators.get('data_points', '?')}봉)

[매매 원칙 — 공격적 모의투자]
- 모의투자이므로 적극적으로 BUY 판단. 확률 55% 이상이면 매수.
- RSI 45 이하 + 아무 양수 시그널 하나 → BUY (MACD 양수, 거래량 증가, BB 하단, 뉴스 긍정 중 1개)
- RSI 35 이하면 거의 무조건 BUY (공포 매수)
- 거래량 2배 이상 급등 + RSI 50 이하 → BUY (모멘텀)
- SELL: RSI 65 이상 + MACD 음수 전환 시에만
- 주봉 DOWNTREND여도 RSI 30 이하면 역발상 BUY 허용
- 단, 거래량 0.3배 이하는 어떤 경우에도 BUY 금지

반드시 아래 JSON만 출력:
{{"action":"BUY|SELL|HOLD","confidence":0~100,"reason":"한줄이유"}}"""

        try:
            res = retry_call(
                lambda: client.chat.completions.create(
                    model='gpt-4o-mini',
                    messages=[{'role': 'user', 'content': prompt}],
                    temperature=0.1,
                    max_tokens=150,
                ),
                max_attempts=2,
                base_delay=3,
                default=None,
            )
            if res is None:
                raise RuntimeError('OpenAI 응답 없음')
            raw = res.choices[0].message.content.strip()
            out = _parse_ai_response(raw)
        except Exception as openai_err:
            log(f'OpenAI 실패, Claude fallback: {openai_err}', 'WARN')
            try:
                import anthropic

                claude = anthropic.Anthropic()
                claude_resp = claude.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=500,
                    messages=[{"role": "user", "content": prompt}],
                )
                response_text = ''.join(
                    block.text for block in claude_resp.content if getattr(block, 'type', '') == 'text'
                ).strip()
                out = _parse_ai_response(response_text)
            except Exception as claude_err:
                log(f'Claude도 실패: {claude_err}', 'WARN')
                raise

        # 보정: 거래량 2배 이상 + BUY면 신뢰도 +10
        if out.get('action') == 'BUY' and indicators.get('vol_ratio', 1.0) >= 2.0:
            out['confidence'] = min(100, out.get('confidence', 0) + 10)

        # 보정: 코스피 RSI 30 이하 + BUY면 신뢰도 +10
        if out.get('action') == 'BUY' and kospi and (kospi.get('rsi') or 50) <= 30:
            out['confidence'] = min(100, out.get('confidence', 0) + 10)

        out['source'] = 'AI'
        return out

    except Exception as e:
        log(f'AI 분석 실패 → 룰 기반 fallback: {e}', 'WARN')
        dart = _get_dart_score(stock['code'])
        # momentum이 try 블록 내에서 정의되므로 미정의 시 안전하게 조회
        if 'momentum' not in dir():
            momentum = calc_momentum_score(stock['code'])
        result = rule_based_signal(indicators, kospi, weekly, has_position, supply, momentum, dart)
        result['source'] = 'RULE_FALLBACK'
        return result


def get_trading_signal(
    stock: dict,
    indicators: dict,
    strategy: dict,
    news: str,
    weekly: dict,
    kospi: dict,
    has_position: bool,
    supply: dict,
) -> dict:
    """
    매매 신호 결정 (우선순위):
    1. ML 모델 (XGBoost) — 고확률 직접 BUY
    2. AI (GPT) or 룰 기반 → ML 블렌딩 (rule 60% / ML 40%)
    3. 룰 기반 — AI도 실패 시
    반환 dict에 'ml_score', 'ml_confidence' 항상 포함

    신호 결정 분기 4가지:
    1. ml_model import 실패 → rule_based 단독 (콜드스타트 극단)
    2. ml_model OK + ensemble_meta.json 부재 → AI 단독 (운영 콜드스타트)
       - 현재 prod 상태: trade_executions < 50건, 매일 retrain 미트리거
       - 실거래 시작 후 50건 도달 시 자동 해제
    3. ML active + 강신호 (action ∈ {BUY/STRONG_BUY/SWING_BUY}, conf ≥ 78)
       → ML 단독 BUY (블렌딩 안 함)
    4. ML active + 약신호 (HOLD 등 또는 conf < 78)
       → rule/AI + ML 60/40 블렌딩 (실제 가중치는 common/config.py ML_BLEND_CONFIG)

    테스트: tests/test_stock_signal.py 4개 분기 명시 커버.
    """
    ml_confidence: float = 0.0
    ml_source: str = 'ML_NA'
    ml_features: dict = {}

    # ML 신호 항상 수집
    try:
        from ml_model import MODEL_DIR, get_ml_signal  # 같은 디렉토리
        if (MODEL_DIR / 'ensemble_meta.json').exists():
            ml = get_ml_signal(stock['code'])
            ml_confidence = float(ml.get('confidence', 0))
            ml_source = ml.get('source', 'ML_XGBOOST')
            ml_features = ml.get('features', {})
            # 고확률 ML → 즉시 BUY
            ml_action = ml.get('action')
            if ml_action in ('BUY', 'STRONG_BUY', 'SWING_BUY') and ml_confidence >= 78:
                log(
                    f"  ML 고확률 {ml_action}: {ml_confidence:.1f}% [{ml_source}]",
                    'INFO',
                )
                return _apply_kr_drift_gate({
                    'action': 'BUY',
                    'confidence': ml_confidence,
                    'reason': f"ML 모델 {ml_action} 확률 {ml_confidence:.1f}%",
                    'source': 'ML_MULTI_HORIZON',
                    'ml_score': ml_confidence,
                    'ml_confidence': ml_confidence,
                    'ml_features': ml_features,
                })
            if ml_confidence >= 65:
                log(f"  ML 보조신호: {ml_confidence:.1f}% → 룰/AI 블렌딩", 'INFO')
    except Exception as e:
        log(f'  ML 모델 오류: {e}', 'WARN')

    # 2차: AI (GPT)
    base_signal: Optional[dict] = None
    try:
        ai_result = analyze_with_ai(
            stock, indicators, strategy, news, weekly, kospi, has_position, supply
        )
        if ai_result and ai_result.get('action') in ('BUY', 'SELL', 'HOLD'):
            base_signal = ai_result
    except Exception as e:
        log(f'AI 분석 실패: {e}', 'WARN')

    # 3차: 룰 기반 fallback
    if base_signal is None:
        momentum = calc_momentum_score(stock['code'])
        dart = _get_dart_score(stock['code'])
        base_signal = rule_based_signal(indicators, kospi, weekly, has_position, supply, momentum, dart)

    # ML 블렌딩: BUY 신호일 때만 confidence 조정 (60/40 — ML_BLEND_CONFIG 참조)
    if base_signal.get('action') == 'BUY' and ml_confidence > 0:
        base_conf = float(base_signal.get('confidence', 0))
        blended = round(
            base_conf * ML_BLEND_CONFIG["rule_weight"]
            + ml_confidence * ML_BLEND_CONFIG["ml_weight"],
            1,
        )
        base_signal['confidence'] = blended
        base_signal['reason'] = (
            base_signal.get('reason', '') + f" [ML블렌딩:{ml_confidence:.0f}%→{blended:.0f}%]"
        )

    base_signal['ml_score'] = round(ml_confidence, 2)
    base_signal['ml_confidence'] = round(ml_confidence, 2)
    base_signal['ml_features'] = ml_features
    return _apply_kr_drift_gate(base_signal)


# ─────────────────────────────────────────────
# DART 재무 품질 스코어 (v3 신규)
# ─────────────────────────────────────────────
_dart_cache: dict = {}
_sector_map: dict = {}


def _get_stock_sector(code: str) -> str:
    """종목 코드로 섹터 조회 (TOP50 WATCHLIST 기반 + DB fallback)."""
    if code in _sector_map:
        return _sector_map[code]
    try:
        from stock_premarket import WATCHLIST
        for w in WATCHLIST:
            _sector_map[w['code']] = w.get('sector', '')
        if code in _sector_map:
            return _sector_map[code]
    except Exception as e:
        _log.debug(f'섹터 맵 로드 실패: {e}')
        pass
    _sector_map[code] = ''
    return ''


def _get_dart_score(code: str) -> dict:
    if code in _dart_cache:
        return _dart_cache[code]
    try:
        from common.market_data import get_dart_financial_score
        result = get_dart_financial_score(code, supabase)
        _dart_cache[code] = result
        return result
    except Exception as e:
        log(f'DART 스코어 실패 {code}: {e}', 'WARN')
        return {'score': 0, 'grade': 'N/A'}


# ─────────────────────────────────────────────
# 전략 로드
# ─────────────────────────────────────────────
def get_today_strategy() -> dict:
    path = Path(WORKSPACE_DIR) / 'stocks' / 'today_strategy.json'
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text())
        if d.get('date') != datetime.now(timezone.utc).date().isoformat():
            log('장 전 전략 날짜 불일치 — 무시', 'WARN')
            return {}
        return d
    except Exception as e:
        _log.debug(f'장 전 전략 로드 실패: {e}')
        return {}


def get_watchlist_from_db() -> list:
    """DB에서 종목 리스트 가져오기 (전략 없을 때 fallback → WATCHLIST 하드코딩)"""
    try:
        rows = (
            supabase.table('top50_stocks')
            .select('stock_code,stock_name')
            .limit(60)
            .execute()
            .data or []
        )
        if rows:
            return [{'code': r['stock_code'], 'name': r['stock_name']} for r in rows]
    except Exception as e:
        log(f'종목 리스트 DB 조회 실패 → WATCHLIST 사용: {e}', 'WARN')

    try:
        from stock_premarket import WATCHLIST
        return [{'code': w['code'], 'name': w['name']} for w in WATCHLIST]
    except Exception as e2:
        log(f'WATCHLIST import 실패: {e2}', 'ERROR')
        return []


# ─────────────────────────────────────────────
# 주문 실행
# ─────────────────────────────────────────────
def execute_buy(
    stock: dict,
    signal: dict,
    indicators: dict,
    kospi: dict = None,
    weekly: dict = None,
) -> dict:
    """매수 실행 (모든 검증 포함)"""
    code = stock['code']
    name = stock['name']
    price = indicators.get('price', 0)

    if not price:
        return {'result': 'NO_PRICE'}

    if _kr_buy_blocked:
        return {'result': 'BLOCKED_DRAWDOWN'}

    # 신뢰도 체크
    if signal.get('confidence', 0) < RISK['min_confidence']:
        return {'result': 'LOW_CONFIDENCE', 'confidence': signal.get('confidence', 0)}

    # ── 차단 조건들 ──
    if kospi and (kospi.get('rsi') or 0) >= RULES['block_kospi_above']:
        log(f'{name}: 코스피 극도과열 — BUY 차단', 'WARN')
        return {'result': 'BLOCKED_KOSPI'}

    if weekly and weekly.get('trend') == 'DOWNTREND':
        log(f'{name}: 주봉 하락 추세 — BUY 차단', 'WARN')
        return {'result': 'BLOCKED_WEEKLY'}

    if indicators.get('vol_ratio', 1.0) <= RULES['block_vol_below']:
        log(f'{name}: 거래량 급감 — BUY 차단', 'WARN')
        return {'result': 'BLOCKED_VOLUME'}

    if indicators.get('bb_pos', 0) >= RULES['block_bb_above']:
        log(f'{name}: 볼린저 상단 — BUY 차단', 'WARN')
        return {'result': 'BLOCKED_BB'}

    # 동일 종목 중복 매수 체크 + 분할매수 차수 확인
    existing = get_position_for_stock(code)
    split_stage = len(existing) + 1

    if split_stage > 3:
        log(f'{name}: 이미 3차 매수 완료 — 추가 매수 차단', 'WARN')
        return {'result': 'MAX_SPLIT_REACHED'}

    # 분할매수 간 최소 시간 간격 (2차·3차부터)
    if existing and split_stage >= 2:
        def _parse_created(s: str):
            s = (s or '2000-01-01T00:00:00').replace('Z', '').replace('+00:00', '')[:19]
            return datetime.fromisoformat(s)
        last_buy_time = max(_parse_created(p.get('created_at')) for p in existing)
        hours_since = (datetime.now(timezone.utc) - last_buy_time).total_seconds() / 3600
        min_hours = RISK.get('min_hours_between_splits', 4)
        if hours_since < min_hours:
            log(f'{name}: {split_stage}차 매수 대기 ({hours_since:.1f}시간/{min_hours}시간)', 'WARN')
            return {'result': 'SPLIT_TOO_SOON'}

    # 분할매수 RSI 기준 체크
    rsi = indicators.get('rsi', 50)
    required_rsi = RISK['split_rsi_thresholds'][split_stage - 1]
    if split_stage >= 2 and rsi > required_rsi:
        log(f'{name}: {split_stage}차 매수 RSI 기준 미달 (현재 {rsi} > 기준 {required_rsi})', 'WARN')
        return {'result': 'RSI_NOT_LOW_ENOUGH'}

    # 쿨다운 체크
    if check_cooldown(code):
        log(f'{name}: 최근 매도 후 쿨다운 중', 'WARN')
        return {'result': 'COOLDOWN'}

    # 최대 포지션 수 체크
    all_open = get_open_positions()
    open_codes = list(set(p['stock_code'] for p in all_open))
    if code not in open_codes and len(open_codes) >= RISK['max_positions']:
        return {'result': 'MAX_POSITIONS'}

    # v3: 섹터 분산 체크 (count + weight 이중 제한)
    max_sector = RISK.get('max_sector_positions', 2)
    max_sector_weight = RISK.get('max_sector_weight', 0.30)
    if code not in open_codes:
        stock_sector = stock.get('sector', '')
        if stock_sector:
            sector_count = 0
            sector_invested = 0.0
            total_invested = 0.0
            for p in all_open:
                p_price = float(p.get('price', 0) or 0)
                p_qty = int(p.get('quantity', 0) or 0)
                p_val = p_price * p_qty
                total_invested += p_val
                if _get_stock_sector(p.get('stock_code', '')) == stock_sector:
                    sector_count += 1
                    sector_invested += p_val
            if sector_count >= max_sector:
                log(f'{name}: 동일 섹터({stock_sector}) {sector_count}개 — count 초과 차단', 'WARN')
                return {'result': 'MAX_SECTOR'}
            if total_invested > 0:
                sector_weight = sector_invested / total_invested
                if sector_weight >= max_sector_weight:
                    log(f'{name}: 섹터({stock_sector}) 비중 {sector_weight*100:.1f}% ≥ {max_sector_weight*100:.0f}% — weight 초과 차단', 'WARN')
                    return {'result': 'MAX_SECTOR_WEIGHT'}

    # ── 주문 수량 계산 ──
    try:
        account = kiwoom.get_account_evaluation()
        summary = account.get('summary', {})
        krw_balance = float(
            summary.get('deposit', 0)
            or summary.get('estimated_asset', 0)
            or 0
        )
        account_equity = float(
            summary.get('estimated_asset', 0)
            or summary.get('total_asset', 0)
            or summary.get('deposit', 0)
            or krw_balance
        )
    except (ConnectionError, TimeoutError, ValueError) as e:
        log(f'잔고 조회 실패: {e}', 'ERROR')
        return {'result': 'BALANCE_ERROR'}
    except RuntimeError as e:
        log(f'잔고 조회 런타임 실패: {e}', 'ERROR')
        return {'result': 'BALANCE_ERROR'}

    target_market_weight = get_effective_market_weight('KR')
    if target_market_weight is not None:
        current_market_weight = _get_kr_market_weight(account_equity)
        if current_market_weight >= target_market_weight + 0.02:
            log(f'{name}: KR 비중 과대 ({current_market_weight:.1%} >= {target_market_weight:.1%})', 'WARN')
            return {'result': 'OVERWEIGHT_MARKET'}

    total_invest = krw_balance * RISK['invest_ratio']
    stage_ratio = RISK['split_ratios'][split_stage - 1]
    invest_krw = total_invest * stage_ratio

    recent_trades = load_recent_trades('kr', limit=100)
    if len(recent_trades) >= 50:
        wins = [t['pnl_pct'] for t in recent_trades if t.get('pnl_pct', 0) > 0]
        losses = [abs(t['pnl_pct']) for t in recent_trades if t.get('pnl_pct', 0) < 0]
        win_rate = len(wins) / len(recent_trades) if recent_trades else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.02
        avg_loss = sum(losses) / len(losses) if losses else 0.03
        atr_pct = _estimate_atr_pct_kr(code, price)
        current_exposure = _get_kr_market_weight(account_equity)
        sizing = KellyPositionSizer().size_position(
            account_equity=account_equity,
            price=price,
            win_rate=win_rate,
            payoff_ratio=avg_win / max(avg_loss, 0.001),
            current_total_exposure=current_exposure,
            atr_pct=atr_pct,
            conviction=max(0.0, min(1.0, signal.get('confidence', 0) / 100.0)),
        )
        kelly_invest = account_equity * float(sizing.get('capped_fraction', 0.0)) * stage_ratio
        if kelly_invest > 0:
            invest_krw = min(invest_krw, kelly_invest) if split_stage > 1 else kelly_invest

    # v3: ATR 기반 변동성 포지션 사이징
    if RISK.get('volatility_sizing'):
        try:
            data = _fetch_live_candles(code, period='1mo', interval='1d') or _fetch_daily_from_db(code)
            if data and len(data.get('closes', [])) >= 14:
                closes = data['closes']
                atr_vals = []
                for i in range(1, min(len(closes), 15)):
                    atr_vals.append(abs(closes[i] - closes[i - 1]))
                atr = sum(atr_vals) / len(atr_vals) if atr_vals else price * 0.02
                atr_pct = atr / price if price > 0 else 0.02
                if atr_pct > 0.04:
                    invest_krw *= 0.6
                    log(f'{name}: 고변동성({atr_pct*100:.1f}%) — 포지션 40% 축소')
                elif atr_pct > 0.03:
                    invest_krw *= 0.8
                    log(f'{name}: 중변동성({atr_pct*100:.1f}%) — 포지션 20% 축소')
        except Exception as e:
            _log.debug(f'{name} ATR 기반 포지션 조정 실패: {e}')

    if invest_krw < RISK['min_order_krw']:
        return {'result': 'INSUFFICIENT_KRW', 'available': invest_krw}

    # 매수 수수료 예비분 제외 후 실투입금 기준으로 수량 계산
    fee_reserve = invest_krw * RISK['fee_buy']
    actual_invest = max(0, invest_krw - fee_reserve)
    quantity = int(actual_invest / price)
    if quantity < 1:
        return {'result': 'INSUFFICIENT_KRW'}

    # ── 실제 주문 ──
    try:
        router_result = SmartRouter().route_order(
            symbol=code,
            side='buy',
            total_qty=quantity,
            market='kr',
            price_hint=price,
            kiwoom_client=kiwoom,
            simulate=False,
        )
        order_result = router_result.get('execution', {}).get('fills', [{}])[0].get('response', {})
        log(f'{name} 매수 주문 응답: {order_result}', 'TRADE')
        decision = router_result.get('decision', {})
        slippage = router_result.get('slippage', {})
        log(f"{name} SmartRouter: {decision.get('route', 'MARKET')} / 슬리피지 {slippage.get('avg_abs_slippage_bps', 0):.1f}bps")
    except ConnectionError as e:
        log(f'{name} 매수 주문 연결 실패: {e}', 'ERROR')
        _log.error(f"{name} 매수 주문 연결 실패: {e}", exc_info=True)
        send_telegram(f'❌ <b>{name} 매수 주문 실패</b>\n연결 오류')
        return {'result': 'ORDER_FAILED', 'error': '연결 오류'}
    except TimeoutError as e:
        log(f'{name} 매수 주문 타임아웃: {e}', 'ERROR')
        _log.error(f"{name} 매수 주문 타임아웃: {e}", exc_info=True)
        send_telegram(f'❌ <b>{name} 매수 주문 실패</b>\n타임아웃')
        return {'result': 'ORDER_FAILED', 'error': '타임아웃'}
    except ValueError as e:
        log(f'{name} 매수 주문 값 오류: {e}', 'ERROR')
        _log.error(f"{name} 매수 주문 값 오류: {e}", exc_info=True)
        send_telegram(f'❌ <b>{name} 매수 주문 실패</b>\n파라미터 오류')
        return {'result': 'ORDER_FAILED', 'error': '파라미터 오류'}
    except Exception as e:
        log(f'{name} 매수 주문 실패: {e}', 'ERROR')
        _log.error(f"{name} 매수 주문 실패: {e}", exc_info=True)
        send_telegram(f'❌ <b>{name} 매수 주문 실패</b>\n주문 처리 실패')
        return {'result': 'ORDER_FAILED', 'error': '주문 처리 실패'}
        # ↑ 주문 실패 시 여기서 return → DB 저장 안 됨 (v1 버그 수정)

    # ── DB 저장 (주문 성공 후에만) ──
    insert_data = {
        'trade_type': 'BUY',
        'stock_code': code,
        'stock_name': name,
        'quantity': quantity,
        'price': price,
        'entry_price': price,  # 일일손실 계산을 위해 진입가 명시 저장
        'strategy': signal.get('source', 'AI') + '+RSI+MACD',
        'reason': signal.get('reason', ''),
        'result': 'OPEN',
        'split_stage': split_stage,
        'ml_score': signal.get('ml_score', 0.0),
        'ml_confidence': signal.get('ml_confidence', 0.0),
        'ml_features_json': json.dumps(signal.get('ml_features', {})) if signal.get('ml_features') else None,
        'composite_score': signal.get('confidence', 0.0),
        'drift_status': signal.get('drift_status', ''),
        'drift_penalty': signal.get('drift_penalty', 0.0),
        'rsi': indicators.get('rsi', 0.0),
        'news_sentiment': signal.get('news_sentiment', None),
    }

    # 팩터 스냅샷 수집 (Phase Level 4: 팩터 로깅)
    try:
        import sys as _sys
        _WORKSPACE_PATH = str(Path(__file__).resolve().parents[1])
        if _WORKSPACE_PATH not in _sys.path:
            _sys.path.insert(0, _WORKSPACE_PATH)
        from quant.factors.registry import FactorContext, calc_all
        _fctx = FactorContext()
        _today_iso = datetime.now(timezone.utc).date().isoformat()
        _all_factors = calc_all(_today_iso, symbol=code, market='kr', context=_fctx)
        _top5 = dict(
            sorted(_all_factors.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
        )
        insert_data['factor_snapshot'] = json.dumps(_top5, ensure_ascii=False)
        log(f'{name} 팩터 스냅샷 수집: {list(_top5.keys())}')
    except Exception as _fe:
        log(f'{name} 팩터 스냅샷 건너뜀: {_fe}', 'WARN')

    try:
        supabase.table('trade_executions').insert(insert_data).execute()
    except Exception as e:
        # factor_snapshot 컬럼 없을 경우 제외 후 재시도
        if 'factor_snapshot' in insert_data:
            del insert_data['factor_snapshot']
        try:
            supabase.table('trade_executions').insert(insert_data).execute()
        except Exception as e2:
            log(f'{name} DB 저장 실패: {e2}', 'ERROR')

    # ── 알림 ──
    avg_entry = calc_avg_entry_price(get_position_for_stock(code))
    send_telegram(
        f"🟢 <b>{name} {split_stage}차 매수</b>\n"
        f"💰 {price:,.0f}원 × {quantity}주\n"
        f"💵 투입: {invest_krw:,.0f}원\n"
        f"📊 평균단가: {avg_entry:,.0f}원\n"
        f"🎯 신뢰도: {signal.get('confidence', 0)}% ({signal.get('source', '?')})\n"
        f"📝 {signal.get('reason', '')}\n"
        f"⚠️ 모의투자"
    )
    if _sheets_append:
        try:
            _sheets_append("kr", "매수", code, price, quantity, None, signal.get("reason", ""))
        except Exception as e:
            _log.debug(f'{name} sheets 매수 기록 실패: {e}')

    return {
        'result': 'BUY',
        'stage': split_stage,
        'quantity': quantity,
        'price': price,
        'invest_krw': invest_krw,
    }


def execute_sell(stock: dict, signal: dict, indicators: dict, reason_prefix: str = '') -> dict:
    """매도 실행"""
    code = stock['code']
    name = stock['name']
    price = indicators.get('price', 0) if isinstance(indicators, dict) else indicators

    positions = get_position_for_stock(code)
    if not positions:
        return {'result': 'NO_POSITION'}

    total_qty = sum(int(p['quantity']) for p in positions)
    avg_entry = calc_avg_entry_price(positions)

    if not price or not avg_entry:
        return {'result': 'NO_PRICE'}

    raw_pnl_pct = (price - avg_entry) / avg_entry
    # 왕복 비용(수수료+거래세) 차감 후 실수익률
    fee_cost = RISK['fee_buy'] + RISK['fee_sell'] + RISK['tax_sell']
    net_pnl_pct = raw_pnl_pct - fee_cost
    pnl_pct = net_pnl_pct * 100
    pnl_krw = (price - avg_entry) * total_qty

    # ── 실제 주문 ──
    try:
        router_result = SmartRouter().route_order(
            symbol=code,
            side='sell',
            total_qty=total_qty,
            market='kr',
            price_hint=price,
            kiwoom_client=kiwoom,
            simulate=False,
        )
        order_result = router_result.get('execution', {}).get('fills', [{}])[0].get('response', {})
        log(f'{name} 매도 주문 응답: {order_result}', 'TRADE')
        decision = router_result.get('decision', {})
        slippage = router_result.get('slippage', {})
        log(f"{name} SmartRouter: {decision.get('route', 'MARKET')} / 슬리피지 {slippage.get('avg_abs_slippage_bps', 0):.1f}bps")
    except ConnectionError as e:
        log(f'{name} 매도 주문 연결 실패: {e}', 'ERROR')
        err_str = str(e)
        send_telegram(f'❌ <b>{name} 매도 주문 실패</b>\n연결 오류: {e}')
        return {'result': 'ORDER_FAILED', 'error': err_str}
    except TimeoutError as e:
        log(f'{name} 매도 주문 타임아웃: {e}', 'ERROR')
        err_str = str(e)
        send_telegram(f'❌ <b>{name} 매도 주문 실패</b>\n타임아웃: {e}')
        return {'result': 'ORDER_FAILED', 'error': err_str}
    except ValueError as e:
        log(f'{name} 매도 주문 값 오류: {e}', 'ERROR')
        err_str = str(e)
        send_telegram(f'❌ <b>{name} 매도 주문 실패</b>\n파라미터 오류: {e}')
        return {'result': 'ORDER_FAILED', 'error': err_str}
    except Exception as e:
        log(f'{name} 매도 주문 실패: {e}', 'ERROR')
        err_str = str(e)
        # 800033: 모의투자 매도가능수량 부족 → 키움 계좌와 DB 불일치
        # 계속 재시도해봐야 의미 없으므로 DB 포지션을 SYNC_ERROR로 닫음
        if '800033' in err_str:
            log(f'{name} 키움 계좌에 수량 없음 (800033) → DB 포지션 SYNC_ERROR 처리', 'WARNING')
            for p in positions:
                pid = p.get('trade_id')
                if pid is not None:
                    try:
                        supabase.table('trade_executions').update({
                            'result': 'SYNC_ERROR',
                            'reason': (p.get('reason') or '') + ' [매도가능수량 없음-자동정리]',
                        }).eq('trade_id', pid).execute()
                    except Exception as db_e:
                        log(f'SYNC_ERROR 업데이트 실패 (trade_id={pid}): {db_e}', 'ERROR')
            send_telegram(
                f'⚠️ <b>{name} 매도 불가 (수량 없음)</b>\n'
                f'키움 모의계좌에 {total_qty}주 없음 → DB 포지션 자동 정리\n'
                f'종목코드: {code}'
            )
            return {'result': 'SYNC_ERROR', 'error': err_str}
        send_telegram(f'❌ <b>{name} 매도 주문 실패</b>\n{e}')
        return {'result': 'ORDER_FAILED', 'error': err_str}

    # ── DB 업데이트 (주문 성공 후에만) ──
    for p in positions:
        pid = p.get('trade_id')
        if pid is not None:
            try:
                supabase.table('trade_executions').update({
                    'result': 'CLOSED',
                    'entry_price': avg_entry,  # 평균 진입가 기록
                    'price': price,
                    'pnl_pct': pnl_pct,
                    'reason': f'{reason_prefix}{signal.get("reason", "")}' if isinstance(signal, dict) else reason_prefix,
                }).eq('trade_id', pid).execute()
            except Exception as e:
                log(f'DB 업데이트 실패 (trade_id={pid}): {e}', 'ERROR')

    # 매도 기록도 별도 저장
    try:
        sell_record: dict = {
            'trade_type': 'SELL',
            'stock_code': code,
            'stock_name': name,
            'quantity': total_qty,
            'price': price,
            'entry_price': avg_entry,
            'strategy': 'SELL',
            'reason': f'{reason_prefix}{signal.get("reason", "")}' if isinstance(signal, dict) else reason_prefix,
            'result': 'CLOSED',
            'pnl_pct': pnl_pct,
            'composite_score': signal.get('confidence', 0.0) if isinstance(signal, dict) else 0.0,
            'drift_status': signal.get('drift_status', '') if isinstance(signal, dict) else '',
            'drift_penalty': signal.get('drift_penalty', 0.0) if isinstance(signal, dict) else 0.0,
        }
        try:
            supabase.table('trade_executions').insert(sell_record).execute()
        except Exception as col_err:
            if 'drift_penalty' in str(col_err) or 'drift_status' in str(col_err):
                # drift_penalty/drift_status 컬럼 미생성 시 해당 필드 제외 후 재시도
                sell_record.pop('drift_penalty', None)
                sell_record.pop('drift_status', None)
                supabase.table('trade_executions').insert(sell_record).execute()
            else:
                raise
    except Exception as e:
        log(f'{name} 매도 기록 저장 실패: {e}', 'ERROR')

    # ── 알림 ──
    emoji = '✅' if pnl_pct > 0 else '🛑'
    send_telegram(
        f"{emoji} <b>{name} 매도</b>\n"
        f"💰 {price:,.0f}원 × {total_qty}주\n"
        f"📊 평균단가: {avg_entry:,.0f}원\n"
        f"📈 수익률(비용 포함): {pnl_pct:+.2f}% ({pnl_krw:+,.0f}원)\n"
        f"📝 {reason_prefix}{signal.get('reason', '') if isinstance(signal, dict) else ''}\n"
        f"⚠️ 모의투자"
    )
    if _sheets_append:
        try:
            action = "손절" if pnl_pct < -2 else "익절" if pnl_pct > 2 else "매도"
            reason = f"{reason_prefix}{signal.get('reason', '') if isinstance(signal, dict) else ''}"
            _sheets_append("kr", action, code, price, total_qty, pnl_pct, reason)
        except Exception as e:
            _log.debug(f'{name} sheets 매도 기록 실패: {e}')

    return {
        'result': 'SELL',
        'pnl_pct': pnl_pct,
        'pnl_krw': pnl_krw,
        'quantity': total_qty,
    }


def execute_trade(
    stock: dict,
    signal: dict,
    indicators: dict,
    kospi: dict = None,
    weekly: dict = None,
) -> dict:
    """매매 실행 라우터"""
    action = signal.get('action', 'HOLD')

    if action == 'BUY':
        return execute_buy(stock, signal, indicators, kospi, weekly)
    elif action == 'SELL':
        return execute_sell(stock, signal, indicators)
    else:
        return {'result': 'HOLD'}


# ─────────────────────────────────────────────
# 손절/익절 자동 체크
# ─────────────────────────────────────────────
def check_stop_loss_take_profit():
    """1분마다 실행: 손절/익절/트레일링 스탑"""
    positions = get_open_positions()
    if not positions:
        return

    from collections import defaultdict
    by_code = defaultdict(list)
    for p in positions:
        code = p.get('stock_code')
        if code:
            by_code[code].append(p)

    for code, trades in by_code.items():
        try:
            name = trades[0].get('stock_name', code)
            total_qty = sum(int(t.get('quantity', 0)) for t in trades)
            total_cost = sum(float(t.get('price', 0)) * int(t.get('quantity', 0)) for t in trades)
            avg_entry = total_cost / total_qty if total_qty > 0 else 0

            price = get_current_price(code)
            if price <= 0 or avg_entry <= 0:
                continue

            # 비용 차감
            fee_cost = RISK['fee_buy'] + RISK['fee_sell'] + RISK['tax_sell']
            raw_pnl_pct = (price - avg_entry) / avg_entry
            net_pnl_pct = raw_pnl_pct - fee_cost

            # ── 고점 갱신 ──
            current_highest = max(float(t.get('highest_price') or 0) for t in trades)
            if price > current_highest:
                current_highest = price
                for t in trades:
                    tid = t.get('trade_id')
                    if tid is None:
                        continue
                    try:
                        supabase.table('trade_executions').update(
                            {'highest_price': price}
                        ).eq('trade_id', tid).execute()
                    except Exception as e:
                        log(f'highest_price 업데이트 실패({code}, trade_id={tid}): {e}', 'WARN')

            # ── 적응형 트레일링 스탑 체크 ──
            trailing_activate = RISK.get('trailing_activate', 0.01)
            if current_highest > 0 and net_pnl_pct > trailing_activate:
                drop_from_high = (current_highest - price) / current_highest
                if RISK.get('trailing_adaptive'):
                    if net_pnl_pct >= 0.06:
                        trail_pct = 0.01    # 6%+ 수익: 1% 트레일링
                    elif net_pnl_pct >= 0.04:
                        trail_pct = 0.012   # 4-6% 수익: 1.2%
                    else:
                        trail_pct = RISK.get('trailing_stop', 0.015)
                else:
                    trail_pct = RISK.get('trailing_stop', 0.015)
                if drop_from_high >= trail_pct:
                    trail_pnl = (price - avg_entry) / avg_entry * 100
                    log(
                        f'{name} 트레일링 스탑 발동: 고점 {current_highest:,.0f} → 현재 {price:,.0f} '
                        f'(하락 {drop_from_high*100:.1f}%, 수익 {trail_pnl:.1f}%)',
                        'TRADE',
                    )
                    execute_sell(
                        {'code': code, 'name': name},
                        {},
                        {'price': price},
                        reason_prefix=(
                            f'📉 트레일링스탑(고점 대비 -{drop_from_high*100:.1f}%, '
                            f'수익 {trail_pnl:.1f}%): '
                        ),
                    )
                    time.sleep(0.3)
                    continue

            # ── 손절 ──
            if net_pnl_pct <= RISK['stop_loss']:
                log(f'{name} 손절: {net_pnl_pct*100:.2f}%', 'TRADE')
                execute_sell(
                    {'code': code, 'name': name},
                    {},
                    {'price': price},
                    reason_prefix=f'🛑 손절({net_pnl_pct*100:.2f}%): ',
                )
                time.sleep(0.3)
                continue

            # ── 부분 익절: 5% 이상 수익 시 50% 매도 ──
            partial_tp = RISK.get('partial_tp_pct', 0.05)
            if net_pnl_pct >= partial_tp:
                already_partial = any(t.get('partial_sold') for t in trades)
                if not already_partial and total_qty >= 2:
                    sell_qty = max(1, int(total_qty * RISK.get('partial_tp_ratio', 0.50)))
                    log(f'{name} 부분 익절: {net_pnl_pct*100:.2f}%, {sell_qty}주 매도', 'TRADE')
                    try:
                        kiwoom.place_order(stock_code=code, order_type='sell', quantity=sell_qty, price=0)
                        remaining_qty = total_qty - sell_qty
                        for t in trades:
                            tid = t.get('trade_id')
                            if not tid:
                                continue
                            t_qty = int(t.get('quantity', 0))
                            proportion = t_qty / total_qty if total_qty > 0 else 0
                            t_sold = round(sell_qty * proportion)
                            new_qty = max(0, t_qty - t_sold)
                            supabase.table('trade_executions').update({
                                'partial_sold': True,
                                'quantity': new_qty,
                                'partial_sell_qty': t_sold,
                                'partial_sell_price': price,
                            }).eq('trade_id', tid).execute()
                        send_telegram(
                            f'🟡 <b>{name} 부분 익절 ({int(RISK.get("partial_tp_ratio",0.5)*100)}%)</b>\n'
                            f'수익: +{net_pnl_pct*100:.2f}% | {sell_qty}주 매도\n'
                            f'잔여 {remaining_qty}주 트레일링 보호'
                        )
                    except ConnectionError as e:
                        log(f'{name} 부분 익절 매도 연결 실패: {e}', 'ERROR')
                    except TimeoutError as e:
                        log(f'{name} 부분 익절 매도 타임아웃: {e}', 'ERROR')
                    except ValueError as e:
                        log(f'{name} 부분 익절 매도 파라미터 오류: {e}', 'ERROR')
                    except Exception as e:
                        log(f'{name} 부분 익절 매도 실패: {e}', 'ERROR')
                    time.sleep(0.3)
                    continue

            # ── 최대 익절 ──
            if net_pnl_pct >= RISK['take_profit']:
                log(f'{name} 최대 익절: {net_pnl_pct*100:.2f}%', 'TRADE')
                execute_sell(
                    {'code': code, 'name': name},
                    {},
                    {'price': price},
                    reason_prefix=f'🎯 최대익절({net_pnl_pct*100:.2f}%): ',
                )
                time.sleep(0.3)
                continue

            # 타임컷: 5일 이상 보유 + 수익 거의 없음
            try:
                oldest_buy = min(
                    datetime.fromisoformat(
                        (t.get('created_at') or '2000-01-01T00:00:00')
                        .replace('Z', '')
                        .replace('+00:00', '')[:19]
                    )
                    for t in trades
                )
                holding_days = (datetime.now(timezone.utc) - oldest_buy).days
            except Exception as e:
                log(f"holding_days calculation failed: {e}", 'WARN')
                holding_days = 0

            if holding_days >= 5 and net_pnl_pct < 0.01:
                log(f'{name} 타임컷: {holding_days}일 보유, 수익 {net_pnl_pct*100:.2f}%', 'TRADE')
                execute_sell(
                    {'code': code, 'name': name},
                    {},
                    {'price': price},
                    reason_prefix=f'⏰ 타임컷({holding_days}일, {net_pnl_pct*100:.2f}%): ',
                )
                time.sleep(0.3)
                continue

            time.sleep(0.3)

        except Exception as e:
            log(f'손절/익절/트레일링 체크 실패 {code}: {e}', 'ERROR')


# ─────────────────────────────────────────────
# 메인 사이클
# ─────────────────────────────────────────────
def _get_regime_factor_adj() -> dict:
    """레짐별 KR 팩터 가중치 조정값 반환 (Phase 3-B).

    RISK_OFF: value/quality ↑, momentum ↓
    RISK_ON:  momentum ↑, value ↓
    Returns dict with 'momentum_mult', 'value_mult', 'quality_mult'
    """
    defaults = {"momentum_mult": 1.0, "value_mult": 1.0, "quality_mult": 1.0, "regime": "UNKNOWN"}
    try:
        import sys as _sys
        _WORKSPACE_ROOT = str(Path(__file__).resolve().parents[1])
        if _WORKSPACE_ROOT not in _sys.path:
            _sys.path.insert(0, _WORKSPACE_ROOT)
        from agents.regime_classifier import RegimeClassifier
        result = RegimeClassifier().classify()
        regime = result.get("regime", "TRANSITION")
        adj = {
            "RISK_ON":     {"momentum_mult": 1.30, "value_mult": 0.80, "quality_mult": 1.00},
            "TRANSITION":  {"momentum_mult": 1.00, "value_mult": 1.00, "quality_mult": 1.00},
            "RISK_OFF":    {"momentum_mult": 0.70, "value_mult": 1.30, "quality_mult": 1.30},
            "CRISIS":      {"momentum_mult": 0.40, "value_mult": 1.50, "quality_mult": 1.50},
        }.get(regime, defaults)
        adj["regime"] = regime
        log(
            f"KR 레짐 적응형 가중치: {regime} "
            f"(mom×{adj['momentum_mult']} val×{adj['value_mult']})"
        )
        return adj
    except Exception as e:
        log(f'레짐 조회 실패 (기본값 사용): {e}', 'WARN')
        return defaults


# 레짐 가중치 캐시 (사이클 내 재사용)
_regime_adj_cache: dict = {}


def run_trading_cycle():
    global _cache, _dart_cache, _regime_adj_cache, _kr_buy_blocked, _kr_drift_cache
    _cache = {}
    _dart_cache = {}
    _kr_drift_cache = {}
    _kr_buy_blocked = False
    _regime_adj_cache = _get_regime_factor_adj()  # 레짐별 팩터 가중치 사이클 초기 로드

    global supabase
    try:
        supabase = get_supabase()
        if not supabase:
            log('Supabase 미연결 — 이번 사이클 스킵', 'WARN')
            return
        supabase.table('trade_executions').select('trade_id').limit(1).execute()
    except Exception as e:
        log(f'Supabase 쿼리 실패, 재연결 시도: {e}', 'WARN')
        _reset_client()
        return

    # STOP 플래그 체크 (텔레그램 /stop 명령으로 생성)
    stop_flag = Path(__file__).parent / 'STOP_TRADING'
    if stop_flag.exists():
        log('⛔ STOP_TRADING 플래그 감지 → 매매 사이클 스킵', 'WARN')
        send_telegram('⛔ STOP_TRADING 플래그 감지 → 이번 사이클 스킵됨\n/resume 으로 재개')
        return

    if not is_market_open():
        log('장 외 시간 — 스킵')
        return

    log('=' * 50)
    log('주식 매매 사이클 시작')

    try:
        account = kiwoom.get_account_evaluation()
        summary = account.get('summary', {})
        account_equity = float(
            summary.get('estimated_asset', 0)
            or summary.get('total_asset', 0)
            or summary.get('deposit', 0)
            or 0
        )
        if account_equity > 0:
            append_equity_snapshot('kr', account_equity, {"source": "kiwoom_summary"})
            tw = get_effective_market_weight('KR')
            if tw is not None:
                log(f'리밸런싱 목표 비중(KR): {tw:.1%}')
            drift = _load_kr_ml_drift_report(force=True)
            if drift:
                log(
                    f"KR ML Drift: {drift.get('status', 'UNKNOWN')} "
                    f"(max_psi={float(drift.get('max_psi', 0.0) or 0.0):.3f})"
                )
    except Exception as e:
        log(f'KR 자산 스냅샷 저장 실패: {e}', 'WARN')

    if _apply_drawdown_guard_kr():
        log('DrawdownGuard FULL_STOP 실행 완료 — 사이클 종료', 'WARN')
        return

    # 일일 손실 한도 체크
    if check_daily_loss():
        log('🚨 일일 손실 한도 초과 — 사이클 스킵', 'WARN')
        return

    # 오늘 신규 매수 건수 한도 체크
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        today_buys = (
            supabase.table('trade_executions')
            .select('trade_id')
            .eq('trade_type', 'BUY')
            .gte('created_at', today)
            .execute()
            .data
            or []
        )
        if len(today_buys) >= RISK['max_trades_per_day']:
            log('오늘 매수 한도 도달 — 사이클 스킵', 'WARN')
            return
    except Exception as e:
        log(f'오늘 매수 건수 조회 실패: {e}', 'WARN')

    # 보유 포지션 손절/익절 먼저 체크
    check_stop_loss_take_profit()

    # 전략 로드 + WATCHLIST 전체 51개 병합 (2단계 커버리지)
    strategy = get_today_strategy()
    try:
        from stock_premarket import WATCHLIST as _WL
        watchlist_all = [{'code': w['code'], 'name': w['name']} for w in _WL]
    except Exception as e:
        log(f"import fallback to DB: {e}", 'WARN')
        watchlist_all = get_watchlist_from_db()

    if strategy:
        log(f"장 전 전략 로드 완료: {strategy.get('market_outlook', '?')}")
        buy_picks = [p for p in strategy.get('top_picks', []) if p.get('action') == 'BUY']
        watch_picks = [p for p in strategy.get('top_picks', []) if p.get('action') == 'WATCH']
        strategy_targets = [{'code': p['code'], 'name': p['name']} for p in (buy_picks + watch_picks)]
        # 전략 picks 우선, 나머지 WATCHLIST로 보충 (중복 제거)
        strategy_codes = {t['code'] for t in strategy_targets}
        extra = [s for s in watchlist_all if s['code'] not in strategy_codes]
        targets = strategy_targets + extra
        log(f"분석 대상: 전략 {len(strategy_targets)}개 + WATCHLIST {len(extra)}개 = 총 {len(targets)}개")
    else:
        log('장 전 전략 없음 → WATCHLIST 전체로 룰 기반 매매', 'WARN')
        targets = watchlist_all

    if not targets:
        log('분석 대상 종목 없음')
        return

    # 코스피 심리
    kospi = get_kospi_sentiment()
    log(f'코스피 심리: {kospi["msg"]}')

    # 보유 종목도 SELL 체크에 포함
    open_positions = get_open_positions()
    open_codes = list(set(p['stock_code'] for p in open_positions))
    # 보유 중이지만 targets에 없는 종목 추가
    for code in open_codes:
        if not any(t['code'] == code for t in targets):
            name = next(
                (p.get('stock_name', code) for p in open_positions if p['stock_code'] == code),
                code,
            )
            targets.append({'code': code, 'name': name})

    # 모멘텀 스코어 기반 정렬 (상위 종목 우선 분석)
    scored_targets = []
    for stock in targets:
        m = calc_momentum_score(stock['code'])
        scored_targets.append((stock, m))
    scored_targets.sort(key=lambda x: x[1].get('score', 0), reverse=True)
    scored_targets = scored_targets[:30]  # 상위 30개 심층 분석 (전체 51개 중)

    # 종목별 분석 + 매매
    for stock, momentum in scored_targets:
        code = stock['code']
        name = stock['name']
        has_position = code in open_codes

        log(f'')
        log(f'  📊 {name} ({code}) 분석 중... {"[보유중]" if has_position else ""}')

        indicators = get_indicators(code)
        if not indicators:
            log(f'  {name}: 지표 없음 — 스킵', 'WARN')
            continue

        log(
            f"  RSI: {indicators['rsi']} / MACD: {indicators['macd']}({indicators.get('macd_histogram', '?')}) / "
            f"거래량: {indicators.get('vol_label', '?')} / BB: {indicators.get('bb_pos', '?')}% [{indicators.get('data_source', '?')}/{indicators.get('data_points', '?')}봉]"
        )

        # 모멘텀 스코어 로깅 및 D등급 차단
        log(
            f"  모멘텀: {momentum.get('grade', 'F')}({momentum.get('score', 0)}) | "
            f"5일 {momentum.get('ret_5d', 0):+.1f}% | "
            f"거래량 {momentum.get('vol_ratio', 1):.1f}배 | "
            f"신고가 {momentum.get('near_high', 0):.0f}%"
        )
        if momentum.get('grade') == 'D' and not has_position:
            rsi_now = indicators.get('rsi', 50)
            bb_pos_now = indicators.get('bb_pos', 50)
            if rsi_now <= 30 and bb_pos_now <= 10:
                log(f'  {name}: 모멘텀 D등급이지만 RSI={rsi_now} + BB하단({bb_pos_now}%) — 예외 허용')
            else:
                log(f'  {name}: 모멘텀 D등급 — BUY 차단')
                continue

        weekly = get_weekly_trend(code)
        log(f'  주봉 추세: {weekly.get("trend", "?")}')

        # DART 재무 스코어 (v3 신규)
        dart = _get_dart_score(code)
        if dart.get('grade') != 'N/A':
            log(
                f"  재무: {dart['grade']}({dart['score']}) | {dart.get('detail', '?')}",
                'INFO',
            )

        news = get_stock_news(name)

        # 수급 데이터 (외국인/기관) — Kiwoom ka10007 우선, KRX fallback
        supply = kiwoom.get_investor_trend(code)
        if not supply:
            supply = get_investor_trend_krx(code)
        foreign_net = supply.get('foreign_net', 0)
        inst_net = supply.get('inst_net', 0)
        if foreign_net or inst_net:
            log(
                f'  수급: 외국인 {foreign_net:+,}주 / 기관 {inst_net:+,}주',
                'INFO',
            )

        signal = get_trading_signal(
            stock, indicators, strategy, news, weekly, kospi, has_position, supply
        )
        log(
            f"  신호: {signal['action']} ({signal.get('confidence', 0)}%) "
            f"[{signal.get('source', '?')}] — {signal.get('reason', '')}"
        )

        result = execute_trade(stock, signal, indicators, kospi=kospi, weekly=weekly)
        log(f"  결과: {result['result']}")

        time.sleep(1.2)  # 키움 429 완화

    log('주식 매매 사이클 완료')
    log('=' * 50)


# ─────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'check':
        if is_market_open():
            log('주식 1분 손절/익절 체크')
            check_stop_loss_take_profit()
        else:
            log('장 외 시간 — 스킵')
    elif len(sys.argv) > 1 and sys.argv[1] == 'status':
        # 현재 포지션 상태 출력
        positions = get_open_positions()
        if not positions:
            log('열린 포지션 없음')
        else:
            from collections import defaultdict
            by_code = defaultdict(list)
            for p in positions:
                by_code[p['stock_code']].append(p)
            for code, pos_list in by_code.items():
                name = pos_list[0].get('stock_name', code)
                avg = calc_avg_entry_price(pos_list)
                qty = sum(int(p['quantity']) for p in pos_list)
                cur = get_current_price(code)
                chg = ((cur - avg) / avg * 100) if avg and cur else 0
                log(f'  {name}: {qty}주 × 평단 {avg:,.0f}원 → 현재 {cur:,.0f}원 ({chg:+.2f}%)')
    else:
        run_trading_cycle()
