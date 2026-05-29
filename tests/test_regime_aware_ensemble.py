"""PR #25: regime-aware fallback blending + 임계값 조정 단위 테스트."""
from __future__ import annotations

import os

# ml_model module-level supabase.create_client 우회 (mock-key가 신규 supabase에서 reject).
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SECRET_KEY"] = ""
os.environ["SUPABASE_KEY"] = ""


def test_regime_weighted_ensemble_bull_favors_xgb(monkeypatch):
    from stocks import ml_model as mm
    probs = {'xgb': 0.9, 'lgbm': 0.5, 'catboost': 0.5}
    # 단순 평균: 0.6333. xgb 가중 1.2 → 더 높아져야 함.
    result = mm._regime_weighted_ensemble(probs, regime='BULL')
    assert result > 0.6333
    # 검증: 정확한 가중평균
    expected = (0.9 * 1.2 + 0.5 * 1.0 + 0.5 * 0.9) / (1.2 + 1.0 + 0.9)
    assert abs(result - expected) < 1e-9


def test_regime_weighted_ensemble_bear_favors_lgbm(monkeypatch):
    from stocks import ml_model as mm
    probs = {'xgb': 0.9, 'lgbm': 0.5, 'catboost': 0.5}
    result = mm._regime_weighted_ensemble(probs, regime='BEAR')
    # BEAR: xgb 0.9, lgbm 1.2 → xgb 영향 약해짐 → 단순평균보다 낮아야
    assert result < 0.6333


def test_regime_weighted_ensemble_transition_matches_simple_avg(monkeypatch):
    from stocks import ml_model as mm
    probs = {'xgb': 0.7, 'lgbm': 0.5, 'catboost': 0.6}
    result = mm._regime_weighted_ensemble(probs, regime='TRANSITION')
    # 모든 가중치 1.0 → 단순평균과 일치
    simple = sum(probs.values()) / len(probs)
    assert abs(result - simple) < 1e-9


def test_regime_weighted_ensemble_unknown_regime_falls_back():
    from stocks import ml_model as mm
    probs = {'xgb': 0.8, 'lgbm': 0.6}
    result = mm._regime_weighted_ensemble(probs, regime='UNKNOWN_X')
    # TRANSITION 가중치 (1.0/1.0/1.0) 적용 → 평균
    assert abs(result - 0.7) < 1e-9


def test_regime_weighted_ensemble_empty_probs_returns_zero():
    from stocks import ml_model as mm
    assert mm._regime_weighted_ensemble({}, regime='BULL') == 0.0


def test_regime_threshold_bull_lower():
    from stocks import ml_model as mm
    assert mm.get_regime_adjusted_ml_threshold(base=0.78, regime='BULL') == 0.76
    assert mm.get_regime_adjusted_ml_threshold(base=0.78, regime='RISK_ON') == 0.76


def test_regime_threshold_bear_higher():
    from stocks import ml_model as mm
    assert abs(mm.get_regime_adjusted_ml_threshold(base=0.78, regime='BEAR') - 0.83) < 1e-9
    assert abs(mm.get_regime_adjusted_ml_threshold(base=0.78, regime='RISK_OFF') - 0.83) < 1e-9


def test_regime_threshold_crisis_much_higher():
    from stocks import ml_model as mm
    assert abs(mm.get_regime_adjusted_ml_threshold(base=0.78, regime='CRISIS') - 0.88) < 1e-9


def test_regime_threshold_clamped():
    from stocks import ml_model as mm

    # base 0.95 + CRISIS +0.10 → 1.05 → clamped to 0.95
    assert mm.get_regime_adjusted_ml_threshold(base=0.95, regime='CRISIS') == 0.95
    # base 0.45 + TRANSITION → 0.45 → clamped to 0.50
    assert mm.get_regime_adjusted_ml_threshold(base=0.45, regime='TRANSITION') == 0.50
