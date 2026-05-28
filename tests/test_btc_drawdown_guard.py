"""BTC DrawdownGuard 통합 테스트 — _apply_drawdown_guard_btc 9 unit tests.

Step 3: 신규 테스트 파일. 기존 파일 수정 없음.

Mock 전략:
- btc.btc_trading_agent import 시 환경변수 필요 → 모듈 레벨에서 os.environ 선설정
- load_equity_curve / load_drawdown_state / save_drawdown_state 는 patch.object
- returns_from_equity_curve 는 DrawdownGuard 클래스 메서드를 patch → daily/weekly/monthly 직접 주입
- close_all_positions / _execute_sell_order / get_open_position 는 patch.object
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# ── 모듈 레벨 환경변수 설정 (import 전 필수) ─────────────────────────────────
os.environ.setdefault("UPBIT_ACCESS_KEY", "test_access_key")
os.environ.setdefault("UPBIT_SECRET_KEY", "test_secret_key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test_supabase_key")
os.environ.setdefault("OPENAI_API_KEY", "test_openai_key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import btc.btc_trading_agent as bta  # noqa: E402 (env vars must be set first)

# ── 헬퍼 ────────────────────────────────────────────────────────────────────

_BASE_EQUITY = [
    {"date": "2026-04-06", "equity": 1_000_000.0},
    {"date": "2026-05-06", "equity": 1_000_000.0},
]

_NORMAL_EQUITY = [
    {"date": "2026-04-06", "equity": 1_000_000.0},
    {"date": "2026-05-06", "equity": 1_010_000.0},  # +1% monthly — 모든 한도 통과
]


def _returns(daily=0.0, weekly=0.0, monthly=0.0) -> dict:
    """returns_from_equity_curve patch 용 반환값."""
    return {"daily_return": daily, "weekly_return": weekly, "monthly_return": monthly}


def _pos(quantity=0.01):
    """get_open_position 반환용 포지션 dict."""
    return {"quantity": quantity, "avg_price": 100_000_000.0}


# ── 테스트 클래스 ────────────────────────────────────────────────────────────

class TestApplyDrawdownGuardBtc:
    """_apply_drawdown_guard_btc 9 unit tests."""

    def setup_method(self):
        """매 테스트 시작 시 _btc_buy_blocked 리셋."""
        bta._btc_buy_blocked = False

    # ── 1. equity_curve 빈 list → False, side effect 0 ───────────────────────

    def test_no_equity_curve_returns_false(self):
        """load_equity_curve 가 빈 list 반환 시 즉시 False, 어떤 side effect 도 없음."""
        with patch.object(bta, "load_equity_curve", return_value=[]) as mock_ec, \
             patch.object(bta, "load_drawdown_state") as mock_lds, \
             patch.object(bta, "save_drawdown_state") as mock_sds, \
             patch.object(bta, "close_all_positions") as mock_close, \
             patch.object(bta, "_execute_sell_order") as mock_sell:

            result = bta._apply_drawdown_guard_btc(current_price=100_000_000.0)

        assert result is False
        assert bta._btc_buy_blocked is False
        mock_lds.assert_not_called()
        mock_sds.assert_not_called()
        mock_close.assert_not_called()
        mock_sell.assert_not_called()

    # ── 2. daily/weekly/monthly 정상 → False, buy 차단 없음 ─────────────────

    def test_normal_returns_false_buy_not_blocked(self):
        """모든 지표 정상 구간 → False 반환, _btc_buy_blocked=False."""
        with patch.object(bta, "load_equity_curve", return_value=_NORMAL_EQUITY), \
             patch.object(bta, "load_drawdown_state", return_value={}), \
             patch.object(bta, "save_drawdown_state") as mock_save, \
             patch("quant.risk.drawdown_guard.DrawdownGuard.returns_from_equity_curve",
                   return_value=_returns(daily=0.005, weekly=0.01, monthly=0.01)), \
             patch.object(bta, "get_open_position", return_value=None):

            result = bta._apply_drawdown_guard_btc(current_price=100_000_000.0)

        assert result is False
        assert bta._btc_buy_blocked is False
        mock_save.assert_called_once()

    # ── 3. daily=-0.04 (한도 -0.03 초과) → False, _btc_buy_blocked=True ────

    def test_daily_loss_block_sets_buy_blocked(self):
        """daily_return=-0.04 → DAILY_BUY_BLOCK → False 반환, _btc_buy_blocked=True."""
        with patch.object(bta, "load_equity_curve", return_value=_BASE_EQUITY), \
             patch.object(bta, "load_drawdown_state", return_value={}), \
             patch.object(bta, "save_drawdown_state"), \
             patch("quant.risk.drawdown_guard.DrawdownGuard.returns_from_equity_curve",
                   return_value=_returns(daily=-0.04, weekly=-0.01, monthly=-0.01)), \
             patch.object(bta, "get_open_position", return_value=None):

            result = bta._apply_drawdown_guard_btc(current_price=100_000_000.0)

        assert result is False
        assert bta._btc_buy_blocked is True

    # ── 4. weekly=-0.10 → DELEVERAGE → _execute_sell_order 호출 (50% qty) ──

    def test_weekly_deleverage_calls_sell_order(self):
        """weekly_return=-0.10 → WEEKLY_DELEVERAGE → 50% qty _execute_sell_order 호출."""
        pos = _pos(quantity=0.1)
        with patch.object(bta, "load_equity_curve", return_value=_BASE_EQUITY), \
             patch.object(bta, "load_drawdown_state", return_value={}), \
             patch.object(bta, "save_drawdown_state"), \
             patch("quant.risk.drawdown_guard.DrawdownGuard.returns_from_equity_curve",
                   return_value=_returns(daily=-0.01, weekly=-0.10, monthly=-0.05)), \
             patch.object(bta, "get_open_position", return_value=pos), \
             patch.object(bta, "_execute_sell_order", return_value=(True, "OK")) as mock_sell, \
             patch.object(bta, "close_all_positions") as mock_close:

            result = bta._apply_drawdown_guard_btc(current_price=100_000_000.0)

        assert result is False
        mock_sell.assert_called_once()
        # 50% qty = 0.05
        sell_qty_arg = mock_sell.call_args[0][0]
        assert abs(sell_qty_arg - 0.05) < 1e-9
        mock_close.assert_not_called()

    # ── 5. monthly=-0.20 → True, close_all_positions 호출 ───────────────────

    def test_monthly_stop_returns_true_calls_close_all(self):
        """monthly_return=-0.20 → MONTHLY_STOP → True 반환, close_all_positions 호출."""
        pos = _pos(quantity=0.05)
        with patch.object(bta, "load_equity_curve", return_value=_BASE_EQUITY), \
             patch.object(bta, "load_drawdown_state", return_value={}), \
             patch.object(bta, "save_drawdown_state"), \
             patch("quant.risk.drawdown_guard.DrawdownGuard.returns_from_equity_curve",
                   return_value=_returns(daily=-0.02, weekly=-0.09, monthly=-0.20)), \
             patch.object(bta, "get_open_position", return_value=pos), \
             patch.object(bta, "close_all_positions", return_value=True) as mock_close, \
             patch.object(bta, "_execute_sell_order") as mock_sell:

            result = bta._apply_drawdown_guard_btc(current_price=100_000_000.0)

        assert result is True
        mock_close.assert_called_once_with(100_000_000.0, exit_reason="DRAWDOWN_FULL_STOP")
        mock_sell.assert_not_called()

    # ── 6. save_drawdown_state 정확히 1회 호출 ──────────────────────────────

    def test_state_saved_after_evaluate(self):
        """정상 경로에서 save_drawdown_state('btc', ...) 가 정확히 1회 호출됨."""
        with patch.object(bta, "load_equity_curve", return_value=_NORMAL_EQUITY), \
             patch.object(bta, "load_drawdown_state", return_value={}), \
             patch.object(bta, "save_drawdown_state") as mock_save, \
             patch("quant.risk.drawdown_guard.DrawdownGuard.returns_from_equity_curve",
                   return_value=_returns(daily=0.0, weekly=0.0, monthly=0.0)), \
             patch.object(bta, "get_open_position", return_value=None):

            bta._apply_drawdown_guard_btc(current_price=100_000_000.0)

        mock_save.assert_called_once()
        # 첫 번째 인자는 'btc', 두 번째는 dict
        args = mock_save.call_args[0]
        assert args[0] == "btc"
        assert isinstance(args[1], dict)

    # ── 7. load_drawdown_state 알 수 없는 키 dict → TypeError → 빈 state ────

    def test_legacy_state_typeerror_falls_back(self):
        """load_drawdown_state 가 알 수 없는 키가 있는 dict 반환 → TypeError → DrawdownGuardState() 기본값 사용.

        DrawdownGuardState(**{"unknown_key": "val"}) → TypeError → except 분기.
        """
        legacy_bad_state = {
            "cooldown_until": None,
            "last_action": "NONE",
            "unknown_legacy_key": "some_value",  # DrawdownGuardState 에 없는 키
        }
        with patch.object(bta, "load_equity_curve", return_value=_NORMAL_EQUITY), \
             patch.object(bta, "load_drawdown_state", return_value=legacy_bad_state), \
             patch.object(bta, "save_drawdown_state") as mock_save, \
             patch("quant.risk.drawdown_guard.DrawdownGuard.returns_from_equity_curve",
                   return_value=_returns(daily=0.0, weekly=0.0, monthly=0.0)), \
             patch.object(bta, "get_open_position", return_value=None):

            # TypeError fallback → DrawdownGuardState() 기본값으로 evaluate 진행 → 예외 없이 False
            result = bta._apply_drawdown_guard_btc(current_price=100_000_000.0)

        assert result is False
        # fallback 후에도 save_drawdown_state 는 호출돼야 함
        mock_save.assert_called_once()

    # ── 8. _btc_buy_blocked 매 호출 시 False reset ───────────────────────────

    def test_buy_blocked_module_global_resets_each_call(self):
        """첫 호출: daily 손실 → _btc_buy_blocked=True.
        두 번째 호출: 정상 → _btc_buy_blocked 가 False 로 리셋됨.
        """
        with patch.object(bta, "load_equity_curve", return_value=_BASE_EQUITY), \
             patch.object(bta, "load_drawdown_state", return_value={}), \
             patch.object(bta, "save_drawdown_state"), \
             patch("quant.risk.drawdown_guard.DrawdownGuard.returns_from_equity_curve",
                   return_value=_returns(daily=-0.04, weekly=-0.01, monthly=-0.01)), \
             patch.object(bta, "get_open_position", return_value=None):

            bta._apply_drawdown_guard_btc(current_price=100_000_000.0)

        # 첫 호출 후 True
        assert bta._btc_buy_blocked is True

        # 두 번째 호출: 정상 수익률
        with patch.object(bta, "load_equity_curve", return_value=_NORMAL_EQUITY), \
             patch.object(bta, "load_drawdown_state", return_value={}), \
             patch.object(bta, "save_drawdown_state"), \
             patch("quant.risk.drawdown_guard.DrawdownGuard.returns_from_equity_curve",
                   return_value=_returns(daily=0.005, weekly=0.01, monthly=0.01)), \
             patch.object(bta, "get_open_position", return_value=None):

            bta._apply_drawdown_guard_btc(current_price=100_000_000.0)

        # 두 번째 호출에서 함수 진입 시 False reset → 정상 수익률이므로 그대로 False
        assert bta._btc_buy_blocked is False

    # ── 9. monthly=-0.20 이지만 pos=None → close 호출 안 함, True 반환 ──────

    def test_no_position_monthly_stop_skips_close(self):
        """monthly_return=-0.20 (MONTHLY_STOP) 이지만 get_open_position()=None.

        → close_all_positions 호출 안 함, True 반환 (사이클 종료 의미).
        btc_trading_agent.py 965~967 라인:
            pos = get_open_position()
            if pos:
                close_all_positions(...)
        """
        with patch.object(bta, "load_equity_curve", return_value=_BASE_EQUITY), \
             patch.object(bta, "load_drawdown_state", return_value={}), \
             patch.object(bta, "save_drawdown_state"), \
             patch("quant.risk.drawdown_guard.DrawdownGuard.returns_from_equity_curve",
                   return_value=_returns(daily=-0.02, weekly=-0.09, monthly=-0.20)), \
             patch.object(bta, "get_open_position", return_value=None), \
             patch.object(bta, "close_all_positions", return_value=True) as mock_close, \
             patch.object(bta, "_execute_sell_order") as mock_sell:

            result = bta._apply_drawdown_guard_btc(current_price=100_000_000.0)

        # MONTHLY_STOP → True
        assert result is True
        # pos=None 이므로 close_all_positions 미호출
        mock_close.assert_not_called()
        mock_sell.assert_not_called()
