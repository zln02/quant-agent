"""PR #29 Prometheus AI/RULE 메트릭 단위 테스트.

record_decision_source 헬퍼의 분류 정확성 검증.
prometheus_client 가 없는 환경에서도 silently pass (fire-and-forget).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from common import prometheus_metrics as pm


class RecordDecisionSource(unittest.TestCase):
    """record_decision_source 의 AI vs RULE 분기."""

    def test_ai_decision_increments_ai_counter(self) -> None:
        if not pm._ENABLED:
            self.skipTest("prometheus_client 미설치")
        with patch.object(pm.AI_DECISION_TOTAL, "labels") as mock_ai, \
                patch.object(pm.RULE_DECISION_TOTAL, "labels") as mock_rule:
            pm.record_decision_source("btc", "AI", "BUY")
        mock_ai.assert_called_once_with(market="btc", action="BUY")
        mock_rule.assert_not_called()

    def test_llm_decision_increments_ai_counter(self) -> None:
        if not pm._ENABLED:
            self.skipTest("prometheus_client 미설치")
        with patch.object(pm.AI_DECISION_TOTAL, "labels") as mock_ai, \
                patch.object(pm.RULE_DECISION_TOTAL, "labels") as mock_rule:
            pm.record_decision_source("kr", "llm", "SELL")
        mock_ai.assert_called_once_with(market="kr", action="SELL")
        mock_rule.assert_not_called()

    def test_rule_decision_increments_rule_counter(self) -> None:
        if not pm._ENABLED:
            self.skipTest("prometheus_client 미설치")
        with patch.object(pm.AI_DECISION_TOTAL, "labels") as mock_ai, \
                patch.object(pm.RULE_DECISION_TOTAL, "labels") as mock_rule:
            pm.record_decision_source("btc", "RULE", "HOLD")
        mock_rule.assert_called_once_with(market="btc", action="HOLD")
        mock_ai.assert_not_called()

    def test_rule_primary_increments_rule_counter(self) -> None:
        """KR 의 'RULE_PRIMARY' / 'RULE_DEFAULT' / 'RULE_COOLDOWN' 도 RULE 분류."""
        if not pm._ENABLED:
            self.skipTest("prometheus_client 미설치")
        for src in ("RULE_PRIMARY", "RULE_DEFAULT", "RULE_COOLDOWN", "rule_btc"):
            with patch.object(pm.AI_DECISION_TOTAL, "labels") as mock_ai, \
                    patch.object(pm.RULE_DECISION_TOTAL, "labels") as mock_rule:
                pm.record_decision_source("kr", src, "BUY")
            mock_rule.assert_called_once_with(market="kr", action="BUY")
            mock_ai.assert_not_called()

    def test_null_source_defaults_to_rule(self) -> None:
        if not pm._ENABLED:
            self.skipTest("prometheus_client 미설치")
        with patch.object(pm.RULE_DECISION_TOTAL, "labels") as mock_rule:
            pm.record_decision_source("us", None, "HOLD")
        mock_rule.assert_called_once_with(market="us", action="HOLD")

    def test_exception_in_labels_silently_passes(self) -> None:
        """labels() 가 예외 던져도 호출자 영향 없어야 (fire-and-forget)."""
        if not pm._ENABLED:
            self.skipTest("prometheus_client 미설치")
        with patch.object(pm.AI_DECISION_TOTAL, "labels", side_effect=RuntimeError("boom")):
            try:
                pm.record_decision_source("btc", "AI", "BUY")
            except Exception:
                self.fail("record_decision_source raised; should be fire-and-forget")


if __name__ == "__main__":
    unittest.main()
