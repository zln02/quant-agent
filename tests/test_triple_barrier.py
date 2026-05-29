"""PR #25: Triple Barrier labeling 단위 테스트.

train 함수 직접 실행은 Supabase 의존이라 라벨링 로직을 격리 검증.
"""
from __future__ import annotations


def _label_triple_barrier(closes, highs, lows, i, target_days, target_return):
    """ml_model.load_training_data 내부 라벨링 로직 미러 (테스트 격리용)."""
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
        if _lo <= sl_price:
            label = 0
            break
        if _hi >= tp_price:
            label = 1
            break
    if label is None:
        _final = closes[min(i + target_days, len(closes) - 1)]
        label = 1 if (_final - entry) / max(entry, 1) >= 0 else 0
    return label


def test_tp_first_yields_positive_label():
    closes = [100, 101, 103, 102]
    highs = [101, 102, 103, 103]   # day2에 103 → TP +2%
    lows = [100, 100, 100, 100]
    assert _label_triple_barrier(closes, highs, lows, 0, 3, 0.02) == 1


def test_sl_first_yields_negative_label():
    closes = [100, 99, 98, 102]
    highs = [101, 100, 100, 103]   # day3엔 103이지만
    lows = [98, 97, 97, 100]       # day2에 97 → SL -2% 먼저 닿음
    assert _label_triple_barrier(closes, highs, lows, 0, 3, 0.02) == 0


def test_neither_touches_uses_close_sign():
    closes = [100, 101, 100.5, 100.3]
    highs = [101, 101, 101, 101]   # TP +2% 못 닿음
    lows = [99, 99, 99, 99]        # SL -2% 못 닿음
    # 종가 100.3 > 100 → 1
    assert _label_triple_barrier(closes, highs, lows, 0, 3, 0.02) == 1


def test_neither_touches_negative_close_yields_zero():
    closes = [100, 100, 99.8, 99.5]
    highs = [101, 101, 101, 101]
    lows = [99, 99, 99, 99]
    # 종가 99.5 < 100 → 0
    assert _label_triple_barrier(closes, highs, lows, 0, 3, 0.02) == 0


def test_same_bar_sl_takes_precedence():
    """같은 봉에 TP/SL 둘 다 닿으면 SL 우선 (보수적)."""
    closes = [100, 101]
    highs = [101, 103]    # high 103 → TP touched
    lows = [99, 97]       # low 97 → SL touched (먼저 검사)
    assert _label_triple_barrier(closes, highs, lows, 0, 1, 0.02) == 0
