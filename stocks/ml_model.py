#!/usr/bin/env python3
"""
주식 매매 ML 모델 v1.0 — KR 종목 ML 신호 모듈

XGBoost 분류 모델 (룰/AI 보조용 60/40 블렌딩 컴포넌트):
- 입력: 기술적 지표 + 가격/거래량 특성
- 출력: 3일 내 +2% 이상 상승 확률

사용법:
    python3 stocks/ml_model.py train          # 모델 학습
    python3 stocks/ml_model.py evaluate       # 성과 평가(=train)
    python3 stocks/ml_model.py predict 005930 # 특정 종목 예측
    python3 stocks/ml_model.py predict_all    # 전체 종목 예측

콜드스타트 정책:
- 학습 트리거: 평일 08:30 retrain (trade_executions ≥ 50건일 때)
- 50건 미달 시: ensemble_meta.json 미생성 → 호출자가 ML 가드 차단 → AI/rule 단독 동작
- get_ml_signal() 자체는 항상 호출 가능하지만 호출자가 MODEL_DIR/ensemble_meta.json
  존재 여부로 결과 채택 여부 결정 (stock_trading_agent.py:1121).

설계 근거: quant/CLAUDE.md '주간 자동화 루프' 섹션 참조.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np

_WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))
from common.env_loader import load_env
from common.logger import get_logger

load_env()
log = get_logger(__name__)

from supabase import create_client  # noqa: E402

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_SECRET_KEY', '')
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

ROOT_DIR = _WORKSPACE_ROOT
MODEL_DIR = ROOT_DIR / 'brain' / 'ml'
MODEL_DIR.mkdir(parents=True, exist_ok=True)
FEATURE_NAMES = [
    'rsi_14',
    'rsi_7',
    'macd',
    'macd_histogram',
    'macd_signal',
    'bb_pos',
    'bb_width_pct',
    'vol_ratio_5',
    'vol_ratio_20',
    'return_1d',
    'return_3d',
    'return_5d',
    'return_10d',
    'return_20d',
    'high_low_range',
    'close_vs_ma5',
    'close_vs_ma20',
    'close_vs_ma60',
    'atr_14',
    'volume_trend',
]

# 활성 팩터 피처 — 실데이터 수집 파이프라인 없으면 비워둠
FACTOR_FEATURES: list[str] = []

MARKET_FEATURES = [
    'kospi_rsi_14',       # yfinance 코스피 RSI
    'kospi_return_5d',    # yfinance 코스피 5일 수익률
    'vix_level',          # yfinance VIX
    # 'fg_index',         # 공포탐욕지수 미수집 → 비활성
    # 'regime_encoded',   # 레짐 인코딩 미수집 → 비활성
]

SUPPLY_FEATURES = [
    # 'foreign_net_buy_5d',  # KRX API 미수집 → 비활성
    # 'inst_net_buy_5d',     # KRX API 미수집 → 비활성
    # 'short_interest_ratio',# 공매도 미수집 → 비활성
    # 'days_to_earnings',    # check_earnings_proximity 실패 시 항상 30.0 → 비활성
    # 'turnover_ratio',      # market_cap 미수집 시 0.0 → _DISABLED로 이동
    # 'market_cap_log',      # market_cap 미수집 시 0.0 → _DISABLED로 이동
    'sector_momentum_rank',          # ret_20d 기반 섹터 내 상대 순위 (가격 데이터만 필요)
    'relative_strength_vs_kospi',    # 개별종목 20일 수익률 - 코스피 5일 수익률
    'avg_spread_bps',                # 틱 사이즈 기반 호가 스프레드 추정
    '52w_high_proximity',            # 52주 고점 대비 현재가 비율
]

FEATURE_NAMES.extend(FACTOR_FEATURES + MARKET_FEATURES + SUPPLY_FEATURES)

HORIZON_CONFIGS = {
    '1d': {'target_days': 1, 'target_return': 0.01, 'label': 'short'},
    '3d': {'target_days': 3, 'target_return': 0.02, 'label': 'mid'},
    '10d': {'target_days': 10, 'target_return': 0.05, 'label': 'swing'},
}


def _horizon_dir(horizon_key: str) -> Path:
    hk = str(horizon_key).lower().replace('horizon_', '').replace('d', '') + 'd'
    if hk == '3d':
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        return MODEL_DIR
    path = MODEL_DIR / f'horizon_{hk}'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _horizon_paths(horizon_key: str) -> dict:
    base = _horizon_dir(horizon_key)
    return {
        'dir': base,
        'xgb': base / 'xgb_model.ubj',
        'lgbm': base / 'lgbm_model.txt',
        'catboost': base / 'catboost_model.cbm',
        'meta_model': base / 'meta_model.pkl',
        'meta_json': base / 'ensemble_meta.json',
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat() + 'Z'


# ─────────────────────────────────────────────
# 피처 계산
# ─────────────────────────────────────────────
def calc_ema(data, period):
    if len(data) < period:
        return data[-1] if data else 0
    k = 2 / (period + 1)
    e = data[0]
    for d in data[1:]:
        e = d * k + e * (1 - k)
    return e


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0
    trs = []
    for i in range(-period, 0):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs) / period


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _tick_size_kr(price: float) -> float:
    px = _safe_float(price, 0.0)
    if px <= 0:
        return 0.0
    if px < 2000:
        return 1.0
    if px < 5000:
        return 5.0
    if px < 20000:
        return 10.0
    if px < 50000:
        return 50.0
    if px < 200000:
        return 100.0
    if px < 500000:
        return 500.0
    return 1000.0


class MLFeatureContext:
    def __init__(self):
        self.factor_ctx = None
        self.market_cache = {}
        self.symbol_info_cache = {}
        self.flow_cache = {}
        self.sector_rank_cache = {}
        self.market_history = None
        self.earnings_cache = {}

    def _get_factor_ctx(self):
        if self.factor_ctx is None:
            workspace_root = str(Path(__file__).resolve().parents[1])
            if workspace_root not in sys.path:
                sys.path.insert(0, workspace_root)
            from quant.factors.registry import FactorContext
            self.factor_ctx = FactorContext()
        return self.factor_ctx

    def get_factor_features(self, stock_code: str, as_of_date: str) -> dict:
        workspace_root = str(Path(__file__).resolve().parents[1])
        if workspace_root not in sys.path:
            sys.path.insert(0, workspace_root)
        from quant.factors.registry import calc_all

        return calc_all(
            as_of=as_of_date,
            symbol=stock_code,
            market='kr',
            factor_names=FACTOR_FEATURES + ['fg_index'],
            context=self._get_factor_ctx(),
        )

    def get_market_features(self, as_of_date: str) -> dict:
        key = str(as_of_date)
        cached = self.market_cache.get(key)
        if cached is not None:
            return cached

        out = self._market_features_from_history(key)

        self.market_cache[key] = out
        return out

    def _load_market_history(self):
        if self.market_history is not None:
            return self.market_history

        history = {'kospi': {}, 'vix': {}, 'fg_index': 50.0, 'regime_encoded': 2.0}
        try:
            import yfinance as yf
            kospi = yf.Ticker('^KS11').history(period='10y')
            if kospi is not None and not kospi.empty and 'Close' in kospi:
                closes = [float(v) for v in kospi['Close']]
                dates = [str(idx.date()) for idx in kospi.index]
                for i, day in enumerate(dates):
                    window = closes[: i + 1]
                    rsi = calc_rsi(window, 14) if len(window) >= 15 else 50.0
                    ret5 = (window[-1] / window[-6] - 1.0) * 100.0 if len(window) >= 6 and window[-6] > 0 else 0.0
                    history['kospi'][day] = {'kospi_rsi_14': rsi, 'kospi_return_5d': ret5}
            vix = yf.Ticker('^VIX').history(period='10y')
            if vix is not None and not vix.empty and 'Close' in vix:
                for idx, val in vix['Close'].items():
                    history['vix'][str(idx.date())] = float(val)
        except Exception as e:
            log.debug(f'시장 히스토리 로드 실패: {e}')

        try:
            workspace_root = str(Path(__file__).resolve().parents[1])
            if workspace_root not in sys.path:
                sys.path.insert(0, workspace_root)
            from quant.factors.registry import calc
            history['fg_index'] = calc('fg_index', as_of=datetime.now(timezone.utc).date().isoformat(), symbol='005930', market='kr', context=self._get_factor_ctx())
        except Exception as e:
            log.debug(f'fg_index 로드 실패: {e}')

        try:
            workspace_root = str(Path(__file__).resolve().parents[1])
            if workspace_root not in sys.path:
                sys.path.insert(0, workspace_root)
            from agents.regime_classifier import RegimeClassifier
            regime = RegimeClassifier().classify().get('regime', 'TRANSITION')
            history['regime_encoded'] = {
                'CRISIS': 0.0,
                'RISK_OFF': 1.0,
                'TRANSITION': 2.0,
                'RISK_ON': 3.0,
            }.get(regime, 2.0)
        except Exception as e:
            log.debug(f'레짐 인코딩 로드 실패: {e}')

        self.market_history = history
        return history

    def _market_features_from_history(self, as_of_date: str) -> dict:
        history = self._load_market_history()
        out = {
            'kospi_rsi_14': 50.0,
            'kospi_return_5d': 0.0,
            'vix_level': 20.0,
            'fg_index': _safe_float(history.get('fg_index'), 50.0),
            'regime_encoded': _safe_float(history.get('regime_encoded'), 2.0),
        }
        kospi_hist = history.get('kospi') or {}
        if as_of_date in kospi_hist:
            out.update(kospi_hist[as_of_date])
        elif kospi_hist:
            eligible = [d for d in kospi_hist.keys() if d <= as_of_date]
            if eligible:
                out.update(kospi_hist[max(eligible)])
        vix_hist = history.get('vix') or {}
        if as_of_date in vix_hist:
            out['vix_level'] = _safe_float(vix_hist[as_of_date], 20.0)
        elif vix_hist:
            eligible = [d for d in vix_hist.keys() if d <= as_of_date]
            if eligible:
                out['vix_level'] = _safe_float(vix_hist[max(eligible)], 20.0)
        return out

    def get_symbol_info(self, stock_code: str) -> dict:
        code = str(stock_code).strip()
        cached = self.symbol_info_cache.get(code)
        if cached is not None:
            return cached
        info = {}
        try:
            import yfinance as yf
            info = yf.Ticker(f'{code}.KS').info or {}
        except Exception as e:
            log.debug(f'yfinance 종목 정보 조회 실패: {e}')
            info = {}
        self.symbol_info_cache[code] = info
        return info

    def get_flow_5d(self, stock_code: str) -> dict:
        code = str(stock_code).strip()
        cached = self.flow_cache.get(code)
        if cached is not None:
            return cached
        out = {'foreign_net_buy_5d': 0.0, 'inst_net_buy_5d': 0.0}
        try:
            rows = (
                supabase.table('investor_flows')
                .select('date,foreign_net,inst_net,institution_net,stock_code')
                .eq('stock_code', code)
                .order('date', desc=False)
                .limit(30)
                .execute()
                .data
                or []
            )
            recent = rows[-5:]
            out['foreign_net_buy_5d'] = sum(_safe_float(r.get('foreign_net'), 0.0) for r in recent)
            out['inst_net_buy_5d'] = sum(_safe_float(r.get('inst_net', r.get('institution_net')), 0.0) for r in recent)
        except Exception as e:
            log.debug(f'investor_flows 조회 실패: {e}')
        self.flow_cache[code] = out
        return out

    def get_sector_momentum_rank(self, stock_code: str, return_20d: float) -> float:
        code = str(stock_code).strip()
        info = self.get_symbol_info(code)
        sector = str(info.get('sector') or '')
        if not sector:
            return 0.5
        cached = self.sector_rank_cache.get(sector)
        if cached is not None and code in cached:
            return cached[code]

        ranks = {}
        try:
            stocks = (
                supabase.table('top50_stocks')
                .select('stock_code')
                .execute()
                .data
                or []
            )
            pairs = []
            for row in stocks:
                peer_code = str(row.get('stock_code') or '').strip()
                if not peer_code:
                    continue
                peer_info = self.get_symbol_info(peer_code)
                if str(peer_info.get('sector') or '') != sector:
                    continue
                peer_rows = (
                    supabase.table('daily_ohlcv')
                    .select('close_price,date')
                    .eq('stock_code', peer_code)
                    .order('date', desc=False)
                    .limit(40)
                    .execute()
                    .data
                    or []
                )
                if len(peer_rows) < 21:
                    continue
                closes = [float(r['close_price']) for r in peer_rows]
                ret20 = (closes[-1] / closes[-21] - 1.0) if closes[-21] > 0 else 0.0
                pairs.append((peer_code, ret20))
            if pairs:
                pairs.sort(key=lambda x: x[1])
                n = max(len(pairs) - 1, 1)
                for idx, (peer_code, _) in enumerate(pairs):
                    ranks[peer_code] = idx / n
        except Exception as e:
            log.debug(f'섹터 모멘텀 순위 계산 실패: {e}')
        self.sector_rank_cache[sector] = ranks
        return ranks.get(code, 0.5)


_ML_FEATURE_CTX = MLFeatureContext()


def _compute_extra_features(stock_code: str, as_of_date: str, closes, volumes, highs, lows, idx):
    price = closes[idx]
    factor_vals = _ML_FEATURE_CTX.get_factor_features(stock_code, as_of_date)
    market_vals = _ML_FEATURE_CTX.get_market_features(as_of_date)
    flow_vals = _ML_FEATURE_CTX.get_flow_5d(stock_code)
    info = _ML_FEATURE_CTX.get_symbol_info(stock_code)

    ret_20d = (closes[idx] / closes[idx - 20] - 1.0) * 100.0 if idx >= 20 and closes[idx - 20] > 0 else 0.0
    kospi_ret_5d = _safe_float(market_vals.get('kospi_return_5d'), 0.0)
    relative_strength = ret_20d - kospi_ret_5d
    high_252 = max(closes[max(0, idx - 251): idx + 1]) if idx >= 1 else price
    proximity_52w = (price / high_252) if high_252 > 0 else 0.0
    market_cap = _safe_float(info.get('marketCap'), 0.0)
    turnover_ratio = (volumes[idx] * price / market_cap) if market_cap > 0 else 0.0
    avg_spread_bps = (_tick_size_kr(price) / price * 10000.0) if price > 0 else 0.0
    short_interest = _safe_float(info.get('sharesShortPriorMonth') or info.get('sharesShort'), 0.0)
    shares_float = _safe_float(info.get('floatShares') or info.get('sharesOutstanding'), 0.0)
    short_interest_ratio = (short_interest / shares_float * 100.0) if shares_float > 0 else 0.0
    market_cap_log = np.log1p(max(market_cap, 0.0)) if market_cap > 0 else 0.0

    days_to_earnings = _ML_FEATURE_CTX.earnings_cache.get(stock_code)
    if days_to_earnings is None:
        days_to_earnings = 30.0
        try:
            workspace_root = str(Path(__file__).resolve().parents[1])
            if workspace_root not in sys.path:
                sys.path.insert(0, workspace_root)
            from common.market_data import check_earnings_proximity
            earnings = check_earnings_proximity(f'{stock_code}.KS', days=30)
            dte = earnings.get('days_to_earnings')
            if dte is not None:
                days_to_earnings = float(max(0, min(int(dte), 30)))
        except Exception as e:
            log.debug(f'실적 일정 조회 실패: {e}')
        _ML_FEATURE_CTX.earnings_cache[stock_code] = days_to_earnings

    sector_rank = _ML_FEATURE_CTX.get_sector_momentum_rank(stock_code, ret_20d)

    extras = {
        'momentum_12m': _safe_float(factor_vals.get('momentum_12m'), 0.0),
        'momentum_1m': _safe_float(factor_vals.get('momentum_1m'), 0.0),
        'pe_ratio': _safe_float(factor_vals.get('pe_ratio'), 0.0),
        'pb_ratio': _safe_float(factor_vals.get('pb_ratio'), 0.0),
        'roe': _safe_float(factor_vals.get('roe'), 0.0),
        'debt_ratio': _safe_float(factor_vals.get('debt_ratio'), 0.0),
        'revenue_growth': _safe_float(factor_vals.get('revenue_growth'), 0.0),
        'earnings_surprise': _safe_float(factor_vals.get('earnings_surprise'), 0.0),
        'volume_ratio_20d': _safe_float(factor_vals.get('volume_ratio_20d'), 1.0),
        'orderbook_imbalance': _safe_float(factor_vals.get('orderbook_imbalance'), 0.0),
        'kospi_rsi_14': _safe_float(market_vals.get('kospi_rsi_14'), 50.0),
        'kospi_return_5d': _safe_float(market_vals.get('kospi_return_5d'), 0.0),
        'vix_level': _safe_float(market_vals.get('vix_level'), 20.0),
        'fg_index': _safe_float(market_vals.get('fg_index'), 50.0),
        'regime_encoded': _safe_float(market_vals.get('regime_encoded'), 2.0),
        'foreign_net_buy_5d': _safe_float(flow_vals.get('foreign_net_buy_5d'), 0.0),
        'inst_net_buy_5d': _safe_float(flow_vals.get('inst_net_buy_5d'), 0.0),
        'short_interest_ratio': short_interest_ratio,
        'days_to_earnings': days_to_earnings,
        'sector_momentum_rank': sector_rank,
        'relative_strength_vs_kospi': relative_strength,
        'avg_spread_bps': avg_spread_bps,
        'turnover_ratio': turnover_ratio * 100.0,
        '52w_high_proximity': proximity_52w,
        'market_cap_log': market_cap_log,
    }
    return [extras[name] for name in FACTOR_FEATURES + MARKET_FEATURES + SUPPLY_FEATURES]


def extract_features(closes, volumes, highs, lows, idx, stock_code: str | None = None, as_of_date: str | None = None):
    """특정 시점(idx)에서 피처 벡터 추출"""
    if idx < 60:  # 최소 60일 필요
        return None

    c = closes[: idx + 1]
    v = volumes[: idx + 1]
    h = highs[: idx + 1]
    l = lows[: idx + 1]

    price = c[-1]
    if price <= 0:
        return None

    # RSI
    rsi_14 = calc_rsi(c, 14)
    rsi_7 = calc_rsi(c, 7)

    # MACD
    ema12 = calc_ema(c, 12)
    ema26 = calc_ema(c, 26)
    macd = ema12 - ema26

    macd_line = []
    for i in range(26, len(c)):
        e12 = calc_ema(c[: i + 1], 12)
        e26 = calc_ema(c[: i + 1], 26)
        macd_line.append(e12 - e26)
    macd_sig = calc_ema(macd_line, 9) if len(macd_line) >= 9 else macd
    macd_hist = macd - macd_sig

    # 볼린저 밴드
    ma20 = sum(c[-20:]) / 20
    std20 = (sum((x - ma20) ** 2 for x in c[-20:]) / 20) ** 0.5
    bb_upper = ma20 + 2 * std20
    bb_lower = ma20 - 2 * std20
    bb_width = bb_upper - bb_lower
    bb_pos = (price - bb_lower) / bb_width * 100 if bb_width > 0 else 50
    bb_width_pct = bb_width / ma20 * 100 if ma20 > 0 else 0

    # 거래량
    avg_vol_5 = sum(v[-6:-1]) / 5 if len(v) >= 6 else 1
    avg_vol_20 = sum(v[-21:-1]) / 20 if len(v) >= 21 else 1
    vol_ratio_5 = v[-1] / avg_vol_5 if avg_vol_5 > 0 else 1
    vol_ratio_20 = v[-1] / avg_vol_20 if avg_vol_20 > 0 else 1

    # 수익률
    return_1d = (c[-1] / c[-2] - 1) * 100 if len(c) >= 2 else 0
    return_3d = (c[-1] / c[-4] - 1) * 100 if len(c) >= 4 else 0
    return_5d = (c[-1] / c[-6] - 1) * 100 if len(c) >= 6 else 0
    return_10d = (c[-1] / c[-11] - 1) * 100 if len(c) >= 11 else 0
    return_20d = (c[-1] / c[-21] - 1) * 100 if len(c) >= 21 else 0

    # 일일 고저 범위
    high_low_range = (h[-1] - l[-1]) / max(price, 1) * 100

    # 이동평균 대비
    ma5 = sum(c[-5:]) / 5
    ma60 = sum(c[-60:]) / 60 if len(c) >= 60 else ma20
    close_vs_ma5 = (price / ma5 - 1) * 100
    close_vs_ma20 = (price / ma20 - 1) * 100
    close_vs_ma60 = (price / ma60 - 1) * 100

    # ATR
    atr = calc_atr(h, l, c, 14)
    atr_pct = atr / price * 100 if price > 0 else 0

    # 거래량 추세
    vol_trend = avg_vol_5 / max(avg_vol_20, 1)

    # 일부 피처는 가격으로 정규화
    features = [
        rsi_14,
        rsi_7,
        macd / price * 100,
        macd_hist / price * 100,
        macd_sig / price * 100,
        bb_pos,
        bb_width_pct,
        vol_ratio_5,
        vol_ratio_20,
        return_1d,
        return_3d,
        return_5d,
        return_10d,
        return_20d,
        high_low_range,
        close_vs_ma5,
        close_vs_ma20,
        close_vs_ma60,
        atr_pct,
        vol_trend,
    ]

    if stock_code and as_of_date:
        features.extend(
            _compute_extra_features(
                stock_code=stock_code,
                as_of_date=as_of_date,
                closes=closes,
                volumes=volumes,
                highs=highs,
                lows=lows,
                idx=idx,
            )
        )
    else:
        features.extend([0.0] * (len(FACTOR_FEATURES) + len(MARKET_FEATURES) + len(SUPPLY_FEATURES)))

    return features


# ─────────────────────────────────────────────
# 데이터 준비
# ─────────────────────────────────────────────
def load_training_data(target_days=3, target_return=0.02):
    """
    DB에서 학습 데이터 생성

    라벨: target_days일 후 수익률 >= target_return이면 1(매수), 아니면 0(관망)
    """
    if not supabase:
        log.warning('Supabase 미연결')
        return None, None

    stocks = (
        supabase.table('top50_stocks')
        .select('stock_code')
        .execute()
        .data
        or []
    )
    log.info(f'데이터 로드: {len(stocks)}종목')

    all_X = []
    all_y = []

    for s in stocks:
        code = s['stock_code']
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

        closes = [float(r['close_price']) for r in rows]
        volumes = [float(r.get('volume', 0)) for r in rows]
        highs = [float(r.get('high_price', r['close_price'])) for r in rows]
        lows = [float(r.get('low_price', r['close_price'])) for r in rows]

        for i in range(60, len(rows)):
            if i + target_days >= len(closes):
                break  # 미래 데이터 부족 시 중단 (데이터 누출 방지)
            features = extract_features(
                closes, volumes, highs, lows, i,
                stock_code=code,
                as_of_date=rows[i]['date'],
            )
            if features is None:
                continue

            future_return = (closes[i + target_days] - closes[i]) / max(closes[i], 1)
            label = 1 if future_return >= target_return else 0

            all_X.append(features)
            all_y.append(label)

    if not all_X:
        log.warning('학습 데이터 없음')
        return None, None

    X = np.array(all_X, dtype=float)
    y = np.array(all_y, dtype=int)
    buys = int(y.sum())
    log.info(f'학습 데이터: {len(X)}개 샘플 (매수: {buys} / 관망: {len(y) - buys})')
    if len(y) > 0:
        log.info(f'매수 비율: {buys / len(y) * 100:.1f}%')

    return X, y


# ─────────────────────────────────────────────
# 모델 학습
# ─────────────────────────────────────────────
def _build_model(scale_pos_weight: float = 1.0, hpo_params: dict | None = None):
    """XGBClassifier 인스턴스 공통 생성."""
    from xgboost import XGBClassifier
    p = hpo_params or {}
    return XGBClassifier(
        n_estimators=p.get('n_estimators', 200),
        max_depth=p.get('max_depth', 5),
        learning_rate=p.get('learning_rate', 0.05),
        subsample=p.get('subsample', 0.8),
        colsample_bytree=p.get('colsample_bytree', 0.8),
        min_child_weight=p.get('min_child_weight', 1),
        gamma=p.get('gamma', 0),
        reg_alpha=p.get('reg_alpha', 0),
        reg_lambda=p.get('reg_lambda', 1),
        scale_pos_weight=scale_pos_weight,
        eval_metric='logloss',
        random_state=42,
        use_label_encoder=False,
    )


def _build_lgbm_model(scale_pos_weight: float = 1.0, hpo_params: dict | None = None):
    try:
        from lightgbm import LGBMClassifier
    except ImportError:
        return None
    p = hpo_params or {}
    return LGBMClassifier(
        n_estimators=p.get('n_estimators', 300),
        num_leaves=p.get('num_leaves', 31),
        learning_rate=p.get('learning_rate', 0.03),
        subsample=p.get('subsample', 0.8),
        colsample_bytree=p.get('colsample_bytree', 0.8),
        min_child_samples=p.get('min_child_samples', 20),
        reg_alpha=p.get('reg_alpha', 0),
        reg_lambda=p.get('reg_lambda', 1),
        random_state=42,
        class_weight={0: 1.0, 1: max(scale_pos_weight, 1.0)},
        verbose=-1,
    )


def _build_catboost_model(scale_pos_weight: float = 1.0, hpo_params: dict | None = None):
    try:
        from catboost import CatBoostClassifier
    except ImportError:
        return None
    p = hpo_params or {}
    return CatBoostClassifier(
        iterations=p.get('iterations', 300),
        depth=p.get('depth', 6),
        learning_rate=p.get('learning_rate', 0.03),
        l2_leaf_reg=p.get('l2_leaf_reg', 3),
        loss_function='Logloss',
        eval_metric='AUC',
        random_seed=42,
        verbose=False,
        class_weights=[1.0, max(scale_pos_weight, 1.0)],
    )


def _fit_model(model, X_train, y_train, X_valid=None, y_valid=None):
    if model is None:
        return None
    model_name = model.__class__.__name__.lower()
    if 'catboost' in model_name:
        if X_valid is not None and y_valid is not None and len(X_valid) > 0:
            model.fit(X_train, y_train, eval_set=(X_valid, y_valid), verbose=False)
        else:
            model.fit(X_train, y_train, verbose=False)
        return model
    if 'lgbm' in model_name:
        if X_valid is not None and y_valid is not None and len(X_valid) > 0:
            model.fit(X_train, y_train, eval_set=[(X_valid, y_valid)])
        else:
            model.fit(X_train, y_train)
        return model
    if X_valid is not None and y_valid is not None and len(X_valid) > 0:
        model.fit(X_train, y_train, eval_set=[(X_valid, y_valid)], verbose=False)
    else:
        model.fit(X_train, y_train, verbose=False)
    return model


def _predict_proba(model, X):
    if model is None:
        return None
    return model.predict_proba(X)[:, 1]


def _save_meta_model(meta_model, horizon_key: str = '3d') -> None:
    joblib.dump(meta_model, _horizon_paths(horizon_key)['meta_model'])


def _load_meta_model(horizon_key: str = '3d'):
    meta_path = _horizon_paths(horizon_key)['meta_model']
    if not meta_path.exists():
        return None
    try:
        return joblib.load(meta_path)
    except Exception as e:
        log.warning(f"joblib 메타 모델 로드 실패, 제한적 pickle fallback 시도: {e}")
        try:
            resolved_meta_path = meta_path.resolve()
            resolved_model_dir = MODEL_DIR.resolve()
            if resolved_model_dir not in resolved_meta_path.parents or resolved_meta_path.suffix != '.pkl':
                log.warning(f'신뢰되지 않은 메타 모델 경로 거부: {resolved_meta_path}')
                return None
            import pickle
            with open(resolved_meta_path, 'rb') as fp:
                return pickle.load(fp)
        except Exception as e:
            log.debug(f'메타 모델 로드 실패: {e}')
            return None


def _available_base_model_names() -> list[str]:
    names = ['xgb']
    if _build_lgbm_model() is not None:
        names.append('lgbm')
    if _build_catboost_model() is not None:
        names.append('catboost')
    return names


def _build_base_models(scale_pos_weight: float = 1.0, hpo_params: dict | None = None) -> dict:
    hp = hpo_params or {}
    models = {'xgb': _build_model(scale_pos_weight=scale_pos_weight, hpo_params=hp.get('xgb'))}
    lgbm = _build_lgbm_model(scale_pos_weight=scale_pos_weight, hpo_params=hp.get('lgbm'))
    cat = _build_catboost_model(scale_pos_weight=scale_pos_weight, hpo_params=hp.get('catboost'))
    if lgbm is not None:
        models['lgbm'] = lgbm
    if cat is not None:
        models['catboost'] = cat
    return models


def walk_forward_validate(X: np.ndarray, y: np.ndarray, n_splits: int = 5) -> dict:
    """
    Walk-forward 교차검증 (시계열 전용).

    각 폴드에서 과거로 학습 → 미래로 검증.
    랜덤 분할 금지 (미래 데이터 누수 방지).

    Returns:
        {
          'fold_aucs': list[float],
          'fold_precisions': list[float],
          'mean_auc': float,
          'mean_precision': float,
          'std_auc': float,
        }
    """
    try:
        from sklearn.metrics import precision_score, roc_auc_score
        from sklearn.model_selection import TimeSeriesSplit
        from xgboost import XGBClassifier
    except ImportError as e:
        log.warning(f'의존성 부족: {e}')
        return {}

    tscv = TimeSeriesSplit(n_splits=n_splits)
    fold_aucs, fold_precisions = [], []

    log.info(f'Walk-forward 교차검증 ({n_splits}폴드)')
    for fold, (train_idx, test_idx) in enumerate(tscv.split(X), 1):
        X_tr, X_te = X[train_idx], X[test_idx]
        y_tr, y_te = y[train_idx], y[test_idx]

        if len(np.unique(y_te)) < 2:
            continue  # 라벨 단일 폴드 스킵

        pos = max(int(y_tr.sum()), 1)
        neg = max(len(y_tr) - pos, 1)
        model = _build_model(scale_pos_weight=neg / pos)
        model.fit(X_tr, y_tr, verbose=False)

        y_prob = model.predict_proba(X_te)[:, 1]
        y_pred = (y_prob >= 0.65).astype(int)

        auc = roc_auc_score(y_te, y_prob)
        prec = precision_score(y_te, y_pred, zero_division=0)

        fold_aucs.append(auc)
        fold_precisions.append(prec)
        log.info(f'  Fold {fold}: AUC={auc:.3f}  Precision@0.65={prec:.3f}  '
                 f'(train={len(X_tr)}, test={len(X_te)})')

    if not fold_aucs:
        return {}

    result = {
        'fold_aucs':       fold_aucs,
        'fold_precisions': fold_precisions,
        'mean_auc':        round(float(np.mean(fold_aucs)), 4),
        'std_auc':         round(float(np.std(fold_aucs)), 4),
        'mean_precision':  round(float(np.mean(fold_precisions)), 4),
    }
    log.info(f'  평균 AUC: {result["mean_auc"]:.3f} ± {result["std_auc"]:.3f}')
    log.info(f'  평균 Precision@0.65: {result["mean_precision"]:.3f}')
    return result


def compute_shap_values(model, X_sample: np.ndarray) -> dict:
    """
    SHAP 값으로 피처 기여도 분석.

    Args:
        model: 학습된 XGBClassifier
        X_sample: 분석할 샘플 (최대 500개 사용)

    Returns:
        {'feature': shap_mean_abs, ...} — 내림차순 정렬
    """
    try:
        import shap
    except ImportError:
        log.warning('shap 미설치: pip install shap')
        return {}

    sample = X_sample[:500] if len(X_sample) > 500 else X_sample
    explainer = shap.TreeExplainer(model)
    shap_vals = explainer.shap_values(sample)

    # 이진 분류: shap_vals shape = (n_samples, n_features)
    if isinstance(shap_vals, list):
        sv = shap_vals[1] if len(shap_vals) > 1 else shap_vals[0]
    else:
        sv = shap_vals  # 1D array for binary classifier
    shap_vals = sv

    mean_abs = np.abs(shap_vals).mean(axis=0)
    ranking = sorted(
        zip(FEATURE_NAMES, mean_abs.tolist()),
        key=lambda x: x[1], reverse=True,
    )

    log.info('=== SHAP 피처 기여도 (평균 절댓값) ===')
    for name, val in ranking[:10]:
        bar = '█' * int(val * 200)
        log.info(f'  {name:<20} {val:.4f}  {bar}')

    return {name: round(val, 6) for name, val in ranking}


# ─────────────────────────────────────────────
# Optuna HPO
# ─────────────────────────────────────────────

def _optuna_hpo(X_train: np.ndarray, y_train: np.ndarray, n_trials: int = 40) -> dict:
    """Optuna로 XGB/LGBM/CatBoost 최적 하이퍼파라미터 탐색.

    Returns:
        {'xgb': {...}, 'lgbm': {...}, 'catboost': {...}}
    """
    try:
        import optuna
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import TimeSeriesSplit
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError as e:
        log.warning(f'optuna 미설치: {e}')
        return {}

    pos = max(int(y_train.sum()), 1)
    neg = max(len(y_train) - pos, 1)
    spw = neg / pos
    tscv = TimeSeriesSplit(n_splits=3)
    best_params: dict = {}

    def _cv_auc(model):
        aucs = []
        for tr_idx, val_idx in tscv.split(X_train):
            Xtr, Xval = X_train[tr_idx], X_train[val_idx]
            ytr, yval = y_train[tr_idx], y_train[val_idx]
            if len(np.unique(yval)) < 2:
                continue
            _fit_model(model, Xtr, ytr, Xval, yval)
            prob = _predict_proba(model, Xval)
            if prob is not None:
                aucs.append(roc_auc_score(yval, prob))
        return float(np.mean(aucs)) if aucs else 0.0

    # ── XGBoost ──
    def _xgb_objective(trial):
        from xgboost import XGBClassifier
        params = dict(
            n_estimators=trial.suggest_int('n_estimators', 100, 500),
            max_depth=trial.suggest_int('max_depth', 3, 8),
            learning_rate=trial.suggest_float('learning_rate', 0.01, 0.15, log=True),
            subsample=trial.suggest_float('subsample', 0.6, 1.0),
            colsample_bytree=trial.suggest_float('colsample_bytree', 0.5, 1.0),
            min_child_weight=trial.suggest_int('min_child_weight', 1, 10),
            gamma=trial.suggest_float('gamma', 0.0, 1.0),
            scale_pos_weight=spw,
            eval_metric='logloss',
            random_state=42,
            use_label_encoder=False,
        )
        return _cv_auc(XGBClassifier(**params))

    study = optuna.create_study(direction='maximize')
    study.optimize(_xgb_objective, n_trials=n_trials, show_progress_bar=False)
    best_params['xgb'] = study.best_params
    log.info(f'  XGB HPO 최적 AUC={study.best_value:.4f} params={study.best_params}')

    # ── LightGBM ──
    try:
        from lightgbm import LGBMClassifier

        def _lgbm_objective(trial):
            params = dict(
                n_estimators=trial.suggest_int('n_estimators', 100, 500),
                num_leaves=trial.suggest_int('num_leaves', 15, 63),
                learning_rate=trial.suggest_float('learning_rate', 0.01, 0.15, log=True),
                subsample=trial.suggest_float('subsample', 0.6, 1.0),
                colsample_bytree=trial.suggest_float('colsample_bytree', 0.5, 1.0),
                min_child_samples=trial.suggest_int('min_child_samples', 5, 50),
                class_weight={0: 1.0, 1: spw},
                random_state=42,
                verbose=-1,
            )
            return _cv_auc(LGBMClassifier(**params))

        study_lgbm = optuna.create_study(direction='maximize')
        study_lgbm.optimize(_lgbm_objective, n_trials=n_trials, show_progress_bar=False)
        best_params['lgbm'] = study_lgbm.best_params
        log.info(f'  LGBM HPO 최적 AUC={study_lgbm.best_value:.4f}')
    except ImportError:
        pass

    # ── CatBoost ──
    try:
        from catboost import CatBoostClassifier

        def _cat_objective(trial):
            params = dict(
                iterations=trial.suggest_int('iterations', 100, 400),
                depth=trial.suggest_int('depth', 4, 8),
                learning_rate=trial.suggest_float('learning_rate', 0.01, 0.15, log=True),
                l2_leaf_reg=trial.suggest_float('l2_leaf_reg', 1.0, 10.0),
                class_weights=[1.0, spw],
                loss_function='Logloss',
                eval_metric='AUC',
                random_seed=42,
                verbose=False,
            )
            return _cv_auc(CatBoostClassifier(**params))

        study_cat = optuna.create_study(direction='maximize')
        study_cat.optimize(_cat_objective, n_trials=n_trials, show_progress_bar=False)
        best_params['catboost'] = study_cat.best_params
        log.info(f'  CatBoost HPO 최적 AUC={study_cat.best_value:.4f}')
    except ImportError:
        pass

    return best_params


# ─────────────────────────────────────────────
# RFECV 피처 선택
# ─────────────────────────────────────────────

def _rfecv_select_features(X: np.ndarray, y: np.ndarray) -> list[int]:
    """RFECV로 유효 피처 인덱스 반환. 실패 시 전체 인덱스 반환."""
    try:
        from sklearn.feature_selection import RFECV
        from sklearn.model_selection import TimeSeriesSplit
        from xgboost import XGBClassifier

        pos = max(int(y.sum()), 1)
        neg = max(len(y) - pos, 1)
        estimator = XGBClassifier(
            n_estimators=100, max_depth=4, learning_rate=0.05,
            scale_pos_weight=neg / pos,
            eval_metric='logloss', random_state=42, use_label_encoder=False,
        )
        selector = RFECV(
            estimator=estimator,
            step=1,
            cv=TimeSeriesSplit(n_splits=3),
            scoring='roc_auc',
            min_features_to_select=max(5, X.shape[1] // 3),
            n_jobs=-1,
        )
        selector.fit(X, y)
        selected = [i for i, s in enumerate(selector.support_) if s]
        removed = [FEATURE_NAMES[i] for i in range(len(FEATURE_NAMES)) if i < X.shape[1] and not selector.support_[i]]
        log.info(f'  RFECV: {X.shape[1]}개 → {len(selected)}개 피처 선택 (제거: {removed})')
        return selected
    except Exception as e:
        log.warning(f'  RFECV 실패, 전체 피처 유지: {e}')
        return list(range(X.shape[1]))


def train_model(horizon_key: str = '3d'):
    """앙상블 스태킹 학습. LightGBM/CatBoost 없으면 XGBoost 폴백."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (accuracy_score, average_precision_score,
                                 classification_report, roc_auc_score)
    from sklearn.model_selection import TimeSeriesSplit

    cfg = HORIZON_CONFIGS[horizon_key]
    paths = _horizon_paths(horizon_key)
    X, y = load_training_data(target_days=cfg['target_days'], target_return=cfg['target_return'])
    if X is None or len(X) < 100:
        log.warning('데이터 부족 (최소 100개 필요)')
        return

    wf = walk_forward_validate(X, y, n_splits=5)

    split_idx = int(len(X) * 0.8)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    pos = max(int(y_train.sum()), 1)
    neg = max(len(y_train) - pos, 1)
    scale_pos_weight = neg / pos

    # ── HPO + RFECV (환경변수로 선택적 활성화) ──────────────
    USE_HPO = os.environ.get('USE_HPO', '').lower() in ('1', 'true')
    USE_RFECV = os.environ.get('USE_RFECV', '').lower() in ('1', 'true')

    hpo_params: dict = {}
    selected_idx: list[int] = list(range(X_train.shape[1]))
    feat_names: list[str] = list(FEATURE_NAMES)

    if USE_RFECV:
        log.info('RFECV 피처 선택 중...')
        selected_idx = _rfecv_select_features(X_train, y_train)
        X_train = X_train[:, selected_idx]
        X_test = X_test[:, selected_idx]
        feat_names = [FEATURE_NAMES[i] for i in selected_idx if i < len(FEATURE_NAMES)]
        log.info(f'  선택 피처: {len(selected_idx)}/{len(FEATURE_NAMES)}개 — {feat_names[:5]}...')

    if USE_HPO:
        log.info('Optuna HPO 실행 중 (n_trials=40)...')
        hpo_params = _optuna_hpo(X_train, y_train)
        for mn, p in hpo_params.items():
            log.info(f'  {mn}: {p}')
    # ────────────────────────────────────────────────────────

    base_model_names = _available_base_model_names()
    use_ensemble = len(base_model_names) >= 2
    log.info(f'기본 모델: {", ".join(base_model_names)}')
    log.info(f'최종 모델 학습: {len(X_train)}개 / OOS 테스트: {len(X_test)}개')

    final_models = {}
    ensemble_prob = None
    meta_model = None
    training_mode = 'xgb_only'
    base_auc = {}

    if use_ensemble:
        tscv = TimeSeriesSplit(n_splits=5)
        oof_preds = {name: np.full(len(X_train), np.nan, dtype=float) for name in base_model_names}

        for fold, (tr_idx, val_idx) in enumerate(tscv.split(X_train), 1):
            X_tr, X_val = X_train[tr_idx], X_train[val_idx]
            y_tr, y_val = y_train[tr_idx], y_train[val_idx]
            if len(np.unique(y_val)) < 2:
                continue
            fold_models = _build_base_models(scale_pos_weight=scale_pos_weight, hpo_params=hpo_params)
            log.info(f'  stacking fold {fold}: train={len(X_tr)} test={len(X_val)}')
            for name, model in fold_models.items():
                try:
                    _fit_model(model, X_tr, y_tr, X_val, y_val)
                    oof_preds[name][val_idx] = _predict_proba(model, X_val)
                except Exception as e:
                    log.warning(f'    {name} 학습 실패: {e}')

        valid_cols = [name for name in base_model_names if not np.isnan(oof_preds[name]).all()]
        valid_mask = np.ones(len(X_train), dtype=bool)
        for name in valid_cols:
            valid_mask &= ~np.isnan(oof_preds[name])

        if len(valid_cols) >= 2 and valid_mask.sum() >= 50:
            meta_X = np.column_stack([oof_preds[name][valid_mask] for name in valid_cols])
            meta_y = y_train[valid_mask]
            meta_model = LogisticRegression(C=1.0, max_iter=1000, random_state=42)
            meta_model.fit(meta_X, meta_y)

            for name, model in _build_base_models(scale_pos_weight=scale_pos_weight, hpo_params=hpo_params).items():
                try:
                    _fit_model(model, X_train, y_train, X_test, y_test)
                    final_models[name] = model
                    probs = _predict_proba(model, X_test)
                    if probs is not None and len(np.unique(y_test)) > 1:
                        base_auc[name] = round(float(roc_auc_score(y_test, probs)), 4)
                except Exception as e:
                    log.warning(f'최종 {name} 학습 실패: {e}')

            test_cols = [name for name in valid_cols if name in final_models]
            if len(test_cols) >= 2:
                meta_test_X = np.column_stack([_predict_proba(final_models[name], X_test) for name in test_cols])
                ensemble_prob = meta_model.predict_proba(meta_test_X)[:, 1]
                training_mode = 'stacking'

        if ensemble_prob is None:
            log.warning('앙상블 스태킹 실패 → XGBoost 단독으로 폴백')

    if ensemble_prob is None:
        model = _build_model(scale_pos_weight=scale_pos_weight, hpo_params=hpo_params.get('xgb'))
        _fit_model(model, X_train, y_train, X_test, y_test)
        final_models = {'xgb': model}
        ensemble_prob = _predict_proba(model, X_test)

    primary_model = final_models['xgb']
    y_pred = (ensemble_prob >= 0.65).astype(int)

    log.info('=== OOS 모델 성과 ===')
    log.info(f'  정확도:     {accuracy_score(y_test, y_pred) * 100:.1f}%')
    auc = 0.0
    ap = 0.0
    if len(np.unique(y_test)) > 1:
        auc = float(roc_auc_score(y_test, ensemble_prob))
        ap = float(average_precision_score(y_test, ensemble_prob))
        log.info(f'  AUC-ROC:    {auc:.3f}')
        log.info(f'  AP (PR):    {ap:.3f}')
    log.info(classification_report(y_test, y_pred, target_names=['관망', '매수']))

    importances = sorted(
        zip(feat_names, primary_model.feature_importances_),
        key=lambda x: x[1], reverse=True,
    )
    log.info('=== XGBoost 피처 중요도 TOP 10 ===')
    for name, imp in importances[:10]:
        log.info(f'  {name}: {imp:.4f}')

    shap_ranking = compute_shap_values(primary_model, X_test)

    thresholds = [0.5, 0.6, 0.65, 0.7, 0.8]
    log.info('=== 확률 임계값별 Precision ===')
    for thresh in thresholds:
        mask = ensemble_prob >= thresh
        if mask.sum() == 0:
            continue
        prec = y_test[mask].sum() / mask.sum() * 100
        log.info(f'  ≥{thresh:.2f}: {int(mask.sum())}건 → Precision {prec:.1f}%')

    primary_model.save_model(str(paths['xgb']))
    os.chmod(paths['xgb'], 0o644)

    if 'lgbm' in final_models:
        final_models['lgbm'].booster_.save_model(str(paths['lgbm']))
    elif paths['lgbm'].exists():
        paths['lgbm'].unlink()

    if 'catboost' in final_models:
        final_models['catboost'].save_model(str(paths['catboost']))
    elif paths['catboost'].exists():
        paths['catboost'].unlink()

    if meta_model is not None and training_mode == 'stacking':
        _save_meta_model(meta_model, horizon_key=horizon_key)
    elif paths['meta_model'].exists():
        paths['meta_model'].unlink()

    meta = {
        'mode': training_mode,
        'horizon': horizon_key,
        'target_days': cfg['target_days'],
        'target_return': cfg['target_return'],
        'base_models': sorted(final_models.keys()),
        'feature_names': feat_names,
        'thresholds': {'buy': 0.65, 'high_confidence': 0.78},
        'trained_at': _utc_now_iso(),
        'n_samples': len(X),
        'train_samples': len(X_train),
        'test_samples': len(X_test),
        'walk_forward': wf,
        'auc': round(float(auc), 4),
        'average_precision': round(float(ap), 4),
        'base_auc': base_auc,
        'feature_importance': [(n, round(float(v), 6)) for n, v in importances],
        'shap_ranking': list(shap_ranking.items())[:10] if shap_ranking else [],
        'hpo_params': hpo_params if hpo_params else None,
        'rfecv_selected_idx': selected_idx if USE_RFECV else None,
    }
    paths['meta_json'].write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

    log.info(f'모델 저장: {paths["xgb"]}')
    if 'lgbm' in final_models:
        log.info(f'LightGBM 저장: {paths["lgbm"]}')
    if 'catboost' in final_models:
        log.info(f'CatBoost 저장: {paths["catboost"]}')
    if meta_model is not None and training_mode == 'stacking':
        log.info(f'메타모델 저장: {paths["meta_model"]}')
    log.info(f'메타 저장: {paths["meta_json"]}')

    return primary_model


def retrain_from_live_trades(min_samples: int = 30, horizon_key: str = '3d') -> bool:
    """
    실제 매매 결과(trade_executions)로 모델 재학습 (온라인 학습).

    XGBoost의 incremental fit은 부재 → 전체 재학습이지만
    라이브 거래 라벨(성공/실패)을 추가 학습 데이터로 포함.

    Args:
        min_samples: 재학습 최소 라이브 샘플 수

    Returns:
        True if 재학습 성공
    """
    if not supabase:
        log.info('Supabase 미연결')
        return False

    # 실매매 결과 로드
    try:
        rows = (
            supabase.table('trade_executions')
            .select('stock_code,price,result,pnl_pct,created_at,ml_features_json')
            .in_('result', ['CLOSED', 'SELL'])
            .order('created_at', desc=False)
            .execute()
            .data or []
        )
    except Exception as e:
        log.info(f'거래 데이터 로드 실패: {e}')
        return False

    if len(rows) < min_samples:
        log.info(f'라이브 샘플 부족: {len(rows)} < {min_samples}')
        return False

    # 라이브 라벨 생성: pnl_pct >= 2% → 1(성공)
    live_X, live_y = [], []
    for r in rows:
        code = r.get('stock_code', '')

        # 저장된 피처 우선 사용 (look-ahead bias 방지)
        saved_json = r.get('ml_features_json')
        features = None
        if saved_json:
            try:
                saved_dict = json.loads(saved_json) if isinstance(saved_json, str) else saved_json
                features = list(saved_dict.values())
            except Exception as e:
                log.debug(f'저장된 라이브 피처 파싱 실패: {e}')
                features = None

        # fallback: 현재 데이터로 재생성
        if features is None:
            pred = predict_stock(code, horizon_key=horizon_key)
            if 'error' in pred:
                continue
            features = list(pred['features'].values())

        pnl = float(r.get('pnl_pct') or 0)
        label = 1 if pnl >= 2.0 else 0
        live_X.append(features)
        live_y.append(label)

    if len(live_X) < min_samples:
        log.info(f'유효 라이브 샘플 부족: {len(live_X)}')
        return False

    # 히스토리컬 데이터와 병합
    cfg = HORIZON_CONFIGS[horizon_key]
    paths = _horizon_paths(horizon_key)
    X_hist, y_hist = load_training_data(target_days=cfg['target_days'], target_return=cfg['target_return'])
    if X_hist is not None and len(X_hist) > 0:
        X_all = np.vstack([X_hist, np.array(live_X, dtype=float)])
        y_all = np.concatenate([y_hist, np.array(live_y, dtype=int)])
    else:
        X_all = np.array(live_X, dtype=float)
        y_all = np.array(live_y, dtype=int)

    live_wins = sum(live_y)
    log.info(f'실매매 데이터 {len(live_X)}건 추가 (성공: {live_wins} / 실패: {len(live_y)-live_wins})')
    log.info(f'전체 학습 데이터: {len(X_all)}건')

    # 재학습
    pos = max(int(y_all.sum()), 1)
    neg = max(len(y_all) - pos, 1)
    model = _build_model(scale_pos_weight=neg / pos)
    split_idx = int(len(X_all) * 0.85)
    model.fit(
        X_all[:split_idx], y_all[:split_idx],
        eval_set=[(X_all[split_idx:], y_all[split_idx:])],
        verbose=False,
    )

    model.save_model(str(paths['xgb']))  # XGBoost 네이티브 저장 (pickle 불사용)
    os.chmod(paths['xgb'], 0o644)
    paths['meta_json'].write_text(json.dumps({
        'mode': 'xgb_only',
        'horizon': horizon_key,
        'target_days': cfg['target_days'],
        'target_return': cfg['target_return'],
        'base_models': ['xgb'],
        'feature_names': FEATURE_NAMES,
        'trained_at': _utc_now_iso(),
        'n_samples': len(X_all),
        'source': 'live_retrain',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    log.info(f'실매매 반영 모델 저장: {paths["xgb"]}')
    return True


# ─────────────────────────────────────────────
# 예측
# ─────────────────────────────────────────────
def _load_model(horizon_key: str = '3d'):
    paths = _horizon_paths(horizon_key)
    if not paths['xgb'].exists():
        # 구버전 .pkl 파일 하위호환: 존재하면 XGBoost 네이티브 포맷으로 마이그레이션
        old_path = paths['xgb'].with_suffix('.pkl')
        if old_path.exists():
            try:
                import joblib
                model = joblib.load(str(old_path))
                model.save_model(str(paths['xgb']))
                os.chmod(paths['xgb'], 0o644)
                old_path.unlink()
                log.info(f'[ml_model] 구버전 .pkl → .ubj 마이그레이션 완료')
                return model
            except Exception as e:
                log.warning(f'[ml_model] 마이그레이션 실패: {e}')
        return None
    from xgboost import XGBClassifier
    model = XGBClassifier()
    model.load_model(str(paths['xgb']))
    return model


def _load_model_bundle(horizon_key: str = '3d') -> dict:
    paths = _horizon_paths(horizon_key)
    bundle = {'xgb': None, 'lgbm': None, 'catboost': None, 'meta': None, 'meta_info': {}}
    bundle['xgb'] = _load_model(horizon_key=horizon_key)

    if paths['lgbm'].exists():
        try:
            import lightgbm as lgb
            bundle['lgbm'] = lgb.Booster(model_file=str(paths['lgbm']))
        except Exception as e:
            log.debug(f'LightGBM 모델 로드 실패: {e}')
            bundle['lgbm'] = None

    if paths['catboost'].exists():
        try:
            from catboost import CatBoostClassifier
            model = CatBoostClassifier()
            model.load_model(str(paths['catboost']))
            bundle['catboost'] = model
        except Exception as e:
            log.debug(f'CatBoost 모델 로드 실패: {e}')
            bundle['catboost'] = None

    bundle['meta'] = _load_meta_model(horizon_key=horizon_key)
    if paths['meta_json'].exists():
        try:
            bundle['meta_info'] = json.loads(paths['meta_json'].read_text(encoding='utf-8'))
        except Exception as e:
            log.debug(f'메타 정보 로드 실패: {e}')
            bundle['meta_info'] = {}
    return bundle


def _bundle_predict_probability(bundle: dict, X: np.ndarray) -> tuple[float, dict]:
    probs = {}
    for name in ('xgb', 'lgbm', 'catboost'):
        model = bundle.get(name)
        if model is None:
            continue
        try:
            if name == 'lgbm':
                prob = float(model.predict(X)[0])
            else:
                prob = float(_predict_proba(model, X)[0])
            probs[name] = prob
        except Exception as e:
            log.debug(f'{name} 확률 예측 실패: {e}')
            continue

    if not probs:
        return 0.0, {}

    meta = bundle.get('meta')
    base_order = [name for name in bundle.get('meta_info', {}).get('base_models', []) if name in probs]
    if meta is not None and len(base_order) >= 2:
        meta_X = np.array([[probs[name] for name in base_order]], dtype=float)
        try:
            ensemble_prob = float(meta.predict_proba(meta_X)[0][1])
            return ensemble_prob, probs
        except Exception as e:
            log.debug(f'메타 모델 앙상블 예측 실패: {e}')

    ensemble_prob = float(sum(probs.values()) / len(probs))
    return ensemble_prob, probs


def predict_stock(stock_code: str, horizon_key: str = '3d') -> dict:
    """특정 종목 매수 확률 예측"""
    if not supabase:
        return {'error': 'Supabase 미연결'}
    bundle = _load_model_bundle(horizon_key=horizon_key)
    if bundle.get('xgb') is None:
        return {'error': '모델 없음. train 먼저 실행'}

    rows = (
        supabase.table('daily_ohlcv')
        .select('date,open_price,high_price,low_price,close_price,volume')
        .eq('stock_code', stock_code)
        .order('date', desc=False)
        .limit(120)
        .execute()
        .data
        or []
    )

    if len(rows) < 61:
        return {'error': f'데이터 부족: {len(rows)}일'}

    closes = [float(r['close_price']) for r in rows]
    volumes = [float(r.get('volume', 0)) for r in rows]
    highs = [float(r.get('high_price', r['close_price'])) for r in rows]
    lows = [float(r.get('low_price', r['close_price'])) for r in rows]

    features = extract_features(
        closes, volumes, highs, lows, len(rows) - 1,
        stock_code=stock_code,
        as_of_date=rows[-1]['date'],
    )
    if features is None:
        return {'error': '피처 추출 실패'}

    X = np.array([features], dtype=float)
    prob, base_probs = _bundle_predict_probability(bundle, X)
    action = 'BUY' if prob >= 0.65 else 'HOLD'

    return {
        'stock_code': stock_code,
        'horizon': horizon_key,
        'buy_probability': round(prob * 100, 1),
        'action': action,
        'model_type': 'ensemble' if len(base_probs) >= 2 else 'xgboost',
        'base_probabilities': {k: round(v * 100, 2) for k, v in base_probs.items()},
        'features': {
            name: round(val, 4) for name, val in zip(FEATURE_NAMES, features)
        },
    }


def predict_all(horizon_key: str = '3d') -> list:
    """전 종목 매수 확률 예측 → 상위 종목 반환"""
    if not supabase:
        log.warning('Supabase 미연결')
        return []
    model = _load_model(horizon_key=horizon_key)
    if model is None:
        log.warning('모델 없음')
        return []

    stocks = (
        supabase.table('top50_stocks')
        .select('stock_code,stock_name')
        .execute()
        .data
        or []
    )
    results = []

    for s in stocks:
        pred = predict_stock(s['stock_code'], horizon_key=horizon_key)
        if 'error' in pred:
            continue
        pred['name'] = s.get('stock_name', s['stock_code'])
        results.append(pred)

    results.sort(key=lambda x: x['buy_probability'], reverse=True)

    log.info('=== 매수 확률 TOP 10 ===')
    for r in results[:10]:
        emoji = '🟢' if r['action'] == 'BUY' else '⚪'
        log.info(f"  {emoji} {r['name']}: {r['buy_probability']}% → {r['action']}")

    log.info(f'매수 신호: {sum(1 for r in results if r["action"] == "BUY")}종목')
    return results


# ─────────────────────────────────────────────
# trading_agent 연동용 함수
# ─────────────────────────────────────────────
def predict_multi_horizon(stock_code: str) -> dict:
    probs = {}
    details = {}
    for hk in ('1d', '3d', '10d'):
        pred = predict_stock(stock_code, horizon_key=hk)
        if 'error' in pred:
            continue
        probs[hk] = float(pred.get('buy_probability', 0.0))
        details[hk] = pred
    if not probs:
        return {'error': '모델 없음. train 먼저 실행'}

    short_prob = probs.get('1d', 0.0)
    mid_prob = probs.get('3d', 0.0)
    swing_prob = probs.get('10d', 0.0)

    if short_prob >= 70 and mid_prob >= 60:
        action = 'STRONG_BUY'
        confidence = max(short_prob, mid_prob)
    elif mid_prob >= 65:
        action = 'BUY'
        confidence = mid_prob
    elif swing_prob >= 70 and mid_prob >= 50:
        action = 'SWING_BUY'
        confidence = swing_prob
    else:
        action = 'HOLD'
        confidence = max(probs.values())

    return {
        'stock_code': stock_code,
        'action': action,
        'confidence': round(confidence, 1),
        'horizon_probabilities': probs,
        'details': details,
    }


def get_ml_signal(stock_code: str) -> dict:
    """
    trading_agent에서 호출하는 인터페이스

    Returns:
        {
            'action': 'BUY' | 'HOLD',
            'confidence': 0~100,
            'source': 'ML_XGBOOST',
        }
    """
    try:
        pred = predict_multi_horizon(stock_code)
        if 'error' in pred:
            return {'action': 'HOLD', 'confidence': 0, 'source': 'ML_ERROR'}

        return {
            'action': pred['action'],
            'confidence': pred['confidence'],
            'source': 'ML_MULTI_HORIZON',
            'horizon_probabilities': pred.get('horizon_probabilities', {}),
        }
    except Exception as e:
        return {'action': 'HOLD', 'confidence': 0, 'source': f'ML_ERROR: {e}'}


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'train'
    horizon_arg = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in HORIZON_CONFIGS else '3d'

    if cmd == 'train':
        train_model(horizon_key=horizon_arg)
    elif cmd == 'train_all':
        for hk in ('1d', '3d', '10d'):
            print(f'\n===== horizon {hk} 학습 시작 =====')
            train_model(horizon_key=hk)
    elif cmd == 'predict' and len(sys.argv) > 2:
        result = predict_multi_horizon(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == 'predict_all':
        predict_all(horizon_key=horizon_arg)
    elif cmd == 'evaluate':
        train_model(horizon_key=horizon_arg)
    elif cmd == 'retrain':
        # 실매매 결과로 재학습
        min_idx = 3 if horizon_arg in HORIZON_CONFIGS else 2
        min_s = int(sys.argv[min_idx]) if len(sys.argv) > min_idx else 30
        ok = retrain_from_live_trades(min_samples=min_s, horizon_key=horizon_arg)
        print('재학습 성공' if ok else '재학습 실패')
    elif cmd == 'validate':
        # Walk-forward 검증만 실행
        cfg = HORIZON_CONFIGS[horizon_arg]
        X, y = load_training_data(target_days=cfg['target_days'], target_return=cfg['target_return'])
        if X is not None:
            walk_forward_validate(X, y)
    elif cmd == 'shap' and len(sys.argv) > 2:
        # 특정 종목 SHAP 분석
        res = predict_stock(sys.argv[2], horizon_key=horizon_arg)
        if 'error' not in res:
            model = _load_model(horizon_key=horizon_arg)
            if model:
                feats = np.array([list(res['features'].values())], dtype=float)
                compute_shap_values(model, feats)
    else:
        print('사용법:')
        print('  python3 ml_model.py train [1d|3d|10d]      # 단일 호라이즌 학습')
        print('  python3 ml_model.py train_all              # 1d/3d/10d 전체 학습')
        print('  python3 ml_model.py validate [1d|3d|10d]   # Walk-forward 검증')
        print('  python3 ml_model.py retrain [1d|3d|10d] [n]# 실매매 결과 반영 재학습')
        print('  python3 ml_model.py predict 005930         # 멀티 호라이즌 예측')
        print('  python3 ml_model.py shap 005930 [1d|3d|10d]# 특정 호라이즌 SHAP')
        print('  python3 ml_model.py predict_all [1d|3d|10d]# 단일 호라이즌 전체 예측')
