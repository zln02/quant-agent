"""PR #25: paper_live_diff 모드 식별 + 통계 단위 테스트."""
from __future__ import annotations

import json


def test_detect_mode_transitions_returns_first_seen(tmp_path, monkeypatch):
    from scripts import paper_live_diff as pld

    eq = tmp_path / "us.jsonl"
    rows = [
        {"timestamp": "2026-05-20T00:00:00+00:00", "metadata": {"mode": "sim"}},
        {"timestamp": "2026-05-21T00:00:00+00:00", "metadata": {"mode": "sim"}},
        {"timestamp": "2026-05-22T00:00:00+00:00", "metadata": {"mode": "paper"}},
        {"timestamp": "2026-05-23T00:00:00+00:00", "metadata": {"mode": "paper"}},
        {"timestamp": "2026-05-25T00:00:00+00:00", "metadata": {"mode": "live"}},
    ]
    eq.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    monkeypatch.setattr(pld, "_equity_file", lambda: eq)
    t = pld._detect_mode_transitions()
    assert t["sim"] == "2026-05-20T00:00:00+00:00"
    assert t["paper"] == "2026-05-22T00:00:00+00:00"
    assert t["live"] == "2026-05-25T00:00:00+00:00"


def test_detect_mode_transitions_missing_file(tmp_path, monkeypatch):
    from scripts import paper_live_diff as pld
    monkeypatch.setattr(pld, "_equity_file", lambda: tmp_path / "missing.jsonl")
    assert pld._detect_mode_transitions() == {}


def test_trade_mode_picks_latest_transition_before_trade():
    from scripts import paper_live_diff as pld
    transitions = {
        "sim": "2026-05-20T00:00:00+00:00",
        "paper": "2026-05-22T00:00:00+00:00",
        "live": "2026-05-25T00:00:00+00:00",
    }
    # 5/21 → sim 이후 paper 이전 → sim
    assert pld._trade_mode("2026-05-21T12:00:00+00:00", transitions) == "sim"
    # 5/23 → paper 이후 live 이전 → paper
    assert pld._trade_mode("2026-05-23T12:00:00+00:00", transitions) == "paper"
    # 5/26 → live 이후 → live
    assert pld._trade_mode("2026-05-26T00:00:00+00:00", transitions) == "live"


def test_trade_mode_unknown_when_no_transitions():
    from scripts import paper_live_diff as pld
    assert pld._trade_mode("2026-05-21T12:00:00+00:00", {}) == "unknown"


def test_analyze_groups_by_mode():
    from scripts import paper_live_diff as pld
    transitions = {"sim": "2026-05-20T00:00:00+00:00",
                   "paper": "2026-05-22T00:00:00+00:00"}
    trades = [
        {"pnl_pct": 2.0, "created_at": "2026-05-21T10:00:00+00:00",
         "signal_source": "RULE", "composite_score": 70},
        {"pnl_pct": -1.5, "created_at": "2026-05-21T15:00:00+00:00",
         "signal_source": "RULE", "composite_score": 60},
        {"pnl_pct": 3.0, "created_at": "2026-05-23T10:00:00+00:00",
         "signal_source": "ML", "composite_score": 80},
        {"pnl_pct": 1.0, "created_at": "2026-05-23T15:00:00+00:00",
         "signal_source": "ML", "composite_score": 75},
    ]
    report = pld.analyze(trades, transitions)
    assert report["total_closed"] == 4
    assert report["by_mode"]["sim"]["n"] == 2
    assert report["by_mode"]["paper"]["n"] == 2
    # sim: mean (2 + -1.5)/2 = 0.25
    assert abs(report["by_mode"]["sim"]["mean"] - 0.25) < 1e-9
    # 시그널 소스별 분리
    assert "RULE" in report["by_signal_source"]
    assert "ML" in report["by_signal_source"]


def test_analyze_skips_null_pnl():
    from scripts import paper_live_diff as pld
    trades = [
        {"pnl_pct": None, "created_at": "2026-05-21T10:00:00+00:00"},
        {"pnl_pct": 1.0, "created_at": "2026-05-21T11:00:00+00:00",
         "signal_source": "RULE"},
    ]
    report = pld.analyze(trades, {})
    # null 제외 1건만 카운트
    assert sum(s.get("n", 0) for s in report["by_mode"].values()) == 1


def test_stats_basic():
    from scripts.paper_live_diff import _stats
    s = _stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert s["n"] == 5
    assert s["mean"] == 3.0
    assert s["median"] == 3.0
    assert s["min"] == 1.0
    assert s["max"] == 5.0
    assert s["sharpe"] > 0


def test_stats_empty():
    from scripts.paper_live_diff import _stats
    assert _stats([]) == {"n": 0}


def test_detect_mode_events_tracks_round_trip(tmp_path, monkeypatch):
    """PR #25 hotfix(#7): sim→paper→sim→live 재전환 추적."""
    from scripts import paper_live_diff as pld

    eq = tmp_path / "us.jsonl"
    rows = [
        {"timestamp": "2026-05-20T00:00:00+00:00", "metadata": {"mode": "sim"}},
        {"timestamp": "2026-05-21T00:00:00+00:00", "metadata": {"mode": "paper"}},
        {"timestamp": "2026-05-22T00:00:00+00:00", "metadata": {"mode": "paper"}},
        {"timestamp": "2026-05-23T00:00:00+00:00", "metadata": {"mode": "sim"}},   # rollback
        {"timestamp": "2026-05-24T00:00:00+00:00", "metadata": {"mode": "live"}},
    ]
    eq.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    monkeypatch.setattr(pld, "_equity_file", lambda: eq)

    events = pld._detect_mode_events()
    # 같은 mode 연속 압축 + rollback 검출
    assert events == [
        ("2026-05-20T00:00:00+00:00", "sim"),
        ("2026-05-21T00:00:00+00:00", "paper"),
        ("2026-05-23T00:00:00+00:00", "sim"),
        ("2026-05-24T00:00:00+00:00", "live"),
    ]


def test_trade_mode_with_events_handles_rollback(tmp_path, monkeypatch):
    """기존 sticky-last 로직은 rollback 후에도 paper로 남았음. events는 sim 복원."""
    from scripts import paper_live_diff as pld
    events = [
        ("2026-05-20T00:00:00+00:00", "sim"),
        ("2026-05-21T00:00:00+00:00", "paper"),
        ("2026-05-23T00:00:00+00:00", "sim"),     # rollback
        ("2026-05-24T00:00:00+00:00", "live"),
    ]
    # 5/22 → paper 윈도우 안
    assert pld._trade_mode("2026-05-22T12:00:00+00:00", {}, events=events) == "paper"
    # 5/23 12:00 → rollback 이후 sim
    assert pld._trade_mode("2026-05-23T12:00:00+00:00", {}, events=events) == "sim"
    # 5/24 12:00 → live
    assert pld._trade_mode("2026-05-24T12:00:00+00:00", {}, events=events) == "live"


def test_trade_mode_events_none_falls_back_to_transitions():
    """events=None 백워드-호환: transitions dict 만으로 동작."""
    from scripts import paper_live_diff as pld
    transitions = {"sim": "2026-05-20T00:00:00+00:00",
                   "paper": "2026-05-22T00:00:00+00:00"}
    # events 미지정 → 기존 sticky-last
    assert pld._trade_mode("2026-05-25T12:00:00+00:00", transitions) == "paper"
