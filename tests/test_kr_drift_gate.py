"""KR ML drift gate — RULE 시그널 pass-through fix 단위 테스트.

8 케이스: drift_report 부재 / STABLE / WARNING(rule/ml) / DANGER block(rule/ml) / DANGER soft(rule/ml).
의도: ML drift 시 ML/composite 시그널만 차단/penalty, 룰 단독은 통과.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocks.stock_trading_agent import _apply_kr_drift_gate  # noqa: E402


def _patch_report(monkeypatch, report):
    monkeypatch.setattr(
        "stocks.stock_trading_agent._load_kr_ml_drift_report",
        lambda: report,
    )


def _mk_report(status, max_psi=0.0, high_psi_count=0):
    return {"status": status, "max_psi": max_psi, "high_psi_count": high_psi_count}


def test_no_report_passthrough(monkeypatch):
    _patch_report(monkeypatch, {})
    sig = {"action": "BUY", "confidence": 80, "source": "RULE_PRIMARY"}
    assert _apply_kr_drift_gate(sig) == sig


def test_stable_rule_passthrough(monkeypatch):
    _patch_report(monkeypatch, _mk_report("STABLE"))
    sig = {"action": "BUY", "confidence": 80, "source": "RULE_PRIMARY"}
    out = _apply_kr_drift_gate(sig)
    assert out["action"] == "BUY"
    assert out["confidence"] == 80
    assert out["drift_status"] == "STABLE"
    assert out["drift_penalty"] == 0.0


def test_warning_rule_passthrough(monkeypatch):
    _patch_report(monkeypatch, _mk_report("WARNING", max_psi=0.15))
    sig = {"action": "BUY", "confidence": 75, "source": "RULE_PRIMARY", "reason": "rule reason"}
    out = _apply_kr_drift_gate(sig)
    assert out["confidence"] == 75
    assert "RULE_PASS" in out["reason"]
    assert out["drift_penalty"] == 0.0


def test_warning_ml_penalty(monkeypatch):
    _patch_report(monkeypatch, _mk_report("WARNING", max_psi=0.15))
    sig = {"action": "BUY", "confidence": 80, "source": "ML_MULTI_HORIZON"}
    out = _apply_kr_drift_gate(sig)
    assert out["confidence"] == 72.0
    assert out["drift_penalty"] == 8.0


def test_danger_block_rule_passthrough(monkeypatch):
    """PSI 8.98 + high_psi=15 (현재 운영 상태) — 룰은 통과해야 함."""
    _patch_report(monkeypatch, _mk_report("DANGER", max_psi=8.98, high_psi_count=15))
    sig = {"action": "BUY", "confidence": 78, "source": "RULE_DEFAULT", "reason": "rule"}
    out = _apply_kr_drift_gate(sig)
    assert out["action"] == "BUY"
    assert out["confidence"] == 78
    assert "RULE_PASS" in out["reason"]
    assert out["drift_penalty"] == 0.0


def test_danger_block_ml_hold(monkeypatch):
    """ML 시그널은 PSI>=1 시 HOLD 강제 (기존 동작)."""
    _patch_report(monkeypatch, _mk_report("DANGER", max_psi=8.98, high_psi_count=15))
    sig = {"action": "BUY", "confidence": 80, "source": "ML_MULTI_HORIZON"}
    out = _apply_kr_drift_gate(sig)
    assert out["action"] == "HOLD"
    assert out["confidence"] == 0.0
    assert out["drift_penalty"] == 100.0


def test_danger_soft_rule_passthrough(monkeypatch):
    """DANGER but psi<1 + high_psi<12 — 룰은 penalty 0."""
    _patch_report(monkeypatch, _mk_report("DANGER", max_psi=0.5, high_psi_count=8))
    sig = {"action": "BUY", "confidence": 75, "source": "RULE_PRIMARY"}
    out = _apply_kr_drift_gate(sig)
    assert out["confidence"] == 75
    assert out["drift_penalty"] == 0.0


def test_danger_soft_ml_penalty(monkeypatch):
    """DANGER but psi<1 + high_psi<12 — ML 시그널 -15 penalty."""
    _patch_report(monkeypatch, _mk_report("DANGER", max_psi=0.5, high_psi_count=8))
    sig = {"action": "BUY", "confidence": 80, "source": "ML_MULTI_HORIZON"}
    out = _apply_kr_drift_gate(sig)
    assert out["confidence"] == 65.0
    assert out["drift_penalty"] == 15.0
