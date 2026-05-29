"""PR #27: CrossMarketRiskManager evaluate 자동 equity 추정 테스트."""
from __future__ import annotations

from unittest.mock import patch


def _make_snapshot(market: str, value: float):
    """MarketSnapshot dataclass stub."""
    from quant.risk.cross_market_manager import MarketSnapshot
    return MarketSnapshot(market=market, position_value=value)


def test_evaluate_uses_equity_loader_when_no_capital(monkeypatch):
    from quant.risk import cross_market_manager as cmm

    mgr = cmm.CrossMarketRiskManager()
    monkeypatch.setattr(mgr, "_load_btc_snapshot", lambda: _make_snapshot("btc", 10_000_000.0))
    monkeypatch.setattr(mgr, "_load_kr_snapshot", lambda: _make_snapshot("kr", 5_000_000.0))
    monkeypatch.setattr(mgr, "_load_us_snapshot", lambda: _make_snapshot("us", 2_000.0))

    # equity_loader.load_equity_curve mock — 모듈 import 시 evaluate 안에서 호출
    def fake_curve(market, lookback_days=2):
        return {
            'btc': [{'equity': 22_000_000.0}],
            'kr': [{'equity': 18_000_000.0}],
            'us': [{'equity': 10_000.0}],
        }[market]

    with patch("common.equity_loader.load_equity_curve", side_effect=fake_curve):
        result = mgr.evaluate(total_capital=0.0)

    # 자동 추정 — total_equity 양수 (>0) 이어야 함
    assert result.total_equity > 0
    # 22M + 18M + 10K*1350 = 22M + 18M + 13.5M = 53.5M
    assert abs(result.total_equity - 53_500_000.0) < 1.0


def test_evaluate_falls_back_when_no_equity_available(monkeypatch):
    """equity_loader 가 빈 데이터 반환 → debug 로그 + total_equity=0."""
    from quant.risk import cross_market_manager as cmm

    mgr = cmm.CrossMarketRiskManager()
    monkeypatch.setattr(mgr, "_load_btc_snapshot", lambda: _make_snapshot("btc", 0.0))
    monkeypatch.setattr(mgr, "_load_kr_snapshot", lambda: _make_snapshot("kr", 0.0))
    monkeypatch.setattr(mgr, "_load_us_snapshot", lambda: _make_snapshot("us", 0.0))

    with patch("common.equity_loader.load_equity_curve", return_value=[]):
        result = mgr.evaluate(total_capital=0.0)

    assert result.total_equity == 0.0
    assert result.buy_blocked is False  # fail-open
    assert result.total_exposure == 0.0


def test_evaluate_explicit_total_capital_skips_estimation(monkeypatch):
    """명시적 total_capital 주면 equity_loader 미호출."""
    from quant.risk import cross_market_manager as cmm

    mgr = cmm.CrossMarketRiskManager()
    monkeypatch.setattr(mgr, "_load_btc_snapshot", lambda: _make_snapshot("btc", 10_000_000.0))
    monkeypatch.setattr(mgr, "_load_kr_snapshot", lambda: _make_snapshot("kr", 5_000_000.0))
    monkeypatch.setattr(mgr, "_load_us_snapshot", lambda: _make_snapshot("us", 0.0))

    called = []
    def fake_curve(market, lookback_days=2):
        called.append(market)
        return []

    with patch("common.equity_loader.load_equity_curve", side_effect=fake_curve):
        result = mgr.evaluate(total_capital=100_000_000.0)

    assert result.total_equity == 100_000_000.0
    assert called == []  # equity_loader 미호출
