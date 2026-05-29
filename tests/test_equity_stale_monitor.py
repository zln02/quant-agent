"""PR #24: silence_monitor equity stale 감지 단위 테스트."""
from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock, patch

# holidays 모듈은 docker container 안에서만 설치됨 — venv 격리 테스트용 stub
if "holidays" not in sys.modules:
    _hol = MagicMock()
    _hol.SouthKorea = MagicMock(return_value=set())
    sys.modules["holidays"] = _hol


def _write_stale(path, hours: float):
    path.write_text('{"equity": 100}\n', encoding="utf-8")
    mtime = time.time() - (hours * 3600)
    import os
    os.utime(str(path), (mtime, mtime))


def test_check_equity_stale_returns_old_files(tmp_path, monkeypatch):
    from scripts import silence_monitor as sm
    monkeypatch.setattr(sm, "_EQUITY_DIR", tmp_path)

    _write_stale(tmp_path / "btc.jsonl", hours=48)   # 48h stale
    _write_stale(tmp_path / "kr.jsonl", hours=1)     # fresh
    _write_stale(tmp_path / "us.jsonl", hours=25)    # 25h stale

    stale = sm._check_equity_stale()
    markets = {m for m, _ in stale}
    assert "btc" in markets
    assert "us" in markets
    assert "kr" not in markets


def test_check_equity_stale_handles_missing_dir(tmp_path, monkeypatch):
    from scripts import silence_monitor as sm
    monkeypatch.setattr(sm, "_EQUITY_DIR", tmp_path / "nonexistent")
    assert sm._check_equity_stale() == []


def test_check_equity_stale_skips_missing_files(tmp_path, monkeypatch):
    from scripts import silence_monitor as sm
    monkeypatch.setattr(sm, "_EQUITY_DIR", tmp_path)
    # 디렉토리 존재, 파일 부재 → 빈 리스트
    assert sm._check_equity_stale() == []


def test_fire_equity_stale_sends_telegram_and_marks_cooldown(tmp_path, monkeypatch):
    from scripts import silence_monitor as sm
    monkeypatch.setattr(sm, "_COOLDOWN_DIR", tmp_path)

    sent = MagicMock(return_value=True)
    monkeypatch.setattr(sm, "send_telegram", sent)

    sm._fire_equity_stale("btc", 36.5)

    assert sent.called
    args, kwargs = sent.call_args
    msg = args[0]
    assert "EQUITY STALE [BTC]" in msg
    assert "36.5" in msg
    # 쿨다운 파일 생성 확인
    assert (tmp_path / "silence_equity_btc.ts").exists()


def test_check_all_includes_equity_stale(tmp_path, monkeypatch):
    from scripts import silence_monitor as sm

    eq_dir = tmp_path / "equity"
    eq_dir.mkdir()
    cd_dir = tmp_path / "cooldown"
    cd_dir.mkdir()

    monkeypatch.setattr(sm, "_EQUITY_DIR", eq_dir)
    monkeypatch.setattr(sm, "_COOLDOWN_DIR", cd_dir)
    _write_stale(eq_dir / "btc.jsonl", hours=72)

    sent = MagicMock(return_value=True)
    monkeypatch.setattr(sm, "send_telegram", sent)
    # 매매 침묵 체크는 스킵 (US log 미존재 + KR/BTC 시장 닫힘 처리)
    monkeypatch.setattr(sm, "_is_market_open_today", lambda m: False)
    monkeypatch.setattr(sm, "_check_us_silence", lambda: False)

    fired = sm.check_all()
    assert "equity_btc" in fired
    assert sent.called
