"""Phase 18 monitoring/report module tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import self_healer
from agents.alert_manager import AlertManager
from agents.daily_report import DailyReportContext, DailyReportGenerator


class AlertManagerTests(unittest.TestCase):
    def test_alert_generation_and_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch("agents.alert_manager._COOLDOWN_DIR", Path(td)):
                mgr = AlertManager()
                snapshot = {
                    "drawdown": -0.04,
                    "var_95": 0.03,
                    "corr_shift": 0.35,
                    "volume_spike_ratio": 2.3,
                }

                first = mgr.process(snapshot, send_telegram_alert=False)
                second = mgr.process(snapshot, send_telegram_alert=False)

                self.assertGreaterEqual(first["candidate_count"], 3)
                self.assertGreater(first["emitted_count"], 0)
                self.assertEqual(second["emitted_count"], 0)  # dedupe cooldown


class DailyReportTests(unittest.TestCase):
    def test_markdown_format(self) -> None:
        gen = DailyReportGenerator(supabase_client=None)
        text = gen.build_markdown(
            DailyReportContext(
                today_pnl_pct=1.2,
                today_pnl_abs=120000,
                trade_count=8,
                wins=5,
                losses=3,
                tomorrow_strategy="Reduce high-beta exposure.",
                risk_status="STABLE",
            ),
            report_date="2026-02-27",
        )
        self.assertIn("Daily Trading Report", text)
        self.assertIn("Tomorrow Strategy", text)


class SelfHealerTests(unittest.TestCase):
    def test_cleanup_disk_space_removes_safe_cache_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pip_cache = base / ".cache" / "pip"
            npm_cache = base / ".npm" / "_cacache"
            pip_cache.mkdir(parents=True)
            npm_cache.mkdir(parents=True)
            (pip_cache / "wheel.bin").write_bytes(b"x" * 128)
            (npm_cache / "pkg.bin").write_bytes(b"y" * 256)

            out = self_healer.cleanup_disk_space((pip_cache, npm_cache))

            self.assertGreater(out["reclaimed_bytes"], 0)
            self.assertFalse(pip_cache.exists())
            self.assertFalse(npm_cache.exists())

    def test_collect_issues_auto_cleans_disk_before_alerting(self) -> None:
        with patch("agents.self_healer.check_dashboard_health", return_value={"ok": True}):
            with patch("agents.self_healer.check_log_freshness", return_value=[]):
                with patch("agents.self_healer.check_docker_containers", return_value=[]):
                    with patch("agents.self_healer.cleanup_disk_space", return_value={"reclaimed_bytes": 1024**3, "reclaimed_gb": 1.0}):
                        with patch("agents.self_healer.check_disk_usage", side_effect=[
                            {"used_pct": 94, "free_gb": 3.2},
                            None,
                        ]):
                            with patch("common.supabase_client.get_supabase", return_value=None):
                                issues = self_healer.collect_issues()

        self.assertEqual(issues, ["🗄️ Supabase 연결 실패: Supabase client unavailable"])

    def test_should_suppress_duplicate_alerts_within_cooldown(self) -> None:
        issues = ["❌ dashboard down", "🗄️ supabase failed"]
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "self_healer_state.json"
            self.assertTrue(
                self_healer.should_send_alert(
                    issues,
                    now_ts=1_000,
                    cooldown_seconds=300,
                    state_path=state_path,
                )
            )
            self_healer.mark_alert_sent(issues, now_ts=1_000, state_path=state_path)
            self.assertFalse(
                self_healer.should_send_alert(
                    issues,
                    now_ts=1_100,
                    cooldown_seconds=300,
                    state_path=state_path,
                )
            )
            self.assertTrue(
                self_healer.should_send_alert(
                    issues,
                    now_ts=1_400,
                    cooldown_seconds=300,
                    state_path=state_path,
                )
            )

    def test_run_sends_alert_only_when_issue_signature_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "self_healer_state.json"
            first_issues = ["❌ dashboard down"]
            second_issues = ["❌ dashboard down", "🗄️ supabase failed"]
            with patch("agents.self_healer.ALERT_STATE_FILE", state_path):
                with patch("agents.self_healer.collect_issues", side_effect=[first_issues, first_issues, second_issues]):
                    with patch("agents.self_healer.send_alert") as send_alert:
                        with patch(
                            "agents.self_healer.time.time",
                            side_effect=([1_000] * 10) + ([1_100] * 10) + ([1_200] * 10),
                        ):
                            self_healer.run()
                            self_healer.run()
                            self_healer.run()
            self.assertEqual(send_alert.call_count, 2)

    def test_disk_issue_signature_is_stable_within_same_bucket(self) -> None:
        low = self_healer._format_disk_issue({"used_pct": 93, "free_gb": 3.2})
        high = self_healer._format_disk_issue({"used_pct": 94, "free_gb": 3.2})
        self.assertEqual(low, high)


if __name__ == "__main__":
    unittest.main()
