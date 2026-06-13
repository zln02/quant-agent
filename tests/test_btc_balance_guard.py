"""BTC 잔고 조회 방어막 테스트 — _fetch_balances_safe + execute_trade BALANCE_UNAVAILABLE.

PR #34: Upbit IP 인증 실패(no_authorization_ip)·API 장애 시 잔고를 0으로 오인하지
않고 사이클을 명시적으로 스킵하는지 검증. 신규 파일, 기존 테스트 무수정.

Mock 전략 (test_btc_drawdown_guard.py 패턴 준용):
- btc.btc_trading_agent import 전 모듈 레벨 env 선설정
- bta.upbit / bta.send_telegram / bta._fetch_balances_safe 를 patch.object
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── 모듈 레벨 환경변수 설정 (import 전 필수) ─────────────────────────────────
os.environ.setdefault("UPBIT_ACCESS_KEY", "test_access_key")
os.environ.setdefault("UPBIT_SECRET_KEY", "test_secret_key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test_supabase_key")
os.environ.setdefault("OPENAI_API_KEY", "test_openai_key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import btc.btc_trading_agent as bta  # noqa: E402 (env vars must be set first)


@pytest.fixture(autouse=True)
def _silence_telegram():
    """방어막의 실패 알림이 실제 텔레그램/자비스로 나가지 않게 차단."""
    with patch.object(bta, "send_telegram"):
        yield


def _mk_upbit(*, returns=None, side_effect=None) -> MagicMock:
    m = MagicMock()
    if side_effect is not None:
        m.get_balances.side_effect = side_effect
    else:
        m.get_balances.return_value = returns
    return m


# ── _fetch_balances_safe ─────────────────────────────────────────────────

def test_normal_list_normalized_to_dict():
    """정상 list 응답 → {통화: float} dict로 정규화."""
    upbit = _mk_upbit(returns=[
        {"currency": "KRW", "balance": "150000.5"},
        {"currency": "BTC", "balance": "0.25"},
    ])
    with patch.object(bta, "upbit", upbit):
        out = bta._fetch_balances_safe()
    assert out == {"KRW": 150000.5, "BTC": 0.25}


def test_error_dict_returns_none():
    """IP 미인증 에러 dict → None (0으로 오인 금지)."""
    upbit = _mk_upbit(returns={
        "name": "no_authorization_ip",
        "message": "This is not a verified IP.",
    })
    with patch.object(bta, "upbit", upbit):
        out = bta._fetch_balances_safe()
    assert out is None


def test_get_balances_exception_returns_none():
    """get_balances 예외 → None."""
    upbit = _mk_upbit(side_effect=RuntimeError("connection reset"))
    with patch.object(bta, "upbit", upbit):
        out = bta._fetch_balances_safe()
    assert out is None


def test_upbit_none_returns_none():
    """upbit 미초기화(키 부재) → None."""
    with patch.object(bta, "upbit", None):
        out = bta._fetch_balances_safe()
    assert out is None


def test_malformed_balance_defaults_zero_and_skips_no_currency():
    """잘못된 balance 값은 0.0, currency 없는 항목은 스킵."""
    upbit = _mk_upbit(returns=[
        {"currency": "KRW", "balance": None},
        {"currency": "BTC", "balance": "not-a-number"},
        {"balance": "1.0"},  # currency 없음 → 스킵
    ])
    with patch.object(bta, "upbit", upbit):
        out = bta._fetch_balances_safe()
    assert out == {"KRW": 0.0, "BTC": 0.0}


# ── execute_trade BALANCE_UNAVAILABLE 분기 ────────────────────────────────

def test_execute_trade_skips_when_balance_unavailable():
    """잔고 조회 실패 시 0 진행하지 않고 BALANCE_UNAVAILABLE 반환."""
    signal = {"action": "SELL", "confidence": 999}  # action!=BUY → 진입 필터 우회
    indicators = {"price": 100_000_000}
    with patch.object(bta, "_fetch_balances_safe", return_value=None):
        result = bta.execute_trade(signal, indicators)
    assert result == {"result": "BALANCE_UNAVAILABLE"}
