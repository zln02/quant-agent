"""Daily Reviewer (PR #28) unit tests.

`test_phase18_monitoring.py` 패턴 차용: supabase_client / llm_fn / sender 주입으로 외부 의존 차단.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scripts.daily_reviewer import DailyReviewer, _compress_btc_trades


class CompressTrades(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(_compress_btc_trades([]), {"total_cycles": 0})

    def test_action_distribution(self) -> None:
        rows = [
            {"action": "HOLD", "reason": "low score", "composite_score": 30, "timestamp": "t1"},
            {"action": "HOLD", "reason": "low score", "composite_score": 35, "timestamp": "t2"},
            {"action": "BUY", "reason": "strong signal", "composite_score": 80, "timestamp": "t3"},
        ]
        result = _compress_btc_trades(rows)
        self.assertEqual(result["total_cycles"], 3)
        self.assertEqual(result["action_dist"], {"HOLD": 2, "BUY": 1})
        self.assertAlmostEqual(result["avg_composite"], 48.3, places=0)
        self.assertEqual(result["first_ts"], "t1")
        self.assertEqual(result["last_ts"], "t3")
        self.assertIn("low score", result["top3_reasons"])


class DailyReviewerInit(unittest.TestCase):
    def test_unknown_market_raises(self) -> None:
        with self.assertRaises(ValueError):
            DailyReviewer("XX", supabase=MagicMock(), llm_fn=lambda *a, **k: "")

    def test_market_normalized_to_upper(self) -> None:
        rv = DailyReviewer("kr", supabase=MagicMock(), llm_fn=lambda *a, **k: "ok", sender=lambda *a, **k: True)
        self.assertEqual(rv.market, "KR")


class CollectContextKR(unittest.TestCase):
    def test_kr_collect_handles_empty_rows(self) -> None:
        sb = MagicMock()
        sb.table.return_value.select.return_value.gte.return_value.execute.return_value.data = []
        rv = DailyReviewer("KR", supabase=sb, llm_fn=lambda *a, **k: "", sender=lambda *a, **k: True)
        ctx = rv.collect_context()
        self.assertEqual(ctx["market"], "KR")
        self.assertEqual(ctx["section1_trading"]["trade_count"], 0)
        self.assertIn("section3_risk", ctx)


class CallReviewMockLLM(unittest.TestCase):
    def test_call_review_passes_market_and_ctx_to_llm(self) -> None:
        captured = {}

        def fake_llm(prompt: str, system=None, max_tokens=0, temperature=0) -> str:
            captured["prompt"] = prompt
            captured["system"] = system
            captured["max_tokens"] = max_tokens
            return "1. 매매 요약\n측정 불가\n\n2. 알고리즘 정합성\n측정 불가"

        rv = DailyReviewer("BTC", supabase=MagicMock(), llm_fn=fake_llm, sender=lambda *a, **k: True)
        ctx = {"market": "BTC", "section1_trading": {}, "section3_risk": {}, "meta": {"kst": "2026-05-18 17:00 KST", "model": "test"}}
        review = rv.call_review(ctx)
        self.assertIn("매매 요약", review)
        self.assertIn("BTC", captured["prompt"])
        self.assertIn("4섹션", captured["system"])
        self.assertEqual(captured["max_tokens"], 1500)


class PersistAndDeliver(unittest.TestCase):
    def test_persist_calls_supabase_insert(self) -> None:
        sb = MagicMock()
        insert_mock = MagicMock()
        sb.table.return_value.insert = insert_mock
        rv = DailyReviewer("KR", supabase=sb, llm_fn=lambda *a, **k: "review", sender=lambda *a, **k: True)
        ok = rv.persist({"x": 1}, "review-body")
        self.assertTrue(ok)
        sb.table.assert_called_with("review_logs")
        args, _ = insert_mock.call_args
        payload = args[0]
        self.assertEqual(payload["market"], "kr")
        self.assertEqual(payload["review_text"], "review-body")
        self.assertEqual(payload["model"], "claude-haiku-4-5-20251001")

    def test_persist_returns_false_when_no_supabase(self) -> None:
        with patch("scripts.daily_reviewer.get_supabase", return_value=None):
            rv2 = DailyReviewer("KR", llm_fn=lambda *a, **k: "r", sender=lambda *a, **k: True)
            self.assertFalse(rv2.persist({}, "r"))

    def test_deliver_calls_sender_with_priority_important(self) -> None:
        sender = MagicMock(return_value=True)
        rv = DailyReviewer("BTC", supabase=MagicMock(), llm_fn=lambda *a, **k: "r", sender=sender)
        ok = rv.deliver("review-body")
        self.assertTrue(ok)
        args, kwargs = sender.call_args
        self.assertIn("Daily Review [BTC]", args[0])
        self.assertIn("review-body", args[0])
        from common.telegram import Priority
        self.assertEqual(kwargs.get("priority"), Priority.IMPORTANT)


class RunIntegration(unittest.TestCase):
    def test_run_pipeline(self) -> None:
        sb = MagicMock()
        sb.table.return_value.select.return_value.gte.return_value.execute.return_value.data = []
        sb.table.return_value.insert.return_value.execute.return_value = None
        sender = MagicMock(return_value=True)
        rv = DailyReviewer(
            "KR",
            supabase=sb,
            llm_fn=lambda *a, **k: "1. 매매 요약\n측정 불가",
            sender=sender,
        )
        result = rv.run()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["persisted"])
        self.assertTrue(result["sent"])
        self.assertGreater(result["review_chars"], 0)

    def test_run_empty_review_skips(self) -> None:
        sender = MagicMock(return_value=True)
        rv = DailyReviewer(
            "BTC",
            supabase=MagicMock(),
            llm_fn=lambda *a, **k: "",
            sender=sender,
        )
        with patch.object(
            rv,
            "collect_context",
            return_value={"market": "BTC", "section1_trading": {}, "section3_risk": {}, "meta": {"kst": "t", "model": "m"}},
        ):
            result = rv.run()
        self.assertEqual(result["status"], "empty_review")
        sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
