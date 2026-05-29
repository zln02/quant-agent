#!/usr/bin/env python3
"""
주식 매매 ML 모델 v1.0

XGBoost 분류 모델:
- 입력: 기술적 지표 + 가격/거래량 특성
- 출력: 3일 내 +2% 이상 상승 확률

사용법:
    python3 stocks/ml_model.py train          # 모델 학습
    python3 stocks/ml_model.py evaluate       # 성과 평가(=train)
    python3 stocks/ml_model.py predict 005930 # 특정 종목 예측
    python3 stocks/ml_model.py predict_all    # 전체 종목 예측
"""

import json
import os
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from common.env_loader import load_env

load_env()

from supabase import create_client  # noqa: E402

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_SECRET_KEY', '')
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

ROOT_DIR = Path(__file__).resolve().parents[1]
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

FACTOR_FEATURES = [
    'momentum_12m',
    'momentum_1m',
    'pe_ratio',
    'pb_ratio',
    'roe',
    'volume_ratio_20d',
]

MARKET_FEATURES = [
    'kospi_rsi_14',
    'kospi_return_5d',
    'vix_level',
    'fg_index',
    'regime_encoded',
]

SUPPLY_FEATURES = [
    'sector_momentum_rank',
    'relative_strength_vs_kospi',
    'avg_spread_bps',
    '52w_high_proximity',
]

# OHLCV에서 직접 계산 가능한 피처 (stock_code 불필요, 항상 유효)
# 제거된 dead features (항상 0이던 것들):
#   FACTOR: debt_ratio, revenue_growth, earnings_surprise, orderbook_imbalance
#   SUPPLY: foreign_net_buy_5d, inst_net_buy_5d, short_interest_ratio,
#           days_to_earnings, turnover_ratio, market_cap_log
OHLCV_EXTRA_FEATURES = [
    'close_vs_ma120',       # 120일 이평 대비 (장기 추세)
    'ma5_vs_ma20',          # 단기/중기 골든크로스 신호
    'ma20_vs_ma60',         # 중기/장기 추세 신호
    'vol_zscore_20',        # 거래량 z-score (이상 거래량 탐지)
    'lower_shadow_ratio',   # 아랫꼬리 비율 (매수세)
    'upper_shadow_ratio',   # 윗꼬리 비율 (매도세)
    'close_pos_5d',         # 5일 고저 내 종가 위치 (0~100)
    'range_expansion',      # 오늘 범위 / 5일 평균 범위
    'consec_up',            # 연속 상승일 수 (0~5)
    'price_acceleration',   # 1일 수익률 - 5일 수익률/5 (가속/감속)
]

# v6: 인터랙션 피처 (모멘텀×거래량, 추세 정렬, BB×거래량, 가속도, 레짐×RSI)
INTERACTION_FEATURES = [
    'rsi_x_vol',          # RSI × vol_ratio_5 (모멘텀 + 거래량 확인)
    'trend_alignment',    # macd_histogram × close_vs_ma20 (추세 정렬)
    'bb_vol_confirm',     # bb_pos × vol_zscore_20 (BB + 거래량 확인)
    'return_accel',       # return_1d - return_5d/5 (가격 가속도)
    'regime_rsi',         # regime_encoded × rsi_14 (레짐×RSI 교차)
]

# v6.2 C2: 멀티타임프레임 피처 (45 → 50)
MTF_FEATURES = [
    'weekly_momentum',   # 5일 수익률
    'monthly_momentum',  # 20일 수익률
    'weekly_rsi',        # 5일 기반 RSI
    'trend_alignment_mtf',  # MA5>MA20>MA60 정렬 여부 (0 or 1)
    'vol_regime',        # 20일 변동성 / 60일 변동성
]

FEATURE_NAMES.extend(OHLCV_EXTRA_FEATURES + INTERACTION_FEATURES + FACTOR_FEATURES + MARKET_FEATURES + SUPPLY_FEATURES + MTF_FEATURES)

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
    return datetime.now(timezone.utc).isoformat()


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
        except Exception:
            pass

        try:
            workspace_root = str(Path(__file__).resolve().parents[1])
            if workspace_root not in sys.path:
                sys.path.insert(0, workspace_root)
            from quant.factors.registry import calc
            history['fg_index'] = calc('fg_index', as_of=datetime.now().date().isoformat(), symbol='005930', market='kr', context=self._get_factor_ctx())
        except Exception:
            pass

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
        except Exception:
            pass

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
        except Exception:
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
        except Exception:
            pass
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
        except Exception:
            pass
        self.sector_rank_cache[sector] = ranks
        return ranks.get(code, 0.5)


_ML_FEATURE_CTX = MLFeatureContext()


def _compute_extra_features(stock_code: str, as_of_date: str, closes, volumes, highs, lows, idx):
    price = closes[idx]
    factor_vals = _ML_FEATURE_CTX.get_factor_features(stock_code, as_of_date)
    market_vals = _ML_FEATURE_CTX.get_market_features(as_of_date)

    ret_20d = (closes[idx] / closes[idx - 20] - 1.0) * 100.0 if idx >= 20 and closes[idx - 20] > 0 else 0.0
    kospi_ret_5d = _safe_float(market_vals.get('kospi_return_5d'), 0.0)
    relative_strength = ret_20d - kospi_ret_5d
    high_252 = max(closes[max(0, idx - 251): idx + 1]) if idx >= 1 else price
    proximity_52w = (price / high_252) if high_252 > 0 else 0.0
    avg_spread_bps = (_tick_size_kr(price) / price * 10000.0) if price > 0 else 0.0

    sector_rank = _ML_FEATURE_CTX.get_sector_momentum_rank(stock_code, ret_20d)

    extras = {
        'momentum_12m': _safe_float(factor_vals.get('momentum_12m'), 0.0),
        'momentum_1m': _safe_float(factor_vals.get('momentum_1m'), 0.0),
        'pe_ratio': _safe_float(factor_vals.get('pe_ratio'), 0.0),
        'pb_ratio': _safe_float(factor_vals.get('pb_ratio'), 0.0),
        'roe': _safe_float(factor_vals.get('roe'), 0.0),
        'volume_ratio_20d': _safe_float(factor_vals.get('volume_ratio_20d'), 1.0),
        'kospi_rsi_14': _safe_float(market_vals.get('kospi_rsi_14'), 50.0),
        'kospi_return_5d': kospi_ret_5d,
        'vix_level': _safe_float(market_vals.get('vix_level'), 20.0),
        'fg_index': _safe_float(market_vals.get('fg_index'), 50.0),
        'regime_encoded': _safe_float(market_vals.get('regime_encoded'), 2.0),
        'sector_momentum_rank': sector_rank,
        'relative_strength_vs_kospi': relative_strength,
        'avg_spread_bps': avg_spread_bps,
        '52w_high_proximity': proximity_52w,
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

    # ── OHLCV 파생 피처 (항상 계산, stock_code 불필요) ──
    ma120 = sum(c[-120:]) / 120 if len(c) >= 120 else sum(c) / max(len(c), 1)
    close_vs_ma120 = (price / max(ma120, 1e-8) - 1) * 100
    ma5_vs_ma20 = (ma5 / max(ma20, 1e-8) - 1) * 100
    ma20_vs_ma60 = (ma20 / max(ma60, 1e-8) - 1) * 100
    v20 = list(v[-21:-1]) if len(v) >= 21 else list(v[:-1])
    vol_mean_20 = sum(v20) / max(len(v20), 1)
    vol_std_20 = (sum((x - vol_mean_20) ** 2 for x in v20) / max(len(v20), 1)) ** 0.5
    vol_zscore_20 = (v[-1] - vol_mean_20) / max(vol_std_20, 1e-8)
    hl_range = max(h[-1] - l[-1], 1e-8)
    open_approx = c[-2] if len(c) >= 2 else price
    body_low = min(price, open_approx)
    body_high = max(price, open_approx)
    lower_shadow_ratio = (body_low - l[-1]) / hl_range
    upper_shadow_ratio = (h[-1] - body_high) / hl_range
    min_5d = min(l[-5:]) if len(l) >= 5 else l[-1]
    max_5d = max(h[-5:]) if len(h) >= 5 else h[-1]
    close_pos_5d = (price - min_5d) / max(max_5d - min_5d, 1e-8) * 100
    h5 = list(h[-6:-1]) if len(h) >= 6 else list(h[:-1])
    l5 = list(l[-6:-1]) if len(l) >= 6 else list(l[:-1])
    avg_range_5 = sum(h5[i] - l5[i] for i in range(len(h5))) / max(len(h5), 1)
    range_expansion = hl_range / max(avg_range_5, 1e-8)
    consec_up = float(sum(1 for i in range(1, min(6, len(c))) if c[-i] > c[-(i + 1)]))
    price_acceleration = return_1d - (return_5d / 5.0)

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
        # OHLCV extra (항상 포함)
        close_vs_ma120,
        ma5_vs_ma20,
        ma20_vs_ma60,
        vol_zscore_20,
        lower_shadow_ratio,
        upper_shadow_ratio,
        close_pos_5d,
        range_expansion,
        consec_up,
        price_acceleration,
        # v6: 인터랙션 피처
        rsi_14 * vol_ratio_5 / 100.0,              # rsi_x_vol
        (macd_hist / price * 100) * close_vs_ma20,  # trend_alignment
        bb_pos * vol_zscore_20 / 100.0,             # bb_vol_confirm
        return_1d - (return_5d / 5.0),              # return_accel
        0.0,  # regime_rsi placeholder — filled below if market features available
    ]

    # regime_rsi will be updated once market features are known
    _regime_rsi_idx = len(features) - 1

    if stock_code and as_of_date:
        extra = _compute_extra_features(
            stock_code=stock_code,
            as_of_date=as_of_date,
            closes=closes,
            volumes=volumes,
            highs=highs,
            lows=lows,
            idx=idx,
        )
        features.extend(extra)
        # v6: regime_rsi 인터랙션 — market features에서 regime_encoded 추출
        try:
            _mkt_offset = len(FACTOR_FEATURES)  # market features start after factor features
            _regime_val = extra[_mkt_offset + MARKET_FEATURES.index('regime_encoded')]
            features[_regime_rsi_idx] = _regime_val * rsi_14 / 100.0
        except (IndexError, ValueError):
            pass
    else:
        features.extend([0.0] * (len(FACTOR_FEATURES) + len(MARKET_FEATURES) + len(SUPPLY_FEATURES)))

    # v6.2 C2: 멀티타임프레임 피처
    # weekly_momentum: 5일 수익률
    weekly_momentum = (c[-1] / c[-6] - 1) * 100 if len(c) >= 6 else 0.0

    # monthly_momentum: 20일 수익률
    monthly_momentum = (c[-1] / c[-21] - 1) * 100 if len(c) >= 21 else 0.0

    # weekly_rsi: 5일 기반 RSI
    if len(c) >= 6:
        _delta5 = [c[i] - c[i - 1] for i in range(max(1, len(c) - 5), len(c))]
        _gain5 = sum(max(d, 0) for d in _delta5) / max(len(_delta5), 1)
        _loss5 = sum(max(-d, 0) for d in _delta5) / max(len(_delta5), 1)
        _rs5 = _gain5 / max(_loss5, 1e-10)
        weekly_rsi = 100 - (100 / (1 + _rs5))
    else:
        weekly_rsi = 50.0

    # trend_alignment_mtf: ma5 > ma20 이고 ma20 > ma60이면 1 (ma5/ma20/ma60은 위에서 이미 계산됨)
    trend_alignment_mtf = 1.0 if (ma5 > ma20 and ma20 > ma60) else 0.0

    # vol_regime: 20일 변동성 / 60일 변동성
    _rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
    _vol20 = (sum(r ** 2 for r in _rets[-20:]) / max(len(_rets[-20:]), 1)) ** 0.5 if len(_rets) >= 20 else 0.0
    _vol60 = (sum(r ** 2 for r in _rets[-60:]) / max(len(_rets[-60:]), 1)) ** 0.5 if len(_rets) >= 60 else 1e-10
    vol_regime = _vol20 / max(_vol60, 1e-10)

    features.extend([
        weekly_momentum,
        monthly_momentum,
        weekly_rsi,
        trend_alignment_mtf,
        vol_regime,
    ])

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
        print('Supabase 미연결')
        return None, None

    stocks = (
        supabase.table('top50_stocks')
        .select('stock_code')
        .execute()
        .data
        or []
    )
    print(f'데이터 로드: {len(stocks)}종목')

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

            # PR #25: Triple Barrier labeling (Lopez de Prado).
            # TP(+target_return) 먼저 닿으면 1, SL(-target_return) 먼저 닿으면 0,
            # 어느 쪽도 안 닿고 vertical barrier(target_days) 만료 시 future_return 부호로 라벨.
            entry = closes[i]
            tp_price = entry * (1.0 + target_return)
            sl_price = entry * (1.0 - target_return)
            label = None
            for _step in range(1, target_days + 1):
                _idx = i + _step
                if _idx >= len(closes):
                    break
                _hi = highs[_idx] if _idx < len(highs) else closes[_idx]
                _lo = lows[_idx] if _idx < len(lows) else closes[_idx]
                # 같은 봉에 TP/SL 둘 다 닿으면 OHLC상 우선순위 결정 불가 → SL 보수적 가정
                if _lo <= sl_price:
                    label = 0
                    break
                if _hi >= tp_price:
                    label = 1
                    break
            if label is None:
                # vertical barrier 만료 — 시점 종가 기준 양수면 1 (약 라벨, 학습 신호 약화)
                _final = closes[min(i + target_days, len(closes) - 1)]
                label = 1 if (_final - entry) / max(entry, 1) >= 0 else 0

            all_X.append(features)
            all_y.append(label)

    if not all_X:
        print('학습 데이터 없음')
        return None, None

    X = np.array(all_X, dtype=float)
    y = np.array(all_y, dtype=int)
    buys = int(y.sum())
    print(f'학습 데이터: {len(X)}개 샘플 (매수: {buys} / 관망: {len(y) - buys})')
    if len(y) > 0:
        print(f'매수 비율: {buys / len(y) * 100:.1f}%')

    return X, y


# ─────────────────────────────────────────────
# 모델 학습
# ─────────────────────────────────────────────
def _build_model(scale_pos_weight: float = 1.0):
    """XGBClassifier 인스턴스 공통 생성."""
    from xgboost import XGBClassifier
    return XGBClassifier(
        n_estimators=200,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale_pos_weight,
        eval_metric='logloss',
        random_state=42,
        use_label_encoder=False,
    )


def _build_lgbm_model(scale_pos_weight: float = 1.0):
    try:
        from lightgbm import LGBMClassifier
    except ImportError:
        return None
    return LGBMClassifier(
        n_estimators=300,
        num_leaves=31,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        class_weight={0: 1.0, 1: max(scale_pos_weight, 1.0)},
        verbose=-1,
    )


def _build_catboost_model(scale_pos_weight: float = 1.0):
    try:
        from catboost import CatBoostClassifier
    except ImportError:
        return None
    return CatBoostClassifier(
        iterations=300,
        depth=6,
        learning_rate=0.03,
        loss_function='Logloss',
        eval_metric='AUC',
        random_seed=42,
        verbose=False,
        class_weights=[1.0, max(scale_pos_weight, 1.0)],
    )


def _fit_model(model, X_train, y_train, X_valid=None, y_valid=None):
    # v6.2 A3: 10d 과적합 방지 — early_stopping_rounds 적용
    if model is None:
        return None
    model_name = model.__class__.__name__.lower()
    if 'catboost' in model_name:
        if X_valid is not None and y_valid is not None and len(X_valid) > 0:
            model.fit(X_train, y_train, eval_set=(X_valid, y_valid), verbose=False, early_stopping_rounds=20)
        else:
            model.fit(X_train, y_train, verbose=False)
        return model
    if 'lgbm' in model_name:
        if X_valid is not None and y_valid is not None and len(X_valid) > 0:
            model.fit(
                X_train, y_train,
                eval_set=[(X_valid, y_valid)],
                callbacks=[__import__('lightgbm').early_stopping(stopping_rounds=20, verbose=False)],
            )
        else:
            model.fit(X_train, y_train)
        return model
    # XGBoost 3.x: early_stopping_rounds는 생성자/set_params로 설정
    if X_valid is not None and y_valid is not None and len(X_valid) > 0:
        model.set_params(early_stopping_rounds=20)
        model.fit(
            X_train, y_train,
            eval_set=[(X_valid, y_valid)],
            verbose=False,
        )
    else:
        model.fit(X_train, y_train, verbose=False)
    return model


def _predict_proba(model, X):
    if model is None:
        return None
    return model.predict_proba(X)[:, 1]


def _save_meta_model(meta_model, horizon_key: str = '3d') -> None:
    with open(_horizon_paths(horizon_key)['meta_model'], 'wb') as fp:
        pickle.dump(meta_model, fp)


def _load_meta_model(horizon_key: str = '3d'):
    meta_path = _horizon_paths(horizon_key)['meta_model']
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, 'rb') as fp:
            return pickle.load(fp)
    except Exception:
        return None


def _available_base_model_names() -> list[str]:
    names = ['xgb']
    if _build_lgbm_model() is not None:
        names.append('lgbm')
    if _build_catboost_model() is not None:
        names.append('catboost')
    return names


def _build_base_models(scale_pos_weight: float = 1.0) -> dict:
    models = {'xgb': _build_model(scale_pos_weight=scale_pos_weight)}
    lgbm = _build_lgbm_model(scale_pos_weight=scale_pos_weight)
    cat = _build_catboost_model(scale_pos_weight=scale_pos_weight)
    if lgbm is not None:
        models['lgbm'] = lgbm
    if cat is not None:
        models['catboost'] = cat
    return models


def walk_forward_validate(X: np.ndarray, y: np.ndarray, n_splits: int = 8) -> dict:  # v6.2 A3: 10d 과적합 방지
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
        print(f'의존성 부족: {e}')
        return {}

    tscv = TimeSeriesSplit(n_splits=n_splits)
    fold_aucs, fold_precisions = [], []

    print(f'\n=== Walk-forward 교차검증 ({n_splits}폴드) ===')
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
        print(f'  Fold {fold}: AUC={auc:.3f}  Precision@0.65={prec:.3f}  '
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
    print(f'\n  평균 AUC: {result["mean_auc"]:.3f} ± {result["std_auc"]:.3f}')
    print(f'  평균 Precision@0.65: {result["mean_precision"]:.3f}')
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
        print('shap 미설치: pip install shap')
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

    print('\n=== SHAP 피처 기여도 (평균 절댓값) ===')
    for name, val in ranking[:10]:
        bar = '█' * int(val * 200)
        print(f'  {name:<20} {val:.4f}  {bar}')

    return {name: round(val, 6) for name, val in ranking}


def save_performance_metrics(
    horizon_key: str, auc: float, accuracy: float, buy_threshold: float,
) -> None:
    """v6: ML 성능 메트릭스를 brain/ml/performance.json에 저장.

    stock_trading_agent의 동적 블렌딩 비율 계산에 사용됨.
    """
    perf_path = MODEL_DIR / 'performance.json'
    try:
        existing = json.loads(perf_path.read_text(encoding='utf-8')) if perf_path.exists() else {}
    except Exception:
        existing = {}
    existing[horizon_key] = {
        'auc': round(float(auc), 4),
        'accuracy': round(float(accuracy), 4),
        'buy_threshold': round(float(buy_threshold), 2),
        'updated': _utc_now_iso(),
    }
    perf_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'성능 메트릭스 저장: {perf_path}')


def load_performance_metrics(horizon_key: str = '3d') -> dict:
    """v6: brain/ml/performance.json에서 ML 성능 메트릭스 로드."""
    perf_path = MODEL_DIR / 'performance.json'
    try:
        if perf_path.exists():
            data = json.loads(perf_path.read_text(encoding='utf-8'))
            return data.get(horizon_key, {})
    except Exception:
        pass
    return {}


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
        print('데이터 부족 (최소 100개 필요)')
        return

    wf = walk_forward_validate(X, y, n_splits=8)  # v6.2 A3: 10d 과적합 방지

    split_idx = int(len(X) * 0.8)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    pos = max(int(y_train.sum()), 1)
    neg = max(len(y_train) - pos, 1)
    scale_pos_weight = neg / pos

    base_model_names = _available_base_model_names()
    use_ensemble = len(base_model_names) >= 2
    print(f'\n기본 모델: {", ".join(base_model_names)}')
    print(f'최종 모델 학습: {len(X_train)}개 / OOS 테스트: {len(X_test)}개')

    final_models = {}
    ensemble_prob = None
    meta_model = None
    training_mode = 'xgb_only'
    base_auc = {}

    if use_ensemble:
        tscv = TimeSeriesSplit(n_splits=8)  # v6.2 A3: 10d 과적합 방지
        oof_preds = {name: np.full(len(X_train), np.nan, dtype=float) for name in base_model_names}

        for fold, (tr_idx, val_idx) in enumerate(tscv.split(X_train), 1):
            X_tr, X_val = X_train[tr_idx], X_train[val_idx]
            y_tr, y_val = y_train[tr_idx], y_train[val_idx]
            if len(np.unique(y_val)) < 2:
                continue
            fold_models = _build_base_models(scale_pos_weight=scale_pos_weight)
            print(f'  stacking fold {fold}: train={len(X_tr)} test={len(X_val)}')
            for name, model in fold_models.items():
                try:
                    _fit_model(model, X_tr, y_tr, X_val, y_val)
                    oof_preds[name][val_idx] = _predict_proba(model, X_val)
                except Exception as e:
                    print(f'    {name} 학습 실패: {e}')

        valid_cols = [name for name in base_model_names if not np.isnan(oof_preds[name]).all()]
        valid_mask = np.ones(len(X_train), dtype=bool)
        for name in valid_cols:
            valid_mask &= ~np.isnan(oof_preds[name])

        if len(valid_cols) >= 2 and valid_mask.sum() >= 50:
            meta_X = np.column_stack([oof_preds[name][valid_mask] for name in valid_cols])
            meta_y = y_train[valid_mask]
            meta_model = LogisticRegression(C=1.0, max_iter=1000, random_state=42)
            meta_model.fit(meta_X, meta_y)

            for name, model in _build_base_models(scale_pos_weight=scale_pos_weight).items():
                try:
                    _fit_model(model, X_train, y_train, X_test, y_test)
                    final_models[name] = model
                    probs = _predict_proba(model, X_test)
                    if probs is not None and len(np.unique(y_test)) > 1:
                        base_auc[name] = round(float(roc_auc_score(y_test, probs)), 4)
                except Exception as e:
                    print(f'최종 {name} 학습 실패: {e}')

            test_cols = [name for name in valid_cols if name in final_models]
            if len(test_cols) >= 2:
                meta_test_X = np.column_stack([_predict_proba(final_models[name], X_test) for name in test_cols])
                ensemble_prob = meta_model.predict_proba(meta_test_X)[:, 1]
                training_mode = 'stacking'

        if ensemble_prob is None:
            print('앙상블 스태킹 실패 → XGBoost 단독으로 폴백')

    if ensemble_prob is None:
        model = _build_model(scale_pos_weight=scale_pos_weight)
        _fit_model(model, X_train, y_train, X_test, y_test)
        final_models = {'xgb': model}
        ensemble_prob = _predict_proba(model, X_test)

    primary_model = final_models['xgb']
    y_pred = (ensemble_prob >= 0.65).astype(int)

    print('\n=== OOS 모델 성과 ===')
    print(f'  정확도:     {accuracy_score(y_test, y_pred) * 100:.1f}%')
    auc = 0.0
    ap = 0.0
    if len(np.unique(y_test)) > 1:
        auc = float(roc_auc_score(y_test, ensemble_prob))
        ap = float(average_precision_score(y_test, ensemble_prob))
        print(f'  AUC-ROC:    {auc:.3f}')
        print(f'  AP (PR):    {ap:.3f}')
    print(classification_report(y_test, y_pred, target_names=['관망', '매수']))

    importances = sorted(
        zip(FEATURE_NAMES, primary_model.feature_importances_),
        key=lambda x: x[1], reverse=True,
    )
    print('\n=== XGBoost 피처 중요도 TOP 10 ===')
    for name, imp in importances[:10]:
        print(f'  {name}: {imp:.4f}')

    shap_ranking = compute_shap_values(primary_model, X_test)

    # 최적 임계값 자동 계산: Precision >= 55% 기준 가장 낮은 임계값
    optimal_buy_thresh = 0.65  # default
    for _t in [0.50, 0.52, 0.55, 0.58, 0.60, 0.62, 0.65]:
        _mask = ensemble_prob >= _t
        if _mask.sum() < 3:
            continue
        _prec = float(y_test[_mask].mean())
        if _prec >= 0.55:
            optimal_buy_thresh = _t
            break

    thresholds = [0.5, 0.6, 0.65, 0.7, 0.8]
    print('\n=== 확률 임계값별 Precision ===')
    for thresh in thresholds:
        mask = ensemble_prob >= thresh
        if mask.sum() == 0:
            continue
        prec = y_test[mask].sum() / mask.sum() * 100
        print(f'  ≥{thresh:.2f}: {int(mask.sum())}건 → Precision {prec:.1f}%')
    print(f'  → 자동 선택 임계값: {optimal_buy_thresh:.2f}')

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
        'feature_names': FEATURE_NAMES,
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
    }
    paths['meta_json'].write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

    # v6: 성능 메트릭스 저장 (동적 블렌딩 비율 산출용)
    save_performance_metrics(horizon_key, auc, accuracy_score(y_test, y_pred), optimal_buy_thresh)

    print(f'\n모델 저장: {paths["xgb"]}')
    if 'lgbm' in final_models:
        print(f'LightGBM 저장: {paths["lgbm"]}')
    if 'catboost' in final_models:
        print(f'CatBoost 저장: {paths["catboost"]}')
    if meta_model is not None and training_mode == 'stacking':
        print(f'메타모델 저장: {paths["meta_model"]}')
    print(f'메타 저장: {paths["meta_json"]}')

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
        print('Supabase 미연결')
        return False

    # 실매매 결과 로드 (ml_features_json 포함: look-ahead bias 방지)
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
        print(f'거래 데이터 로드 실패: {e}')
        return False

    if len(rows) < min_samples:
        print(f'라이브 샘플 부족: {len(rows)} < {min_samples}')
        return False

    # 라이브 라벨 생성: pnl_pct >= 2% → 1(성공)
    # 매수 시점에 저장된 피처를 우선 사용 → look-ahead bias 방지
    live_X, live_y = [], []
    for r in rows:
        code = r.get('stock_code', '')
        pnl = float(r.get('pnl_pct') or 0)
        label = 1 if pnl >= 2.0 else 0
        # 저장된 피처 우선 사용
        stored_json = r.get('ml_features_json')
        if stored_json:
            try:
                stored = json.loads(stored_json)
                features = [float(stored.get(n, 0.0)) for n in FEATURE_NAMES]
                live_X.append(features)
                live_y.append(label)
                continue
            except Exception:
                pass
        # fallback: 현재 데이터로 피처 재계산
        pred = predict_stock(code, horizon_key=horizon_key)
        if 'error' in pred:
            continue
        features = list(pred['features'].values())
        live_X.append(features)
        live_y.append(label)

    if len(live_X) < min_samples:
        print(f'유효 라이브 샘플 부족: {len(live_X)}')
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
    print(f'\n실매매 데이터 {len(live_X)}건 추가 (성공: {live_wins} / 실패: {len(live_y)-live_wins})')
    print(f'전체 학습 데이터: {len(X_all)}건')

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
    print(f'실매매 반영 모델 저장: {paths["xgb"]}')
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
                print(f'[ml_model] 구버전 .pkl → .ubj 마이그레이션 완료')
                return model
            except Exception as e:
                print(f'[ml_model] 마이그레이션 실패: {e}')
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
        except Exception:
            bundle['lgbm'] = None

    if paths['catboost'].exists():
        try:
            from catboost import CatBoostClassifier
            model = CatBoostClassifier()
            model.load_model(str(paths['catboost']))
            bundle['catboost'] = model
        except Exception:
            bundle['catboost'] = None

    bundle['meta'] = _load_meta_model(horizon_key=horizon_key)
    if paths['meta_json'].exists():
        try:
            bundle['meta_info'] = json.loads(paths['meta_json'].read_text(encoding='utf-8'))
        except Exception:
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
        except Exception:
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
        except Exception:
            pass

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
        print('Supabase 미연결')
        return []
    model = _load_model(horizon_key=horizon_key)
    if model is None:
        print('모델 없음')
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

    print('\n=== 매수 확률 TOP 10 ===')
    for r in results[:10]:
        emoji = '🟢' if r['action'] == 'BUY' else '⚪'
        print(f"  {emoji} {r['name']}: {r['buy_probability']}% → {r['action']}")

    print(f'\n매수 신호: {sum(1 for r in results if r["action"] == "BUY")}종목')
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
