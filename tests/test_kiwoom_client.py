"""tests/test_kiwoom_client.py — STRATEGY_MAP / place_order 추상화 단위 테스트.

PR #9a 검증 항목:
- STRATEGY_MAP 키 + 키움 REST 매직 코드 매핑 정확성
- place_order() 시그니처 하위 호환 (price 기반 자동 추론)
- order_strategy 명시 시 우선
- 알 수 없는 strategy → ValueError
- retries=0 보존 (kiwoom_client.py 절대 규칙 #2 — 이중주문 방지)
"""
from unittest.mock import MagicMock

import pytest

from stocks.kiwoom_client import STRATEGY_MAP, KiwoomClient


@pytest.fixture
def kiwoom_client(monkeypatch):
    """env monkeypatch로 KiwoomClient() 정상 init, _call_api는 Mock으로 우회."""
    monkeypatch.setenv("TRADING_ENV", "mock")
    monkeypatch.setenv("KIWOOM_MOCK_REST_API_APP_KEY", "test-key")
    monkeypatch.setenv("KIWOOM_MOCK_REST_API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("KIWOOM_MOCK_ACCOUNT_NO", "5012345678")
    client = KiwoomClient()
    client._call_api = MagicMock(return_value={"ord_no": "ORDER_123", "return_msg": "OK"})
    return client


def test_strategy_map_has_limit_and_market():
    """STRATEGY_MAP은 LIMIT, MARKET 두 키만 가진다 (IOC/FOK는 PR #9b)."""
    assert set(STRATEGY_MAP.keys()) == {"LIMIT", "MARKET"}


def test_strategy_map_field_pairs_match_kiwoom_spec():
    """STRATEGY_MAP 값은 키움 REST 스펙(trde_tp + ord_prc_ptn_cd)을 정확히 매핑."""
    assert STRATEGY_MAP["LIMIT"] == {"trde_tp": "0", "ord_prc_ptn_cd": "00"}
    assert STRATEGY_MAP["MARKET"] == {"trde_tp": "3", "ord_prc_ptn_cd": "03"}


def test_place_order_default_market_when_price_zero(kiwoom_client):
    """price=0 → order_strategy 미지정 시 MARKET 자동 추론 (하위 호환)."""
    kiwoom_client.place_order("005930", "sell", 10)
    body = kiwoom_client._call_api.call_args.kwargs["body"]
    assert body["trde_tp"] == "3"
    assert body["ord_prc_ptn_cd"] == "03"


def test_place_order_default_limit_when_price_positive(kiwoom_client):
    """price>0 → order_strategy 미지정 시 LIMIT 자동 추론 (하위 호환)."""
    kiwoom_client.place_order("005930", "buy", 10, price=70000)
    body = kiwoom_client._call_api.call_args.kwargs["body"]
    assert body["trde_tp"] == "0"
    assert body["ord_prc_ptn_cd"] == "00"


def test_place_order_explicit_strategy_overrides_price_default(kiwoom_client):
    """order_strategy 명시 시 price 기반 추론보다 우선 (예: price=0이어도 LIMIT 강제)."""
    kiwoom_client.place_order("005930", "sell", 10, price=0, order_strategy="LIMIT")
    body = kiwoom_client._call_api.call_args.kwargs["body"]
    assert body["trde_tp"] == "0"
    assert body["ord_prc_ptn_cd"] == "00"


def test_place_order_unknown_strategy_raises(kiwoom_client):
    """알 수 없는 order_strategy 값 → ValueError ('IOC' 등 PR #9b 후보 포함)."""
    with pytest.raises(ValueError, match="unknown order_strategy"):
        kiwoom_client.place_order("005930", "buy", 10, order_strategy="IOC")


def test_place_order_retries_zero(kiwoom_client):
    """절대 규칙 #2: _call_api 호출 시 retries=0 강제 보존 (이중주문 방지)."""
    kiwoom_client.place_order("005930", "sell", 10)
    assert kiwoom_client._call_api.call_args.kwargs["retries"] == 0
