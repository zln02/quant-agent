"""PR #28: SpaceX 노출 ETF 유니버스 추가 + 보수적 사이징 테스트."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stocks"))


def test_us_universe_includes_spacex_proxies():
    from us_momentum_backtest import US_UNIVERSE
    for sym in ("DXYZ", "NASA", "ARKX", "XOVR"):
        assert sym in US_UNIVERSE, f"{sym} 가 US_UNIVERSE 에 없음"


def test_spacex_proxy_meta_keys_consistent():
    from us_momentum_backtest import SPACEX_PROXY_META, US_UNIVERSE

    # META 의 모든 키는 US_UNIVERSE 에 있어야 함
    for sym in SPACEX_PROXY_META.keys():
        assert sym in US_UNIVERSE, f"META 의 {sym} 가 US_UNIVERSE 에 없음"
    # 프록시 ETF 4종 + SPCX 본주(PR #33) 모두 메타 포함
    assert set(SPACEX_PROXY_META.keys()) == {"DXYZ", "NASA", "ARKX", "XOVR", "SPCX"}


def test_spcx_registered_for_ipo():
    """PR #33: SPCX 본주가 유니버스 등록 + 고변동성 사이징."""
    from us_momentum_backtest import (SPACEX_PROXY_META, US_UNIVERSE,
                                      is_spacex_proxy)
    assert "SPCX" in US_UNIVERSE
    assert is_spacex_proxy("SPCX") is True
    # IPO 신규상장 고변동성 → premium_warn=True (사이즈 50%)
    assert SPACEX_PROXY_META["SPCX"]["premium_warn"] is True
    assert SPACEX_PROXY_META["SPCX"]["spacex_pct"] == 100.0


def test_is_spacex_proxy_recognises_all_four():
    from us_momentum_backtest import is_spacex_proxy
    for sym in ("DXYZ", "NASA", "ARKX", "XOVR"):
        assert is_spacex_proxy(sym) is True
    # 대소문자 무관
    assert is_spacex_proxy("dxyz") is True
    # 일반 종목은 False
    for sym in ("AAPL", "TSLA", "SPY", "QQQ"):
        assert is_spacex_proxy(sym) is False


def test_is_spacex_proxy_empty_and_garbage():
    from us_momentum_backtest import is_spacex_proxy
    assert is_spacex_proxy("") is False
    assert is_spacex_proxy("XX_FAKE") is False


def test_dxyz_marked_premium_warn():
    """DXYZ 는 NAV premium 큼 — premium_warn=True."""
    from us_momentum_backtest import SPACEX_PROXY_META
    assert SPACEX_PROXY_META["DXYZ"]["premium_warn"] is True
    assert SPACEX_PROXY_META["DXYZ"]["spacex_pct"] == 16.2


def test_other_proxies_not_premium_warn():
    """DXYZ 외 3개는 premium_warn=False."""
    from us_momentum_backtest import SPACEX_PROXY_META
    for sym in ("NASA", "ARKX", "XOVR"):
        assert SPACEX_PROXY_META[sym]["premium_warn"] is False
