"""PR #25: BTC ML 모델 인프라 테스트 — 피처 추출 + Triple Barrier."""
from __future__ import annotations

import numpy as np


def _make_series(n: int = 100, base: float = 50_000_000.0, vol: float = 0.005, seed: int = 42):
    rng = np.random.default_rng(seed)
    returns = rng.normal(0, vol, n)
    closes = base * np.cumprod(1 + returns)
    highs = closes * (1 + np.abs(rng.normal(0, vol / 2, n)))
    lows = closes * (1 - np.abs(rng.normal(0, vol / 2, n)))
    volumes = np.abs(rng.normal(100, 20, n))
    return closes, volumes, highs, lows


def test_extract_features_returns_correct_length():
    from btc import btc_ml_model as bm
    closes, volumes, highs, lows = _make_series(100)
    feats = bm.extract_features(closes, volumes, highs, lows, 99)
    assert feats is not None
    assert len(feats) == len(bm.FEATURE_NAMES) == 10


def test_extract_features_too_early_returns_none():
    from btc import btc_ml_model as bm
    closes, volumes, highs, lows = _make_series(100)
    # idx < FEATURE_LOOKBACK (24) → None
    assert bm.extract_features(closes, volumes, highs, lows, 10) is None


def test_extract_features_all_finite():
    from btc import btc_ml_model as bm
    closes, volumes, highs, lows = _make_series(100)
    feats = bm.extract_features(closes, volumes, highs, lows, 99)
    assert all(np.isfinite(f) for f in feats)


def test_rsi_extreme_values():
    from btc import btc_ml_model as bm
    rising = np.linspace(100, 200, 30)
    falling = np.linspace(200, 100, 30)
    assert bm._rsi(rising, 14) > 70   # 강세 RSI 높음
    assert bm._rsi(falling, 14) < 30  # 약세 RSI 낮음


def test_bb_position_within_unit_range():
    from btc import btc_ml_model as bm
    closes = np.array([100.0] * 30)
    pos = bm._bb_position(closes, 20)
    # std=0 → 0.5 반환
    assert pos == 0.5
    closes2 = np.concatenate([np.full(20, 100.0), np.array([150.0])])
    pos2 = bm._bb_position(closes2, 20)
    assert 0.0 <= pos2 <= 1.0


def test_triple_barrier_tp_first():
    from btc import btc_ml_model as bm

    # i=0, TARGET_HORIZON=6, TARGET_RETURN=0.02
    closes = np.array([100.0, 101.0, 103.0, 100.0, 100.0, 100.0, 100.0])
    highs = np.array([101.0, 102.0, 103.0, 101.0, 101.0, 101.0, 101.0])
    lows = np.array([99.0, 100.0, 101.0, 99.0, 99.0, 99.0, 99.0])
    # day2에 high 103 → TP 102 닿음 → 1
    assert bm._triple_barrier_label(closes, highs, lows, 0) == 1


def test_triple_barrier_sl_first():
    from btc import btc_ml_model as bm
    closes = np.array([100.0, 99.0, 105.0, 100.0, 100.0, 100.0, 100.0])
    highs = np.array([100.0, 100.0, 105.0, 100.0, 100.0, 100.0, 100.0])
    lows = np.array([100.0, 97.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    # day1 low 97 → SL 98 먼저 닿음 → 0
    assert bm._triple_barrier_label(closes, highs, lows, 0) == 0


def test_predict_btc_no_model_returns_hold(tmp_path, monkeypatch):
    from btc import btc_ml_model as bm
    monkeypatch.setattr(bm, "MODEL_PATH", tmp_path / "nonexistent.ubj")
    result = bm.predict_btc()
    assert result["action"] == "HOLD"
    assert "NO_MODEL" in result["source"]


def test_drift_report_no_model(tmp_path, monkeypatch):
    from btc import btc_ml_model as bm
    monkeypatch.setattr(bm, "PERF_PATH", tmp_path / "nonexistent.json")
    report = bm.build_drift_report()
    assert report["status"] == "NO_MODEL"


def test_psi_stable_distribution_low():
    from btc import btc_ml_model as bm
    rng = np.random.default_rng(0)
    a = rng.normal(0, 1, 500)
    b = rng.normal(0, 1, 500)
    psi = bm._psi(a, b)
    assert psi < 0.10  # 같은 분포 → stable


def test_psi_shifted_distribution_high():
    from btc import btc_ml_model as bm
    rng = np.random.default_rng(0)
    a = rng.normal(0, 1, 500)
    b = rng.normal(3, 1, 500)   # mean shift
    psi = bm._psi(a, b)
    assert psi > 0.25  # 큰 shift → danger
